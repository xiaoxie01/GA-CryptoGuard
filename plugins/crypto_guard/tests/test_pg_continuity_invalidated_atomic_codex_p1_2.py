# -*- coding: utf-8 -*-
"""Codex terminal-review P1-2: continuity-invalidated finalizer is ATOMIC.

Codex finding (verbatim essence):
    continuity_trigger_invalidated 分支必须无条件：plan_execution_state=
    invalidated、plan_status=withheld、has_trade_plan=False、trade_plan=None、
    不得保留 create_paper_order/trade_plan_available 等执行决定。原计划必须
    保存在 candidate_trade_plan，blocker 必须保留。不得用 setdefault 保留
    executable。增加直接调用 ``_finalize_plan_lifecycle`` 的 RED/revert-fail
    测试，输入 invalidated + executable + live plan，修复后必须 fail-closed。

The finalizer's continuity branch currently does
``legacy.setdefault("plan_status", "withheld")`` and returns — so when the
incoming state still carries an executable claim (plan_status="executable",
has_trade_plan=True, a live trade_plan, decision in {create_paper_order,
trade_plan_available}), the surviving executable fields persist on a row the
finalizer just labelled ``invalidated``. That is the invalidated+executable
contradiction the P1-1 finalizer contract forbids. The branch must be
UNCONDITIONAL and mirror the risk_rejected branch (534-551): preserve the
dead plan under ``candidate_trade_plan``, clear the executable fields, keep the
structured blocker.

RED-first + revert-fail: each test calls ``_finalize_plan_lifecycle`` DIRECTLY
with the exact shape the finding names (invalidated + executable + live plan)
and asserts the fail-closed atomic output. Today the executable fields survive
-> RED. After the fix they are unconditionally cleared -> GREEN; reverting the
branch flips every assertion back to RED.
"""

from __future__ import annotations

import pytest

from plugins.crypto_guard.ga_master.controller import _finalize_plan_lifecycle

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

_LIVE_PLAN = {
    "side": "LONG",
    "entry_type": "limit",
    "entry_price": 100.0,
    "trigger_price": 100.5,
    "stop_loss": 95.0,
    "take_profits": [
        {"price": 108.0, "ratio": 0.5},
        {"price": 112.0, "ratio": 0.5},
    ],
    "entry_trigger_confirmation": {
        "type": "closed_candle_confirmation", "timeframe": "15m",
        "event_type": "BOS", "direction": "bullish",
        "candle_close_time": 1700000000000, "price": 99.5,
        "source": "price_action", "symbol": "ADAUSDT",
    },
}

_INVALIDATED_BLOCKER = [
    {"code": "continuity_trigger_invalidated", "stage": "synthesis",
     "detail": "前次 LONG 触发已被反转 invalidated"},
]


def _executable_claiming_legacy(**overrides) -> dict:
    """The finding's exact input shape: invalidated blocker + executable
    plan_status + live plan + an executable claiming decision."""
    legacy = {
        "symbol": "ADAUSDT",
        "analysis_time_utc": 1700000000000,
        "decision": "create_paper_order",
        "signal_grade": "S",
        "effective_signal_grade": "S",
        "confidence": 0.9,
        "has_trade_plan": True,
        "trade_plan": dict(_LIVE_PLAN),
        "candidate_trade_plan": None,
        "plan_origin": "llm_confirmed",
        "plan_execution_state": "confirmed",
        "plan_status": "executable",
        "plan_blockers": [dict(b) for b in _INVALIDATED_BLOCKER],
    }
    legacy.update(overrides)
    return legacy


class TestContinuityInvalidatedAtomicCodexP1_2:
    def test_invalidated_executable_claim_fails_closed(self) -> None:
        """RED->GREEN: invalidated + executable + live plan + a claiming
        decision -> the finalizer must unconditionally fail closed: withheld,
        no executable claim, plan preserved as candidate, blocker kept."""
        legacy = _executable_claiming_legacy()
        _finalize_plan_lifecycle(legacy, {"ok": True})
        assert legacy["plan_execution_state"] == "invalidated", (
            legacy.get("plan_execution_state"))
        assert legacy["plan_status"] == "withheld", (
            f"setdefault must not preserve 'executable'; got {legacy.get('plan_status')!r}")
        assert legacy["has_trade_plan"] is False, (
            f"invalidated rows must not keep has_trade_plan; got {legacy.get('has_trade_plan')!r}")
        assert legacy["trade_plan"] is None, (
            f"invalidated rows must not keep a live trade_plan; got {legacy.get('trade_plan')!r}")
        assert legacy["decision"] == "monitor_only", (
            f"an invalidated row must not claim create_paper_order/trade_plan_available; "
            f"got {legacy.get('decision')!r}")
        candidate = legacy.get("candidate_trade_plan")
        assert isinstance(candidate, dict) and candidate.get("side") == "LONG", (
            "the dead plan must be preserved as candidate_trade_plan")
        blockers = legacy.get("plan_blockers") or []
        assert any(
            isinstance(b, dict) and b.get("code") == "continuity_trigger_invalidated"
            for b in blockers
        ), legacy.get("plan_blockers")

    def test_invalidated_trade_plan_available_claim_fails_closed(self) -> None:
        """RED->GREEN: same with decision="trade_plan_available" — the OTHER
        claiming decision value must also be reset to monitor_only."""
        legacy = _executable_claiming_legacy(decision="trade_plan_available")
        _finalize_plan_lifecycle(legacy, {"ok": True})
        assert legacy["plan_execution_state"] == "invalidated"
        assert legacy["plan_status"] == "withheld"
        assert legacy["has_trade_plan"] is False
        assert legacy["trade_plan"] is None
        assert legacy["decision"] == "monitor_only", legacy.get("decision")
        assert isinstance(legacy.get("candidate_trade_plan"), dict)

    def test_invalidated_preserves_non_claiming_gate_decision(self) -> None:
        """GREEN both: a non-claiming gate decision (wait_for_pullback /
        opportunity_watch) must NOT be clobbered to monitor_only — the
        finalizer only neutralizes executable claims."""
        legacy = _executable_claiming_legacy(decision="wait_for_pullback")
        _finalize_plan_lifecycle(legacy, {"ok": True})
        assert legacy["plan_execution_state"] == "invalidated"
        assert legacy["plan_status"] == "withheld"
        assert legacy["has_trade_plan"] is False
        assert legacy["trade_plan"] is None
        assert legacy["decision"] == "wait_for_pullback", legacy.get("decision")
        assert isinstance(legacy.get("candidate_trade_plan"), dict)
        assert any(
            isinstance(b, dict) and b.get("code") == "continuity_trigger_invalidated"
            for b in (legacy.get("plan_blockers") or [])
        )

    def test_invalidated_without_candidate_still_preserves_plan(self) -> None:
        """RED->GREEN: no candidate yet + live plan -> the plan must be moved
        to candidate_trade_plan by the finalizer (atomicity, not left to the
        producer)."""
        legacy = _executable_claiming_legacy(candidate_trade_plan=None)
        _finalize_plan_lifecycle(legacy, {"ok": True})
        candidate = legacy.get("candidate_trade_plan")
        assert isinstance(candidate, dict) and candidate.get("side") == "LONG", (
            "the finalizer must preserve the dead plan as candidate even when "
            "the producer left none")
        assert legacy["plan_status"] == "withheld"
        assert legacy["has_trade_plan"] is False
        assert legacy["trade_plan"] is None
        assert legacy["decision"] == "monitor_only"
