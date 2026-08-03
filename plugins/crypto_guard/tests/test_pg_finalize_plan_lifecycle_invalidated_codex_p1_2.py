# -*- coding: utf-8 -*-
"""Codex terminal-review P1-2: continuity-invalidated finalizer is ATOMIC.

Finding (verbatim essence):
    continuity_trigger_invalidated 分支必须无条件：plan_execution_state=
    invalidated、plan_status=withheld、has_trade_plan=False、trade_plan=None、
    不得保留 create_paper_order/trade_plan_available 等执行决定。原计划必须
    保存在 candidate_trade_plan，blocker 必须保留。不得用 setdefault 保留
    executable。增加直接调用 _finalize_plan_lifecycle 的 RED/revert-fail 测试，
    输入 invalidated + executable + live plan，修复后必须 fail-closed。

RED-first + revert-fail: each test calls ``_finalize_plan_lifecycle`` DIRECTLY
(no controller/DB) with a legacy dict carrying the continuity_trigger_invalidated
blocker AND a surviving executable claim (plan_status="executable",
has_trade_plan=True, a live trade_plan, decision in {create_paper_order,
trade_plan_available}) — the exact shape the finding names. It then asserts the
fail-closed atomic output. Today the branch does
``legacy.setdefault("plan_status", "withheld")`` and returns, so the executable
fields survive -> RED. After the fix the branch mirrors the risk_rejected
branch (unconditional plan_status + candidate preservation + clears + claiming
decision reset) -> GREEN. Reverting the fix flips every assertion back to RED.
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


def _invalidated_executable_legacy(**overrides) -> dict:
    """The finding's exact RED input: continuity-invalidated blocker +
    surviving executable claim (executable plan_status + live plan +
    claiming decision)."""
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


class TestFinalizeLifecycleInvalidatedAtomicCodexP1_2:
    def test_invalidated_with_executable_claim_fails_closed(self) -> None:
        """RED->GREEN: invalidated + executable + live plan + create_paper_order
        -> the finalizer must fail closed: invalidated/withheld, plan cleared,
        plan preserved as candidate, blocker kept, decision neutralized."""
        legacy = _invalidated_executable_legacy()
        _finalize_plan_lifecycle(legacy, {"ok": True})
        assert legacy["plan_execution_state"] == "invalidated", legacy
        assert legacy["plan_status"] == "withheld", (
            f"setdefault must not preserve 'executable'; got "
            f"{legacy.get('plan_status')!r}")
        assert legacy["has_trade_plan"] is False, (
            f"invalidated rows must not keep has_trade_plan; got "
            f"{legacy.get('has_trade_plan')!r}")
        assert legacy["trade_plan"] is None, (
            f"invalidated rows must not keep a live trade_plan; got "
            f"{legacy.get('trade_plan')!r}")
        assert legacy["decision"] == "monitor_only", (
            f"invalidated rows must not claim create_paper_order; got "
            f"{legacy.get('decision')!r}")
        candidate = legacy.get("candidate_trade_plan")
        assert isinstance(candidate, dict) and candidate.get("side") == "LONG", (
            "the dead plan must be preserved as candidate_trade_plan")
        assert any(
            isinstance(b, dict) and b.get("code") == "continuity_trigger_invalidated"
            for b in (legacy.get("plan_blockers") or [])
        ), legacy.get("plan_blockers")

    def test_invalidated_with_trade_plan_available_claim_fails_closed(self) -> None:
        """RED->GREEN: decision=trade_plan_available (the OTHER claiming
        decision value) must also be neutralized to monitor_only."""
        legacy = _invalidated_executable_legacy(decision="trade_plan_available")
        _finalize_plan_lifecycle(legacy, {"ok": True})
        assert legacy["plan_execution_state"] == "invalidated"
        assert legacy["plan_status"] == "withheld"
        assert legacy["has_trade_plan"] is False
        assert legacy["trade_plan"] is None
        assert legacy["decision"] == "monitor_only", legacy.get("decision")
        assert isinstance(legacy.get("candidate_trade_plan"), dict)
        assert any(
            isinstance(b, dict) and b.get("code") == "continuity_trigger_invalidated"
            for b in (legacy.get("plan_blockers") or [])
        )

    def test_invalidated_preserves_non_claiming_gate_decision(self) -> None:
        """GREEN both: a non-claiming gate decision (opportunity_watch /
        wait_for_pullback) must NOT be clobbered to monitor_only — the
        invalidated branch only neutralizes executable claims, mirroring the
        risk_rejected branch."""
        legacy = _invalidated_executable_legacy(decision="opportunity_watch")
        _finalize_plan_lifecycle(legacy, {"ok": True})
        assert legacy["plan_execution_state"] == "invalidated"
        assert legacy["plan_status"] == "withheld"
        assert legacy["has_trade_plan"] is False
        assert legacy["trade_plan"] is None
        assert legacy["decision"] == "opportunity_watch", legacy.get("decision")
        assert isinstance(legacy.get("candidate_trade_plan"), dict)
        assert any(
            isinstance(b, dict) and b.get("code") == "continuity_trigger_invalidated"
            for b in (legacy.get("plan_blockers") or [])
        )

    def test_invalidated_preserves_existing_candidate(self) -> None:
        """GREEN both: when a candidate already exists, the finalizer keeps
        the existing candidate (not overwritten by the live plan) and still
        clears the live plan."""
        preset_candidate = dict(_LIVE_PLAN)
        preset_candidate["side"] = "SHORT"
        legacy = _invalidated_executable_legacy(candidate_trade_plan=preset_candidate)
        _finalize_plan_lifecycle(legacy, {"ok": True})
        assert legacy["plan_execution_state"] == "invalidated"
        assert legacy["plan_status"] == "withheld"
        assert legacy["has_trade_plan"] is False
        assert legacy["trade_plan"] is None
        assert legacy["decision"] == "monitor_only", legacy.get("decision")
        candidate = legacy.get("candidate_trade_plan")
        assert isinstance(candidate, dict) and candidate.get("side") == "SHORT", (
            "existing candidate must be preserved, not overwritten by the "
            "live plan")
