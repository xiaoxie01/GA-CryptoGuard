# -*- coding: utf-8 -*-
"""08-10 Step 9 producer RED contract: persist risk-governance audit (P1).

Contract under test (prd.md P1-3/P1-6, design.md §11 Stage B, implement.md
Step 9 item 1, research/producer-gap-2026-08-11.md):

  The production watch-recheck seam MUST SEPARATELY persist candidate,
  lifecycle, proposal, normalized plan, verifier, final risk result, mode,
  policy version and latency as audit fields (``entry_confirmation_lifecycle`` /
  ``llm_risk_proposal`` / ``risk_adjustment_verification`` / ``risk_advisory``
  + ``policy_version`` + ``llm_latency_ms`` + ``evidence_ids``) via a narrow
  ``ga_decisions`` UPDATE by ``ga_decision_id``.

  - ``mode=off`` -> legacy path byte-for-byte: no envelope, no LLM call, no
    persist.
  - ``shadow`` / ``paper_bounded`` -> ALWAYS stamp the system-only envelope
    ``{mode, proposal_status, verification_ok, final_risk_check_ok}`` on the
    returned dict AND persist the audit keys, even when the LLM call / schema
    validation / verifier / producer throws (fail-closed always-stamp).
  - ``llm_status != "ok"`` -> ``proposal_status="failed"`` WITHOUT parsing the
    deterministic fallback; a provider failure is not a legitimate verdict.
  - ``run_agent_json_task`` merged-result internal keys (``agent_source``,
    ``llm_status``, ``llm_error``, ``llm_failure_category``) never leak into
    ``llm_risk_proposal``.
  - The producer lives in ``run_ga_workers.py`` wired into
    ``_run_recheck_analysis`` (scope = watch-recheck seam ONLY; the main batch
    order path is out of scope).

LTC 4985->4997 fixture: a 5m bearish BOS at T0 carries into the next eligible
bar (``valid`` / ``carried_forward``), but the SHORT plan stays
NON-executable (news_like_event + stop distance 0.791% < 0.8% + ATR buffer),
so ``verification_ok=False`` / ``final_risk_check_ok=False`` is the HONEST
recorded value — the producer must persist it, never re-route to the legacy
path and never fabricate a pass.

RED-first: ``run_ga_workers._attach_risk_governance``,
``repository.CryptoGuardRepository.update_ga_decision_risk_governance`` and
``risk.risk_policy.KNOWN_REASON_CODES`` do not exist in the committed code.
"""
from __future__ import annotations

import json
import math
import types
from contextlib import contextmanager
from unittest import mock

import pytest

from plugins.crypto_guard.reasoning.entry_confirmation_lifecycle import (
    _extract_structured_entry_confirmation,
)
from plugins.crypto_guard.tests import pg_fixtures as fx
from plugins.crypto_guard.tests.test_pg_08_10_llm_risk_rollout_p1 import (
    _T0,
    _T1,
    _ltc_confirmation,
    _ltc_event,
    _ltc_plan,
    _ltc_snapshot,
    _persist_ltc_source_event,
    _policy,
)

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

def _APPROVE_STUB(prompt: str) -> str:
    """Echo the round identity verbatim from the producer's prompt envelope.

    08-10 P2-1 (reviewer) fail-closed contract: the proposal MUST quote
    symbol / side / analysis_time_utc / candidate_fingerprint verbatim. The
    producer's task prompt (``build_agent_json_task_prompt``) carries the
    risk-review envelope JSON in ``payload.risk_context``; the fingerprint and
    analysis time arrive as trusted_facts items, side lives on the
    candidate_plan item, symbol sits at the envelope top level.

    08-10 fresh-reviewer P2-1 (completeness): the round carries its ACTUAL
    failing adaptive gates as the ``round_blockers`` trusted fact, and the
    committee now fails closed when any round blocker is unacknowledged. The
    honest stub echoes ``blocker_ids`` verbatim so every round (0/2/3 blockers)
    passes completeness without hard-coding geometry.
    """
    body = json.loads(prompt)
    envelope = json.loads(body["payload"]["risk_context"])
    trusted = {
        str(item.get("kind")): (item.get("payload") or {})
        for item in envelope["partitions"].get("trusted_facts") or ()
    }
    return json.dumps({
        "verdict": "approve_as_is",
        "reason_codes": [],
        "summary": "LLM 风险委员会模拟：保持现状（测试）",
        "evidence_refs": [],
        "counter_evidence_refs": [],
        "symbol": envelope["symbol"],
        "side": trusted.get("candidate_plan", {}).get("side"),
        "analysis_time_utc": trusted.get("round_analysis_time", {}).get(
            "analysis_time_utc"),
        "candidate_fingerprint": trusted.get("candidate_fingerprint", {}).get(
            "fingerprint"),
        "uncertainty": 0.2,
        "acknowledged_blockers": list(
            trusted.get("round_blockers", {}).get("blocker_ids") or []
        ),
    })


def _ADJUST_STUB(prompt: str) -> str:
    """Mirror ``_APPROVE_STUB`` but return a compliant ``adjust`` proposal.

    08-10 P1-2 (reviewer): the LTC candidate's stop (45.70, distance 0.36)
    fails ``minimum_stop_distance``, so the LLM adjusts the stop to 45.90
    (distance 0.56, the p0 verifier geometry). The proposal echoes the round
    identity verbatim and carries ONLY the allowlisted ``stop_loss``
    adjustment — the verifier scales ``risk_percent`` so monetary risk never
    increases (0.5 * 0.36 / 0.56 = 0.3214).
    """
    body = json.loads(prompt)
    envelope = json.loads(body["payload"]["risk_context"])
    trusted = {
        str(item.get("kind")): (item.get("payload") or {})
        for item in envelope["partitions"].get("trusted_facts") or ()
    }
    return json.dumps({
        "verdict": "adjust",
        "reason_codes": ["minimum_stop_distance"],
        "summary": "止损过近，调整为合规止损并等比缩减风险（测试）",
        "evidence_refs": [],
        "counter_evidence_refs": [],
        "symbol": envelope["symbol"],
        "side": trusted.get("candidate_plan", {}).get("side"),
        "analysis_time_utc": trusted.get("round_analysis_time", {}).get(
            "analysis_time_utc"),
        "candidate_fingerprint": trusted.get("candidate_fingerprint", {}).get(
            "fingerprint"),
        "uncertainty": 0.2,
        "acknowledged_blockers": list(
            trusted.get("round_blockers", {}).get("blocker_ids") or []
        ),
        "adjustments": {"stop_loss": 45.90},
    })

_INTERNAL_KEYS = ("agent_source", "llm_status", "llm_error", "llm_failure_category")


# ── helpers ────────────────────────────────────────────────────────────────


@contextmanager
def _stub_ga_llm(stub):
    """Patch ``llm_agent_judge._call_ga_llm`` (module global, restored on exit).

    ``stub`` may be a callable (called with the prompt) or a value: a dict is
    serialized to JSON, anything else (e.g. the string ``"not json"``) is
    returned verbatim.
    """
    from plugins.crypto_guard.reasoning import llm_agent_judge as laj

    original = laj._call_ga_llm
    if callable(stub):
        laj._call_ga_llm = stub
    else:
        payload = json.dumps(stub) if isinstance(stub, dict) else stub
        laj._call_ga_llm = lambda prompt: payload
    try:
        yield
    finally:
        laj._call_ga_llm = original


def _counting_stub(count: dict) -> callable:
    """A ``_call_ga_llm`` stub that counts invocations then returns the stub."""

    def _counting(prompt):
        count["n"] += 1
        return _APPROVE_STUB(prompt)

    return _counting


def _raise_stub(prompt):
    raise RuntimeError("provider simulated outage")


