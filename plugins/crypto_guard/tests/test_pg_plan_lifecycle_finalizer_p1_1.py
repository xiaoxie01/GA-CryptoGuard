# -*- coding: utf-8 -*-
"""08-02 P1-1: single final plan-lifecycle normalizer (controller path).

Audit: the controller carried a scattered plan_execution_state override block
that only patched 3 cases and left "otherwise keep upstream state" — so the
disabled-with-plan path persisted ``plan_execution_state="confirmed"`` +
``plan_status="withheld"`` (confirmed+withheld contradiction, the 30 production
``confirmed_without_plan`` rows) and the account-risk-rejected path fell to
``no_candidate`` even though the LLM HAD confirmed the plan (hiding the real
risk rejection).

PRD P1-1 verbatim:
- Run exactly once after all risk/hysteresis/clamp/performance/continuity
  gates, before action builder and persistence.
- confirmed requires: LLM confirmed AND final has_trade_plan=true AND non-empty
  trade_plan AND risk_ok=true AND effective grade S/A.
- candidate exists but LLM not confirmed -> unconfirmed.
- plan rejected by risk -> risk_rejected.
- continuity inverted -> invalidated.
- no candidate and no plan -> no_candidate.
- FORBIDDEN "otherwise keep upstream state".
- plan_status / plan_origin / plan_blockers set atomically by the finalizer;
  forbidden: confirmed+withheld, no_candidate+candidate, executable+no-plan.

RED-first: T2 (disabled-with-plan) and T5 (account risk rejected after LLM
confirmation) assert the CORRECT finalizer states and FLIP RED against the
current scattered override block. T1/T3/T4/T6/T7 assert stable states that must
survive the refactor. Control test ``test_revert_fail_scattered_override``
proves the upstream disabled path really produces the confirmed+withheld
contradiction that the finalizer is load-bearing for.

Real production paths only: real ``GAMasterController(repo).analyze_symbol`` on
a real per-test PostgreSQL schema (``make_repo``). No mocking of the function
under test. LLM is not called on any path (preset-candidate injection or
``CRYPTO_GUARD_LLM_ANALYSIS=0`` disabled path).
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.ga_master.controller import GAMasterController
from plugins.crypto_guard.ga_master.decision_schema import GAAnalysisRequest
from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_sop_decision
from plugins.crypto_guard.reasoning.watch_conditions import is_structured_watch
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
    """Deterministic no-plan fixture: raw SOP yields ``no_plan`` with no
    trade_plan / no candidate (monitor_only / C grade)."""
    return _build_snapshot(symbol="DOGEUSDT", bias="neutral", stage="range",
                           structure="range", momentum_dir="neutral",
                           candles_count=200)


def _entry_conf() -> dict:
    return {"type": "closed_candle_confirmation", "timeframe": "15m",
            "event_type": "BOS", "direction": "bullish",
            "candle_close_time": _AT_MS - 900_000, "price": 99.5,
            "source": "price_action", "symbol": "ADAUSDT"}


def _llm_confirmed_candidate(snapshot: dict, *, tp_override: dict | None = None,
                             entry_conf: dict | None = None) -> dict:
    """Build the fair-adapter-shaped LLM-confirmed preset candidate.

    Shape mirrors the real ``_normalize_llm_decision`` success envelope: the
    deterministic raw SOP decision with an executable trade_plan,
    ``plan_origin="llm_confirmed"`` / ``plan_execution_state="confirmed"`` /
    ``plan_status="executable"`` / ``candidate_trade_plan=None`` (confirmed
    rows do NOT carry a separate withheld candidate) and a full §8
    attempt_meta envelope with ``llm_status="ok"``.
    """
    raw = run_ga_sop_decision(snapshot)
    tp = dict(raw.get("trade_plan") or {})
    if entry_conf is not None:
        tp["entry_trigger_confirmation"] = entry_conf
    if tp_override:
        tp.update(tp_override)
    candidate = dict(raw)
    candidate["has_trade_plan"] = True
    candidate["trade_plan"] = tp
    candidate["candidate_trade_plan"] = None
    candidate["plan_origin"] = "llm_confirmed"
    candidate["plan_execution_state"] = "confirmed"
    candidate["plan_status"] = "executable"
    candidate["plan_blockers"] = []
    candidate["decision"] = "trade_plan_available"
    # §8 attempt_meta envelope (llm_status=ok -> provider call succeeded).
    candidate.update({
        "llm_status": "ok",
        "llm_model": "test-model",
        "llm_terminal_reason": None,
        "llm_fallback_reason": None,
        "llm_attempt_count": 1,
        "llm_provider_call_count": 1,
        "llm_latency_ms": 120,
        "llm_prompt_bytes": 1000,
        "llm_continuity_included": False,
        "llm_schedule_round": 1,
        "llm_schedule_position": 0,
        "llm_error": None,
        "llm_error_category": None,
        "llm_error_stage": None,
        "llm_repair_event": None,
    })
    return candidate


def _no_plan_candidate(symbol: str) -> dict:
    """LLM call succeeded but produced NO plan (monitor_only / no edge). The
    success path keeps ``plan_origin`` at the fallback's value (never
    llm_confirmed) — mirroring ``_normalize_llm_decision``."""
    return {
        "symbol": symbol,
        "analysis_time_utc": _AT_MS,
        "decision": "monitor_only",
        "signal_grade": "C",
        "market_bias": "neutral",
        "trend_stage": "range",
        "confidence": 0.45,
        "summary": f"{symbol} no edge",
        "evidence": ["no edge"],
        "counter_evidence": [],
        "risk_notes": [],
        "has_trade_plan": False,
        "trade_plan": None,
        "candidate_trade_plan": None,
        "opportunity_watch": None,
        "suggested_actions": ["monitor_only"],
        "plan_origin": "deterministic_sop",
        "plan_execution_state": "no_candidate",
        "plan_status": "no_plan",
        "plan_blockers": [],
        "plan_source": "deterministic_sop",
        "llm_status": "ok",
        "llm_model": "test-model",
        "llm_terminal_reason": None,
        "llm_fallback_reason": None,
        "llm_attempt_count": 1,
        "llm_provider_call_count": 1,
        "llm_latency_ms": 90,
        "llm_prompt_bytes": 800,
        "llm_continuity_included": False,
        "llm_schedule_round": 1,
        "llm_schedule_position": 1,
        "llm_error": None,
        "llm_error_category": None,
        "llm_error_stage": None,
        "llm_repair_event": None,
    }


def _blocker_codes(raw: dict) -> list[str]:
    blockers = raw.get("plan_blockers") or []
    return [str(b.get("code") or "") for b in blockers if isinstance(b, dict)]


def _run_controller(repo, snapshot: dict, *, symbol: str | None = None,
                    preset_candidate: dict | None = None,
                    preset_attempt_meta: dict | None = None,
                    use_llm: bool | None = None,
                    account_equity: float | None = None) -> dict:
    """Run the real controller on the real repo and return the persisted
    ``raw_decision_json`` (top-level, JSONB-deserialized)."""
    symbol = symbol or str(snapshot.get("symbol") or "ADAUSDT")
    if account_equity is not None:
        # Seed the ONLY paper account (production default name) at a drawdown
        # that trips hard_risk_off (equity <= 97% of initial 10000).
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
        # Preset tests MUST take the preset (LLM-enabled) path; disabled tests
        # MUST take the deterministic-only path. Set explicitly, never inherit.
        os.environ["CRYPTO_GUARD_LLM_ANALYSIS"] = "0" if use_llm is False else "1"
        snapshot_id = repo.save_market_snapshot(snapshot)
        request = GAAnalysisRequest(
            symbol=symbol, decision_type="scheduled_analysis",
            snapshot=snapshot, snapshot_id=snapshot_id,
            mode="scheduled", batch_id=f"15m:{snapshot.get('analysis_time_utc')}",
        )
        controller = GAMasterController(repo)
        ga_decision = controller.analyze_symbol(
            request,
            preset_llm_candidate=preset_candidate,
            preset_llm_attempt_meta=preset_attempt_meta,
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


# ── tests ───────────────────────────────────────────────────────────────────


class TestPlanLifecycleFinalizerP1_1:
    def test_t1_confirmed_happy_path(self) -> None:
        """GREEN both: raw deterministic S/A + explicit LLM confirmation +
        risk pass -> final confirmed / executable / S/A / create_paper_order."""
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            candidate = _llm_confirmed_candidate(snap, entry_conf=_entry_conf())
            raw = _run_controller(handle.repo, snap, preset_candidate=candidate)
            assert raw["plan_execution_state"] == "confirmed", raw.get("plan_execution_state")
            assert raw["plan_origin"] == "llm_confirmed", raw.get("plan_origin")
            assert raw["plan_status"] == "executable", raw.get("plan_status")
            tp = raw.get("trade_plan") or {}
            assert isinstance(tp, dict) and tp.get("side") == "LONG", tp
            assert raw["decision"] == "create_paper_order", raw.get("decision")
            assert raw["effective_signal_grade"] in ("S", "A"), raw.get("effective_signal_grade")
            assert raw["llm_status"] == "ok", raw.get("llm_status")
            assert raw["candidate_trade_plan"] == raw.get("trade_plan"), (
                "confirmed rows surface the executable plan as candidate too")
            assert not _blocker_codes(raw), raw.get("plan_blockers")
        finally:
            handle.close()

    def test_t2_disabled_with_plan_unconfirmed_not_confirmed(self) -> None:
        """RED->GREEN: deterministic-only (LLM disabled) path with a plan. The
        upstream path leaves plan_execution_state=confirmed + plan_status=
        withheld (the confirmed+withheld contradiction). The finalizer MUST
        flip it to unconfirmed + deterministic_fallback + withheld, never
        confirmed."""
        handle = make_repo()
        try:
            raw = _run_controller(handle.repo, _bullish_snapshot(), use_llm=False)
            assert raw["plan_execution_state"] == "unconfirmed", (
                f"disabled-with-plan must be unconfirmed, not {raw.get('plan_execution_state')!r} "
                f"(confirmed+withheld is FORBIDDEN)")
            assert raw["plan_origin"] == "deterministic_fallback", raw.get("plan_origin")
            assert raw["plan_status"] == "withheld", raw.get("plan_status")
            assert raw["llm_status"] == "disabled", raw.get("llm_status")
            assert raw["llm_terminal_reason"] == "llm_disabled"
            assert raw.get("trade_plan") is None
            assert isinstance(raw.get("candidate_trade_plan"), dict), (
                "the withheld deterministic plan must be preserved as candidate")
            assert "llm_disabled" in _blocker_codes(raw), raw.get("plan_blockers")
            assert raw["decision"] != "create_paper_order", raw.get("decision")
        finally:
            handle.close()

    def test_t3_llm_success_no_plan_no_candidate(self) -> None:
        """GREEN both: LLM call succeeded but produced no plan -> no_candidate
        / no_plan, NEVER llm_confirmed (nothing was confirmed)."""
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            candidate = _no_plan_candidate("ADAUSDT")
            raw = _run_controller(handle.repo, snap, preset_candidate=candidate)
            assert raw["plan_execution_state"] == "no_candidate", raw.get("plan_execution_state")
            assert raw["plan_status"] == "no_plan", raw.get("plan_status")
            assert raw["plan_origin"] != "llm_confirmed", raw.get("plan_origin")
            assert raw["llm_status"] == "ok", raw.get("llm_status")
            assert raw.get("trade_plan") is None
            assert raw.get("candidate_trade_plan") is None
            assert raw["decision"] != "create_paper_order", raw.get("decision")
        finally:
            handle.close()

    def test_t4_llm_confirmed_plan_rejected_by_risk(self) -> None:
        """GREEN both: LLM confirmed a plan that risk rejects (RR below
        threshold) -> risk_rejected with the real RR blocker, never
        'unconfirmed' (the LLM DID confirm; risk refused it)."""
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            candidate = _llm_confirmed_candidate(
                snap, entry_conf=_entry_conf(),
                tp_override={"take_profits": [
                    {"price": 101.0, "ratio": 0.5},
                    {"price": 101.5, "ratio": 0.5},
                ]},
            )
            raw = _run_controller(handle.repo, snap, preset_candidate=candidate)
            assert raw["plan_execution_state"] == "risk_rejected", raw.get("plan_execution_state")
            assert raw["plan_status"] == "risk_rejected", raw.get("plan_status")
            assert raw["llm_status"] == "ok", raw.get("llm_status")
            assert isinstance(raw.get("candidate_trade_plan"), dict), (
                "the rejected plan must be preserved as candidate")
            assert raw.get("trade_plan") is None
            assert "risk_rejected" in _blocker_codes(raw), raw.get("plan_blockers")
            assert raw["decision"] != "create_paper_order", raw.get("decision")
        finally:
            handle.close()

    def test_t5_llm_confirmed_account_risk_rejected(self) -> None:
        """RED->GREEN: LLM confirmed a valid plan, apply_risk passed it, but
        the account gate (hard_risk_off) blocks -> risk_rejected with the real
        account reason, NOT no_candidate (the plan existed and was rejected by
        risk, so no_candidate would hide the rejection)."""
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            candidate = _llm_confirmed_candidate(snap, entry_conf=_entry_conf())
            raw = _run_controller(handle.repo, snap, preset_candidate=candidate,
                                  account_equity=9600.0)
            assert raw["plan_execution_state"] == "risk_rejected", (
                f"account-risk rejection of an LLM-confirmed plan must be "
                f"risk_rejected, not {raw.get('plan_execution_state')!r}")
            assert raw["plan_status"] == "risk_rejected", raw.get("plan_status")
            assert raw["llm_status"] == "ok", raw.get("llm_status")
            risk = raw.get("risk_check") or {}
            assert (risk.get("account_risk") or {}).get("hard_risk_off") is True, risk
            assert "risk_rejected" in _blocker_codes(raw), raw.get("plan_blockers")
            assert raw["decision"] != "create_paper_order", raw.get("decision")
        finally:
            handle.close()

    def test_t6_disabled_no_plan_no_candidate(self) -> None:
        """GREEN both: deterministic-only path with NO plan and NO candidate ->
        no_candidate / no_plan."""
        handle = make_repo()
        try:
            raw = _run_controller(handle.repo, _neutral_snapshot(), use_llm=False)
            assert raw["plan_execution_state"] == "no_candidate", raw.get("plan_execution_state")
            assert raw["plan_status"] == "no_plan", raw.get("plan_status")
            assert raw["llm_status"] == "disabled", raw.get("llm_status")
            assert raw.get("trade_plan") is None
            assert raw.get("candidate_trade_plan") is None
            assert raw["decision"] != "create_paper_order", raw.get("decision")
        finally:
            handle.close()

    def test_t7_continuity_inverted_invalidated(self) -> None:
        """GREEN both: continuity gate invalidated the prior trigger ->
        invalidated + withheld, candidate preserved."""
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            snap["analysis_continuity"] = {
                "contract_version": "analysis_continuity_v1",
                "delta": {"trigger_progress": [
                    {"type": "breakout_confirm", "status": "invalidated",
                     "candle_close_time": _AT_MS - 900_000},
                ]},
            }
            raw = _run_controller(handle.repo, snap, use_llm=False)
            assert raw["plan_execution_state"] == "invalidated", raw.get("plan_execution_state")
            assert raw["plan_status"] == "withheld", raw.get("plan_status")
            assert "continuity_trigger_invalidated" in _blocker_codes(raw), (
                raw.get("plan_blockers"))
            assert isinstance(raw.get("candidate_trade_plan"), dict), (
                "the invalidated plan must be preserved as candidate")
            assert raw.get("trade_plan") is None
            assert raw["decision"] != "create_paper_order", raw.get("decision")
        finally:
            handle.close()

    def test_revert_fail_scattered_override_upstream_contradiction(self) -> None:
        """Revert-fail control: the upstream disabled path really produces the
        confirmed+withheld contradiction the finalizer is load-bearing for. If
        the scattered override block (or the finalizer) is ever removed, this
        proves T2's assertion is load-bearing."""
        snap = _bullish_snapshot()
        old = run_agent_sop_decision(snap, use_llm=False)
        assert old.get("llm_status") == "disabled"
        assert old.get("plan_origin") == "deterministic_sop"
        assert old.get("plan_execution_state") == "confirmed", (
            "upstream disabled-with-plan path sets confirmed (the contradiction "
            f"the finalizer must fix); got {old.get('plan_execution_state')!r}")
        assert old.get("plan_status") == "withheld"
        assert old.get("has_trade_plan") is False
        assert isinstance(old.get("candidate_trade_plan"), dict)
        assert "llm_disabled" in _blocker_codes(old), old.get("plan_blockers")

    def test_t8_unstructured_watch_does_not_re_advertise_opportunity_watch(
            self) -> None:
        """Finding 2 (P2): build_feishu_actions emits ``create_opportunity_watch``
        unconditionally for WATCH_GRADES (B) and PUSH_GRADES (S/A) whether or not
        a structured opportunity watch was actually materialized. A decision whose
        opportunity_watch is NOT structured (here: the ``{"needed": True,
        "direction": "bidirectional"}`` blob the P0-2 weak path produces, which
        survives to the persisted envelope) must NOT re-advertise a watch the
        funnel never created. The controller filter strips it; the persisted
        raw_decision_json stores actions at top level under ``feishu_actions``
        (decision_schema §8), which drives the hourly report funnel.

        Control run: a genuinely structured watch keeps ``create_opportunity_watch``
        (no over-strip), and both paths keep the S+plan+risk_ok paper-order action.
        """
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            # Control: structured watch from the raw SOP -> keep the action.
            structured = _llm_confirmed_candidate(snap, entry_conf=_entry_conf())
            raw_struct = _run_controller(handle.repo, snap, preset_candidate=structured)
            struct_actions = raw_struct.get("feishu_actions") or []
            assert "create_opportunity_watch" in struct_actions, struct_actions
            assert "create_paper_order" in struct_actions, struct_actions
            assert is_structured_watch(structured["opportunity_watch"]), (
                structured["opportunity_watch"])
            # Finding 2: unstructured watch survives to legacy -> strip.
            cand = _llm_confirmed_candidate(snap, entry_conf=_entry_conf())
            cand["opportunity_watch"] = {"needed": True, "direction": "bidirectional"}
            raw = _run_controller(handle.repo, snap, preset_candidate=cand)
            assert raw["plan_execution_state"] == "confirmed", raw.get("plan_execution_state")
            assert raw["plan_status"] == "executable", raw.get("plan_status")
            assert raw["effective_signal_grade"] == "S", raw.get("effective_signal_grade")
            actions = raw.get("feishu_actions") or []
            assert "create_opportunity_watch" not in actions, (
                f"unstructured watch must not re-advertise create_opportunity_watch: {actions}")
            assert "create_paper_order" in actions, (
                f"S + confirmed plan + risk_ok keeps the paper-order action: {actions}")
        finally:
            handle.close()

    def test_t9_grade_clamped_confirmed_plan_has_no_false_risk_blocker(
            self) -> None:
        """Finding 4 (P2): an LLM-confirmed plan whose grade is clamped below
        S/A (counter-evidence clamp S->B) while the risk engine PASSES lands in
        the risk_rejected branch. The blocker detail must describe the actual
        refusal — the grade clamp — not a fabricated 风控 claim: risk.ok is True
        and risk.reasons is empty, so the old default "风控未通过，禁止开仓"
        mislabelled a grade-gate decision as a risk failure.
        """
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            cand = _llm_confirmed_candidate(snap, entry_conf=_entry_conf())
            # Clamp S/A -> B via the counter-evidence cap
            # (SA_MAX_COUNTER_EVIDENCE = 3). The risk engine does not gate on
            # counter_evidence count, so risk passes while the clamp fires.
            cand["counter_evidence"] = ["矛盾证据1", "矛盾证据2", "矛盾证据3"]
            raw = _run_controller(handle.repo, snap, preset_candidate=cand)
            assert raw["plan_execution_state"] == "risk_rejected", (
                raw.get("plan_execution_state"))
            assert raw["plan_status"] == "risk_rejected", raw.get("plan_status")
            assert raw["effective_signal_grade"] == "B", raw.get("effective_signal_grade")
            risk = raw.get("risk_check") or {}
            assert risk.get("ok") is True, risk
            assert raw.get("trade_plan") is None
            assert isinstance(raw.get("candidate_trade_plan"), dict), (
                "the clamped plan must be preserved as candidate")
            rejected = [
                b for b in (raw.get("plan_blockers") or [])
                if isinstance(b, dict) and b.get("code") == "risk_rejected"
            ]
            assert rejected, raw.get("plan_blockers")
            detail = str(rejected[0].get("detail") or "")
            assert "风控未通过" not in detail, detail
            assert "不足 S/A" in detail, detail
            assert "clamp_sa_evidence" in detail, detail
        finally:
            handle.close()
