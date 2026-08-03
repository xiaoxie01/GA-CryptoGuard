# -*- coding: utf-8 -*-
"""Codex terminal-review P1-1: risk validated EXACTLY ONCE on the real path.

Codex finding (verbatim essence):
    ``run_agent_sop_decision`` / ``apply_risk_to_decision`` 已对原始计划执行
    validate_trade_plan。``RiskGate.check`` 不得对已清空或已验证的 decision
    再次调用 validate_trade_plan。复用并复制上游不可变 risk_check，再独立
    附加 account-risk 结果。account-risk 的 side 应来自原始/llm_synthesis
    plan，而不是已清空的 trade_plan。

The real controller path currently validates the SAME plan twice:
  1. ``apply_risk_to_decision`` (risk_engine.py:19) validates the ORIGINAL
     proposed plan once and (on rejection) clears has_trade_plan/trade_plan,
     preserving the rejected plan under ``candidate_trade_plan`` and setting a
     structured ``plan_blockers`` entry.
  2. ``RiskGate.check`` (risk_gate.py:15) re-validates the ALREADY-CLEARED
     decision, re-deriving ``缺少完整 trade_plan`` and collapsing the real
     blocker reason.

RED-first + revert-fail: T1 wraps ``validate_trade_plan`` at BOTH module
references (``risk_engine`` and ``risk_gate``) with a counting spy that still
calls the real function, and asserts the total call count across the full
controller path is exactly 1. Today: 2 -> RED. After the fix (RiskGate.check
copies the immutable upstream risk_check instead of re-validating): 1 -> GREEN,
and removing the fix flips back to RED because the risk_gate re-call returns.

T2 proves the ORIGINAL risk reason survives (as a standalone list element, not
just embedded inside the "风控未通过（…）" wrapper), never overwritten by the
missing-plan recompute. T3 proves account-risk's ``side`` comes from the
preserved candidate/llm_synthesis plan, not the cleared ``trade_plan``.

Real production paths only: real ``GAMasterController(repo).analyze_symbol`` on
a real per-test PostgreSQL schema (``make_repo``). The spy wraps the REAL
``validate_trade_plan`` / ``AccountRiskGuard.check`` (counts + captures, never
mocks the function under test). No environment variable is used to green the
test.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.ga_master import risk_gate as risk_gate_mod
from plugins.crypto_guard.risk import risk_engine as risk_engine_mod
from plugins.crypto_guard.risk.account_risk_guard import AccountRiskGuard
from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.tests.test_pg_plan_lifecycle_finalizer_p1_1 import (
    _bullish_snapshot,
    _entry_conf,
    _llm_confirmed_candidate,
    _run_controller,
)


def _risk_rejected_candidate(snapshot: dict) -> dict:
    """LLM-confirmed LONG plan that upstream risk REJECTS (RR below 2.0).

    ``apply_risk_to_decision`` validates the ORIGINAL plan once, clears
    has_trade_plan/trade_plan, preserves the rejected plan under
    ``candidate_trade_plan`` and sets ``plan_blockers=[{code: risk_rejected,
    stage: risk_gate, detail: 'RR ... 低于 2.0'}]`` BEFORE ``RiskGate.check``
    runs — the exact state the P1-1 re-validation bug operates on.
    """
    return _llm_confirmed_candidate(
        snapshot, entry_conf=_entry_conf(),
        tp_override={"take_profits": [
            {"price": 101.0, "ratio": 0.5},
            {"price": 101.5, "ratio": 0.5},
        ]},
    )


class TestRiskGateSingleValidationCodexP1_1:
    def test_validate_trade_plan_called_exactly_once(self) -> None:
        """RED->GREEN: total ``validate_trade_plan`` calls across the real
        controller path == 1. The one legitimate validation happens in
        ``apply_risk_to_decision`` on the ORIGINAL proposed plan; RiskGate.check
        must reuse that immutable result, never re-validate the cleared state.

        Revert-fail: reintroducing the ``validate_trade_plan`` re-call inside
        RiskGate.check (or anywhere else on the path) bumps the count to 2 and
        flips this test RED.
        """
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            candidate = _risk_rejected_candidate(snap)
            counter = {"n": 0}
            real_vtp = risk_engine_mod.validate_trade_plan

            def _spy(decision, snapshot=None):
                counter["n"] += 1
                return real_vtp(decision, snapshot)

            patchers = [
                mock.patch.object(risk_engine_mod, "validate_trade_plan",
                                  side_effect=_spy),
            ]
            # RiskGate imports validate_trade_plan at module top, so it is a
            # SEPARATE module reference. Patch it only when present: the fix
            # removes the import, so post-fix hasattr() is False and this
            # patch is skipped (the counting is then only over the one
            # legitimate upstream reference).
            if hasattr(risk_gate_mod, "validate_trade_plan"):
                patchers.append(
                    mock.patch.object(risk_gate_mod, "validate_trade_plan",
                                      side_effect=_spy))
            for p in patchers:
                p.start()
            try:
                raw = _run_controller(handle.repo, snap,
                                      preset_candidate=candidate)
            finally:
                for p in patchers:
                    p.stop()
            assert raw["plan_execution_state"] == "risk_rejected", (
                raw.get("plan_execution_state"))
            assert counter["n"] == 1, (
                f"validate_trade_plan called {counter['n']} times on the "
                "controller path; the risk gate must reuse the immutable "
                "upstream risk_check, not re-validate the cleared decision")
        finally:
            handle.close()

    def test_original_risk_reason_not_overwritten_by_missing_plan(self) -> None:
        """RED->GREEN: the ORIGINAL risk reason (``RR <x> 低于 2.0``) must
        appear as a standalone element of the persisted ``risk_check.reasons``.
        The re-validated ``缺少完整 trade_plan`` on the cleared state must NOT
        overwrite it.

        Pre-fix the reason list collapses to
        ``['风控未通过（risk_gate）：RR 0.25 低于 2.0']`` (the raw reason is
        dropped by the re-derivation and only survives embedded in the
        wrapper) -> RED. Post-fix the raw reason leads the list
        ``['RR 0.25 低于 2.0', '风控未通过（risk_gate）：RR 0.25 低于 2.0']``
        -> GREEN.
        """
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            raw = _run_controller(handle.repo, snap,
                                  preset_candidate=_risk_rejected_candidate(snap))
            assert raw["plan_execution_state"] == "risk_rejected", (
                raw.get("plan_execution_state"))
            risk = raw.get("risk_check") or {}
            reasons = list(risk.get("reasons") or [])
            assert "缺少完整 trade_plan" not in reasons, reasons
            assert any(
                r.startswith("RR ") and "低于" in r for r in reasons
            ), (
                "the ORIGINAL risk reason (RR below threshold) must survive "
                f"as a standalone reason; got {reasons!r}")
            assert any(
                "风控" in r or "止损" in r or "风险" in r for r in reasons
            ), reasons
        finally:
            handle.close()

    def test_account_risk_side_from_preserved_candidate_plan(self) -> None:
        """RED->GREEN: ``AccountRiskGuard.check`` receives the ``side`` from
        the PRESERVED original/llm_synthesis plan (``candidate_trade_plan``),
        never from the cleared ``trade_plan`` (which is None on the
        risk-rejected path).

        Pre-fix ``decision.get("trade_plan")`` is None because
        ``apply_risk_to_decision`` already cleared it -> side="" -> RED.
        Post-fix the account side falls back to the preserved candidate plan
        -> side="LONG" for this LLM-confirmed LONG plan -> GREEN.
        """
        handle = make_repo()
        try:
            snap = _bullish_snapshot()
            captured = {"side": None}
            real_check = AccountRiskGuard.check

            def _capture(*args, **kwargs):
                captured["side"] = kwargs.get("side")
                return real_check(*args, **kwargs)

            with mock.patch.object(
                AccountRiskGuard, "check",
                autospec=True, side_effect=_capture,
            ) as m_check:
                raw = _run_controller(handle.repo, snap,
                                      preset_candidate=_risk_rejected_candidate(snap))
            assert raw["plan_execution_state"] == "risk_rejected", (
                raw.get("plan_execution_state"))
            assert isinstance(raw.get("candidate_trade_plan"), dict), (
                "the rejected plan must be preserved as candidate")
            assert m_check.call_count == 1, m_check.call_args_list
            assert captured["side"] == "LONG", (
                f"account-risk side must come from the preserved "
                f"candidate/llm_synthesis plan, got {captured['side']!r}")
        finally:
            handle.close()
