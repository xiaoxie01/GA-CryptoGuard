# -*- coding: utf-8 -*-
"""08-04 contract D (PRD): LLM prompt audit.

D1/D2/D4: each of the 11 ``run_agent_json_task`` task_names has its own
dedicated system prompt (``TASK_SYSTEM_PROMPTS``); none of them reuses the
market-decision ``GADecision`` marker; ``build_agent_json_task_prompt`` stops
prepending the global ``SYSTEM_PROMPT``; ``_call_ga_llm`` puts the task prompt
into ``session.system`` (thread-local override) and the user message does NOT
repeat it.

D3: every task_name has a ``schemas/<task_name>.schema.json`` with root
``additionalProperties: false``; ``run_agent_json_task`` always validates the
LLM candidate against the per-task schema and applies a per-task semantic
validation hook; an unknown top-level key / semantically-inconsistent candidate
fails closed to ``deterministic_fallback``.

D6 (evidence_id fail-closed): in ``_normalize_llm_decision`` an LLM-provided
trade_plan that explicitly claims ``evidence_refs`` which are NOT present in
the snapshot's deterministic evidence set is neutralized to
``monitor_only`` / no-plan / grade C (never order-eligible). Matching refs
preserve the plan; plans without refs on a grounded snapshot are preserved
(backward compatibility).

D7: a checklist test scans the codebase for every ``run_agent_json_task`` and
LLM prompt-builder call and asserts each is (a) documented in ``LLM_PROMPTS.md``
and (b) covered by a schema.

RED-first + revert-fail: every assertion here fails against the pre-fix code
(no TASK_SYSTEM_PROMPTS / per-task prompts, no per-task schemas, no candidate
validation, no evidence grounding gate) and passes after the fix. No production
DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import json
import re

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from unittest.mock import MagicMock, patch

from plugins.crypto_guard.config.loader import PLUGIN_ROOT

# The 11 task_names registered with run_agent_json_task (market-decision keeps
# SYSTEM_PROMPT and is NOT part of this map).
_TASKS = [
    "historical_replay_backtest_analysis",
    "daily_paper_review_summary",
    "trade_review_attribution",
    "opportunity_watch_review",
    "higher_timeframe_kline_summary",
    "hourly_alert_quality_brief",
    "paper_execution_quality_update",
    "strategy_version_management_summary",
    "candidate_strategy_config_review",
    "shadow_test_strategy_verdict",
    "self_evolution_candidate_patch",
]


def _llm_judge():
    from plugins.crypto_guard.reasoning import llm_agent_judge
    return llm_agent_judge


# ── D1/D2/D4: per-task system prompts, no GADecision marker ────────────────


class TestTaskSystemPrompts:
    """D1: TASK_SYSTEM_PROMPTS covers all 11 task_names. D2: no generic task
    prompt mentions GADecision. D4: the task prompt lands in session.system."""

    def test_task_system_prompts_cover_all_11(self) -> None:
        judge = _llm_judge()
        prompts = judge.TASK_SYSTEM_PROMPTS
        missing = [t for t in _TASKS if t not in prompts]
        assert not missing, f"D1: TASK_SYSTEM_PROMPTS missing task_names: {missing}"
        for t in _TASKS:
            assert isinstance(prompts[t], str) and prompts[t].strip(), f"D1: empty prompt for {t}"

    def test_no_generic_prompt_mentions_gadecision(self) -> None:
        judge = _llm_judge()
        for t in _TASKS:
            sp = judge.TASK_SYSTEM_PROMPTS[t]
            assert "GADecision" not in sp, f"D2: task {t} prompt reuses GADecision marker"
            built = judge.build_agent_json_task_prompt(
                task_name=t, payload={"p": 1}, fallback={"summary": "x"},
            )
            assert "GADecision" not in built, f"D2: task {t} prompt payload reuses GADecision marker"
            # D4: the build output IS the user-message text; the per-task prompt
            # is routed to ``session.system`` ONLY (thread-local override), so
            # the user message must NOT repeat it (previously asserted the
            # opposite — that stale assertion pinned the duplicated prompt).
            assert sp not in built, (
                f"D4: task {t} build output (user message) must not embed the "
                "per-task system prompt"
            )

    def test_build_agent_json_task_prompt_does_not_prepend_global_system(self) -> None:
        judge = _llm_judge()
        built = judge.build_agent_json_task_prompt(
            task_name="opportunity_watch_review",
            payload={"watch": {"id": 1}},
            fallback={"summary": "x", "status": "waiting", "action": "keep_waiting", "risk_notes": []},
        )
        # Must NOT prepend the global market-decision SYSTEM_PROMPT.
        assert judge.SYSTEM_PROMPT not in built, (
            "D2: build_agent_json_task_prompt must not prepend the global SYSTEM_PROMPT"
        )
        # The body must still parse as strict JSON after the system prompt.
        body_start = built.find("{")
        assert body_start >= 0, "D2: build output must contain a JSON body"
        body = json.loads(built[body_start:])
        assert body["task_name"] == "opportunity_watch_review"

    def test_call_ga_llm_puts_task_prompt_in_session_system(self) -> None:
        judge = _llm_judge()
        task_prompt = judge.TASK_SYSTEM_PROMPTS["hourly_alert_quality_brief"]
        # Drive the REAL builder output through ``_call_ga_llm`` so the
        # "user message does not repeat the system prompt" assertion is NOT
        # vacuous (a hardcoded dummy payload would satisfy it trivially).
        real_prompt = judge.build_agent_json_task_prompt(
            task_name="hourly_alert_quality_brief",
            payload={"brief": {"ok": True}},
            fallback={"summary": "x", "grade": "B", "issues": []},
        )
        captured = MagicMock()
        captured.read_timeout = 30
        captured.raw_ask.return_value = ["{}"]

        # The prompt builder (D4) stashes the task prompt as the system override.
        judge._llm_call_state.system_override = task_prompt
        try:
            with patch.object(judge, "_resolve_llm_config_name", return_value="test"):
                with patch("llmcore.resolve_session", return_value=captured):
                    judge._call_ga_llm(real_prompt)
        finally:
            if hasattr(judge._llm_call_state, "system_override"):
                delattr(judge._llm_call_state, "system_override")

        assert captured.system == task_prompt, (
            f"D4: session.system must be the task prompt, got {captured.system!r}"
        )
        assert captured.system != judge.SYSTEM_PROMPT, (
            "D4: session.system must NOT be the global SYSTEM_PROMPT"
        )
        # The user message must not repeat the system prompt.
        raw_args = captured.raw_ask.call_args
        assert raw_args is not None, "D4: _call_ga_llm must invoke raw_ask"
        content = raw_args[0][0]
        user_text = content[0]["content"][0]["text"]
        assert task_prompt not in user_text, (
            "D4: user message must not duplicate the system prompt"
        )
        # The user message must still carry the JSON task body (the builder
        # output), not be emptied by the D4 de-duplication.
        assert json.loads(user_text)["task_name"] == "hourly_alert_quality_brief", (
            "D4: user message must be the JSON task body"
        )


# ── D3: per-task schemas + always-on candidate validation + semantic hooks ──


class TestTaskSchemas:
    """D3: every task_name has a schema with root additionalProperties=false;
    run_agent_json_task validates the candidate and runs the semantic hook."""

    def test_all_task_schemas_exist_and_enforce_root_additional_properties_false(self) -> None:
        judge = _llm_judge()
        schema_dir = PLUGIN_ROOT / "schemas"
        for t in _TASKS:
            schema_file = judge.TASK_SCHEMAS.get(t)
            assert schema_file, f"D3: no schema registered for task {t}"
            assert schema_file.endswith(".schema.json"), f"D3: {t} schema name malformed: {schema_file}"
            path = schema_dir / schema_file
            assert path.exists(), f"D3: missing schema file {path}"
            doc = json.loads(path.read_text(encoding="utf-8"))
            assert doc.get("additionalProperties") is False, (
                f"D3: {schema_file} root additionalProperties must be false"
            )
            assert doc.get("type") == "object", f"D3: {schema_file} must be an object schema"

    def test_run_agent_json_task_rejects_candidate_with_unknown_top_level_key(self) -> None:
        judge = _llm_judge()
        bad_candidate = {"summary": "ok", "focus_symbols": ["BTCUSDT"], "unknown_key": 1}
        with patch.object(judge, "_call_ga_llm", return_value=json.dumps(bad_candidate, ensure_ascii=False)):
            result = judge.run_agent_json_task(
                task_name="hourly_alert_quality_brief",
                payload={"signals": []},
                fallback={"summary": "f", "focus_symbols": [], "why_no_opportunity": "", "next_checks": []},
                use_llm=True,
            )
        assert result["llm_status"] == "failed", (
            f"D3: unknown top-level key must fail closed, got llm_status={result['llm_status']!r}"
        )
        assert result["agent_source"] == "deterministic_fallback"
        assert "unknown_key" not in result, "D3: rejected candidate must not leak unknown_key"

    def test_run_agent_json_task_accepts_valid_candidate(self) -> None:
        judge = _llm_judge()
        good = {"summary": "ok", "focus_symbols": ["BTCUSDT"], "why_no_opportunity": "", "next_checks": []}
        with patch.object(judge, "_call_ga_llm", return_value=json.dumps(good, ensure_ascii=False)):
            result = judge.run_agent_json_task(
                task_name="hourly_alert_quality_brief",
                payload={"signals": []},
                fallback={"summary": "f", "focus_symbols": [], "why_no_opportunity": "", "next_checks": []},
                use_llm=True,
            )
        assert result["llm_status"] == "ok", f"D3: valid candidate must pass, got {result['llm_status']!r}"
        assert result["summary"] == "ok"
        assert result["agent_source"] == "llm_agent"

    def test_run_agent_json_task_semantic_hook_rejects_inconsistent_candidate(self) -> None:
        judge = _llm_judge()
        # needs_patch=True but patch is None -> semantically inconsistent (schema-valid).
        bad = {"needs_patch": True, "patch": None, "rationale": "x"}
        with patch.object(judge, "_call_ga_llm", return_value=json.dumps(bad, ensure_ascii=False)):
            result = judge.run_agent_json_task(
                task_name="self_evolution_candidate_patch",
                payload={"aggregation": {}},
                fallback={"patch": None, "rationale": "f", "needs_patch": False},
                use_llm=True,
            )
        assert result["llm_status"] == "failed", (
            f"D3 semantic hook: needs_patch=True with patch=None must fail closed, "
            f"got llm_status={result['llm_status']!r}"
        )


# ── D6: evidence_id fail-closed in _normalize_llm_decision ──────────────────


def _snapshot_with_module(*, as_of: int = 1_700_000_000_000, tf: str = "15m") -> dict:
    # partial_tf_mode: True isolates the D6 evidence-grounding gate from the
    # unrelated market_semantics timeframe fail-close (codes=['data_incomplete']).
    # With partial_tf_mode the strict close check is skipped, so the ONLY thing
    # that can neutralize an LLM plan here is the D6 evidence-id gate.
    return {
        "symbol": "BTCUSDT",
        "analysis_time_utc": as_of,
        "partial_tf_mode": True,
        "modules": {
            "smc": {"timeframe": tf, "as_of": as_of, "direction": "bullish"},
            "price_action": {"timeframe": tf, "as_of": as_of, "structure": "range"},
        },
    }


def _base_fallback(symbol: str = "BTCUSDT", analysis_time_utc: int = 1_700_000_000_000) -> dict:
    return {
        "symbol": symbol,
        "analysis_time_utc": analysis_time_utc,
        "decision": "trade_plan_available",
        "signal_grade": "A",
        "market_bias": "bullish",
        "trend_stage": "early",
        "confidence": 0.8,
        "summary": "det",
        "evidence": [],
        "counter_evidence": ["r"],
        "risk_notes": [],
        "has_trade_plan": True,
        "trade_plan": {"side": "LONG", "entry_type": "limit", "entry_price": 100.0, "stop_loss": 95.0,
                       "take_profits": [{"price": 110.0, "ratio": 1.0}], "risk_percent": 1.0, "reason": "det"},
        "opportunity_watch": None,
        "suggested_actions": ["create_paper_order"],
        "analysis_source": "llm_agent",
        "llm_status": "ok",
    }


def _candidate_with_plan(evidence_refs) -> dict:
    return {
        "decision": "trade_plan_available",
        "signal_grade": "A",
        "market_bias": "bullish",
        "confidence": 0.8,
        "has_trade_plan": True,
        "trade_plan": {
            "side": "LONG", "entry_type": "limit", "entry_price": 100.0,
            "stop_loss": 95.0, "take_profits": [{"price": 110.0, "ratio": 1.0}],
            "risk_percent": 1.0, "reason": "llm", "evidence_refs": evidence_refs,
        },
        "suggested_actions": ["create_paper_order"],
    }


class TestEvidenceIdFailClosed:
    """D6: an LLM trade_plan that explicitly claims evidence_refs not backed by
    the snapshot's deterministic evidence is neutralized (never order-eligible).
    Matching refs preserve the plan; refs-absent plans on a grounded snapshot
    are preserved."""

    def test_fabricated_evidence_refs_neutralize_trade_plan(self) -> None:
        judge = _llm_judge()
        snapshot = _snapshot_with_module(as_of=1_700_000_000_000, tf="15m")
        candidate = _candidate_with_plan(["smc:BTCUSDT:15m:9999999999999"])  # fabricated as_of
        decision = judge._normalize_llm_decision(candidate, snapshot, _base_fallback())
        assert decision["has_trade_plan"] is False, (
            "D6: fabricated evidence_refs must neutralize the trade plan"
        )
        assert decision["trade_plan"] is None
        assert decision["decision"] == "monitor_only"
        assert decision["signal_grade"] == "C"
        assert "create_paper_order" not in (decision.get("suggested_actions") or [])
        notes = " ".join(str(n) for n in decision.get("risk_notes") or [])
        assert "evidence" in notes and "fail-closed" in notes, (
            "D6: neutralization must record an evidence fail-closed risk note"
        )

    def test_matching_evidence_refs_preserve_trade_plan(self) -> None:
        judge = _llm_judge()
        as_of = 1_700_000_000_000
        snapshot = _snapshot_with_module(as_of=as_of, tf="15m")
        candidate = _candidate_with_plan([f"smc:BTCUSDT:15m:{as_of}"])
        decision = judge._normalize_llm_decision(candidate, snapshot, _base_fallback())
        assert decision["has_trade_plan"] is True, "D6: matching evidence_refs must preserve the plan"
        assert decision["trade_plan"] is not None
        assert decision["decision"] == "trade_plan_available"

    def test_llm_plan_without_refs_preserved_when_grounded(self) -> None:
        judge = _llm_judge()
        snapshot = _snapshot_with_module(as_of=1_700_000_000_000, tf="15m")
        candidate = _candidate_with_plan([])  # no explicit evidence claim
        decision = judge._normalize_llm_decision(candidate, snapshot, _base_fallback())
        assert decision["has_trade_plan"] is True, (
            "D6: a plan without explicit evidence_refs on a grounded snapshot is preserved "
            "(backward compatibility)"
        )

    def test_ungrounded_snapshot_with_claimed_refs_neutralizes(self) -> None:
        judge = _llm_judge()
        snapshot = {
            "symbol": "BTCUSDT",
            "analysis_time_utc": 1_700_000_000_000,
            "partial_tf_mode": True,  # isolate the D6 gate (see _snapshot_with_module)
            "modules": {},
        }
        candidate = _candidate_with_plan(["smc:BTCUSDT:15m:1_700_000_000_000"])
        decision = judge._normalize_llm_decision(candidate, snapshot, _base_fallback())
        assert decision["has_trade_plan"] is False, (
            "D6: an ungrounded snapshot (no deterministic modules) cannot ground any "
            "claimed evidence -> neutralized"
        )


# ── D7: checklist — every LLM builder/call documented + schema-covered ──────


class TestPromptAuditChecklist:
    """D7: a checklist test scans the codebase and asserts each production
    run_agent_json_task / prompt-builder call is (a) documented in
    LLM_PROMPTS.md and (b) covered by a schema."""

    def _task_names_in_source(self) -> set[str]:
        judge = _llm_judge()
        found: set[str] = set()
        root = PLUGIN_ROOT
        for py in root.rglob("*.py"):
            if "tests" in py.parts:
                continue
            text = py.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r'task_name\s*=\s*"([A-Za-z0-9_]+)"', text):
                found.add(m.group(1))
        return found

    def test_all_task_call_sites_documented_and_schema_covered(self) -> None:
        judge = _llm_judge()
        md_path = PLUGIN_ROOT / "LLM_PROMPTS.md"
        md = md_path.read_text(encoding="utf-8")
        found = self._task_names_in_source()
        for t in sorted(found):
            assert t in _TASKS, f"D7: unknown task_name in source: {t}"
            assert t in md, f"D7: task {t} not documented in LLM_PROMPTS.md"
            assert t in judge.TASK_SCHEMAS, f"D7: task {t} not schema-covered"

    def test_prompt_builders_documented(self) -> None:
        md = (PLUGIN_ROOT / "LLM_PROMPTS.md").read_text(encoding="utf-8")
        builders = [
            "build_llm_decision_prompt",
            "build_llm_strict_json_prompt",
            "build_llm_minimal_safe_prompt",
            "build_agent_json_task_prompt",
        ]
        for b in builders:
            assert b in md, f"D7: prompt builder {b} not documented in LLM_PROMPTS.md"
