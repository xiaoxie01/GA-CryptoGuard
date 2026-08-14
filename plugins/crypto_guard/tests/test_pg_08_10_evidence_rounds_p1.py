# -*- coding: utf-8 -*-
"""08-10 Step 6 RED contract: read-only evidence rounds (P1-4).

design.md §6.2/§8 + prd.md P1-4. The risk proposal LLM may request ONLY a
bounded set of read-only broker methods through a structured tool-request
schema; results carry source/as-of/age/trust/schema metadata; failed or stale
evidence can never support approval; "no further loop: more requests, unknown
methods, or budget exhaustion returns ``wait``".

The 08-04 contract `AnalysisToolBroker.METHODS` (exactly five) is FROZEN by
`test_pg_08_04_skills_tools_e.py::TestSkillToolBroker::test_five_readonly_methods_ok`.
Step 6 therefore extends the broker with TWO narrow risk reads
(``confirmation_lifecycle_evidence`` / ``adaptive_risk_budget``) that are
reachable only through the enumerated ``RISK_READ_METHODS`` set + the broker
``call`` dispatch — never by adding to ``METHODS`` itself.

Contract under test:

  1. Narrow schema-validated reads: the two new methods return envelopes whose
     ``data`` is schema-valid and self-describing (source/as_of/age_ms/trust/
     schema_version). ``confirmation_lifecycle_evidence`` reads one prior
     trusted confirmation event; ``adaptive_risk_budget`` reads a compact
     account risk summary.
  2. Structured tool-request schema: ``validate_tool_request`` accepts exactly
     ``{method, params}`` for enumerated methods and rejects unknown methods,
     extra/missing keys, non-object params, wrong param types and bad enum
     values (side/timeframe/regime).
  3. Supplement-round executor ``run_risk_supplement_round``: executes a
     validated request list through the broker, stamps every result with
     metadata, raises ``BrokerRoundLimitError`` on over-capacity and
     ``BrokerForbiddenError`` on an unknown method (fail-closed ``wait``), and
     returns ``ok=False`` (never approvable) when any executed evidence fails.
  4. Fail-closed on stale/oversized/unavailable results: stale lifecycle
     evidence raises ``BrokerStaleError`` (default TTL, no cap passed, OR an
     explicit tighter cap); oversized results fail the round; repo failures
     become ``evidence_failed``.
  5. Write methods stay forbidden through both ``call`` and ``__getattr__``;
     the new reads never touch a write seam; no external network/MCP
     dependency (source-scan).

RED-first + revert-fail: every assertion fails against the pre-Step-6 broker
(no ``RISK_READ_METHODS``, no new methods, no validator, no executor) and
passes after the fix. No production DB mutation, no marker write, no service
restart, no commit.
"""
from __future__ import annotations

import inspect

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.tests import pg_fixtures as fx
from plugins.crypto_guard.tests.test_pg_08_10_llm_risk_rollout_p1 import (
    _ltc_confirmation,
    _ltc_event,
    _ltc_plan,
    _ltc_snapshot,
)
from plugins.crypto_guard.tools import analysis_tool_broker as broker_mod
from plugins.crypto_guard.tools.analysis_tool_broker import AnalysisToolBroker

AT = 1_700_000_000_000
FRESH_AGE_MS = 60_000  # confirmation event 60s before analysis time


