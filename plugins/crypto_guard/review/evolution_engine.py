from __future__ import annotations

from typing import Any


def build_candidate_patch(trade: dict[str, Any], primary_reason: str) -> dict[str, Any] | None:
    if primary_reason == "good_execution":
        return None
    return {
        "strategy_name": "smc_pullback_long",
        "from_version": "1.0",
        "candidate_version": "1.1-candidate",
        "change_reason": f"模拟盘复盘触发：{primary_reason}",
        "patch": {
            "score_adjustments": {f"{primary_reason}_penalty": -0.05},
            "risk_filters": ["candidate_only_shadow_testing_before_active"],
        },
    }
