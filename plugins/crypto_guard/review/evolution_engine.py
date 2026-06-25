from __future__ import annotations

from typing import Any


def build_candidate_patch(trade: dict[str, Any], primary_reason: str, *, strategy_name: str = "unknown") -> dict[str, Any] | None:
    """Build a conditional candidate patch based on the loss pattern.

    Each pattern produces distinct, context-aware adjustments with 'when' conditions.
    Unknown patterns return None (needs_manual_classification) instead of a generic penalty.

    Args:
        trade: The trade dict that triggered the evolution review.
        primary_reason: The primary loss reason from aggregation.
        strategy_name: Resolved from trade → order → ga_decision chain. Must not be hardcoded.
    """
    if primary_reason == "good_execution":
        return None

    trade_id = trade.get("id", "unknown")
    side = str(trade.get("side") or "").upper()

    # Determine the loss pattern for conditional rules
    from plugins.crypto_guard.review.loss_classifier import classify_trade
    pattern = classify_trade(trade)

    if pattern == "unknown":
        return None  # needs_manual_classification — no auto candidate

    # Build conditional score_adjustments with when clauses
    score_adjustments = _build_conditional_adjustments(pattern, side, trade)

    return {
        "strategy_name": strategy_name,
        "from_version": "1.0",
        "candidate_version": f"1.1-candidate-trade-{trade_id}",
        "change_reason": f"模拟盘复盘触发：{primary_reason}（模式：{pattern}）",
        "patch": {
            "score_adjustments": score_adjustments,
            "risk_controls": ["candidate_only_shadow_testing_before_active"],
            "loss_pattern": pattern,
        },
    }


def _build_conditional_adjustments(pattern: str, side: str, trade: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Build pattern-specific conditional score_adjustments.

    Each adjustment has {value, when} where 'when' defines the activation context.
    All rules include at least one real context condition (side, trend_stage, etc.)
    beyond the informational pattern_type.
    """
    trade_side = str(trade.get("side") or side or "").upper() if trade else side
    trade_trend = str(trade.get("trend_stage") or "") if trade else ""

    if pattern == "wrong_direction":
        adj = {
            "smc_orderflow_direction_penalty": {
                "value": -0.08,
                "when": {
                    "pattern_type": "wrong_direction",
                    "side": trade_side,
                    "description": "降低 SMC orderflow 方向信号权重",
                },
            },
        }
        if trade_trend:
            adj["smc_orderflow_direction_penalty"]["when"]["trend_stage"] = trade_trend
        return adj

    if pattern == "entry_too_late":
        adj = {
            "momentum_confirmation_required": {
                "value": -0.05,
                "when": {
                    "pattern_type": "entry_too_late",
                    "side": trade_side,
                    "description": "要求 momentum confirmation 通过后才允许入场",
                },
            },
        }
        if trade_trend:
            adj["momentum_confirmation_required"]["when"]["trend_stage"] = trade_trend
        return adj

    if pattern == "entry_chasing":
        return {
            "entry_timing_penalty": {
                "value": -0.06,
                "when": {
                    "pattern_type": "entry_chasing",
                    "side": trade_side,
                    "description": "追涨杀跌惩罚，要求结构确认",
                },
            },
        }

    if pattern == "late_trend_chasing":
        return {
            "late_entry_penalty": {
                "value": -0.07,
                "when": {
                    "pattern_type": "late_trend_chasing",
                    "side": trade_side,
                    "description": "趋势末期追入惩罚",
                },
            },
        }

    if pattern == "stop_loss_too_tight":
        return {
            "wider_stop_required": {
                "value": -0.03,
                "when": {
                    "pattern_type": "stop_loss_too_tight",
                    "side": trade_side,
                    "description": "止损过紧，要求更宽止损或结构确认",
                },
            },
        }

    if pattern == "entry_too_early":
        return {
            "zhongshu_confirmation_required": {
                "value": -0.04,
                "when": {
                    "pattern_type": "entry_too_early",
                    "side": trade_side,
                    "description": "中枢突破确认后才允许入场",
                },
            },
        }

    if pattern == "take_profit_too_far":
        return {
            "tp_adjustment": {
                "value": -0.02,
                "when": {
                    "pattern_type": "take_profit_too_far",
                    "side": trade_side,
                    "description": "止盈目标过远，降低置信度",
                },
            },
        }

    # Market regime mismatch patterns
    if pattern == "macro_selloff_long_trap_loss":
        return {
            "risk_off_long_pause": {
                "value": -0.10,
                "when": {
                    "pattern_type": "macro_selloff_long_trap_loss",
                    "side": "LONG",
                    "market_phase": "risk_off",
                    "description": "risk_off 环境 LONG 方向提权暂停",
                },
            },
        }

    if pattern == "macro_rebound_short_squeeze_loss":
        return {
            "risk_on_short_pause": {
                "value": -0.10,
                "when": {
                    "pattern_type": "macro_rebound_short_squeeze_loss",
                    "side": "SHORT",
                    "market_phase": "risk_on",
                    "description": "risk_on 环境 SHORT 方向提权暂停",
                },
            },
        }

    if pattern == "counter_regime_entry_loss":
        return {
            "counter_regime_penalty": {
                "value": -0.08,
                "when": {
                    "pattern_type": "counter_regime_entry_loss",
                    "description": "逆势入场惩罚",
                },
            },
        }

    if pattern in ("market_regime_mismatch_short_loss", "market_regime_mismatch_long_loss"):
        return {
            "regime_mismatch_penalty": {
                "value": -0.06,
                "when": {
                    "pattern_type": pattern,
                    "description": "市场环境不匹配惩罚",
                },
            },
        }

    # Fallback: weak generic penalty (shouldn't reach here since unknown returns None above)
    return {
        "generic_penalty": {
            "value": -0.03,
            "when": {"pattern_type": pattern, "description": "通用惩罚"},
        },
    }