def _candidate_decision(*, ga_decision_id: int, at: int = _T1) -> dict:
    """A watch-recheck compat decision carrying the LTC SHORT candidate."""
    return {
        "symbol": "LTCUSDT",
        "ga_decision_id": ga_decision_id,
        "analysis_time": at,
        "analysis_time_utc": at,
        "decision_type": "opportunity_watch_recheck",
        "signal_grade": "A",
        "confidence": 0.8,
        "market_bias": "bearish",
        "trend_stage": "early",
        "decision": "trade_plan_available",
        "risk_check": {"ok": True},
        "trade_plan": {
            **_ltc_plan(close_time=_T0),
            # Honest mirror of production ``_bind_trusted_entry_confirmation``
            # (llm_agent_judge.py): every round this helper feeds uses a
            # carried-only snapshot (``_ltc_snapshot_t1`` events=[] or an
            # empty snapshot), where the binder's fail-closed semantics leave
            # the recheck plan's ``entry_trigger_confirmation`` None. Injecting
            # it here would mask the very carried-only producer defect the P1-1
            # seam test exists to catch (fresh reviewer P2).
            "entry_trigger_confirmation": None,
        },
        "evidence": [],
        "counter_evidence": [],
        "final_summary": "ltc-recheck",
        "llm_status": "ok",
    }


def _attach(h, decision: dict, snapshot: dict, *, mode: str) -> dict:
    """Invoke the production producer (RED: symbol absent -> AttributeError)."""
    from plugins.crypto_guard import run_ga_workers as rw

    return rw._attach_risk_governance(
        h.repo,
        decision=decision,
        symbol="LTCUSDT",
        snapshot=snapshot,
        policy=_policy(mode),
    )


def _ltc_snapshot_t1() -> dict:
    # T1 = the NEXT closed 5m bar after the T0 event. The T0 bearish BOS is NOT
    # in this snapshot's structure_events (so the resolver's current-event path
    # yields nothing); it only exists in the persisted store
    # (``_persist_ltc_source_event``), so the resolver carries it forward with
    # origin="carried_forward" / source_decision_id=dec_id / source_snapshot_id
    # =snap_id. This is the honest provenance chain the audit must record.
    return _ltc_snapshot(at=_T1, events=[])


def _normal_ltc_snapshot_t1() -> dict:
    """T1 recheck snapshot for the P1-2 order-bridge test.

    NORMAL regime + ATR current 0.5 (NOT the news_like_event/ATR-2.0 default of
    ``_ltc_snapshot``) so the verifier's wider-stop adjust (45.70 -> 45.90) is
    engine-valid per the p0 geometry and does NOT trip the news_like_event or
    ATR-buffer adaptive blockers. The T0 bearish BOS is a REAL structure event
    in ``modules.price_action.structure_events`` so the verifier's
    ``_find_matching_real_event`` confirms the carried-forward
    ``entry_trigger_confirmation`` against real market data (the persisted
    lifecycle alone is not enough for the confirmation gate).
    """
    return _ltc_snapshot(
        at=_T1, events=[_ltc_event(close_time=_T0)], regime="normal", atr=0.5,
    )


def _save_bearish_snapshot(repo) -> int:
    """Persist a normal-regime bearish LTC snapshot (signal reference)."""
    return repo.save_market_snapshot(
        _ltc_snapshot(at=_T0, events=[], regime="normal", atr=0.5)
    )


def _materialize_short_watch(repo) -> dict:
    """Create an active structured breakout watch (SHORT) via the real button
    path — the SHORT mirror of the LONG bridge-test helper. ``_recheck_order_gate``
    requires the watch direction to match the plan side, so the watch must be
    SHORT for the LTC SHORT plan."""
    from plugins.crypto_guard.run_ga_workers import handle_button_callback

    snapshot_id = _save_bearish_snapshot(repo)
    signal_id = repo.create_signal(
        {
            "symbol": "LTCUSDT",
            "decision": "wait_for_pullback",
            "signal_grade": "B",
            "confidence": 0.67,
            "summary": "测试机会监控（SHORT）",
            "market_bias": "bearish",
            "risk_notes": ["仅用于测试"],
            "has_trade_plan": False,
            "opportunity_watch": {
                "needed": True,
                "direction": "SHORT",
                "reason": "等待下破确认",
                "conditions": [{"type": "breakout", "side": "SHORT",
                                "level": 45.0, "timeframe": "15m"}],
                "invalid_condition": {"type": "close_above", "side": "SHORT",
                                      "level": 46.2},
                "expires_minutes": 60,
            },
        },
        snapshot_id,
    )
    button = handle_button_callback(
        repo,
        {"action": "create_opportunity_watch", "symbol": "LTCUSDT",
         "signal_id": signal_id},
    )
    assert button["ok"] is True, f"button must succeed; {button}"
    return {"watch_id": button["watch_id"]}


# ── narrow repository UPDATE (design §9.1 / §11 Stage B) ───────────────────


class TestRepoNarrowUpdate:
    """``update_ga_decision_risk_governance`` merges audit into raw_decision_json."""

    def test_update_merges_audit_keys_and_preserves_decision(self) -> None:
        h = fx.make_repo()
        try:
            dec_id = h.repo.create_ga_decision({
                "symbol": "LTCUSDT",
                "analysis_time": _T1,
                "analysis_time_utc": _T1,
                "decision_type": "opportunity_watch_recheck",
                "signal_grade": "A",
                "decision": "trade_plan_available",
                "risk_check": {"ok": True},
            })
            audit = {
                "entry_confirmation_lifecycle": {
                    "status": "valid", "origin": "carried_forward"},
                "llm_risk_proposal": {
                    "proposal_status": "ok", "verdict": "approve_as_is"},
                "risk_adjustment_verification": {"verification_ok": False},
                "risk_advisory": {
                    "mode": "shadow", "proposal_status": "ok",
                    "verification_ok": False, "final_risk_check_ok": False},
                "policy_version": 1,
                "llm_latency_ms": 12,
                "evidence_ids": ["b", "a"],
            }
            # RED: method does not exist yet -> AttributeError.
            h.repo.update_ga_decision_risk_governance(dec_id, audit=audit)

            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            # audit keys merged at the top level of the same JSON column
            assert raw["entry_confirmation_lifecycle"] == audit["entry_confirmation_lifecycle"]
            assert raw["llm_risk_proposal"] == audit["llm_risk_proposal"]
            assert raw["risk_adjustment_verification"] == audit["risk_adjustment_verification"]
            assert raw["risk_advisory"] == audit["risk_advisory"]
            assert raw["policy_version"] == 1
            assert raw["llm_latency_ms"] == 12
            # narrow UPDATE is verbatim: the PRODUCER canonicalises ordering,
            # the repo method never re-orders audit input
            assert raw["evidence_ids"] == ["b", "a"]
            # original decision keys survive the jsonb ``||`` merge
            assert raw["decision_type"] == "opportunity_watch_recheck"
            assert raw["risk_check"] == {"ok": True}
        finally:
            h.close()

    def test_update_overwrites_colliding_key(self) -> None:
        h = fx.make_repo()
        try:
            dec_id = h.repo.create_ga_decision({
                "symbol": "LTCUSDT",
                "analysis_time": _T1,
                "analysis_time_utc": _T1,
                "decision_type": "opportunity_watch_recheck",
                "signal_grade": "A",
                "decision": "trade_plan_available",
                "risk_advisory": {"mode": "off"},  # stale legacy value
            })
            h.repo.update_ga_decision_risk_governance(dec_id, audit={
                "risk_advisory": {
                    "mode": "shadow", "proposal_status": "ok",
                    "verification_ok": False, "final_risk_check_ok": False},
            })
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            assert raw["risk_advisory"]["mode"] == "shadow"
            assert raw["risk_advisory"]["proposal_status"] == "ok"
        finally:
            h.close()


