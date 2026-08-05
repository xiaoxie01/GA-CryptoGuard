# -*- coding: utf-8 -*-
"""08-04 contract E (PRD): skills & tools (E1-E8).

E1/E2: the runtime keeps a single canonical skill contract — no ``*_skill``
duplicate dirs remain and ``_load_skill_contract`` resolves the canonical dirs
only; the runner normalize/feedback alias sets drop the ``_skill`` names.

E3: skill prompts must not embed market-data payloads. ``_run_skill`` no longer
embeds ``prompt.md`` as ``ga_interpretation["prompt"]`` (a high-trust LLM
instruction); the skill contract free text remains audit-only and tagged
``untrusted_data``. Only schema-validated deterministic skill results are
emitted.

E4-E6: ``AnalysisToolBroker`` is read-only with exactly five evidence methods,
strict enum params, a per-call timeout, a result size budget and a per-method
result schema. Arbitrary SQL / web search / production writes / order-writes /
service control are rejected (forbidden names raise ``BrokerForbiddenError``);
max 3 tool requests per round.

E7/E8: normal market quotes run a single round; conflicts / watch hits add an
evidence supplement round; order candidates add a verifier round that is
VETO-ONLY — it can never bypass the deterministic risk gate (a blocked gate
always yields ``veto`` and ``order_allowed=False``).

RED-first + revert-fail: every assertion here fails against the pre-fix code
(the ``*_skill`` dirs still exist, ``ga_interpretation.prompt`` is still
embedded, and no ``AnalysisToolBroker`` module exists) and passes after the
fix. No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.config.loader import PLUGIN_ROOT
from plugins.crypto_guard.skills import runner as skills_runner

SKILLS_ROOT = PLUGIN_ROOT / "skills"
CANONICAL_SKILLS = ("price_action", "momentum", "trend_stage", "smc_orderflow", "chanlun")
LEGACY_SKILL_NAMES = ("price_action_skill", "momentum_skill", "trend_stage_skill", "smc_orderflow_skill", "chanlun_skill")


class _FakeRepo:
    """In-memory read-only repo for the broker / runner tests (pure, no DB)."""

    def __init__(
        self,
        *,
        candles=None,
        skill_refs=None,
        analysis_state=None,
        analysis_states=None,
        watches=None,
        orders=None,
        slow=False,
    ) -> None:
        self._candles = candles
        self._skill_refs = skill_refs
        self._analysis_state = analysis_state
        self._analysis_states = analysis_states
        self._watches = watches
        self._orders = orders
        self._slow = slow
        self.logs: list[dict] = []

    def _maybe_sleep(self) -> None:
        if self._slow:
            time.sleep(0.2)

    # ── runner read/write seams ──────────────────────────────────────────
    def save_skill_execution_log(self, **kwargs) -> int:
        self.logs.append(kwargs)
        return 1

    def save_skill_feedback_memory(self, **kwargs) -> int:
        return 1

    # ── broker read seams ────────────────────────────────────────────────
    def get_candles(self, symbol: str, interval: str, *, analysis_time_utc: int, limit: int = 200):
        self._maybe_sleep()
        return self._candles if self._candles is not None else []

    def latest_skill_result_refs(self, symbol: str, analysis_time_utc: int):
        self._maybe_sleep()
        return self._skill_refs if self._skill_refs is not None else {}

    def latest_analysis_state(self, symbol: str):
        return self._analysis_state

    def latest_analysis_states(self, limit: int = 50):
        return self._analysis_states if self._analysis_states is not None else []

    def list_active_opportunity_watches_for_symbol(self, symbol: str):
        return self._watches if self._watches is not None else []

    def list_open_paper_orders(self):
        return self._orders if self._orders is not None else []


def _default_candles(analysis_time_utc: int) -> list[dict]:
    return [
        {
            "open": 60000.0,
            "high": 60100.0,
            "low": 59900.0,
            "close": 60050.0,
            "open_time": analysis_time_utc - 900_000,
            "close_time": analysis_time_utc,
        },
        {
            "open": 60050.0,
            "high": 60200.0,
            "low": 60000.0,
            "close": 60100.0,
            "open_time": analysis_time_utc - 600_000,
            "close_time": analysis_time_utc + 300_000,
        },
    ]


def _default_repo(**kw) -> _FakeRepo:
    at = 1_700_000_000_000
    defaults = dict(
        candles=_default_candles(at),
        skill_refs={"price_action": 11, "momentum": 12},
        analysis_state={
            "id": 9,
            "symbol": "BTCUSDT",
            "analysis_time": at,
            "state": {"decision": "monitor_only", "signal_grade": "B", "confidence": 0.55},
        },
        analysis_states=[
            {
                "id": 9,
                "symbol": "BTCUSDT",
                "analysis_time": at,
                "state": {"decision": "monitor_only", "signal_grade": "B", "confidence": 0.55},
            }
        ],
        watches=[
            {"id": 1, "symbol": "BTCUSDT", "direction": "LONG", "status": "active", "watch_reason": "测试观察", "expires_at": None}
        ],
        orders=[{"id": 1, "symbol": "BTCUSDT", "side": "LONG", "entry_price": 60000.0, "status": "open"}],
    )
    defaults.update(kw)
    return _FakeRepo(**defaults)


AT = 1_700_000_000_000


class TestSkillContractCanonicalOnly:
    """E1/E2: single canonical skill contract; no *_skill duplicate dirs."""

    def test_no_skill_suffix_dirs_remain(self) -> None:
        subdirs = [d.name for d in SKILLS_ROOT.iterdir() if d.is_dir()]
        leftovers = [n for n in subdirs if n.endswith("_skill")]
        assert leftovers == [], f"*_skill duplicate dirs must be deleted, found: {leftovers}"

    def test_canonical_contracts_resolve(self) -> None:
        for name in CANONICAL_SKILLS:
            contract = skills_runner._load_skill_contract(name)
            assert contract["files_present"] == {
                "skill.yaml": True,
                "prompt.md": True,
                "tools.py": True,
                "schema.json": True,
                "feedback_rules.yaml": True,
            }, f"canonical contract {name} must resolve all five files"

    def test_runner_normalize_aliases_canonical_only(self) -> None:
        # A *_skill alias must NO LONGER trigger canonical normalization.
        res = {"key_levels": {"support": [1], "resistance": [2]}}
        skills_runner._normalize_skill_contract(res, "price_action_skill")
        assert "pattern" not in res, "price_action_skill alias must be dropped"

        res = {"key_levels": {"support": [1], "resistance": [2]}, "range_status": "uptrend"}
        skills_runner._normalize_skill_contract(res, "price_action")
        assert res.get("pattern") is not None, "canonical price_action must normalize"
        assert res["pattern"] == "uptrend"

    def test_runner_feedback_aliases_canonical_only(self) -> None:
        payload = skills_runner._collect_skill_feedback(
            "price_action_skill", "BTCUSDT", "1h", 0.7, [], {"market_structure": "range", "confidence": 0.7},
        )
        assert payload is None, "price_action_skill alias must not fire feedback"


class TestRunnerNoPromptEmbedding:
    """E3: prompt.md is never embedded as a high-trust LLM instruction."""

    def test_run_skill_interpretation_has_no_prompt(self) -> None:
        repo = _FakeRepo()
        result = skills_runner._run_skill(
            repo,
            "price_action",
            "BTCUSDT",
            "1h",
            AT,
            {"symbol": "BTCUSDT"},
            lambda: {"market_structure": "trend", "confidence": 0.6, "key_levels": {"support": [1], "resistance": [2]}},
            skill_log_sink=[],
        )
        interpretation = result["ga_interpretation"]
        assert "prompt" not in interpretation, "prompt.md must NOT be embedded as ga_interpretation['prompt']"
        assert interpretation.get("untrusted_data") is True, "contract free text is tagged untrusted_data"
        # prompt.md may live only inside the audit-only skill_contract free text.
        audit = interpretation.get("skill_contract") or {}
        assert "prompt_md" in audit, "prompt.md retained as audit-only contract field"


class TestAnalysisToolBrokerReadOnly:
    """E4/E5/E6: read-only broker, enum params, timeout, size budget, schema."""

    def _broker(self, **kw) -> "AnalysisToolBroker":
        from plugins.crypto_guard.tools.analysis_tool_broker import AnalysisToolBroker

        return AnalysisToolBroker(_default_repo(), now_ms=AT, **kw)

    def test_five_readonly_methods_ok(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import AnalysisToolBroker

        broker = self._broker()
        assert AnalysisToolBroker.METHODS == {
            "latest_closed_market_summary",
            "deterministic_skill_evidence",
            "previous_round_state",
            "relevant_watch_evidence",
            "simulated_account_state",
        }
        env = broker.call("latest_closed_market_summary", symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT)
        assert env["ok"] is True and env["method"] == "latest_closed_market_summary"
        assert env["data"]["symbol"] == "BTCUSDT" and env["data"]["count"] == 2
        assert env["data"]["last_close"] == 60100.0

        env = broker.call("deterministic_skill_evidence", symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT)
        assert env["ok"] is True and env["data"]["skill_refs"]["price_action"] == 11

        env = broker.call("previous_round_state", symbol="BTCUSDT")
        assert env["ok"] is True and env["data"]["latest"]["signal_grade"] == "B"

        env = broker.call("relevant_watch_evidence", symbol="BTCUSDT", regime="normal")
        assert env["ok"] is True and env["data"]["count"] == 1

        env = broker.call("simulated_account_state")
        assert env["ok"] is True and env["data"]["open_orders_count"] == 1

    def test_forbidden_operations_rejected(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerForbiddenError

        broker = self._broker()
        for name in ("execute_sql", "web_search", "create_paper_order", "cancel_order",
                     "restart_service", "stop_service", "start_service", "write_config",
                     "add_symbol", "delete_symbol", "transfer_funds"):
            with pytest.raises(BrokerForbiddenError, match=name):
                broker.call(name, **{"query": "SELECT 1"} if name == "execute_sql" else {})
            with pytest.raises(BrokerForbiddenError, match=name):
                getattr(broker, name)
        with pytest.raises(BrokerForbiddenError):
            broker.call("not_a_broker_method")

    def test_strict_enum_params(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerParamError

        broker = self._broker()
        # lower-case / malformed symbol
        with pytest.raises(BrokerParamError):
            broker.call("latest_closed_market_summary", symbol="btcusdt", timeframe="1h", analysis_time_utc=AT)
        # unsupported timeframe (3m is explicitly not canonical)
        with pytest.raises(BrokerParamError):
            broker.call("latest_closed_market_summary", symbol="BTCUSDT", timeframe="3m", analysis_time_utc=AT)
        # unknown regime
        with pytest.raises(BrokerParamError):
            broker.call("relevant_watch_evidence", symbol="BTCUSDT", regime="bogus")

    def test_per_call_timeout_enforced(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerTimeoutError

        slow_repo = _default_repo(slow=True)
        from plugins.crypto_guard.tools.analysis_tool_broker import AnalysisToolBroker

        broker = AnalysisToolBroker(slow_repo, timeout_s=0.02, now_ms=AT)
        with pytest.raises(BrokerTimeoutError):
            broker.call("latest_closed_market_summary", symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT)

    def test_size_budget_enforced(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import BrokerSizeBudgetError

        repo = _default_repo(candles=[{"close_time": AT, "open_time": AT, "close": 1.0, "high": 1.0, "low": 1.0, "open": 1.0, "note": "x" * 2000}])
        from plugins.crypto_guard.tools.analysis_tool_broker import AnalysisToolBroker

        broker = AnalysisToolBroker(repo, max_result_bytes=64, now_ms=AT)
        with pytest.raises(BrokerSizeBudgetError):
            broker.call("latest_closed_market_summary", symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT)

    def test_result_schemas_conform(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import (
            RESULT_SCHEMAS,
            _validate_against_schema,
        )

        broker = self._broker()
        calls = [
            ("latest_closed_market_summary", dict(symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT)),
            ("deterministic_skill_evidence", dict(symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT)),
            ("previous_round_state", dict(symbol="BTCUSDT")),
            ("relevant_watch_evidence", dict(symbol="BTCUSDT", regime="normal")),
            ("simulated_account_state", {}),
        ]
        for method, kwargs in calls:
            env = broker.call(method, **kwargs)
            assert env["ok"] is True
            assert _validate_against_schema(env["data"], RESULT_SCHEMAS[method]) == [], method


class TestRounds:
    """E7/E8: single-round normal; supplement on conflict/watch-hit; verifier veto-only."""

    def _broker(self, **kw) -> "AnalysisToolBroker":
        from plugins.crypto_guard.tools.analysis_tool_broker import AnalysisToolBroker

        return AnalysisToolBroker(_default_repo(**{k: v for k, v in kw.items()}), now_ms=AT)

    def test_normal_single_round(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import run_analysis_rounds

        out = run_analysis_rounds(
            self._broker(), symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT,
            conflict=False, watch_hit=False, order_candidate=False,
        )
        labels = [r["round"] for r in out["rounds"]]
        assert labels == ["normal"], labels
        assert out["requests_used"] == 2
        assert out["verifier"] is None
        assert out["order_allowed"] is False

    def test_supplement_round_on_conflict(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import run_analysis_rounds

        out = run_analysis_rounds(
            self._broker(), symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT,
            conflict=True, watch_hit=False, order_candidate=False,
        )
        assert [r["round"] for r in out["rounds"]] == ["normal", "supplement"]
        assert out["requests_used"] == 4

    def test_supplement_round_on_watch_hit(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import run_analysis_rounds

        out = run_analysis_rounds(
            self._broker(), symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT,
            conflict=False, watch_hit=True, order_candidate=False,
        )
        assert [r["round"] for r in out["rounds"]] == ["normal", "supplement"]
        supplement = out["rounds"][1]
        methods = [out["requests"][i]["method"] for i in supplement["request_indexes"]]
        assert "relevant_watch_evidence" in methods

    def test_verifier_round_on_order_candidate(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import run_analysis_rounds

        out = run_analysis_rounds(
            self._broker(), symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT,
            conflict=False, watch_hit=False, order_candidate=True,
            deterministic_risk_ok=True,
        )
        assert [r["round"] for r in out["rounds"]] == ["normal", "verifier"]
        assert out["verifier"]["verdict"] == "approve"
        assert out["order_allowed"] is True

    def test_verifier_never_bypasses_risk_gate(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import run_analysis_rounds

        # Even with clean evidence, a blocked deterministic risk gate MUST veto.
        out = run_analysis_rounds(
            self._broker(), symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT,
            conflict=False, watch_hit=False, order_candidate=True,
            deterministic_risk_ok=False,
        )
        assert out["verifier"]["verdict"] == "veto"
        assert "deterministic_risk_gate_blocked" in out["verifier"]["reasons"]
        assert out["order_allowed"] is False

    def test_verifier_veto_on_evidence_unavailable(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import run_analysis_rounds

        repo = _FakeRepo(
            candles=_default_candles(AT),
            skill_refs=None,
            watches=[{"id": 1, "symbol": "BTCUSDT", "direction": "LONG", "status": "active", "watch_reason": "", "expires_at": None}],
            orders=[{"id": 1, "symbol": "BTCUSDT", "side": "LONG", "entry_price": 60000.0, "status": "open"}],
        )
        repo.latest_skill_result_refs = lambda symbol, at: (_ for _ in ()).throw(RuntimeError("boom"))
        from plugins.crypto_guard.tools.analysis_tool_broker import AnalysisToolBroker, run_analysis_rounds

        broker = AnalysisToolBroker(repo, now_ms=AT)
        out = run_analysis_rounds(
            broker, symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT,
            conflict=False, watch_hit=False, order_candidate=True, deterministic_risk_ok=True,
        )
        assert out["verifier"]["verdict"] == "veto"
        assert out["order_allowed"] is False

    def test_verifier_veto_on_concentration_breach(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import run_analysis_rounds

        orders = [{"id": i, "symbol": "BTCUSDT", "side": "LONG", "entry_price": 60000.0, "status": "open"} for i in range(1, 7)]
        out = run_analysis_rounds(
            self._broker(orders=orders), symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT,
            conflict=False, watch_hit=False, order_candidate=True, deterministic_risk_ok=True,
        )
        assert out["verifier"]["verdict"] == "veto"
        assert "concentration_breach" in out["verifier"]["reasons"]

    def test_verifier_never_emits_write_instruction(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import run_analysis_rounds

        out = run_analysis_rounds(
            self._broker(), symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT,
            conflict=False, watch_hit=False, order_candidate=True, deterministic_risk_ok=True,
        )
        assert "verdict" in out["verifier"]
        assert "approve" in (out["verifier"]["verdict"],)
        assert not any("create_order" in str(r) or "order_written" in str(r) for r in out["rounds"])

    def test_per_round_request_cap(self) -> None:
        from plugins.crypto_guard.tools.analysis_tool_broker import (
            MAX_TOOL_REQUESTS_PER_ROUND,
            BrokerRoundLimitError,
            BrokerRoundManager,
        )

        assert MAX_TOOL_REQUESTS_PER_ROUND == 3
        manager = BrokerRoundManager(self._broker())
        manager.begin_round("normal")
        manager.request("latest_closed_market_summary", symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT)
        manager.request("deterministic_skill_evidence", symbol="BTCUSDT", timeframe="1h", analysis_time_utc=AT)
        manager.request("simulated_account_state")
        with pytest.raises(BrokerRoundLimitError):
            manager.request("simulated_account_state")