class _FakeRepo:
    """In-memory read-only repo exposing the five 08-04 seams plus the two
    Step 6 seams (``confirmation_lifecycle`` / ``adaptive_risk_budget_summary``).
    Any write attempt is recorded in ``write_log`` and must never be observed.
    """

    def __init__(
        self,
        *,
        lifecycle=None,
        budget=None,
        candles=None,
        skill_refs=None,
        watches=None,
        orders=None,
        fail_lifecycle=False,
        fail_budget=False,
    ) -> None:
        self._lifecycle = lifecycle
        self._budget = budget
        self._candles = candles or []
        self._skill_refs = skill_refs or {}
        self._watches = watches or []
        self._orders = orders or []
        self._fail_lifecycle = fail_lifecycle
        self._fail_budget = fail_budget
        self.write_log: list[dict] = []

    # ── Step 6 read seams ─────────────────────────────────────────────────
    def confirmation_lifecycle(self, symbol: str, side: str, *, analysis_time_utc: int):
        if self._fail_lifecycle:
            raise RuntimeError("repo lifecycle read failed")
        return self._lifecycle

    def adaptive_risk_budget_summary(self, symbol: str | None, *, as_of: int):
        if self._fail_budget:
            raise RuntimeError("repo budget read failed")
        return self._budget

    # ── 08-04 read seams (needed because new reads dispatch via call()) ───
    def get_candles(self, symbol: str, interval: str, *, analysis_time_utc: int, limit: int = 200):
        return self._candles

    def latest_skill_result_refs(self, symbol: str, analysis_time_utc: int):
        return self._skill_refs

    def latest_analysis_state(self, symbol: str):
        return None

    def latest_analysis_states(self, limit: int = 50):
        return []

    def list_active_opportunity_watches_for_symbol(self, symbol: str):
        return self._watches

    def list_open_paper_orders(self):
        return self._orders

    # ── a write seam that the broker must never invoke ────────────────────
    def create_paper_order(self, **kwargs):
        self.write_log.append(kwargs)
        return 1


def _fresh_lifecycle() -> dict:
    return {
        "fingerprint": "fp_candidate_v1",
        "status": "confirmed",
        "direction": "LONG",
        "side": "LONG",
        "close_time": AT - FRESH_AGE_MS,
        "as_of": AT - FRESH_AGE_MS,
        "age_bars": 1,
    }


def _fresh_budget() -> dict:
    return {
        "open_orders_count": 1,
        "risk_units_free": 8.0,
        "risk_units_total": 10.0,
        "risk_units_used": 2.0,
        "budget_pct_used": 0.2,
        "concentration_breach": False,
        "symbols": ["BTCUSDT"],
    }


def _risk_broker(**kw) -> AnalysisToolBroker:
    return AnalysisToolBroker(_FakeRepo(**kw), now_ms=AT)


# ── #1/#4: narrow schema-validated risk reads with metadata ───────────────