# ── mode=off: legacy path byte-for-byte ────────────────────────────────────


class TestModeOffLegacy:
    """off -> no envelope, no LLM call, no persist (PRD P1-6)."""

    def test_off_returns_same_dict_without_envelope_or_persist(self) -> None:
        h = fx.make_repo()
        try:
            _snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            snapshot = _ltc_snapshot_t1()
            decision = _candidate_decision(ga_decision_id=dec_id)
            count = {"n": 0}
            with _stub_ga_llm(_counting_stub(count)):
                result = _attach(h, decision, snapshot, mode="off")

            # byte-for-byte: the SAME dict comes back, untouched
            assert result is decision
            assert "risk_advisory" not in result
            assert count["n"] == 0  # no LLM round
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            assert "risk_advisory" not in raw
            assert "llm_risk_proposal" not in raw
            assert "entry_confirmation_lifecycle" not in raw
            assert "risk_adjustment_verification" not in raw
        finally:
            h.close()


# ── shadow: always stamp + always persist ──────────────────────────────────


class TestShadowStampAndPersist:
    """shadow -> envelope stamped AND the four audit keys persisted."""

    def test_shadow_stamps_envelope_and_persists_all_audit_keys(self) -> None:
        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            snap_id, dec_id, ev_id = _persist_ltc_source_event(h)
            snapshot = _ltc_snapshot_t1()
            decision = _candidate_decision(ga_decision_id=dec_id)
            count = {"n": 0}
            with _stub_ga_llm(_counting_stub(count)):
                result = _attach(h, decision, snapshot, mode="shadow")

            assert count["n"] == 1  # exactly one risk-review round
            # system-only envelope on the returned decision
            env = result["risk_advisory"]
            assert env == {
                "mode": "shadow", "proposal_status": "ok",
                "verification_ok": False, "final_risk_check_ok": False,
            }

            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            # lifecycle (flat, from the deterministic resolver)
            lifecycle = raw["entry_confirmation_lifecycle"]
            assert lifecycle["status"] == "valid"
            assert lifecycle["origin"] == "carried_forward"
            assert lifecycle["source"] == "price_action"
            assert lifecycle["event_type"] == "BOS"
            assert lifecycle["timeframe"] == "5m"
            assert lifecycle["source_decision_id"] == dec_id
            assert lifecycle["source_snapshot_id"] == snap_id
            assert "invalidation_reason" in lifecycle

            # proposal (parsed + validated, no internal keys)
            proposal = raw["llm_risk_proposal"]
            assert proposal["proposal_status"] == "ok"
            assert proposal["verdict"] == "approve_as_is"
            assert proposal["reason_codes"] == []
            for key in _INTERNAL_KEYS:
                assert key not in proposal

            # verification (honest failed value for the non-executable SHORT)
            verif = raw["risk_adjustment_verification"]
            assert verif["verification_ok"] is False
            assert verif["accepted"] is False
            assert verif["final_risk_check_ok"] is False
            assert verif["effective_order_allowed"] is False
            assert verif["rejection_reasons"]

            # envelope persisted identically + mode/policy/latency/evidence
            assert raw["risk_advisory"] == env
            assert raw["policy_version"] == 1
            assert isinstance(raw["llm_latency_ms"], int) and raw["llm_latency_ms"] >= 0
            assert raw["evidence_ids"]

            # original decision keys survive the merge
            assert raw["decision_type"] == "opportunity_watch_recheck"
            assert raw["trade_plan"]["side"] == "SHORT"
        finally:
            h.close()


# ── P2-1 (fresh reviewer): blocker completeness is fail-closed end-to-end ──


class TestBlockerCompletenessFailClosed:
    """08-10 fresh-reviewer P2-1: ``round_ctx.blocker_ids`` now carry the
    round's ACTUAL failing adaptive gates (``candidate_adaptive_blockers``),
    and the committee rejects a proposal that acknowledges only a SUBSET. This
    is the pipeline-level proof: a partial acknowledgment on the failing LTC
    candidate must stamp ``proposal_status="failed"`` and never reach a
    verified envelope (no order is ever authorised on an unacknowledged
    blocker)."""

    def test_partial_blocker_acknowledgment_fails_closed(self) -> None:
        import json as _json

        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            _snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            snapshot = _ltc_snapshot_t1()  # news regime + atr 2.0 -> 3 blockers
            decision = _candidate_decision(ga_decision_id=dec_id)

            def _partial_ack_stub(prompt: str) -> str:
                """Echo identity verbatim but acknowledge only ONE blocker."""
                body = _json.loads(prompt)
                envelope = _json.loads(body["payload"]["risk_context"])
                trusted = {
                    str(item.get("kind")): (item.get("payload") or {})
                    for item in envelope["partitions"].get("trusted_facts") or ()
                }
                blockers = list(
                    trusted.get("round_blockers", {}).get("blocker_ids") or []
                )
                assert len(blockers) >= 2  # the round really has blockers
                return _json.dumps({
                    "verdict": "approve_as_is",
                    "reason_codes": [],
                    "summary": "部分确认阻塞项（测试）",
                    "evidence_refs": [],
                    "counter_evidence_refs": [],
                    "symbol": envelope["symbol"],
                    "side": trusted.get("candidate_plan", {}).get("side"),
                    "analysis_time_utc": trusted.get(
                        "round_analysis_time", {}).get("analysis_time_utc"),
                    "candidate_fingerprint": trusted.get(
                        "candidate_fingerprint", {}).get("fingerprint"),
                    "uncertainty": 0.2,
                    "acknowledged_blockers": blockers[:1],  # subset only
                })

            with _stub_ga_llm(_partial_ack_stub):
                result = _attach(h, decision, snapshot, mode="shadow")

            # fail-closed: partial acknowledgment -> failed, never verified
            assert result["risk_advisory"] == {
                "mode": "shadow", "proposal_status": "failed",
                "verification_ok": False, "final_risk_check_ok": False,
            }
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            proposal = raw["llm_risk_proposal"]
            assert proposal["proposal_status"] == "failed"
            assert "缺少阻塞项确认" in str(proposal.get("schema_error") or "")
        finally:
            h.close()


# ── no candidate: stamp + persist without an LLM round ─────────────────────


class TestNoCandidate:
    """No LONG/SHORT trade_plan -> ``no_candidate``, no LLM call, still sealed."""

    def test_no_trade_plan_stamps_no_candidate_without_llm(self) -> None:
        h = fx.make_repo()
        try:
            _snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            snapshot = _ltc_snapshot_t1()
            decision = _candidate_decision(ga_decision_id=dec_id)
            decision["trade_plan"] = None
            count = {"n": 0}
            with _stub_ga_llm(_counting_stub(count)):
                result = _attach(h, decision, snapshot, mode="shadow")

            assert count["n"] == 0
            assert result["risk_advisory"] == {
                "mode": "shadow", "proposal_status": "no_candidate",
                "verification_ok": False, "final_risk_check_ok": False,
            }
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            assert raw["llm_risk_proposal"]["proposal_status"] == "no_candidate"
            assert "verdict" not in raw["llm_risk_proposal"]
            assert raw["risk_adjustment_verification"]["verification_ok"] is False
            assert raw["risk_advisory"] == result["risk_advisory"]
        finally:
            h.close()


# ── fail-closed always-stamp: LLM / schema / verifier / producer failure ───


