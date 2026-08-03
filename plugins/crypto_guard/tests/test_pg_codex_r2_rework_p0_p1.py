"""08-02 Codex terminal-review round 2 — P0 + P1 targeted wire-in tests.

P0 (fake CVD): ``opportunity_watcher._condition_hit`` used to compare only the
condition's OWN persisted ``flow_confirmation`` string — never the real
order-flow CVD — so a ``supports_long`` watch fired IMMEDIATELY on a static
string match (regardless of K-lines) and ``supports_short`` / ``divergence``
values NEVER fired (silent permanent wait). With no analysis-time-aligned
order-flow read in the repository the conservative path is taken: the kind is
dropped from the schema AND ``SUPPORTED_WATCH_CONDITION_KINDS``. A persisted
``flow_confirmation`` can never stand in for live CVD.

P1 (envelope): a MATERIALIZABLE structured watch requires ``needed is True``,
``reason`` str-or-None, and ``expires_minutes`` None-or-positive-int. The
worker auto-materialize gate and both manual-button paths re-check
``is_structured_watch``.

Every test here is RED on the pre-fix (buggy) code and GREEN on the fixed code
(revert-fail): re-introducing the cvd kind / string-trigger acceptance or
removing the envelope gate makes these assertions fail.

RED / revert-fail proof is recorded in final-seal.md; the temporary revert was
performed, the tests observed RED, then the fix was re-applied and these tests
went GREEN.
"""

from __future__ import annotations

from plugins.crypto_guard.reasoning.watch_conditions import (
    is_structured_watch,
    normalize_opportunity_watch,
)
from plugins.crypto_guard.scheduler.opportunity_watcher import (
    update_opportunity_watches,
)
from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.tests.test_pg_opportunity_watch_auto_materialize_p0_2 import (
    _compat_decision,
    _run_effects,
    _save_risk_approved_snapshot,
    _signal_decision,
    _structured_watch,
)

_ANALYSIS_TIME_UTC = 1_700_000_000_000


def _seed_closed_candle(repo, symbol: str, *, close: float,
                        at_ms: int = _ANALYSIS_TIME_UTC) -> None:
    """One closed 15m candle at ``close`` for ``symbol`` (watcher sees it)."""
    span = 900_000
    base = at_ms - span
    repo.upsert_candles([
        {
            "symbol": symbol,
            "interval": "15m",
            "open_time": base,
            "close_time": base + span - 1,
            "open": close - 1.0,
            "high": close + 1.0,
            "low": close - 1.5,
            "close": close,
            "volume": 1000,
            "is_closed": True,
        }
    ])


def _cvd_watch(flow: str) -> dict:
    """A cvd_confirmation watch (the removed kind) with the given flow."""
    return {
        "needed": True,
        "direction": "LONG",
        "reason": "等待 CVD 确认",
        "conditions": [{"type": "cvd_confirmation", "side": "LONG",
                        "flow_confirmation": flow, "timeframe": "15m"}],
        "invalid_condition": None,
        "expires_minutes": 60,
    }


# ── P0: fake CVD — never triggers, never a permanent-wait "triggerable" ──────


