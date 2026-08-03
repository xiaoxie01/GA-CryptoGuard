# -*- coding: utf-8 -*-
"""08-02 P1-2: immutable LLM synthesis evidence, no double risk collapse.

Audit: three defect chains.

1. The S/A auto-build block in ``_normalize_llm_decision`` builds a SYSTEM
   trade_plan when the LLM gives an S/A grade but no plan, then the caller's
   unconditional marking (``if has_trade_plan and trade_plan:
   plan_origin="llm_confirmed"``) labels that system-built plan
   llm_confirmed — the 30 production ``confirmed_without_plan`` rows: the LLM
   never confirmed a plan it never wrote.

2. Nothing records what the LLM actually synthesized before
   ``apply_risk_to_decision`` strips the plan — reports can only observe the
   post-clear state and collapse "LLM confirmed but risk rejected" into
   "no plan".

3. ``RiskGate.check`` re-runs ``validate_trade_plan`` on the ALREADY-CLEARED
   decision, re-deriving ``["缺少完整 trade_plan"]`` ahead of the real blocker
   reason — the double risk validation overwriting the true reason.

PRD P1-2 verbatim:
- Record BEFORE any risk gate strips the plan: ``llm_synthesis_signal_grade``,
  ``llm_synthesis_decision``, ``llm_synthesis_has_trade_plan``,
  ``llm_synthesis_trade_plan``, ``llm_plan_verdict``, ``llm_plan_source``.
- Distinguish provider/schema success from plan confirmation.
- System-auto-built plans MUST NOT be marked llm_confirmed.
- Risk validates the original proposed plan once; MUST NOT re-validate after
  the plan is cleared and overwrite the real reason with 'missing trade_plan'.

RED-first: T1 (auto-built plan) and T4 (risk reason collapse) and T5
(immutability) assert the CORRECT post-fix states and flip RED against the
old code. Revert-fail controls prove (a) the old unconditional marking really
labels an auto-built plan llm_confirmed and (b) validate_trade_plan really
re-derives "缺少完整 trade_plan" on the cleared state — so T1/T4's assertions
are load-bearing.

Real production paths only: the real ``fair_llm_call_adapter`` (patched
provider ``_call_ga_llm`` returns raw LLM JSON — same seam P0-1 uses) feeding
the real ``GAMasterController(repo).analyze_symbol`` on a real per-test
PostgreSQL schema (``make_repo``). No mocking of the function under test.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.ga_master.controller import GAMasterController
from plugins.crypto_guard.ga_master.decision_schema import GAAnalysisRequest
from plugins.crypto_guard.reasoning import llm_agent_judge
from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.tests.test_pg_fair_llm_raw_reference_p0_1 import (
    _AT_MS,
    _build_snapshot,
)

# ── fixtures ────────────────────────────────────────────────────────────────


def _bullish_snapshot() -> dict:
    """Deterministic S/A fixture: raw SOP yields a LONG trade plan at
    ``plan_status="executable"`` (S grade / 0.95 confidence / entry 100.0 /
    stop 95.0 / TPs 108,112). Injects a closed bullish BOS event so the
    structured ``entry_trigger_confirmation`` is consistent with the snapshot."""
    snap = _build_snapshot(symbol="ADAUSDT", bias="bullish", stage="middle",
                           structure="bullish", momentum_dir="bullish",
                           candles_count=250)
    snap.setdefault("modules", {}).setdefault("price_action", {})["structure_events"] = [
        {"event": "bullish_bos", "timeframe": "15m", "direction": "bullish",
         "candle_close_time": _AT_MS - 900_000, "price": 99.5, "closed": True,
         "symbol": "ADAUSDT"},
    ]
    return snap


def _neutral_snapshot() -> dict:
    """Deterministic no-plan fixture: raw SOP yields ``no_plan`` with no trade
    plan / no candidate (monitor_only / C grade). The auto-build scenario
    needs this base so the LLM's S/A-grade bullish claim has no deterministic
    plan to inherit — the auto-build block fires."""
    return _build_snapshot(symbol="DOGEUSDT", bias="neutral", stage="range",
                           structure="range", momentum_dir="neutral",
                           candles_count=200)


def _entry_conf() -> dict:
    return {"type": "closed_candle_confirmation", "timeframe": "15m",
            "event_type": "BOS", "direction": "bullish",
            "candle_close_time": _AT_MS - 900_000, "price": 99.5,
            "source": "price_action", "symbol": "ADAUSDT"}


def _timeframe_context() -> dict:
    return {
        tf: {"bias": "unknown", "structure": "unknown", "closed": True,
             "close_time": _AT_MS - offset}
        for tf, offset in (("1d", 86_400_000), ("4h", 14_400_000),
                           ("1h", 3_600_000), ("15m", 900_000))
    }


def _raw_sop_plan(snapshot: dict, *, tp_override: dict | None = None,
                  entry_conf: dict | None = None) -> dict:
    """The deterministic S/A trade_plan, shaped the way the real LLM response
    would carry it (with an optional structured entry confirmation and
    take-profit override)."""
    raw = run_ga_sop_decision(snapshot)
    tp = dict(raw.get("trade_plan") or {})
    if entry_conf is not None:
        tp["entry_trigger_confirmation"] = entry_conf
    if tp_override:
        tp.update(tp_override)
    return tp


def _fake_s_a_no_plan_call(symbol: str) -> callable:
    """LLM claims an S-grade bullish trade_plan_available but provides NO
    trade_plan — the exact shape that triggers the system auto-build."""

    def fake_call(prompt: str) -> str:
        return json.dumps({
            "symbol": symbol,
            "analysis_time_utc": _AT_MS,
            "decision": "trade_plan_available",
            "signal_grade": "S",
            "market_bias": "bullish",
            "trend_stage": "middle",
            "confidence": 0.85,
            "summary": "强烈看涨，系统补建计划",
            "evidence": ["momentum"],
            "counter_evidence": [],
            "risk_notes": [],
            "has_trade_plan": False,
            "trade_plan": None,
            "opportunity_watch": None,
            "suggested_actions": ["create_paper_order"],
            "timeframe_context": _timeframe_context(),
            "alignment": "aligned",
            "htf_conflict": False,
            "market_reason_codes": [],
        }, ensure_ascii=False)

    return fake_call


def _fake_c_no_plan_call(symbol: str) -> callable:
    """LLM succeeds but produces NO plan (monitor_only / C grade) — mirrors
    the P0-1 no-plan reference fixture."""

    def fake_call(prompt: str) -> str:
        return json.dumps({
            "symbol": symbol,
            "analysis_time_utc": _AT_MS,
            "decision": "monitor_only",
            "signal_grade": "C",
            "market_bias": "neutral",
            "trend_stage": "range",
            "confidence": 0.45,
            "summary": f"{symbol} no edge",
            "evidence": ["no edge"],
            "counter_evidence": ["no edge"],
            "risk_notes": [],
            "has_trade_plan": False,
            "trade_plan": None,
            "opportunity_watch": None,
            "suggested_actions": ["ignore"],
            "timeframe_context": _timeframe_context(),
            "alignment": "unknown",
            "htf_conflict": False,
            "market_reason_codes": [],
        }, ensure_ascii=False)

    return fake_call


def _fake_llm_provided_plan_call(snapshot: dict, *, tp_override: dict | None = None,
                                 entry_conf: dict | None = None) -> callable:
    """LLM PROVIDES a full executable trade_plan (with structured entry
    confirmation) — the genuine confirmation path. tp_override can weaken the
    take-profits to make risk reject the plan."""
    symbol = str(snapshot.get("symbol") or "ADAUSDT")
    plan = _raw_sop_plan(snapshot, tp_override=tp_override, entry_conf=entry_conf)

    def fake_call(prompt: str) -> str:
        return json.dumps({
            "symbol": symbol,
            "analysis_time_utc": _AT_MS,
            "decision": "trade_plan_available",
            "signal_grade": "S",
            "market_bias": "bullish",
            "trend_stage": "middle",
            "confidence": 0.95,
            "summary": "强势看涨，创建模拟盘多单",
            "evidence": ["bullish_bos", "momentum"],
            "counter_evidence": [],
            "risk_notes": [],
            "has_trade_plan": True,
            "trade_plan": plan,
            "opportunity_watch": None,
            "suggested_actions": ["create_paper_order"],
            "timeframe_context": _timeframe_context(),
            "alignment": "aligned",
            "htf_conflict": False,
            "market_reason_codes": [],
        }, ensure_ascii=False)

    return fake_call


def _adapter_call(snapshot: dict, fake_call: callable) -> tuple[dict, dict]:
    """Run the real fair-adapter on a patched provider and return the
    normalized (candidate, attempt_meta) — the exact seam the fair
    coordinator uses in production."""
    from plugins.crypto_guard.reasoning.llm_breaker import (
        CircuitBreaker, PerSymbolDeadline,
    )
    from plugins.crypto_guard.reasoning.llm_fair_scheduler import (
        resolve_fair_batch_config,
    )
    clock = {"t": 0}

    def fake_now_ms():
        return clock["t"]

    with mock.patch("plugins.crypto_guard.reasoning.llm_breaker._now_ms",
                    side_effect=fake_now_ms):
        deadline = PerSymbolDeadline(
            per_symbol_timeout_seconds=300,
            per_attempt_timeout_seconds=180,
        )
    breaker = CircuitBreaker(enabled=True, consecutive_threshold=3,
                             rate_threshold=0.5, rate_window=10,
                             min_rate_samples=5)
    breaker.llm_config_name = "test_cfg"
    breaker.llm_model = "test_model"
    cfg = resolve_fair_batch_config({
        "scheduling": {"mode": "fair_pool", "max_concurrency": 4,
                       "per_symbol_timeout_seconds": 300,
                       "per_attempt_timeout_seconds": 180,
                       "batch_completion_guard_seconds": 60,
                       "rotate_start_symbol": True},
        "retry": {"max_attempts_per_symbol": 3},
    })
    with mock.patch("plugins.crypto_guard.reasoning.llm_agent_judge._call_ga_llm",
                    side_effect=fake_call):
        candidate, attempt_meta = llm_agent_judge.fair_llm_call_adapter(
            snapshot=snapshot, deadline=deadline, breaker=breaker,
            retry_budget=cfg, wall_clock_budget=cfg,
            attempt=1, max_attempts=cfg.max_attempts_per_symbol,
            schedule_position=0, schedule_round=1, context=None,
        )
    assert candidate is not None, attempt_meta
    return candidate, attempt_meta


def _run_controller(repo, snapshot: dict, candidate: dict,
                    attempt_meta: dict, *,
                    account_equity: float | None = None) -> dict:
    """Run the real controller on the real repo with the adapter-produced
    preset candidate and return the persisted ``raw_decision_json``
    (top-level, JSONB-deserialized)."""
    symbol = str(snapshot.get("symbol") or "ADAUSDT")
    if account_equity is not None:
        # Seed the ONLY paper account at a drawdown that trips risk_off
        # (-2.5%) but NOT hard_risk_off (-3.0%): equity 9740 = -2.6%.
        repo.ensure_paper_account("default", initial_balance=10000.0)
        with repo.conn.cursor() as cur:
            cur.execute(
                "UPDATE paper_accounts SET equity=%s, current_balance=%s "
                "WHERE account_name='default'",
                (float(account_equity), float(account_equity)),
            )
        repo.conn.commit()

    saved_llm_env = os.environ.get("CRYPTO_GUARD_LLM_ANALYSIS")
    try:
        # Preset tests MUST take the LLM-enabled preset path.
        os.environ["CRYPTO_GUARD_LLM_ANALYSIS"] = "1"
        snapshot_id = repo.save_market_snapshot(snapshot)
        request = GAAnalysisRequest(
            symbol=symbol, decision_type="scheduled_analysis",
            snapshot=snapshot, snapshot_id=snapshot_id,
            mode="scheduled", batch_id=f"15m:{snapshot.get('analysis_time_utc')}",
        )
        controller = GAMasterController(repo)
        ga_decision = controller.analyze_symbol(
            request,
            preset_llm_candidate=candidate,
            preset_llm_attempt_meta=attempt_meta,
        )
        ga_id = int(ga_decision.get("ga_decision_id") or 0)
        assert ga_id > 0, ga_decision
        row = repo.get_ga_decision(ga_id)
        raw = row.get("raw_decision_json") or {}
        if isinstance(raw, str):
            raw = json.loads(raw)
        return raw
    finally:
        if saved_llm_env is None:
            os.environ.pop("CRYPTO_GUARD_LLM_ANALYSIS", None)
        else:
            os.environ["CRYPTO_GUARD_LLM_ANALYSIS"] = saved_llm_env


def _blocker_codes(raw: dict) -> list[str]:
    blockers = raw.get("plan_blockers") or []
    return [str(b.get("code") or "") for b in blockers if isinstance(b, dict)]


# ── tests ───────────────────────────────────────────────────────────────────


class TestLlmSynthesisEvidenceP1_2:
    def test_t1_auto_built_plan_never_llm_confirmed(self) -> None:
        """RED->GREEN: an S/A-grade-no-plan LLM response triggers the system
        auto-build. The synthesized plan MUST be recorded with
        llm_plan_source="auto_built" / verdict="auto_built", MUST NOT be
        marked llm_confirmed, and MUST land unconfirmed+withheld through the
        full controller path (the 30 confirmed_without_plan rows)."""
        handle = make_repo()
        try:
            snap = _neutral_snapshot()
            candidate, attempt_meta = _adapter_call(
                snap, _fake_s_a_no_plan_call("DOGEUSDT"))

            # Adapter-level: the auto-built plan is recorded and NOT confirmed.
            assert attempt_meta.get("llm_status") == "ok", attempt_meta
            assert candidate["llm_plan_source"] == "auto_built", (
                f"got {candidate.get('llm_plan_source')!r}")
            assert candidate["llm_plan_verdict"] == "auto_built", (
                f"got {candidate.get('llm_plan_verdict')!r}")
            assert candidate["plan_origin"] != "llm_confirmed", (
                "a SYSTEM auto-built plan must never be llm_confirmed; got "
                f"{candidate.get('plan_origin')!r}")
            assert candidate["llm_synthesis_has_trade_plan"] is True
            syn_plan = candidate.get("llm_synthesis_trade_plan")
            assert isinstance(syn_plan, dict) and syn_plan.get("side") == "LONG", (
                "synthesis evidence must carry the deep-copied auto-built plan")
            assert candidate["llm_synthesis_decision"] == "trade_plan_available"

            # Controller-level: finalizer folds the unconfirmed auto-built
            # plan into candidate, never confirms it.
            raw = _run_controller(handle.repo, snap, candidate, attempt_meta)
            assert raw["plan_execution_state"] == "unconfirmed", (
                f"auto-built plan must be unconfirmed, not "
                f"{raw.get('plan_execution_state')!r} (never llm_confirmed)")
            assert raw["plan_origin"] == "deterministic_fallback", raw.get("plan_origin")
            assert raw["plan_status"] == "withheld", raw.get("plan_status")
            assert raw.get("trade_plan") is None
            assert isinstance(raw.get("candidate_trade_plan"), dict), (
                "the unconfirmed auto-built plan must be preserved as candidate")
            assert raw["llm_plan_source"] == "auto_built", raw.get("llm_plan_source")
            assert raw["llm_plan_verdict"] == "auto_built", raw.get("llm_plan_verdict")
            assert raw["llm_synthesis_has_trade_plan"] is True
            assert isinstance(raw.get("llm_synthesis_trade_plan"), dict), (
                "synthesis evidence must survive persistence")
            assert raw["decision"] != "create_paper_order", raw.get("decision")
        finally:
            handle.close()

    def test_t2_llm_provided_plan_confirmed_regression(self) -> None:
        """GREEN both: when the LLM PROVIDES a full executable plan, the
        confirmed marking still applies (the llm_plan_source gate must not
        over-block genuine confirmation)."""
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            candidate, attempt_meta = _adapter_call(
                snap, _fake_llm_provided_plan_call(snap, entry_conf=_entry_conf()))

            assert candidate["llm_plan_source"] == "llm_provided", (
                f"got {candidate.get('llm_plan_source')!r}")
            assert candidate["llm_plan_verdict"] == "confirmed", (
                f"got {candidate.get('llm_plan_verdict')!r}")
            assert candidate["plan_origin"] == "llm_confirmed", candidate.get("plan_origin")
            assert candidate["plan_execution_state"] == "confirmed", (
                candidate.get("plan_execution_state"))
            assert candidate["llm_synthesis_signal_grade"] in ("S", "A")
            assert isinstance(candidate.get("llm_synthesis_trade_plan"), dict)

            raw = _run_controller(handle.repo, snap, candidate, attempt_meta)
            assert raw["plan_execution_state"] == "confirmed", raw.get("plan_execution_state")
            assert raw["plan_origin"] == "llm_confirmed", raw.get("plan_origin")
            assert raw["plan_status"] == "executable", raw.get("plan_status")
            assert raw["llm_plan_source"] == "llm_provided", raw.get("llm_plan_source")
            assert raw["llm_plan_verdict"] == "confirmed", raw.get("llm_plan_verdict")
            assert raw["decision"] == "create_paper_order", raw.get("decision")
        finally:
            handle.close()

    def test_t3_llm_success_no_plan_no_auto_build(self) -> None:
        """GREEN both: LLM succeeded but produced no plan on a no-plan
        deterministic base -> verdict="no_plan" / source="none", never
        confirmed, never auto-built."""
        handle = make_repo()
        try:
            snap = _neutral_snapshot()
            candidate, attempt_meta = _adapter_call(
                snap, _fake_c_no_plan_call("DOGEUSDT"))
            assert attempt_meta.get("llm_status") == "ok", attempt_meta
            assert candidate["llm_plan_source"] == "none", candidate.get("llm_plan_source")
            assert candidate["llm_plan_verdict"] == "no_plan", candidate.get("llm_plan_verdict")
            assert candidate["plan_origin"] != "llm_confirmed", candidate.get("plan_origin")
            assert candidate["llm_synthesis_has_trade_plan"] is False
            assert candidate["llm_synthesis_trade_plan"] is None

            raw = _run_controller(handle.repo, snap, candidate, attempt_meta)
            assert raw["plan_execution_state"] == "no_candidate", raw.get("plan_execution_state")
            assert raw["plan_status"] == "no_plan", raw.get("plan_status")
            assert raw.get("trade_plan") is None
            assert raw.get("candidate_trade_plan") is None
            assert raw["llm_plan_verdict"] == "no_plan", raw.get("llm_plan_verdict")
        finally:
            handle.close()

    def test_t4_risk_rejected_confirmed_plan_no_missing_plan_reason(self) -> None:
        """RED->GREEN: the LLM CONFIRMED a plan that risk rejects. The
        persisted risk_check must carry the REAL blocker reason and must NOT
        re-derive '缺少完整 trade_plan' from the now-cleared state (Fix C).
        The immutable synthesis evidence must still show what was confirmed."""
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            candidate, attempt_meta = _adapter_call(
                snap, _fake_llm_provided_plan_call(
                    snap, entry_conf=_entry_conf(),
                    tp_override={"take_profits": [
                        {"price": 101.0, "ratio": 0.5},
                        {"price": 101.5, "ratio": 0.5},
                    ]}))

            assert candidate["llm_plan_source"] == "llm_provided"
            assert candidate["llm_plan_verdict"] == "confirmed"
            assert candidate["plan_origin"] == "llm_confirmed"

            raw = _run_controller(handle.repo, snap, candidate, attempt_meta)
            assert raw["plan_execution_state"] == "risk_rejected", (
                f"got {raw.get('plan_execution_state')!r}")
            assert raw["plan_status"] == "risk_rejected", raw.get("plan_status")
            # The immutable evidence still records the confirmed plan.
            assert raw["llm_plan_verdict"] == "confirmed", raw.get("llm_plan_verdict")
            assert raw["llm_plan_source"] == "llm_provided", raw.get("llm_plan_source")
            assert isinstance(raw.get("llm_synthesis_trade_plan"), dict), (
                "synthesis plan must survive the risk rejection")
            assert raw["llm_synthesis_has_trade_plan"] is True
            assert isinstance(raw.get("candidate_trade_plan"), dict), (
                "the rejected plan must be preserved as candidate")
            # Fix C: no collapsed '缺少完整 trade_plan' overwriting the real reason.
            risk = raw.get("risk_check") or {}
            reasons = [str(r) for r in (risk.get("reasons") or [])]
            assert not any("缺少完整" in r for r in reasons), (
                f"reasons must not re-derive the missing-plan collapse: {reasons}")
            assert any("风控" in r or "止损" in r or "风险" in r for r in reasons), (
                f"reasons must carry the real risk reason: {reasons}")
            assert "risk_rejected" in _blocker_codes(raw), raw.get("plan_blockers")
        finally:
            handle.close()

    def test_t5_synthesis_evidence_immutable_under_in_place_mutation(self) -> None:
        """RED->GREEN: the controller injects risk_percent into trade_plan IN
        PLACE (account risk_off at -2.6% drawdown). The deep-copied
        llm_synthesis_trade_plan must NOT carry that injected field — the
        audit record stays immutable."""
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            candidate, attempt_meta = _adapter_call(
                snap, _fake_llm_provided_plan_call(snap, entry_conf=_entry_conf()))
            raw = _run_controller(handle.repo, snap, candidate, attempt_meta,
                                  account_equity=9740.0)

            # The in-place mutation must have been exercised on the live plan:
            # the deterministic SOP plan's risk_percent (0.5) must be REPLACED
            # by the risk_off effective risk percent (0.25).
            assert raw["plan_execution_state"] == "confirmed", raw.get("plan_execution_state")
            live_plan = raw.get("trade_plan") or {}
            assert isinstance(live_plan, dict), live_plan
            assert live_plan.get("risk_percent") == 0.25, (
                "the risk_off mutation must have replaced risk_percent on the "
                f"live plan; got {live_plan.get('risk_percent')!r}")
            # ... but the immutable synthesis evidence keeps the ORIGINAL
            # capture-time value (0.5 from the deterministic plan), proving
            # the deep copy never sees the later in-place mutation.
            syn_plan = raw.get("llm_synthesis_trade_plan") or {}
            assert isinstance(syn_plan, dict), syn_plan
            assert syn_plan.get("risk_percent") == 0.5, (
                "the synthesis evidence is a deep copy — the later risk_off "
                f"injection must not propagate; got {syn_plan.get('risk_percent')!r}")
            assert syn_plan.get("side") == "LONG"
            assert syn_plan.get("entry_price") == live_plan.get("entry_price")
            assert raw["llm_plan_source"] == "llm_provided", raw.get("llm_plan_source")
            assert raw["llm_plan_verdict"] == "confirmed", raw.get("llm_plan_verdict")
        finally:
            handle.close()

    def test_revert_fail_old_unconditional_marking_would_label_auto_built_confirmed(
            self) -> None:
        """Revert-fail control: the OLD caller marking (unconditional on
        has_trade_plan+trade_plan) really labels a SYSTEM auto-built plan
        llm_confirmed. Only the llm_plan_source gate prevents it — if Fix B
        is ever reverted, T1 flips RED."""
        snap = _neutral_snapshot()
        candidate, _ = _adapter_call(snap, _fake_s_a_no_plan_call("DOGEUSDT"))
        assert candidate["llm_plan_source"] == "auto_built", (
            candidate.get("llm_plan_source"))
        assert candidate["plan_origin"] != "llm_confirmed"
        # Re-apply the OLD unconditional marking (pre-Fix-B).
        old = dict(candidate)
        if old.get("has_trade_plan") and old.get("trade_plan"):
            old["plan_origin"] = "llm_confirmed"
            old["plan_execution_state"] = "confirmed"
        assert old["plan_origin"] == "llm_confirmed", (
            "the old unconditional marking labels the SYSTEM auto-built plan "
            "llm_confirmed — only the llm_plan_source gate prevents it, and "
            "T1's assertions are load-bearing on that gate")

    def test_revert_fail_risk_validation_rederives_missing_plan(self) -> None:
        """Revert-fail control: validate_trade_plan on the post-clear state
        really re-derives ['缺少完整 trade_plan'] — the collapse T4 guards
        against. Proves Fix C (dropping the re-derived reason when structured
        blockers exist) is load-bearing."""
        from plugins.crypto_guard.risk.risk_engine import validate_trade_plan
        snap = _bullish_snapshot()
        cleared = {
            "symbol": "ADAUSDT",
            "analysis_time_utc": _AT_MS,
            "has_trade_plan": False,
            "trade_plan": None,
            "plan_blockers": [{"code": "risk_rejected", "stage": "risk_engine",
                               "detail": "止损空间不足，风险回报比不达标"}],
        }
        risk = validate_trade_plan(cleared, snap)
        assert "缺少完整 trade_plan" in (risk.get("reasons") or []), (
            "validate_trade_plan on the cleared state re-derives the "
            "misleading reason — exactly what Fix C drops when a structured "
            "blocker exists, and what T4 asserts never persists")