class TestFailClosedAlwaysStamp:
    """shadow -> a failing round records ``failed`` and NEVER falls through."""

    def test_provider_exception_fails_closed(self) -> None:
        h = fx.make_repo()
        try:
            _snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            snapshot = _ltc_snapshot_t1()
            decision = _candidate_decision(ga_decision_id=dec_id)
            with _stub_ga_llm(_raise_stub):
                result = _attach(h, decision, snapshot, mode="shadow")

            assert result["risk_advisory"] == {
                "mode": "shadow", "proposal_status": "failed",
                "verification_ok": False, "final_risk_check_ok": False,
            }
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            proposal = raw["llm_risk_proposal"]
            assert proposal["proposal_status"] == "failed"
            assert "verdict" not in proposal  # a provider failure is not a verdict
            assert raw["risk_adjustment_verification"]["verification_ok"] is False
            assert raw["risk_adjustment_verification"]["final_risk_check_ok"] is False
            assert raw["risk_advisory"] == result["risk_advisory"]
        finally:
            h.close()

    def test_invalid_json_marks_failed_without_parsing_fallback(self) -> None:
        h = fx.make_repo()
        try:
            _snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            snapshot = _ltc_snapshot_t1()
            decision = _candidate_decision(ga_decision_id=dec_id)
            with _stub_ga_llm("not json"):
                result = _attach(h, decision, snapshot, mode="shadow")

            # llm_status="failed" -> the deterministic fallback verdict must
            # NOT be treated as a legitimate proposal
            assert result["risk_advisory"]["proposal_status"] == "failed"
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            assert "verdict" not in raw["llm_risk_proposal"]
            assert raw["llm_risk_proposal"]["proposal_status"] == "failed"
        finally:
            h.close()

    def test_producer_exception_fails_closed(self) -> None:
        h = fx.make_repo()
        try:
            _snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            # snapshot without symbol/analysis_time -> lifecycle resolver raises
            decision = _candidate_decision(ga_decision_id=dec_id)
            with _stub_ga_llm(_APPROVE_STUB):
                result = _attach(h, decision, {}, mode="shadow")

            assert result["risk_advisory"] == {
                "mode": "shadow", "proposal_status": "failed",
                "verification_ok": False, "final_risk_check_ok": False,
            }
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            assert raw["llm_risk_proposal"]["proposal_status"] == "failed"
            assert raw["risk_advisory"]["proposal_status"] == "failed"
        finally:
            h.close()


# ── production wiring: _run_recheck_analysis stamps + persists ─────────────


class TestRunRecheckWiring:
    """``_run_recheck_analysis`` (watch-recheck seam) runs the producer."""

    def test_run_recheck_analysis_stamps_and_persists(self) -> None:
        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            snapshot = _ltc_snapshot_t1()
            decision = _candidate_decision(ga_decision_id=dec_id)

            from plugins.crypto_guard import run_ga_workers as rw

            captured: dict[str, object] = {}

            class _FakeController:
                def __init__(self, repo):
                    self.repo = repo

                def analyze_symbol(self, request):
                    captured["request"] = request
                    return decision

            with _stub_ga_llm(_APPROVE_STUB), \
                    mock.patch.object(
                        rw, "build_market_state_snapshot", return_value=snapshot,
                    ), \
                    mock.patch.object(rw, "GAMasterController", _FakeController):
                result = rw._run_recheck_analysis(
                    h.repo, symbol="LTCUSDT",
                    analysis_time_utc=_T1, snapshot_id=snap_id,
                )

            # production config mode (shadow) drives the envelope + persistence
            assert result["risk_advisory"]["mode"] == "shadow"
            assert result["risk_advisory"]["proposal_status"] == "ok"
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            assert raw["risk_advisory"]["mode"] == "shadow"
            assert raw["llm_risk_proposal"]["proposal_status"] == "ok"
            assert raw["entry_confirmation_lifecycle"]["status"] == "valid"
            assert raw["entry_confirmation_lifecycle"]["source_decision_id"] == dec_id
        finally:
            h.close()

    def test_recheck_seam_writes_confirmation_event_and_carries(self) -> None:
        """P1-1 (reviewer): the seam is the production writer for
        ``entry_confirmation_events``.

        Round 1 (closed bearish BOS in the snapshot) appends the canonical
        event with BOTH FKs non-null. Round 2 (range snapshot at the next
        closed 5m bar) resolves the carried event with provenance
        (``origin == "carried_forward"`` + ``source_decision_id``). An insert
        failure still stamps the failed envelope -- a missing event insert
        never weakens the gate.
        """
        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            from plugins.crypto_guard import run_ga_workers as rw

            class _PersistingFakeController:
                """Honour the production persistence contract: the decision row
                owns the request snapshot_id, the plan carries the canonical
                entry confirmation, and the returned dict carries the real id
                (the seam saves the snapshot when snapshot_id is None)."""

                def __init__(self, repo, *, at, close_time):
                    self.repo = repo
                    self.at = at
                    self.close_time = close_time

                def analyze_symbol(self, request):
                    # Honest mirror of production ``_bind_trusted_entry_confirmation``
                    # (llm_agent_judge.py): the recheck plan carries the trusted
                    # confirmation ONLY when the CURRENT snapshot's structure
                    # events yield one (``_extract_structured_entry_confirmation``
                    # current-event path). The round-2 carried-only recheck
                    # (``events=[]`` range snapshot) must leave it None — an
                    # unconditional injection here would mask the very defect
                    # this test exists to catch (5th fresh-reviewer P2-3;
                    # TEMP-REVERT-PROOF: injection -> the confirmation-missing
                    # rejection_reasons assertion REDs, restored -> GREEN).
                    plan = dict(_ltc_plan(close_time=self.close_time))
                    if _extract_structured_entry_confirmation(
                        request.snapshot, side="SHORT",
                        entry=float(plan.get("entry_price") or 0.0),
                    ) is None:
                        plan["entry_trigger_confirmation"] = None
                    dec = {
                        "symbol": "LTCUSDT",
                        "analysis_time": self.at,
                        "analysis_time_utc": self.at,
                        "decision_type": "opportunity_watch_recheck",
                        "signal_grade": "A",
                        "confidence": 0.8,
                        "market_bias": "bearish",
                        "trend_stage": "early",
                        "decision": "trade_plan_available",
                        "risk_check": {"ok": True},
                        "trade_plan": plan,
                        "evidence": [],
                        "counter_evidence": [],
                        "final_summary": "ltc-recheck-persisting",
                        "llm_status": "ok",
                        "snapshot_id": request.snapshot_id,
                    }
                    dec_id = self.repo.create_ga_decision(dec)
                    dec["ga_decision_id"] = dec_id
                    return dec

            def _run(at, events, close_time):
                snapshot = _ltc_snapshot(at=at, events=events)
                controller = _PersistingFakeController(
                    h.repo, at=at, close_time=close_time,
                )
                with _stub_ga_llm(_APPROVE_STUB), \
                        mock.patch.object(
                            rw, "build_market_state_snapshot", return_value=snapshot,
                        ), \
                        mock.patch.object(
                            rw, "GAMasterController", lambda repo: controller,
                        ):
                    return rw._run_recheck_analysis(
                        h.repo, symbol="LTCUSDT",
                        analysis_time_utc=at, snapshot_id=None,
                    )

            # (a) round 1: the seam persists the rebuilt snapshot and the
            # producer appends the current BOS event with both FKs non-null.
            r1 = _run(_T0, [_ltc_event(close_time=_T0)], _T0)
            assert r1["risk_advisory"]["proposal_status"] == "ok"
            round1_dec_id = int(r1["ga_decision_id"])
            rows = h.repo.list_recent_entry_confirmation_events(
                symbol="LTCUSDT", direction="bearish", since_ms=0, limit=50,
            )
            assert len(rows) == 1
            assert rows[0]["event_close_time"] == _T0
            assert rows[0]["source_decision_id"] == round1_dec_id
            assert rows[0]["source_snapshot_id"] is not None

            # (b) round 2: range snapshot at the next closed 5m bar -> the T0
            # event carries forward with provenance.
            r2 = _run(_T1, [], _T0)
            assert r2["risk_advisory"]["proposal_status"] == "ok"
            raw2 = h.repo.get_ga_decision(int(r2["ga_decision_id"]))[
                "raw_decision_json"
            ]
            lifecycle2 = raw2["entry_confirmation_lifecycle"]
            assert lifecycle2["status"] == "valid"
            assert lifecycle2["origin"] == "carried_forward"
            assert lifecycle2["source_decision_id"] == round1_dec_id
            # 5th fresh-reviewer P2-3: the carried round is advisory-ONLY —
            # the produced plan carries NO entry_trigger_confirmation (the
            # current range snapshot yields no structure event, so the trusted
            # binder leaves it None) and the verifier MUST therefore reject
            # the round: no order may ever be authorised. The confirmation-
            # missing engine reason is the load-bearing proof of the seam.
            assert r2["risk_advisory"]["verification_ok"] is False
            assert r2["risk_advisory"]["final_risk_check_ok"] is False
            ver2 = raw2["risk_adjustment_verification"]
            assert ver2["verification_ok"] is False
            assert ver2["final_risk_check_ok"] is False
            assert ver2["effective_order_allowed"] is False
            assert any(
                "缺少入场确认" in r for r in ver2["rejection_reasons"]
            ), ver2["rejection_reasons"]
            # the carried re-observation dedupes against the original row
            rows = h.repo.list_recent_entry_confirmation_events(
                symbol="LTCUSDT", direction="bearish", since_ms=0, limit=50,
            )
            assert len(rows) == 1

            # (c) insert failure -> outer fail-closed handler stamps the failed
            # envelope and persists it; no exception escapes.
            snapshot_t0 = _ltc_snapshot(
                at=_T0, events=[_ltc_event(close_time=_T0)],
            )
            controller_c = _PersistingFakeController(
                h.repo, at=_T0, close_time=_T0,
            )
            with _stub_ga_llm(_APPROVE_STUB), \
                    mock.patch.object(
                        rw, "build_market_state_snapshot", return_value=snapshot_t0,
                    ), \
                    mock.patch.object(
                        rw, "GAMasterController", lambda repo: controller_c,
                    ), \
                    mock.patch.object(
                        h.repo, "insert_entry_confirmation_event_after_decision",
                        side_effect=RuntimeError("simulated event persist failure"),
                    ):
                r3 = rw._run_recheck_analysis(
                    h.repo, symbol="LTCUSDT",
                    analysis_time_utc=_T0, snapshot_id=None,
                )
            assert r3["risk_advisory"] == {
                "mode": "shadow", "proposal_status": "failed",
                "verification_ok": False, "final_risk_check_ok": False,
            }
            raw3 = h.repo.get_ga_decision(int(r3["ga_decision_id"]))[
                "raw_decision_json"
            ]
            assert raw3["llm_risk_proposal"]["proposal_status"] == "failed"
            assert raw3["risk_advisory"] == r3["risk_advisory"]
        finally:
            h.close()