class TestCvdRemovalWireInCodexR2P0:
    def test_supports_long_cvd_watch_never_triggers_two_kline_sets(self) -> None:
        """RED (revert-fail): under the pre-fix watcher a ``supports_long`` cvd
        watch fired IMMEDIATELY on a static string match — no matter what the
        market looked like. Two completely different K-line sets (one strongly
        bullish, one strongly bearish) both feed a supports_long cvd watch;
        NEITHER may trigger, and both are flagged untriggerable."""
        handle = make_repo()
        try:
            repo = handle.repo
            repo.create_opportunity_watch("AAAUSDT", _cvd_watch("supports_long"))
            repo.create_opportunity_watch("BBBUSDT", _cvd_watch("supports_long"))
            _seed_closed_candle(repo, "AAAUSDT", close=105.0)  # strongly bullish
            _seed_closed_candle(repo, "BBBUSDT", close=80.0)   # strongly bearish
            update = update_opportunity_watches(repo, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert update["checked"] == 2, update
            assert update["triggered"] == 0, f"supports_long cvd fired: {update}"
            assert len(update["results"]) == 2
            for r in update["results"]:
                assert r["status"] == "waiting", r
                assert r.get("untriggerable") is True, (
                    "a cvd watch must be flagged untriggerable, never a "
                    "permanent-wait triggerable watch"
                )
        finally:
            handle.close()

    def test_cvd_watch_untriggerable_no_misfire(self) -> None:
        """RED (revert-fail): a single cvd watch never auto-triggers (no误触发)
        and is surfaced as untriggerable rather than silently waiting forever."""
        handle = make_repo()
        try:
            repo = handle.repo
            repo.create_opportunity_watch("CCCUSDT", _cvd_watch("supports_long"))
            _seed_closed_candle(repo, "CCCUSDT", close=102.0)
            update = update_opportunity_watches(repo, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert update["triggered"] == 0
            r = update["results"][0]
            assert r["status"] == "waiting"
            assert r.get("untriggerable") is True, r
            # The DB row stays active (waiting) — it did NOT trigger.
            active = repo.list_active_opportunity_watches_for_symbol("CCCUSDT")
            assert len(active) == 1
        finally:
            handle.close()

    def test_divergence_cvd_never_fires_and_not_triggerable(self) -> None:
        """RED (revert-fail): under the OLD watcher a ``divergence`` flow NEVER
        fired (silent permanent wait). After the fix the watch is dropped as
        unsupported and flagged untriggerable — closing BOTH the misfire (LONG
        fires immediately) and the never-fires (divergence waits forever)
        defects. There is no CVD flow value that yields a triggerable watch."""
        handle = make_repo()
        try:
            repo = handle.repo
            repo.create_opportunity_watch("DDDUSDT", _cvd_watch("divergence"))
            _seed_closed_candle(repo, "DDDUSDT", close=100.0)
            update = update_opportunity_watches(repo, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert update["triggered"] == 0
            assert update["results"][0].get("untriggerable") is True
            # Materialization gate also rejects it: never a triggerable watch.
            assert is_structured_watch(_cvd_watch("divergence")) is False
        finally:
            handle.close()


# ── P1: envelope — needed=True, reason str-or-None, expires positive-or-None ──


def _clone_watch(base: dict, **overrides) -> dict:
    import copy
    out = copy.deepcopy(base)
    out.update(overrides)
    return out


class TestEnvelopeStrictnessWireInCodexR2P1:
    def _assert_button_rejects(self, bad_watch: dict) -> dict:
        from plugins.crypto_guard.run_ga_workers import handle_button_callback

        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(_signal_decision(watch=bad_watch), snapshot_id)
            button = handle_button_callback(
                repo,
                {"action": "create_opportunity_watch", "symbol": "SOLUSDT",
                 "signal_id": signal_id},
            )
            assert button["ok"] is False, f"button must be rejected; {button}"
            assert "结构化校验" in button["error"], button
            assert repo.list_active_opportunity_watches_for_symbol("SOLUSDT") == []
            return button
        finally:
            handle.close()

    def test_reason_int_button_rejected(self) -> None:
        """RED (revert-fail): ``reason`` int must not pass the materialization
        gate — the manual button fails closed."""
        bad = _clone_watch(_structured_watch(), reason=123)
        assert is_structured_watch(bad) is False
        self._assert_button_rejects(bad)

    def test_needed_false_button_rejected(self) -> None:
        """RED (revert-fail): ``needed`` False must not materialize via the
        manual button."""
        bad = _clone_watch(_structured_watch(), needed=False)
        assert is_structured_watch(bad) is False
        self._assert_button_rejects(bad)

    def test_expires_zero_button_rejected(self) -> None:
        """RED (revert-fail): ``expires_minutes`` 0 is not a positive integer
        and must NOT short-circuit the gate."""
        bad = _clone_watch(_structured_watch(), expires_minutes=0)
        assert is_structured_watch(bad) is False
        self._assert_button_rejects(bad)

    def test_expires_negative_button_rejected(self) -> None:
        """RED (revert-fail): ``expires_minutes`` -5 must NOT materialize."""
        bad = _clone_watch(_structured_watch(), expires_minutes=-5)
        assert is_structured_watch(bad) is False
        self._assert_button_rejects(bad)

    def test_reason_int_normalize_repairs_to_string(self) -> None:
        """P1: the repair chain never persists a schema-invalid reason — an int
        is repaired to the default string, never coerced with ``str(...)``, and
        the normalized watch is schema-valid."""
        bad = _clone_watch(_structured_watch(), reason=123)
        normalized, _notes = normalize_opportunity_watch(bad, None)
        assert normalized is not None
        assert normalized["reason"] == "等待结构确认", normalized["reason"]
        assert is_structured_watch(normalized) is True

    def test_needed_false_normalize_preserves_false(self) -> None:
        """R2 review Finding 2 (brand-new reviewer): ``needed`` is LLM intent
        ("no watch wanted"). The repair chain must NOT erase an explicit
        ``needed=False`` into True — pre-fix it did, making the P1-1 gate
        vacuous on the production LLM path (every watch that reached repair came
        out needed=True and materialized). A False is preserved so the
        normalized watch still fails ``is_structured_watch`` (RED pre-fix:
        normalize forced needed=True)."""
        bad = _clone_watch(_structured_watch(), needed=False)
        normalized, _notes = normalize_opportunity_watch(bad, None)
        assert normalized is not None
        assert normalized["needed"] is False, normalized["needed"]
        assert is_structured_watch(normalized) is False

    def test_needed_false_repair_chain_keeps_false_and_not_materialized(self) -> None:
        """R2 review Finding 2 (wire-in): the production LLM repair path
        (``_try_repair_opportunity_watch`` → ``normalize_opportunity_watch``)
        keeps an explicit ``needed=False`` watch at needed=False, so the shared
        ``_post_decision_effects`` gate rejects it and NO watch auto-materializes
        (RED pre-fix: repair forced needed=True and the watch materialized)."""
        from plugins.crypto_guard.reasoning.llm_agent_judge import (
            _try_repair_opportunity_watch,
        )

        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(_signal_decision(watch=_structured_watch()), snapshot_id)
            bad = _clone_watch(_structured_watch(), needed=False)
            d = {"opportunity_watch": bad, "trade_plan": None, "candidate_trade_plan": None}
            repaired, _notes, changed = _try_repair_opportunity_watch(d, None)
            assert changed is True
            repaired_watch = repaired["opportunity_watch"]
            assert repaired_watch is not None
            assert repaired_watch.get("needed") is False, repaired_watch.get("needed")
            assert is_structured_watch(repaired_watch) is False
            result = _run_effects(repo, _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9103,
                watch=repaired_watch,
            ))
            assert result["auto_watch"] is None, result
            assert repo.list_active_opportunity_watches_for_symbol("SOLUSDT") == []
        finally:
            handle.close()

    def test_needed_false_auto_not_materialized(self) -> None:
        """P1 (auto-path wire-in): the worker auto-materialize gate
        (``is_structured_watch`` in ``_post_decision_effects``) rejects a
        needed=False watch — no auto watch is created."""
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(_signal_decision(watch=_structured_watch()), snapshot_id)
            bad = _clone_watch(_structured_watch(), needed=False)
            result = _run_effects(repo, _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9101, watch=bad,
            ))
            assert result["auto_watch"] is None, result
            assert repo.list_active_opportunity_watches_for_symbol("SOLUSDT") == []
        finally:
            handle.close()

    def test_expires_zero_auto_not_materialized(self) -> None:
        """P1 (auto-path wire-in): ``expires_minutes`` 0 must not short-circuit
        the auto gate (pre-fix the repository ``expires_at`` falsy check dropped
        the TTL) — the auto path fails closed."""
        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            signal_id = repo.create_signal(_signal_decision(watch=_structured_watch()), snapshot_id)
            bad = _clone_watch(_structured_watch(), expires_minutes=0)
            result = _run_effects(repo, _compat_decision(
                grade="B", signal_id=signal_id, ga_decision_id=9102, watch=bad,
            ))
            assert result["auto_watch"] is None, result
            assert repo.list_active_opportunity_watches_for_symbol("SOLUSDT") == []
        finally:
            handle.close()


# ── P2-3 (fresh reviewer): supported-kind-no-level = untriggerable ──────────


class TestSupportedKindNoLevelUntriggerableCodexR2P2_3:
    def test_watcher_flags_supported_kind_no_level_untriggerable(self) -> None:
        """RED (revert-fail): a SUPPORTED kind (pullback) with NO level/price is
        never evaluated — every ``_condition_hit`` branch is gated on
        ``level is not None``, so the pre-fix watcher fell through to a silent
        permanent wait. After the fix it is flagged ``untriggerable`` (fail
        closed), status stays waiting, and nothing triggers."""
        handle = make_repo()
        try:
            repo = handle.repo
            watch = {
                "needed": True,
                "direction": "LONG",
                "reason": "无 level 的回踩",
                "conditions": [{"type": "pullback", "side": "LONG", "timeframe": "15m"}],
                "invalid_condition": None,
                "expires_minutes": 60,
            }
            repo.create_opportunity_watch("EEEXUSDT", watch)
            _seed_closed_candle(repo, "EEEXUSDT", close=102.0)
            update = update_opportunity_watches(repo, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert update["triggered"] == 0
            r = update["results"][0]
            assert r["status"] == "waiting", r
            assert r.get("untriggerable") is True, r
            assert any(item.get("untriggerable") for item in r.get("condition_results", [])), r
        finally:
            handle.close()

    def test_watcher_flags_bool_string_price_untriggerable(self) -> None:
        """RED (revert-fail, R2 P2-1): the pre-fix ``_float_or_none(... or ...)``
        coerced bool/string levels — True -> 1.0, "100" -> 100.0 — so a
        supported kind (price_above) carrying a bool or numeric-string level
        silently TRIGGERED (close 102 > 1.0 / > 100), diverging from the
        diagnostic's ``_condition_is_untriggerable`` fail-closed contract. After
        the fix the watcher flags each watch untriggerable, status stays waiting,
        and nothing triggers."""
        handle = make_repo()
        try:
            repo = handle.repo
            bad_values = {
                "FF1USDT": {"level": True},
                "FF2USDT": {"level": "100"},
                "FF3USDT": {"price": True},
                "FF4USDT": {"price": "100"},
            }
            for symbol, cond_extra in bad_values.items():
                cond = {"type": "price_above", "side": "LONG", "timeframe": "15m", **cond_extra}
                repo.create_opportunity_watch(symbol, {
                    "needed": True,
                    "direction": "LONG",
                    "reason": "非法 level/price 的突破",
                    "conditions": [cond],
                    "invalid_condition": None,
                    "expires_minutes": 60,
                })
                _seed_closed_candle(repo, symbol, close=102.0)
            update = update_opportunity_watches(repo, analysis_time_utc=_ANALYSIS_TIME_UTC)
            assert update["checked"] == len(bad_values), update
            assert update["triggered"] == 0, f"bool/string level fired: {update}"
            for r in update["results"]:
                assert r["status"] == "waiting", r
                assert r.get("untriggerable") is True, r
                assert any(item.get("untriggerable") for item in r.get("condition_results", [])), r
        finally:
            handle.close()

    def test_diagnostic_flags_supported_kind_no_level_untriggerable(self) -> None:
        """RED (revert-fail): the P1-3 diagnostic ``_condition_is_untriggerable``
        must agree with the watcher — a supported kind with no usable level/price
        is untriggerable; a supported kind WITH a numeric level is not."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            _condition_is_untriggerable,
        )

        assert _condition_is_untriggerable(
            {"type": "pullback", "side": "LONG", "timeframe": "15m"}
        ) is True
        assert _condition_is_untriggerable(
            {"type": "pullback", "side": "LONG", "timeframe": "15m", "price": 100.0}
        ) is False
        assert _condition_is_untriggerable(
            {"type": "pullback", "side": "LONG", "timeframe": "15m", "level": "abc"}
        ) is True
        assert _condition_is_untriggerable(
            {"type": "pullback", "side": "LONG", "timeframe": "15m", "level": True}
        ) is True


# ── P2-4 (fresh reviewer): manual watch with expires_minutes=None → 240 min ──


class TestManualWatchDefaultTTLCodexR2P2_4:
    def test_button_expires_minutes_none_defaults_to_240(self) -> None:
        """RED (revert-fail): the pre-fix manual path persisted a PERMANENT watch
        (``expires_at`` NULL) when ``expires_minutes`` was None, while the auto
        path defaulted to 240. After the fix the button path fails closed to a
        240-minute TTL, so every materialized watch is bounded."""
        from datetime import datetime, timezone

        from plugins.crypto_guard.run_ga_workers import handle_button_callback

        handle = make_repo()
        try:
            repo = handle.repo
            snapshot_id = _save_risk_approved_snapshot(repo, "SOLUSDT")
            watch = _clone_watch(_structured_watch(), expires_minutes=None)
            signal_id = repo.create_signal(_signal_decision(watch=watch), snapshot_id)
            button = handle_button_callback(
                repo,
                {"action": "create_opportunity_watch", "symbol": "SOLUSDT",
                 "signal_id": signal_id},
            )
            assert button["ok"] is True, button
            row = repo.get_opportunity_watch(int(button["watch_id"]))
            assert row is not None
            expires_at = row["expires_at"]
            assert expires_at is not None, "manual watch must NOT be permanent"
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            delta = (expires_at - datetime.now(timezone.utc)).total_seconds()
            # ≈ now + 240 min; allow slack so a slow runner doesn't flake.
            assert 3 * 3600 < delta <= 4 * 3600, f"expires_at {expires_at} delta={delta}s"
        finally:
            handle.close()