class TestBrokerRiskReads:
    """Step 6: the broker gains two narrow risk reads that are schema-valid and
    self-describing; they fail closed on stale/absent-malformed input."""

    def test_confirmation_lifecycle_evidence_reads_prior_event(self) -> None:
        broker = _risk_broker(lifecycle=_fresh_lifecycle())
        env = broker.call(
            "confirmation_lifecycle_evidence",
            symbol="BTCUSDT", side="LONG", analysis_time_utc=AT,
        )
        assert env["ok"] is True
        assert env["method"] == "confirmation_lifecycle_evidence"
        data = env["data"]
        assert data["symbol"] == "BTCUSDT"
        assert data["side"] == "LONG"
        assert data["status"] == "confirmed"
        assert data["fingerprint"] == "fp_candidate_v1"
        assert data["direction"] == "LONG"
        assert data["close_time"] == AT - FRESH_AGE_MS
        assert data["age_bars"] == 1
        # metadata contract (source / as-of / age / trust / schema version)
        assert data["source"] == "analysis_tool_broker"
        assert data["as_of"] == AT - FRESH_AGE_MS
        assert data["age_ms"] == FRESH_AGE_MS
        assert data["trust"] == "trusted"
        assert data["schema_version"] == "confirmation_lifecycle_v1"
        # schema-valid
        assert broker_mod._validate_against_schema(
            data, broker_mod.RESULT_SCHEMAS["confirmation_lifecycle_evidence"]
        ) == []

    def test_confirmation_lifecycle_absent_is_not_stale(self) -> None:
        broker = _risk_broker(lifecycle=None)
        env = broker.call(
            "confirmation_lifecycle_evidence",
            symbol="BTCUSDT", side="LONG", analysis_time_utc=AT,
        )
        assert env["ok"] is True
        assert env["data"]["status"] == "absent"
        assert env["data"]["fingerprint"] is None
        assert env["data"]["age_ms"] == 0

    def test_confirmation_lifecycle_stale_fails_closed_with_explicit_cap(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerStaleError

        broker = _risk_broker(lifecycle=_fresh_lifecycle())
        with pytest.raises(BrokerStaleError, match="stale"):
            broker.call(
                "confirmation_lifecycle_evidence",
                symbol="BTCUSDT", side="LONG",
                analysis_time_utc=AT, max_age_ms=1000,  # tighter than 60s age
            )

    def test_confirmation_lifecycle_stale_fails_closed_by_default_ttl(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerStaleError

        # event 5h old, analysis now -> older than the default lifecycle TTL
        old = {
            "fingerprint": "fp_candidate_v1",
            "status": "confirmed",
            "direction": "LONG",
            "side": "LONG",
            "close_time": AT - 5 * 3600 * 1000,
            "as_of": AT - 5 * 3600 * 1000,
            "age_bars": 20,
        }
        broker = _risk_broker(lifecycle=old)
        with pytest.raises(BrokerStaleError, match="stale"):
            broker.call(
                "confirmation_lifecycle_evidence",
                symbol="BTCUSDT", side="LONG", analysis_time_utc=AT,
            )

    def test_confirmation_lifecycle_rejects_bad_side(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerParamError

        broker = _risk_broker(lifecycle=_fresh_lifecycle())
        with pytest.raises(BrokerParamError):
            broker.call(
                "confirmation_lifecycle_evidence",
                symbol="BTCUSDT", side="MIDDLE", analysis_time_utc=AT,
            )

    def test_adaptive_risk_budget_reads_compact_summary(self) -> None:
        broker = _risk_broker(budget=_fresh_budget())
        env = broker.call("adaptive_risk_budget", symbol="BTCUSDT", analysis_time_utc=AT)
        assert env["ok"] is True
        data = env["data"]
        assert data["symbol"] == "BTCUSDT"
        assert data["open_orders_count"] == 1
        assert data["risk_units_free"] == 8.0
        assert data["budget_pct_used"] == 0.2
        assert data["concentration_breach"] is False
        assert data["symbols"] == ["BTCUSDT"]
        # metadata contract
        assert data["source"] == "analysis_tool_broker"
        assert data["as_of"] == AT
        assert data["age_ms"] == 0
        assert data["trust"] == "trusted"
        assert data["schema_version"] == "adaptive_risk_budget_v1"
        assert broker_mod._validate_against_schema(
            data, broker_mod.RESULT_SCHEMAS["adaptive_risk_budget"]
        ) == []

    def test_adaptive_risk_budget_symbol_optional_all_symbols(self) -> None:
        broker = _risk_broker(budget=_fresh_budget())
        env = broker.call("adaptive_risk_budget", analysis_time_utc=AT)
        assert env["ok"] is True
        assert env["data"]["symbol"] is None

    def test_new_reads_are_bound_read_only_methods(self) -> None:
        broker = _risk_broker(lifecycle=_fresh_lifecycle(), budget=_fresh_budget())
        assert callable(getattr(broker, "confirmation_lifecycle_evidence"))
        assert callable(getattr(broker, "adaptive_risk_budget"))
        # reads must never touch a write seam
        broker.call("confirmation_lifecycle_evidence", symbol="BTCUSDT", side="LONG", analysis_time_utc=AT)
        broker.call("adaptive_risk_budget", symbol="BTCUSDT", analysis_time_utc=AT)
        assert broker._repo.write_log == [], "new reads must never write"


# ── #2: structured tool-request schema + method/param validation ──────────


class TestToolRequestSchema:
    """Step 6: ``validate_tool_request`` accepts only the enumerated read-only
    methods with exactly ``{method, params}`` and type/enum-correct params."""

    def test_valid_lifecycle_request_accepted(self) -> None:
        ok, err, normalized = broker_mod.validate_tool_request({
            "method": "confirmation_lifecycle_evidence",
            "params": {"symbol": "BTCUSDT", "side": "LONG", "analysis_time_utc": AT},
        })
        assert ok is True and err is None
        assert normalized == {
            "method": "confirmation_lifecycle_evidence",
            "params": {"symbol": "BTCUSDT", "side": "LONG", "analysis_time_utc": AT},
        }

    def test_valid_existing_method_request_accepted(self) -> None:
        ok, err, normalized = broker_mod.validate_tool_request({
            "method": "latest_closed_market_summary",
            "params": {"symbol": "BTCUSDT", "timeframe": "1h", "analysis_time_utc": AT},
        })
        assert ok is True and err is None
        assert normalized["method"] == "latest_closed_market_summary"

    def test_unknown_method_rejected(self) -> None:
        ok, err, _ = broker_mod.validate_tool_request({
            "method": "create_paper_order", "params": {},
        })
        assert ok is False and err is not None
        assert "create_paper_order" in err

    def test_extra_key_rejected(self) -> None:
        ok, err, _ = broker_mod.validate_tool_request({
            "method": "adaptive_risk_budget",
            "params": {"symbol": "BTCUSDT"},
            "evil": "prompt injection",
        })
        assert ok is False and "evil" in err

    def test_missing_params_key_rejected(self) -> None:
        ok, err, _ = broker_mod.validate_tool_request({"method": "adaptive_risk_budget"})
        assert ok is False and "params" in err

    def test_non_object_request_rejected(self) -> None:
        ok, err, _ = broker_mod.validate_tool_request(["method", "params"])
        assert ok is False

    def test_non_object_params_rejected(self) -> None:
        ok, err, _ = broker_mod.validate_tool_request({
            "method": "adaptive_risk_budget", "params": ["symbol"],
        })
        assert ok is False and "params" in err

    def test_wrong_param_type_rejected(self) -> None:
        ok, err, _ = broker_mod.validate_tool_request({
            "method": "confirmation_lifecycle_evidence",
            "params": {"symbol": 123, "side": "LONG"},
        })
        assert ok is False and "symbol" in err

    def test_bad_side_enum_rejected(self) -> None:
        ok, err, _ = broker_mod.validate_tool_request({
            "method": "confirmation_lifecycle_evidence",
            "params": {"symbol": "BTCUSDT", "side": "MIDDLE"},
        })
        assert ok is False and "side" in err

    def test_unknown_param_key_rejected(self) -> None:
        ok, err, _ = broker_mod.validate_tool_request({
            "method": "adaptive_risk_budget",
            "params": {"symbol": "BTCUSDT", "max_age_ms": 1000},  # not in spec
        })
        assert ok is False and "max_age_ms" in err


# ── #3/#4: supplement-round executor, caps, fail-closed evidence ───────────


class TestSupplementRoundExecutor:
    """Step 6: ``run_risk_supplement_round`` executes validated requests with
    metadata, and fails closed (``wait`` semantics) on over-capacity, unknown
    methods, stale/oversized evidence and repo failures."""

    def _exec(self, requests, **kw):
        return broker_mod.run_risk_supplement_round(
            _risk_broker(lifecycle=_fresh_lifecycle(), budget=_fresh_budget(), **kw),
            requests=requests,
            symbol="BTCUSDT",
            analysis_time_utc=AT,
        )

    def test_executes_allowed_requests_with_metadata(self) -> None:
        out = self._exec([
            {
                "method": "confirmation_lifecycle_evidence",
                "params": {"symbol": "BTCUSDT", "side": "LONG"},
            },
            {
                "method": "adaptive_risk_budget",
                "params": {"symbol": "BTCUSDT"},
            },
        ])
        assert out["ok"] is True
        assert out["requests_used"] == 2
        assert len(out["results"]) == 2
        for env in out["results"]:
            assert env["ok"] is True
            meta = env.get("meta")
            assert meta is not None, "every result must carry metadata"
            assert set(meta) == {"source", "as_of", "age_ms", "trust", "schema_version"}
            assert meta["source"] == "analysis_tool_broker"
            assert meta["trust"] in {"trusted", "model_derived", "untrusted_data"}
            assert isinstance(meta["as_of"], int) and isinstance(meta["age_ms"], int)

    def test_injects_round_time_when_absent(self) -> None:
        out = self._exec([
            {
                "method": "confirmation_lifecycle_evidence",
                "params": {"symbol": "BTCUSDT", "side": "LONG"},
            },
        ])
        assert out["ok"] is True
        data = out["results"][0]["data"]
        assert data["analysis_time_utc"] == AT, "round analysis time must be injected"
        # meta.as_of is the evidence's own as-of (when the confirmation closed);
        # meta.age_ms is measured against the round analysis time.
        meta = out["results"][0]["meta"]
        assert meta["as_of"] == AT - FRESH_AGE_MS
        assert meta["age_ms"] == FRESH_AGE_MS

    def test_empty_requests_ok(self) -> None:
        out = self._exec([])
        assert out["ok"] is True
        assert out["requests_used"] == 0 and out["results"] == []

    def test_too_many_requests_fails_closed(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerRoundLimitError

        requests = [{"method": "adaptive_risk_budget", "params": {"symbol": "BTCUSDT"}}] * 7
        with pytest.raises(BrokerRoundLimitError, match="max_requests"):
            self._exec(requests)

    def test_unknown_method_fails_closed(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerForbiddenError

        with pytest.raises(BrokerForbiddenError, match="tool request"):
            self._exec([
                {"method": "confirmation_lifecycle_evidence", "params": {"symbol": "BTCUSDT", "side": "LONG"}},
                {"method": "web_search", "params": {"query": "nope"}},
            ])

    def test_stale_evidence_fails_round_closed(self) -> None:
        old = {
            "fingerprint": "fp_candidate_v1", "status": "confirmed",
            "direction": "LONG", "side": "LONG",
            "close_time": AT - 5 * 3600 * 1000,
            "as_of": AT - 5 * 3600 * 1000, "age_bars": 20,
        }
        out = broker_mod.run_risk_supplement_round(
            _risk_broker(lifecycle=old),
            requests=[
                {"method": "confirmation_lifecycle_evidence", "params": {"symbol": "BTCUSDT", "side": "LONG"}},
                {"method": "adaptive_risk_budget", "params": {"symbol": "BTCUSDT"}},
            ],
            symbol="BTCUSDT", analysis_time_utc=AT,
        )
        assert out["ok"] is False, "stale evidence must make the round not approvable"
        failed = [env for env in out["results"] if not env.get("ok")]
        assert failed and failed[0]["error"] == "evidence_failed"
        assert "stale" in failed[0].get("message", "")

    def test_repo_failure_marks_round_not_approvable(self) -> None:
        out = broker_mod.run_risk_supplement_round(
            _risk_broker(lifecycle=None, fail_lifecycle=True),
            requests=[
                {"method": "confirmation_lifecycle_evidence", "params": {"symbol": "BTCUSDT", "side": "LONG"}},
            ],
            symbol="BTCUSDT", analysis_time_utc=AT,
        )
        assert out["ok"] is False
        assert out["results"][0]["error"] == "evidence_failed"

    def test_oversized_result_fails_round_closed(self) -> None:
        big = _fresh_budget()
        big["symbols"] = [f"SYM{i:08d}" for i in range(300)]  # many long strings
        out = broker_mod.run_risk_supplement_round(
            AnalysisToolBroker(_FakeRepo(budget=big), now_ms=AT, max_result_bytes=256),
            requests=[{"method": "adaptive_risk_budget", "params": {"symbol": "BTCUSDT"}}],
            symbol="BTCUSDT", analysis_time_utc=AT,
        )
        assert out["ok"] is False, "oversized evidence must fail the round closed"
        assert out["results"][0]["error"] == "evidence_failed"


# ── #5: no write method through call/__getattr__, no network/MCP ───────────


class TestNoWriteAndNoNetwork:
    """Step 6: the broker still exposes no write method through ``call`` or
    ``__getattr__``, and production has no external network/MCP dependency."""

    def test_write_methods_still_forbidden(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerForbiddenError

        broker = _risk_broker()
        for name in (
            "execute_sql", "web_search", "create_paper_order", "cancel_order",
            "restart_service", "stop_service", "start_service", "write_config",
            "add_symbol", "delete_symbol", "transfer_funds",
        ):
            with pytest.raises(BrokerForbiddenError, match=name):
                broker.call(name)
            with pytest.raises(BrokerForbiddenError, match=name):
                getattr(broker, name)
        with pytest.raises(BrokerForbiddenError):
            broker.call("not_a_broker_method")

    def test_no_external_network_or_mcp_dependency(self) -> None:
        src = inspect.getsource(broker_mod)
        for token in (
            "http://", "https://", "urlopen", "import requests", "import urllib",
            "import socket", "websocket", "subprocess",
        ):
            assert token not in src, (
                f"Step 6: broker must stay offline (no external MCP/network), found {token!r}"
            )


# ── 5th fresh reviewer P1 (2026-08-12): real-repo seams for the two narrow
# ── risk reads ──────────────────────────────────────────────────────────────


class TestBrokerRiskReadsAgainstRealRepo:
    """5th fresh-reviewer P1: the broker's two narrow risk reads must dispatch
    over the REAL ``CryptoGuardRepository`` (scratch schema), not only
    ``_FakeRepo`` (the fake-method-only seam would AttributeError in
    production). Seeds production-shaped rows through the repo's own writers
    (snapshot -> decision -> event, and paper orders) and asserts both
    ``call()`` envelopes are schema-valid end-to-end."""

    def test_confirmation_lifecycle_reads_real_event_row(self) -> None:
        h = fx.make_repo()
        try:
            at = AT
            close = at - 390_000  # 6.5 five-minute bars before analysis time
            snap_id = h.repo.save_market_snapshot(
                _ltc_snapshot(at=at, events=[_ltc_event(close_time=close)])
            )
            plan = {**_ltc_plan(close_time=close),
                    "entry_trigger_confirmation": _ltc_confirmation(close_time=close)}
            dec_id = h.repo.create_ga_decision({
                "symbol": "LTCUSDT", "analysis_time": at, "analysis_time_utc": at,
                "decision_type": "opportunity_watch_recheck", "signal_grade": "A",
                "confidence": 0.8, "market_bias": "bearish", "trend_stage": "early",
                "decision": "trade_plan_available", "skill_result_refs": {},
                "evidence": [], "counter_evidence": [],
                "risk_check": {"ok": True}, "feishu_actions": [],
                "trade_plan": plan, "snapshot_id": snap_id,
                "final_summary": "ltc-evidence-rounds",
                "raw_llm_summary": "ltc-evidence-rounds",
                "rendered_summary": "ltc-evidence-rounds", "batch_id": None,
                "previous_grade": "D", "llm_status": "ok",
            })
            ev_id = h.repo.insert_entry_confirmation_event_after_decision(
                decision_id=dec_id, snapshot_id=snap_id,
                confirmation=_ltc_confirmation(close_time=close),
                analysis_time_ms=at,
            )
            assert ev_id > 0

            broker = AnalysisToolBroker(h.repo, now_ms=at)
            env = broker.call(
                "confirmation_lifecycle_evidence",
                symbol="LTCUSDT", side="SHORT", analysis_time_utc=at,
            )
            assert env["ok"] is True
            data = env["data"]
            assert data["status"] == "confirmed"
            assert data["fingerprint"] is not None
            assert data["direction"] == "bearish"  # SHORT -> expected direction
            assert data["close_time"] == close
            assert data["as_of"] == close
            assert data["age_ms"] == at - close
            assert data["age_bars"] == 1  # 390_000 // 300_000 (5m bar)
            assert data["trust"] == "trusted"
            assert data["schema_version"] == "confirmation_lifecycle_v1"
            assert broker_mod._validate_against_schema(
                data, broker_mod.RESULT_SCHEMAS["confirmation_lifecycle_evidence"]
            ) == []
        finally:
            h.close()

    def test_confirmation_lifecycle_absent_without_event_row(self) -> None:
        h = fx.make_repo()
        try:
            broker = AnalysisToolBroker(h.repo, now_ms=AT)
            env = broker.call(
                "confirmation_lifecycle_evidence",
                symbol="BTCUSDT", side="LONG", analysis_time_utc=AT,
            )
            assert env["ok"] is True
            assert env["data"]["status"] == "absent"
            assert env["data"]["fingerprint"] is None
            assert env["data"]["age_ms"] == 0
        finally:
            h.close()

    def test_adaptive_risk_budget_aggregates_real_paper_orders(self) -> None:
        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            for sym, side, risk in (
                ("BTCUSDT", "LONG", 1.5),
                ("BTCUSDT", "LONG", 2.5),
                ("ETHUSDT", "SHORT", 1.0),
            ):
                oid, created = h.repo.create_paper_order(
                    None, {"symbol": sym},
                    {"side": side, "entry_type": "limit", "entry_price": 100.0,
                     "stop_loss": 95.0, "risk_percent": risk},
                    source="risk_budget_seed", risk_advisory_mode="off",
                )
                assert created and oid > 0

            broker = AnalysisToolBroker(h.repo, now_ms=AT)
            env = broker.call("adaptive_risk_budget", analysis_time_utc=AT)
            assert env["ok"] is True
            data = env["data"]
            assert data["symbol"] is None
            assert data["open_orders_count"] == 3
            assert data["symbols"] == ["BTCUSDT", "ETHUSDT"]
            assert data["risk_units_used"] == 5.0
            assert data["risk_units_total"] == 10.0  # max_total_risk_pct
            assert data["risk_units_free"] == 5.0
            assert data["budget_pct_used"] == 0.5
            assert data["concentration_breach"] is True  # 3 open orders >= max 3
            assert broker_mod._validate_against_schema(
                data, broker_mod.RESULT_SCHEMAS["adaptive_risk_budget"]
            ) == []
        finally:
            h.close()

    def test_adaptive_risk_budget_per_symbol_caps_at_single_trade(self) -> None:
        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            for sym, risk in (("BTCUSDT", 1.5), ("BTCUSDT", 2.5)):
                h.repo.create_paper_order(
                    None, {"symbol": sym},
                    {"side": "LONG", "entry_type": "limit", "entry_price": 100.0,
                     "stop_loss": 95.0, "risk_percent": risk},
                    source="risk_budget_seed", risk_advisory_mode="off",
                )

            broker = AnalysisToolBroker(h.repo, now_ms=AT)
            env = broker.call(
                "adaptive_risk_budget", symbol="BTCUSDT", analysis_time_utc=AT,
            )
            assert env["ok"] is True
            data = env["data"]
            assert data["symbol"] == "BTCUSDT"
            assert data["open_orders_count"] == 2
            assert data["symbols"] == ["BTCUSDT"]
            assert data["risk_units_used"] == 4.0
            assert data["risk_units_total"] == 2.0  # max_single_trade_risk_pct
            assert data["risk_units_free"] == 0.0  # max(0, 2.0 - 4.0)
            assert data["budget_pct_used"] == 2.0
            assert data["concentration_breach"] is False  # 2 < 3
        finally:
            h.close()