class TestPaperBoundedAdjustedPlanReachesOrder:
    """08-10 P1-2 (reviewer finding): the verifier's ADJUSTED plan is the ONLY
    plan that may reach order creation.

    End-to-end through the REAL recheck seam (``handle_opportunity_watch_recheck``
    with the default ``_run_recheck_analysis``): a stop-tight SHORT candidate
    (entry 45.34 / stop 45.70, distance 0.36 < 0.8% minimum) that is
    NON-executable at ``minimum_stop_distance``, an LLM ``adjust`` proposal
    (``stop_loss`` 45.90, the p0 verifier geometry), a deterministic
    verification ``ok=True`` (risk_percent scaled to 0.5*0.36/0.56 so monetary
    risk never increases), and ONE paper order whose ``stop_loss`` /
    ``initial_stop_loss`` / ``risk_percent`` come from the ADJUSTED plan —
    never the candidate's 45.70. Revert-fail: removing the binding at
    ``run_ga_workers.py:2333-2339`` makes this test fail on the 45.70 stop.
    """

    def test_paper_bounded_adjusted_plan_reaches_order(self) -> None:
        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            from plugins.crypto_guard import run_ga_workers as rw

            # The T0 trusted bearish BOS is the persisted source the recheck
            # carries forward to T1 (proven by the P1-1 carry test).
            _snap_id, _dec_id, _ev_id = _persist_ltc_source_event(h)
            watch_id = int(_materialize_short_watch(h.repo)["watch_id"])

            class _PersistingAdjustedController:
                """Honour the production persistence contract AND clear the
                order gate: a persisted SHORT decision (confirmed / A / llm ok /
                llm_confirmed / risk_check.ok) carrying the stop-tight candidate
                plan. The verifier's adjust then binds the wider stop (P1-2)."""

                def __init__(self, repo):
                    self.repo = repo
                    self.analyze_calls = 0

                def analyze_symbol(self, request):
                    self.analyze_calls += 1
                    plan = {
                        **_ltc_plan(close_time=_T0, entry=45.34, stop=45.70,
                                    invalid=45.80),
                        "entry_trigger_confirmation": _ltc_confirmation(
                            close_time=_T0),
                    }
                    dec = {
                        "symbol": "LTCUSDT",
                        "analysis_time": _T1,
                        "analysis_time_utc": _T1,
                        "decision_type": "opportunity_watch_recheck",
                        "signal_grade": "A",
                        "effective_signal_grade": "A",
                        "confidence": 0.8,
                        "market_bias": "bearish",
                        "trend_stage": "early",
                        "decision": "trade_plan_available",
                        "plan_execution_state": "confirmed",
                        "plan_origin": "llm_confirmed",
                        "llm_status": "ok",
                        "risk_check": {"ok": True},
                        "trade_plan": plan,
                        "evidence": [],
                        "counter_evidence": [],
                        "final_summary": "ltc-recheck-adjusted",
                        "snapshot_id": request.snapshot_id,
                    }
                    dec_id = self.repo.create_ga_decision(dec)
                    dec["ga_decision_id"] = dec_id
                    # retain the SAME dict ``_run_recheck_analysis`` returns and
                    # ``_attach_risk_governance`` mutates in place, so the P2-4
                    # in-memory envelope can be asserted after the recheck.
                    self.last_decision = dec
                    return dec

            controller = _PersistingAdjustedController(h.repo)
            snapshot = _normal_ltc_snapshot_t1()
            policy = _policy("paper_bounded")
            with _stub_ga_llm(_ADJUST_STUB), \
                    mock.patch.object(
                        rw, "build_market_state_snapshot", return_value=snapshot,
                    ), \
                    mock.patch.object(
                        rw, "GAMasterController", lambda repo: controller,
                    ), \
                    mock.patch.object(
                        rw, "load_config",
                        return_value=types.SimpleNamespace(
                            risk_assistance=policy,
                        ),
                    ):
                result = rw.handle_opportunity_watch_recheck(
                    h.repo, {"watch_id": watch_id}, send_message=None,
                )
                # once-ever: a second trigger with an existing order is a
                # duplicate (no re-analysis, no second order).
                duplicate = rw.handle_opportunity_watch_recheck(
                    h.repo, {"watch_id": watch_id}, send_message=None,
                )

            assert result.get("ok") is True, result
            assert result.get("created") is True, result
            assert result.get("rejected") is not True, result
            assert duplicate.get("duplicate") is True, duplicate
            assert controller.analyze_calls == 1  # once-ever analysis

            # P1-2: the order row carries the ADJUSTED geometry.
            order = h.repo.get_paper_order_by_trigger_watch(watch_id)
            assert order is not None
            assert float(order["stop_loss"]) == 45.90
            assert float(order["initial_stop_loss"]) == 45.90
            scaled = 0.5 * 0.36 / 0.56  # risk scaled so monetary risk never rises
            assert math.isclose(float(order["risk_percent"]), scaled, rel_tol=1e-9)
            assert int(order["trigger_watch_id"]) == watch_id
            assert order["symbol"] == "LTCUSDT"
            assert str(order["side"]).upper() == "SHORT"
            assert int(order["id"]) == int(result["paper_order_id"])
            # P2-2 (08-12): the created order records the governance mode that
            # produced it, so the persist-loss diagnostic can tell a governance
            # order (mode NOT NULL, != 'off') from a legacy order (NULL) even
            # when the owning decision lost the audit row.
            assert order["risk_advisory_mode"] == "paper_bounded"

            # P2-4 (reviewer finding): the IN-MEMORY envelope the production
            # order notification consumes must carry the ORIGINAL candidate
            # plan and the flat confirmation lifecycle (system-only; the
            # persisted envelope below stays minimal). The adjusted plan is on
            # the decision trade_plan via the P1-2 binding.
            env = controller.last_decision["risk_advisory"]
            assert env["mode"] == "paper_bounded"
            assert env["proposal_status"] == "ok"
            assert env["verification_ok"] is True
            assert env["final_risk_check_ok"] is True
            assert float(env["candidate_plan"]["entry_price"]) == 45.34
            assert float(env["candidate_plan"]["stop_loss"]) == 45.70
            assert float(env["candidate_plan"]["risk_percent"]) == 0.5
            lc_env = env["entry_confirmation_lifecycle"]
            assert lc_env["source"] == "price_action"
            assert lc_env["timeframe"] == "5m"
            assert lc_env["status"] == "valid"
            # honest resolver output: the T0 source event is observed one 5m
            # bar later at T1 -> age_bars=1, ttl_bars=3 (5m policy).
            assert lc_env["age_bars"] == 1
            assert lc_env["ttl_bars"] == 3
            assert float(controller.last_decision["trade_plan"]["stop_loss"]) == 45.90
            assert math.isclose(
                float(controller.last_decision["trade_plan"]["risk_percent"]),
                scaled, rel_tol=1e-9)

            # audit + provenance persisted on the owning decision
            raw = h.repo.get_ga_decision(int(result["ga_decision_id"]))[
                "raw_decision_json"
            ]
            # system-only envelope persisted on the owning decision (paper_bounded
            # success path: proposal verified AND final risk check ok)
            assert raw["risk_advisory"] == {
                "mode": "paper_bounded", "proposal_status": "ok",
                "verification_ok": True, "final_risk_check_ok": True,
            }
            proposal = raw["llm_risk_proposal"]
            assert proposal["proposal_status"] == "ok"
            assert proposal["verdict"] == "adjust"
            assert proposal["reason_codes"] == ["minimum_stop_distance"]
            assert proposal["adjustments"] == {"stop_loss": 45.90}
            ver = raw["risk_adjustment_verification"]
            assert ver["verification_ok"] is True
            assert ver["final_risk_check_ok"] is True
            assert ver["effective_order_allowed"] is True
            assert float(ver["adjusted_plan"]["stop_loss"]) == 45.90
            assert math.isclose(
                float(ver["adjusted_plan"]["risk_percent"]), scaled, rel_tol=1e-9)
            assert float(ver["monetary_risk_delta"]) < 0
            # the persisted candidate trade_plan stays verbatim (stop 45.70):
            # P1-2 binds the adjusted plan only into the returned dict + audit
            assert float(raw["trade_plan"]["stop_loss"]) == 45.70
            lc = raw["entry_confirmation_lifecycle"]
            assert lc["status"] == "valid"
            # strict provenance: the current snapshot's T0 BOS is the matching
            # real event, so the current event wins (origin=current_snapshot,
            # no carried-forward source decision).
            assert lc["origin"] == "current_snapshot"
            assert lc["source_decision_id"] is None

            # the P1-1 writer dedupes the re-observation against the T0 row
            # (ON CONFLICT (event_fingerprint) DO NOTHING): still exactly one
            # canonical event, still owned by the original source decision.
            rows = h.repo.list_recent_entry_confirmation_events(
                symbol="LTCUSDT", direction="bearish", since_ms=0, limit=50,
            )
            assert len(rows) == 1
            assert rows[0]["source_decision_id"] == _dec_id
        finally:
            h.close()


# ── P2-2 (reviewer finding): persist-throws always-stamp ────────────────────


class TestPersistFailureAlwaysStamps:
    """08-10 P2-2 (reviewer finding): the audit persist can fail (separate
    transaction from the decision insert) WITHOUT losing the always-stamp
    invariant. ``_record`` stamps the system-only envelope on the returned dict
    BEFORE the persist attempt and swallows a persist exception (LOGGER.warning),
    so a ``repo.update_ga_decision_risk_governance`` raise must never escape AND
    must never downgrade/erase the envelope.

    Three branches are proven:
      (a) a FAILED round + persist failure -> the failed envelope is still on
          the returned dict;
      (b) a VERIFIED round + persist failure -> the ok envelope is still on the
          returned dict (a persist failure never weakens the gate);
      (c) the full watch-recheck handler + persist failure -> rejected, zero
          orders, no exception escapes.
    """

    def test_persist_throw_failed_round_still_stamps_envelope(self) -> None:
        h = fx.make_repo()
        try:
            _snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            snapshot = _ltc_snapshot_t1()
            decision = _candidate_decision(ga_decision_id=dec_id)
            with _stub_ga_llm(_raise_stub), \
                    mock.patch.object(
                        h.repo, "update_ga_decision_risk_governance",
                        side_effect=RuntimeError("simulated persist outage"),
                    ):
                # must NOT raise: the persist failure is swallowed after the
                # envelope was already stamped on the returned dict.
                result = _attach(h, decision, snapshot, mode="shadow")

            assert result["risk_advisory"] == {
                "mode": "shadow", "proposal_status": "failed",
                "verification_ok": False, "final_risk_check_ok": False,
            }
            # the envelope is the ONLY governance artifact that survives a
            # persist failure; nothing was written to the decision row.
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            assert raw.get("llm_risk_proposal") is None
            assert raw.get("risk_advisory") is None
        finally:
            h.close()

    def test_persist_throw_verified_round_still_stamps_ok_envelope(self) -> None:
        h = fx.make_repo()
        try:
            # the verifier's account gate requires an enabled account
            # (``_gate_account``), so the verified branch is reachable
            h.repo.ensure_paper_account("default")
            _snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            snapshot = _normal_ltc_snapshot_t1()  # real T0 event + normal regime
            decision = _candidate_decision(ga_decision_id=dec_id)
            # an approveable candidate (stop 45.90 = 1.235% distance, the p0
            # APPROVE geometry) so the verifier ok=True without any adjustment.
            decision["trade_plan"] = {
                **_ltc_plan(close_time=_T0, entry=45.34, stop=45.90,
                            invalid=45.80),
                "entry_trigger_confirmation": _ltc_confirmation(close_time=_T0),
            }
            with _stub_ga_llm(_APPROVE_STUB), \
                    mock.patch.object(
                        h.repo, "update_ga_decision_risk_governance",
                        side_effect=RuntimeError("simulated persist outage"),
                    ):
                result = _attach(h, decision, snapshot, mode="shadow")

            # a verified round keeps its VERIFIED envelope even when the audit
            # row cannot be written -- a persist failure never downgrades the
            # in-memory gate (P2-2 always-stamp).
            assert result["risk_advisory"] == {
                "mode": "shadow", "proposal_status": "ok",
                "verification_ok": True, "final_risk_check_ok": True,
            }
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            assert raw.get("risk_advisory") is None
        finally:
            h.close()

    def test_handle_recheck_persist_failure_rejects_and_orders_nothing(self) -> None:
        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            from plugins.crypto_guard import run_ga_workers as rw

            _snap_id, _dec_id, _ev_id = _persist_ltc_source_event(h)
            watch_id = int(_materialize_short_watch(h.repo)["watch_id"])

            class _PersistingController:
                """Gate-passing SHORT decision (confirmed / A / llm ok /
                llm_confirmed / risk_check.ok / valid plan) that is persisted,
                so the producer attempts the risk-governance audit persist."""

                def __init__(self, repo):
                    self.repo = repo
                    self.analyze_calls = 0

                def analyze_symbol(self, request):
                    self.analyze_calls += 1
                    plan = {
                        **_ltc_plan(close_time=_T0, entry=45.34, stop=45.70,
                                    invalid=45.80),
                        "entry_trigger_confirmation": _ltc_confirmation(
                            close_time=_T0),
                    }
                    dec = {
                        "symbol": "LTCUSDT",
                        "analysis_time": _T1,
                        "analysis_time_utc": _T1,
                        "decision_type": "opportunity_watch_recheck",
                        "signal_grade": "A",
                        "effective_signal_grade": "A",
                        "confidence": 0.8,
                        "market_bias": "bearish",
                        "trend_stage": "early",
                        "decision": "trade_plan_available",
                        "plan_execution_state": "confirmed",
                        "plan_origin": "llm_confirmed",
                        "llm_status": "ok",
                        "risk_check": {"ok": True},
                        "trade_plan": plan,
                        "evidence": [],
                        "counter_evidence": [],
                        "final_summary": "ltc-recheck-persist-fail",
                        "snapshot_id": request.snapshot_id,
                    }
                    dec_id = self.repo.create_ga_decision(dec)
                    dec["ga_decision_id"] = dec_id
                    return dec

            controller = _PersistingController(h.repo)
            snapshot = _normal_ltc_snapshot_t1()
            policy = _policy("paper_bounded")
            with _stub_ga_llm(_raise_stub), \
                    mock.patch.object(
                        rw, "build_market_state_snapshot", return_value=snapshot,
                    ), \
                    mock.patch.object(
                        rw, "GAMasterController", lambda repo: controller,
                    ), \
                    mock.patch.object(
                        rw, "load_config",
                        return_value=types.SimpleNamespace(
                            risk_assistance=policy,
                        ),
                    ), \
                    mock.patch.object(
                        h.repo, "update_ga_decision_risk_governance",
                        side_effect=RuntimeError("simulated persist outage"),
                    ):
                result = rw.handle_opportunity_watch_recheck(
                    h.repo, {"watch_id": watch_id}, send_message=None,
                )

            # provider outage -> failed envelope; persist ALSO fails -> the
            # always-stamp invariant still holds and the handler STILL rejects
            # the order. No exception escapes; zero orders; recheck recorded.
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, result
            assert result.get("reason") == "risk_advisory_rejected:paper_bounded"
            assert result.get("paper_order_id") is None
            assert controller.analyze_calls == 1
            assert h.repo.get_paper_order_by_trigger_watch(watch_id) is None
            watch = h.repo.get_opportunity_watch(watch_id)
            assert watch["recheck_status"] == "recheck_rejected"
        finally:
            h.close()


class TestDeriveAccountStateRiskCaps:
    """08-10 P2-3 (reviewer finding): the production account snapshot must
    populate the per-trade / total risk caps from the account-risk config and
    derive ``open_position_risk_pct`` from live orders — never None in the
    happy path. The verifier enforces these caps fail-closed (see
    ``test_pg_08_10_risk_adjustment_verifier_p0.py::TestAccountRiskCapsFailClosed``)."""

    def test_caps_populated_from_config(self):
        from plugins.crypto_guard import run_ga_workers as rw

        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            state = rw._derive_account_state(h.repo)
            assert state["enabled"] is True
            assert state["max_single_trade_risk_pct"] == 2.0
            assert state["max_total_risk_pct"] == 10.0
            assert state["open_position_risk_pct"] == 0.0
        finally:
            h.close()

    def test_open_position_risk_summed_from_live_orders(self):
        from plugins.crypto_guard import run_ga_workers as rw

        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            h.repo.create_paper_order(
                signal_id=None,
                signal={"symbol": "LTCUSDT"},
                trade_plan={"side": "SHORT", "entry_type": "limit",
                            "entry_price": 45.34, "trigger_price": 45.34,
                            "stop_loss": 45.90, "risk_percent": 1.0},
                source="test_caps",
            )
            h.repo.create_paper_order(
                signal_id=None,
                signal={"symbol": "LTCUSDT"},
                trade_plan={"side": "SHORT", "entry_type": "limit",
                            "entry_price": 45.34, "trigger_price": 45.34,
                            "stop_loss": 45.90, "risk_percent": 0.5},
                source="test_caps",
            )
            state = rw._derive_account_state(h.repo)
            assert state["open_orders"] == 2
            assert math.isclose(state["open_position_risk_pct"], 1.5)
            assert state["max_single_trade_risk_pct"] == 2.0
            assert state["max_total_risk_pct"] == 10.0
        finally:
            h.close()

    def test_cfg_pct_absent_key_returns_none(self):
        """08-10 P2-2: ``_cfg_pct`` returns None ONLY when the key is absent
        (the account_risk_guard DEFAULTS then fill the gap)."""
        from plugins.crypto_guard import run_ga_workers as rw

        assert rw._cfg_pct(None) is None

    def test_cfg_pct_valid_returns_float(self):
        from plugins.crypto_guard import run_ga_workers as rw

        assert rw._cfg_pct(2.0) == 2.0
        assert rw._cfg_pct(0.5) == 0.5
        assert rw._cfg_pct(2) == 2.0

    def test_cfg_pct_present_but_invalid_raises(self):
        """08-10 P2-2 (reviewer finding): a PRESENT-but-invalid cap must NEVER
        silently become 'no cap'. Each of these raises ValueError; the loader
        ``_validate_account_risk`` already rejected them at startup, and this
        is the second fail-closed layer in the production snapshot path."""
        from plugins.crypto_guard import run_ga_workers as rw

        for bad in (0.0, -1.0, True, float("nan"), float("inf"), "2.0"):
            with pytest.raises(ValueError):
                rw._cfg_pct(bad)


# ── Recommended-2 (fresh reviewer): untrusted_data is NOT citable ────────────


class TestUntrustedEvidenceNotCitable:
    """08-10 fresh-reviewer Recommended-2: the producer's ``evidence_ids`` may
    only quote the trusted ``trusted_facts`` / ``model_derived`` partitions;
    ``counter_evidence_ids`` keeps its own counter partition, and
    ``untrusted_data`` (watch reasons, free text, tool output) ids are NEVER
    citable. The committee (``risk_committee.py``) rejects any ``evidence_ref``
    outside ``evidence_ids`` with ``证据引用不存在``, so an LLM that cites a
    watch/text blob must fail closed end-to-end and never reach a verified
    envelope. Revert-fail: restoring the producer's old ``ctx.partitions.values()``
    sweep (all four partitions citable) lets this exact citation pass the
    committee, the round records ``proposal_status="ok"``, and the test fails."""

    def test_citing_untrusted_data_evidence_fails_closed(self) -> None:
        import json as _json

        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            _snap_id, dec_id, _ev_id = _persist_ltc_source_event(h)
            snapshot = _ltc_snapshot_t1()
            decision = _candidate_decision(ga_decision_id=dec_id)
            # untrusted market text rides the decision's ``evidence`` list; the
            # producer puts it in the ``untrusted_data`` partition (line ~2408).
            decision["evidence"] = [
                {"kind": "untrusted_blob",
                 "payload": {"text": "watch 理由：免费文本不可作为证据引用"}},
            ]

            def _cite_untrusted_stub(prompt: str) -> str:
                """Echo identity verbatim, acknowledge ALL blockers, and cite
                the untrusted blob's evidence_id as the only evidence_ref."""
                body = _json.loads(prompt)
                envelope = _json.loads(body["payload"]["risk_context"])
                trusted = {
                    str(item.get("kind")): (item.get("payload") or {})
                    for item in envelope["partitions"].get("trusted_facts") or ()
                }
                untrusted = list(
                    envelope["partitions"].get("untrusted_data") or []
                )
                assert untrusted, "the round really carries untrusted_data"
                untrusted_id = str(untrusted[0]["evidence_id"])
                return _json.dumps({
                    "verdict": "approve_as_is",
                    "reason_codes": [],
                    "summary": "引用不可信数据证据（测试）",
                    "evidence_refs": [untrusted_id],
                    "counter_evidence_refs": [],
                    "symbol": envelope["symbol"],
                    "side": trusted.get("candidate_plan", {}).get("side"),
                    "analysis_time_utc": trusted.get(
                        "round_analysis_time", {}).get("analysis_time_utc"),
                    "candidate_fingerprint": trusted.get(
                        "candidate_fingerprint", {}).get("fingerprint"),
                    "uncertainty": 0.2,
                    "acknowledged_blockers": list(
                        trusted.get("round_blockers", {}).get("blocker_ids") or []
                    ),
                })

            with _stub_ga_llm(_cite_untrusted_stub):
                result = _attach(h, decision, snapshot, mode="shadow")

            # fail-closed: the untrusted citation is rejected by the committee,
            # never a verified envelope (no order is ever authorised on a
            # citation of free-text data).
            assert result["risk_advisory"] == {
                "mode": "shadow", "proposal_status": "failed",
                "verification_ok": False, "final_risk_check_ok": False,
            }
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            proposal = raw["llm_risk_proposal"]
            assert proposal["proposal_status"] == "failed"
            assert "证据引用不存在" in str(proposal.get("schema_error") or "")
        finally:
            h.close()


class TestMalformedRiskAdvisoryFailsClosed:
    """08-12 fresh-reviewer Recommended: a decision that CARRIES a
    present-but-NON-dict ``risk_advisory`` envelope must fail closed
    (``risk_advisory_rejected:malformed``) and never fall through to the
    legacy ordering path. Only a decision WITHOUT the envelope (off /
    pre-rollout) is allowed the legacy path byte-for-byte — a corrupt
    envelope is never treated as "governance did not run".
    """

    def test_non_dict_risk_advisory_fails_closed(self) -> None:
        h = fx.make_repo()
        try:
            h.repo.ensure_paper_account("default")
            from plugins.crypto_guard import run_ga_workers as rw

            _snap_id, _dec_id, _ev_id = _persist_ltc_source_event(h)
            watch_id = int(_materialize_short_watch(h.repo)["watch_id"])

            def _analyze_malformed(repo, *, symbol, analysis_time_utc,
                                   snapshot_id=None):
                """A gate-passing SHORT recheck decision (confirmed / A / llm
                ok / llm_confirmed / risk_check.ok / valid plan) whose
                ``risk_advisory`` is a STRING, not the dict envelope. Today the
                producer always stamps a dict, so this is unreachable from
                production — the seam proves the gate holds it closed anyway."""
                plan = {
                    **_ltc_plan(close_time=_T0, entry=45.34, stop=45.70,
                                invalid=45.80),
                    "entry_trigger_confirmation": _ltc_confirmation(
                        close_time=_T0),
                }
                dec = {
                    "symbol": "LTCUSDT",
                    "analysis_time": _T1,
                    "analysis_time_utc": _T1,
                    "decision_type": "opportunity_watch_recheck",
                    "signal_grade": "A",
                    "effective_signal_grade": "A",
                    "confidence": 0.8,
                    "market_bias": "bearish",
                    "trend_stage": "early",
                    "decision": "trade_plan_available",
                    "plan_execution_state": "confirmed",
                    "plan_origin": "llm_confirmed",
                    "llm_status": "ok",
                    "risk_check": {"ok": True},
                    "trade_plan": plan,
                    "evidence": [],
                    "counter_evidence": [],
                    "final_summary": "ltc-recheck-malformed-envelope",
                    "snapshot_id": snapshot_id,
                }
                dec_id = repo.create_ga_decision(dec)
                dec["ga_decision_id"] = dec_id
                # Malformed envelope: present but NOT a dict.
                dec["risk_advisory"] = "not-a-dict"
                return dec

            result = rw.handle_opportunity_watch_recheck(
                h.repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_analyze_malformed,
            )

            # fail-closed: the corrupt envelope is rejected, never silently
            # routed to the legacy order path (which would create an order).
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, result
            assert result.get("reason") == "risk_advisory_rejected:malformed"
            assert result.get("paper_order_id") is None
            assert h.repo.get_paper_order_by_trigger_watch(watch_id) is None
            watch = h.repo.get_opportunity_watch(watch_id)
            assert watch["recheck_status"] == "recheck_rejected"
        finally:
            h.close()


class TestProducerConfigLoadFailClosed:
    """08-10 fresh-reviewer P2-2: the always-stamp invariant (design.md §5.4)
    extends to the POLICY/CONFIG read itself.

    Before the fix, ``load_config()`` ran OUTSIDE the always-stamp try: a
    corrupted ``trading_mode.yaml`` / DSN resolution failure escaped BARE with
    NO ``risk_advisory`` key on the returned decision at all — the gate silently
    vanished for the round. The fix moved the read INSIDE the try (any producer
    exception stamps ``proposal_status="failed"``) and pre-initializes
    ``mode="shadow"`` so the inner except can never NameError on ``mode``."""

    def test_load_config_raise_stamps_failed(self):
        from plugins.crypto_guard import run_ga_workers as rw

        h = fx.make_repo()
        try:
            decision = _candidate_decision(ga_decision_id=0, at=_T1)
            dec_id = h.repo.create_ga_decision({
                "symbol": "LTCUSDT",
                "analysis_time": _T1,
                "analysis_time_utc": _T1,
                "decision_type": "opportunity_watch_recheck",
                "signal_grade": "A",
                "decision": "trade_plan_available",
                "risk_check": {"ok": True},
                "trade_plan": decision["trade_plan"],
                "evidence": [],
                "counter_evidence": [],
                "final_summary": "ltc-recheck-config-outage",
            })
            decision["ga_decision_id"] = dec_id
            with mock.patch.object(
                rw, "load_config",
                side_effect=RuntimeError("simulated config outage"),
            ):
                # policy=None is the ONLY legitimate way to reach the
                # config-read seam directly (the recheck handler already
                # resolves the policy before invoking the producer).
                out = rw._attach_risk_governance(
                    h.repo,
                    decision=decision,
                    symbol="LTCUSDT",
                    snapshot=_ltc_snapshot_t1(),
                    policy=None,
                )

            # Always-stamp: a config failure is a producer exception, NOT a
            # bare escape — the in-memory envelope exists and records the
            # failure, and the pre-init mode leaves a coherent record.
            assert out is decision
            advisory = decision["risk_advisory"]
            assert advisory["mode"] == "shadow"
            assert advisory["proposal_status"] == "failed"
            assert advisory["verification_ok"] is False
            assert advisory["final_risk_check_ok"] is False
            # The same audit is persisted for the owning decision.
            raw = h.repo.get_ga_decision(dec_id)["raw_decision_json"]
            assert raw["risk_advisory"]["mode"] == "shadow"
            assert raw["risk_advisory"]["proposal_status"] == "failed"
            assert raw["llm_risk_proposal"]["proposal_status"] == "failed"
            assert "producer_error" in raw["llm_risk_proposal"]
            assert raw["risk_adjustment_verification"]["verification_ok"] is False
        finally:
            h.close()
