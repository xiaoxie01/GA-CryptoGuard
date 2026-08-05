from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.ga_master.decision_schema import controller_decision_from_legacy
from plugins.crypto_guard.ga_master.feishu_action_builder import build_feishu_actions
from plugins.crypto_guard.paper.execution_quality import close_quality_metrics, evaluate_exit, market_from_price, update_trade_path_metrics
from plugins.crypto_guard.paper.sizing import (
    DEFAULT_ACCOUNT_BALANCE,
    DEFAULT_RISK_PERCENT,
    DEFAULT_SLIPPAGE_PCT,
    compute_fill_price,
    compute_position_size,
)
from plugins.crypto_guard.risk.risk_engine import validate_trade_plan

from plugins.crypto_guard.storage.repository import CryptoGuardRepository, utc_iso
from plugins.crypto_guard.utils import utc_ms


class _ConflictCancelRaceLost(Exception):
    """Sentinel raised to roll back the conflict-cancel savepoint when the
    CAS UPDATE matched zero rows (another worker won the race).

    psycopg3 ``with conn.transaction():`` rolls the savepoint back when the
    ``with`` block exits via this exception, reverting the no-op UPDATE
    without disturbing the caller's outer transaction.
    """


def _safe_json(raw: Any, default: Any = None) -> Any:
    """JSONB-aware decode: psycopg3 returns JSONB columns as already-decoded
    Python dict/list, so ``json.loads(dict)`` raises TypeError. Pass dict/list
    through; only ``json.loads`` a str/bytes. Returns ``default`` for None/empty.
    """
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            return json.loads(raw)
        except Exception:
            return default
    return default


def create_paper_order_from_signal(repo: CryptoGuardRepository, signal_id: int) -> dict[str, Any]:
    signal = repo.get_signal(signal_id)
    if not signal:
        return {"ok": False, "error": "signal 不存在", "signal_id": signal_id}
    if not signal.get("trade_plan_json"):
        return {"ok": False, "error": "该 signal 没有完整 trade_plan，不能加入模拟盘", "signal_id": signal_id}
    trade_plan = _safe_json(signal["trade_plan_json"], {})
    required = ["side", "entry_type", "stop_loss", "take_profits", "risk_percent", "invalid_condition", "reason"]
    missing = [k for k in required if k not in trade_plan or trade_plan[k] in (None, [], "")]
    if missing:
        return {"ok": False, "error": f"trade_plan 字段不完整: {missing}", "signal_id": signal_id}

    # Account risk guard — hard_risk_off / daily_loss_pause 阻断
    account_risk = _check_account_risk(repo, signal.get("symbol", ""), trade_plan.get("side", ""))
    if account_risk.get("pause_active"):
        return {
            "ok": False,
            "error": "账户暂停开仓",
            "pause_reason": account_risk.get("pause_reason"),
            "account_risk": account_risk,
            "signal_id": signal_id,
        }

    # Account feedback gate — check before order creation
    from plugins.crypto_guard.risk.account_feedback_gate import check_account_feedback_gate
    symbol = signal.get("symbol", "")
    side = trade_plan.get("side", "")
    confidence = float(signal.get("confidence") or 0)
    entry_quality = _extract_entry_quality(trade_plan)
    feedback_gate = check_account_feedback_gate(repo, symbol, side, confidence, entry_quality)

    # Only enforce gate decisions in controlled mode; shadow mode always proceeds
    if feedback_gate.get("mode") != "shadow":
        gate_decision = feedback_gate.get("would_decide") or feedback_gate.get("decision", "")
        if gate_decision in ("downgrade_to_watch", "block_order"):
            # Resolve ga_decision_id for audit persistence (legacy compatibility)
            ga_id_for_gate = signal.get("ga_decision_id")
            if not ga_id_for_gate:
                # Create a pending GA decision with honest risk status
                ga_id_for_gate = _ensure_ga_decision_for_legacy_signal(
                    repo, signal, trade_plan,
                    {"ok": False, "reasons": ["gate_blocked_before_risk_validation"], "metrics": {}, "pending": True},
                )
            # Always persist gate result to GA decision
            _save_gate_result_to_ga_decision(repo, int(ga_id_for_gate), feedback_gate)
            # Create opportunity watch linked to the GA decision
            if gate_decision == "downgrade_to_watch":
                _create_opportunity_watch_from_gate(repo, symbol, side, int(ga_id_for_gate), feedback_gate)
            return {
                "ok": False,
                "error": "gate_blocked",
                "gate_decision": gate_decision,
                "gate_reason": feedback_gate.get("reason"),
                "feedback_gate": feedback_gate,
                "signal_id": signal_id,
                "ga_decision_id": int(ga_id_for_gate),
            }

    # Snapshot + risk validation (must run before creating compatibility GA decision)
    snapshot = None
    if signal.get("market_snapshot_id"):
        row = repo.get_market_snapshot(int(signal["market_snapshot_id"]))
        if row:
            snapshot = _safe_json(row.get("snapshot_json"), {})
    _gd = signal.get("ga_decision_json")
    decision = _safe_json(_gd, {}) if _gd else {"confidence": signal.get("confidence"), "trade_plan": trade_plan, "has_trade_plan": True}
    decision["trade_plan"] = trade_plan
    decision["has_trade_plan"] = True
    risk = validate_trade_plan(decision, snapshot or {})

    # Now resolve ga_decision_id with REAL risk result (no synthetic approval)
    ga_decision_id = signal.get("ga_decision_id")
    if not ga_decision_id:
        ga_decision_id = _ensure_ga_decision_for_legacy_signal(
            repo, signal, trade_plan, risk,
        )

    # Always persist account feedback gate result to GA decision
    _save_gate_result_to_ga_decision(repo, int(ga_decision_id), feedback_gate)

    if not risk["ok"]:
        return {
            "ok": False,
            "error": "模拟盘风控未通过，不能创建订单；建议加入机会监控。",
            "risk_reasons": risk["reasons"],
            "risk_check": risk,
            "signal_id": signal_id,
        }

    # Market regime gate — soft downgrade/restrict counter-regime entries
    # Use the decision's original analysis_time to avoid lookahead bias
    signal_analysis_time, time_source = _resolve_analysis_time(repo, signal, ga_decision_id)
    regime_gate = _apply_regime_gate_if_enabled(
        repo,
        symbol=signal["symbol"],
        side=str(trade_plan.get("side", "")).upper(),
        signal_grade=str(decision.get("signal_grade", "D")).upper(),
        confidence=_signal_decision_confidence(decision, signal),
        analysis_time_utc=signal_analysis_time,
        order_type=str(trade_plan.get("entry_type", "")),
        time_source=time_source,
    )
    # Always save regime result for audit (even in shadow mode)
    if regime_gate:
        _save_regime_gate_to_ga_decision(repo, int(ga_decision_id), regime_gate)
    if regime_gate and regime_gate.get("regime_gate_applied"):
        adjustments = regime_gate.get("adjustments", {})
        regime_mode = regime_gate.get("mode", "shadow")
        if adjustments.get("watch_only") and regime_mode == "controlled":
            # Create opportunity watch instead of paper order
            _create_opportunity_watch_from_gate(repo, signal["symbol"],
                trade_plan.get("side", ""), int(ga_decision_id),
                {"reason": f"counter_regime_watch_only: {regime_gate.get('market_regime', {}).get('market_phase')}",
                 "would_decide": "downgrade_to_watch",
                 "regime_gate": regime_gate})
            return {
                "ok": False,
                "error": "regime_gate_watch_only",
                "regime_gate": regime_gate,
                "signal_id": signal_id,
                "ga_decision_id": int(ga_decision_id),
            }
        # In shadow mode or non-watch_only: apply adjustments but proceed
        if regime_mode == "controlled" and not adjustments.get("watch_only"):
            # Check allowed_order_types and min_rr before applying adjustments
            downgrade_reason = _should_downgrade_to_watch_by_regime(trade_plan, adjustments)
            if downgrade_reason:
                _create_opportunity_watch_from_gate(repo, signal["symbol"],
                    trade_plan.get("side", ""), int(ga_decision_id),
                    {"reason": f"regime_downgrade: {downgrade_reason}",
                     "would_decide": "downgrade_to_watch",
                     "regime_gate": regime_gate})
                return {
                    "ok": False,
                    "error": "regime_gate_watch_only",
                    "regime_gate": regime_gate,
                    "signal_id": signal_id,
                    "ga_decision_id": int(ga_decision_id),
                    "regime_downgrade_reason": downgrade_reason,
                }
            trade_plan = _apply_regime_adjustments(trade_plan, adjustments)
            # Check if effective grade/confidence still qualifies for paper order
            effective_grade = adjustments.get("effective_grade", "")
            effective_confidence = _get_effective_regime_confidence(adjustments)
            from plugins.crypto_guard.strategy.grade_config import is_paper_order_eligible
            if not is_paper_order_eligible(effective_grade, effective_confidence):
                _create_opportunity_watch_from_gate(repo, signal["symbol"],
                    trade_plan.get("side", ""), int(ga_decision_id),
                    {"reason": f"regime_downgrade: effective_grade={effective_grade}, effective_confidence={effective_confidence:.2f} below paper order eligibility",
                     "would_decide": "downgrade_to_watch",
                     "regime_gate": regime_gate})
                return {
                    "ok": False,
                    "error": "regime_gate_watch_only",
                    "regime_gate": regime_gate,
                    "signal_id": signal_id,
                    "ga_decision_id": int(ga_decision_id),
                    "regime_downgrade_reason": f"effective_grade={effective_grade}, effective_confidence={effective_confidence:.2f} below paper order eligibility",
                }
            # Enforce require_stronger_confirmation: check actual confidence/entry_quality
            # against raised thresholds set by _apply_regime_adjustments.
            # Confidence comes from the decision dict (same source as the gate call at line 111),
            # entry_quality is extracted from trade_plan via _extract_entry_quality.
            stronger_reason = _check_stronger_confirmation(
                trade_plan, adjustments,
                confidence=_signal_decision_confidence(decision, signal),
                entry_quality=_extract_entry_quality(trade_plan),
            )
            if stronger_reason:
                _create_opportunity_watch_from_gate(repo, signal["symbol"],
                    trade_plan.get("side", ""), int(ga_decision_id),
                    {"reason": f"regime_{stronger_reason}",
                     "would_decide": "downgrade_to_watch",
                     "regime_gate": regime_gate})
                return {
                    "ok": False,
                    "error": "regime_gate_watch_only",
                    "regime_gate": regime_gate,
                    "signal_id": signal_id,
                    "ga_decision_id": int(ga_decision_id),
                    "regime_downgrade_reason": stronger_reason,
                }

    order_id, created = repo.create_paper_order(
        signal_id,
        signal,
        trade_plan,
        ga_decision_id=int(ga_decision_id),
        source="ga_decision",
        risk_check_passed=True,
    )
    return {"ok": True, "order_id": order_id, "created": created, "idempotent": not created, "ga_decision_id": int(ga_decision_id)}


def create_paper_order_from_ga_decision(repo: CryptoGuardRepository, ga_decision_id: int) -> dict[str, Any]:
    ga_decision = repo.get_ga_decision(int(ga_decision_id))
    if not ga_decision:
        return {"ok": False, "error": "GA decision 不存在", "ga_decision_id": ga_decision_id}
    actions = set(ga_decision.get("feishu_actions") or [])
    if "create_paper_order" not in actions:
        return {"ok": False, "error": "该 GA decision 不允许加入模拟盘", "ga_decision_id": ga_decision_id}
    trade_plan = ga_decision.get("trade_plan")
    if not isinstance(trade_plan, dict):
        return {"ok": False, "error": "该 GA decision 没有完整 trade_plan，不能加入模拟盘", "ga_decision_id": ga_decision_id}
    required = ["side", "entry_type", "stop_loss", "take_profits", "risk_percent", "invalid_condition", "reason"]
    missing = [k for k in required if k not in trade_plan or trade_plan[k] in (None, [], "")]
    if missing:
        return {"ok": False, "error": f"trade_plan 字段不完整: {missing}", "ga_decision_id": ga_decision_id}

    # Account risk guard — hard_risk_off / daily_loss_pause 阻断
    account_risk = _check_account_risk(repo, ga_decision.get("symbol", ""), trade_plan.get("side", ""))
    if account_risk.get("pause_active"):
        return {
            "ok": False,
            "error": "账户暂停开仓",
            "pause_reason": account_risk.get("pause_reason"),
            "account_risk": account_risk,
            "ga_decision_id": ga_decision_id,
        }

    # Account feedback gate — check before order creation
    from plugins.crypto_guard.risk.account_feedback_gate import check_account_feedback_gate
    symbol = ga_decision.get("symbol", "")
    side = trade_plan.get("side", "")
    confidence = float(ga_decision.get("confidence") or 0)
    entry_quality = _extract_entry_quality(trade_plan)
    feedback_gate = check_account_feedback_gate(repo, symbol, side, confidence, entry_quality)

    # Only enforce gate decisions in controlled mode; shadow mode always proceeds
    if feedback_gate.get("mode") != "shadow":
        gate_decision = feedback_gate.get("would_decide") or feedback_gate.get("decision", "")
        if gate_decision in ("downgrade_to_watch", "block_order"):
            # Create opportunity watch so user can monitor the missed signal
            if gate_decision == "downgrade_to_watch":
                _create_opportunity_watch_from_gate(repo, symbol, side, ga_decision_id, feedback_gate)
            # Persist gate result even when blocking (for shadow reporting accuracy)
            _save_gate_result_to_ga_decision(repo, int(ga_decision_id), feedback_gate)
            return {
                "ok": False,
                "error": "gate_blocked",
                "gate_decision": gate_decision,
                "gate_reason": feedback_gate.get("reason"),
                "feedback_gate": feedback_gate,
                "ga_decision_id": ga_decision_id,
            }

    # Always persist account feedback gate result to GA decision BEFORE risk validation
    _save_gate_result_to_ga_decision(repo, int(ga_decision_id), feedback_gate)

    raw = dict(ga_decision.get("raw_decision") or {})
    raw.update(
        {
            "symbol": ga_decision["symbol"],
            "confidence": ga_decision["confidence"],
            "has_trade_plan": True,
            "trade_plan": trade_plan,
            "risk_check": ga_decision.get("risk_check") or {},
        }
    )
    snapshot = {}
    if ga_decision.get("snapshot_id"):
        row = repo.get_market_snapshot(int(ga_decision["snapshot_id"]))
        if row:
            snapshot = _safe_json(row.get("snapshot_json"), {})
    risk = validate_trade_plan(raw, snapshot)
    if not risk["ok"]:
        return {
            "ok": False,
            "error": "模拟盘风控未通过，不能创建订单；建议加入机会监控。",
            "risk_reasons": risk["reasons"],
            "risk_check": risk,
            "ga_decision_id": ga_decision_id,
        }

    # Market regime gate — soft downgrade/restrict counter-regime entries
    # Use the GA decision's original analysis_time to avoid lookahead bias
    ga_analysis_time = ga_decision.get("analysis_time") or utc_ms()
    ga_time_source = "original_analysis_time" if ga_decision.get("analysis_time") else "fallback_now"
    regime_gate = _apply_regime_gate_if_enabled(
        repo,
        symbol=ga_decision.get("symbol", ""),
        side=str(trade_plan.get("side", "")).upper(),
        signal_grade=str(ga_decision.get("signal_grade", "D")).upper(),
        confidence=float(ga_decision.get("confidence") or 0),
        analysis_time_utc=ga_analysis_time,
        order_type=str(trade_plan.get("entry_type", "")),
        time_source=ga_time_source,
    )
    # Always save regime result for audit (even in shadow mode)
    if regime_gate:
        _save_regime_gate_to_ga_decision(repo, int(ga_decision_id), regime_gate)
    if regime_gate and regime_gate.get("regime_gate_applied"):
        adjustments = regime_gate.get("adjustments", {})
        regime_mode = regime_gate.get("mode", "shadow")
        if adjustments.get("watch_only") and regime_mode == "controlled":
            # Create opportunity watch instead of paper order
            _create_opportunity_watch_from_gate(repo, ga_decision.get("symbol", ""),
                trade_plan.get("side", ""), int(ga_decision_id),
                {"reason": f"counter_regime_watch_only: {regime_gate.get('market_regime', {}).get('market_phase')}",
                 "would_decide": "downgrade_to_watch",
                 "regime_gate": regime_gate})
            return {
                "ok": False,
                "error": "regime_gate_watch_only",
                "regime_gate": regime_gate,
                "ga_decision_id": ga_decision_id,
            }
        # In shadow mode or non-watch_only: apply adjustments but proceed
        if regime_mode == "controlled" and not adjustments.get("watch_only"):
            # Check allowed_order_types and min_rr before applying adjustments
            downgrade_reason = _should_downgrade_to_watch_by_regime(trade_plan, adjustments)
            if downgrade_reason:
                _create_opportunity_watch_from_gate(repo, symbol,
                    trade_plan.get("side", ""), int(ga_decision_id),
                    {"reason": f"regime_downgrade: {downgrade_reason}",
                     "would_decide": "downgrade_to_watch",
                     "regime_gate": regime_gate})
                return {
                    "ok": False,
                    "error": "regime_gate_watch_only",
                    "regime_gate": regime_gate,
                    "ga_decision_id": int(ga_decision_id),
                    "regime_downgrade_reason": downgrade_reason,
                }
            trade_plan = _apply_regime_adjustments(trade_plan, adjustments)
            # Check if effective grade/confidence still qualifies for paper order
            effective_grade = adjustments.get("effective_grade", "")
            effective_confidence = _get_effective_regime_confidence(adjustments)
            from plugins.crypto_guard.strategy.grade_config import is_paper_order_eligible
            if not is_paper_order_eligible(effective_grade, effective_confidence):
                _create_opportunity_watch_from_gate(repo, symbol,
                    trade_plan.get("side", ""), int(ga_decision_id),
                    {"reason": f"regime_downgrade: effective_grade={effective_grade}, effective_confidence={effective_confidence:.2f} below paper order eligibility",
                     "would_decide": "downgrade_to_watch",
                     "regime_gate": regime_gate})
                return {
                    "ok": False,
                    "error": "regime_gate_watch_only",
                    "regime_gate": regime_gate,
                    "ga_decision_id": int(ga_decision_id),
                    "regime_downgrade_reason": f"effective_grade={effective_grade}, effective_confidence={effective_confidence:.2f} below paper order eligibility",
                }
            # Enforce require_stronger_confirmation (same as signal path)
            stronger_reason = _check_stronger_confirmation(
                trade_plan, adjustments,
                confidence=float(ga_decision.get("confidence") or 0),
                entry_quality=_extract_entry_quality(trade_plan),
            )
            if stronger_reason:
                _create_opportunity_watch_from_gate(repo, symbol,
                    trade_plan.get("side", ""), int(ga_decision_id),
                    {"reason": f"regime_{stronger_reason}",
                     "would_decide": "downgrade_to_watch",
                     "regime_gate": regime_gate})
                return {
                    "ok": False,
                    "error": "regime_gate_watch_only",
                    "regime_gate": regime_gate,
                    "ga_decision_id": int(ga_decision_id),
                    "regime_downgrade_reason": stronger_reason,
                }

    signal = {
        "symbol": ga_decision["symbol"],
        "market_snapshot_id": ga_decision.get("snapshot_id"),
        "ga_decision_json": json.dumps(raw, ensure_ascii=False),
    }
    signal_row = repo.conn.execute("SELECT id FROM signals WHERE ga_decision_id=%s ORDER BY id DESC LIMIT 1", (int(ga_decision_id),)).fetchone()
    signal_id = int(signal_row["id"]) if signal_row else None

    order_id, created = repo.create_paper_order(
        signal_id,
        signal,
        trade_plan,
        ga_decision_id=int(ga_decision_id),
        source="ga_decision",
        risk_check_passed=True,
    )
    return {"ok": True, "order_id": order_id, "created": created, "idempotent": not created, "ga_decision_id": ga_decision_id}


def _ensure_ga_decision_for_legacy_signal(repo: CryptoGuardRepository, signal: dict[str, Any], trade_plan: dict[str, Any], risk: dict[str, Any]) -> int:
    # R11-4: preserve analysis_time_utc from the original signal's ga_decision_json
    # so subsequent reads of ga_decision_json can satisfy the strict-positive-int contract.
    _legacy_analysis_time: Any = None
    try:
        _orig_decision = _safe_json(signal.get("ga_decision_json"), {}) if signal.get("ga_decision_json") else {}
        if isinstance(_orig_decision, dict):
            _legacy_analysis_time = _orig_decision.get("analysis_time_utc")
    except (json.JSONDecodeError, TypeError):
        pass
    legacy = {
        "symbol": signal["symbol"],
        "decision": signal.get("decision") or "trade_plan_available",
        "signal_grade": signal.get("signal_grade") or "D",
        "confidence": float(signal.get("confidence") or 0),
        "summary": signal.get("ga_reason") or "兼容旧 signal 创建的 GA decision。",
        "market_bias": signal.get("direction") or "neutral",
        "trend_stage": signal.get("trend_stage") or "unknown",
        "has_trade_plan": True,
        "analysis_time_utc": _legacy_analysis_time,
        "trade_plan": trade_plan,
        "risk_check": risk,
        "evidence": [],
        "counter_evidence": [],
        "risk_notes": _json_list(signal.get("risk_notes")),
    }
    actions = build_feishu_actions(legacy, risk)
    analysis_time = utc_ms()
    ga_decision = controller_decision_from_legacy(
        legacy=legacy,
        decision_type="legacy_signal_compat",
        analysis_time=analysis_time,
        skill_result_refs={},
        feishu_actions=actions,
        snapshot_id=signal.get("market_snapshot_id"),
        analysis_state_id=None,
    )
    ga_decision_id = repo.create_ga_decision(ga_decision)
    legacy["ga_decision_id"] = ga_decision_id
    # 07-16 cutover: ``?`` -> ``%s`` (psycopg3); the JSONB ``ga_decision_json``
    # accepts a JSON string via the ``%s`` param (PG auto-casts str->jsonb). The
    # bare ``conn.commit()`` was replaced by ``conn.transaction()`` so the write
    # is a self-contained BEGIN/COMMIT on the pooled (autocommit=False) conn and
    # cannot mis-scope a caller's outer transaction.
    with repo.conn.transaction():
        repo.conn.execute(
            "UPDATE signals SET ga_decision_id=%s, ga_decision_json=%s WHERE id=%s",
            (ga_decision_id, json.dumps(legacy, ensure_ascii=False), int(signal["id"])),
        )
    return int(ga_decision_id)


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except Exception:
        return [raw]


def _check_account_risk(repo: CryptoGuardRepository, symbol: str, side: str) -> dict[str, Any]:
    """Check account-level risk guard for paper order creation."""
    from plugins.crypto_guard.risk.account_risk_guard import AccountRiskGuard

    guard = AccountRiskGuard(repo)
    return guard.check(symbol=symbol, side=side)


def _is_unhealthy_pullback_bar(market: dict[str, Any], side: str, wick_ratio: float = 2.0) -> bool:
    """BTC#9 fix: 判断当前 K 线是否为「阴线插针」式不健康回踩。

    判定条件（同时满足）：
    1. 实体反向：LONG 时 close < open（阴线）；SHORT 时 close > open（阳线）
    2. 影线过长：(high - low) > wick_ratio * abs(close - open)
       即整根 K 线长度远超实体，说明上下影插针、方向未确认。

    十字星（close == open）不视为不健康：实体为零代表方向未定，但非反向，
    交由后续 GA 复核与触发条件把关。

    Args:
        market: 含 open/high/low/close 的 dict
        side: "LONG" 或 "SHORT"
        wick_ratio: 影线/实体倍数阈值，默认 2.0
    """
    try:
        open_p = float(market.get("open") or 0)
        high = float(market.get("high") or 0)
        low = float(market.get("low") or 0)
        close = float(market.get("close") or 0)
    except (TypeError, ValueError):
        return False
    if open_p <= 0 or high <= 0 or low <= 0 or close <= 0:
        return False
    body = abs(close - open_p)
    if body <= 0:
        # 十字星：实体为零，方向未定但非反向，不视为不健康
        return False
    full_range = high - low
    side_u = str(side or "").upper()
    body_adverse = (side_u == "LONG" and close < open_p) or (side_u == "SHORT" and close > open_p)
    return body_adverse and full_range > wick_ratio * body


def _validate_limit_fill_candle(market: dict, order: dict, prev_close: float = None) -> tuple[bool, str]:
    """Validate a closed candle for limit order fill. Returns (pass, reason).

    BTC#9 Phase C Section 5: entry reclaim is mandatory. A purely
    bullish/bearish candle that does NOT close back through entry is
    insufficient — price has not reclaimed the entry zone.

    LONG limit fill requires ALL of:
    - low <= entry <= high (candle traded through entry)
    - close >= entry (reclaims entry zone) OR structured reclaim event
      present in market["reclaim_event"]
    - NOT (close < open AND prev_close and close < prev_close) (adverse
      momentum)
    - candle is closed (close_time < now — enforced by caller)

    SHORT limit fill requires ALL of:
    - low <= entry <= high (candle traded through entry)
    - close <= entry (reclaims entry zone from above) OR structured
      reclaim event present in market["reclaim_event"]
    - NOT (close > open AND prev_close and close > prev_close) (adverse
      momentum)

    Missing fields / unparseable -> fail-closed.
    """
    try:
        open_p = float(market.get("open") or 0)
        close = float(market.get("close") or 0)
    except (TypeError, ValueError):
        return False, "candle_failed_entry_zone_reclaim"

    try:
        entry = float(order.get("entry_price") or 0)
    except (TypeError, ValueError):
        return False, "candle_failed_entry_zone_reclaim"

    if open_p <= 0 or close <= 0 or entry <= 0:
        return False, "candle_failed_entry_zone_reclaim"

    # Require high/low for entry-zone touch verification
    try:
        high = float(market.get("high") or 0)
        low = float(market.get("low") or 0)
    except (TypeError, ValueError):
        return False, "candle_failed_entry_zone_reclaim"
    if high <= 0 or low <= 0:
        return False, "candle_failed_entry_zone_reclaim"

    side = str(order.get("side") or "").upper()

    # Structured reclaim event (optional): caller may attach a real
    # module_analysis_results / snapshot event proving reclaim.
    reclaim_event = market.get("reclaim_event")

    if side == "LONG":
        # Gate 1: candle must have traded through entry (low <= entry <= high)
        if not (low <= entry <= high):
            return False, "candle_failed_entry_zone_reclaim"

        # Gate 2: close must reclaim above entry, OR a structured reclaim
        # event must be present. Pure bullishness is NOT a substitute.
        reclaimed = close >= entry
        if not reclaimed and not reclaim_event:
            return False, "candle_failed_entry_zone_reclaim"

        # Gate 3: adverse momentum — bearish candle closing below prev close
        if close < open_p and prev_close is not None and close < prev_close:
            return False, "adverse_momentum_candle"

        return True, "ok"

    elif side == "SHORT":
        # Gate 1: candle must have traded through entry (low <= entry <= high)
        if not (low <= entry <= high):
            return False, "candle_failed_entry_zone_reclaim"

        # Gate 2: close must reclaim below entry, OR structured reclaim event
        reclaimed = close <= entry
        if not reclaimed and not reclaim_event:
            return False, "candle_failed_entry_zone_reclaim"

        # Gate 3: adverse momentum — bullish candle closing above prev close
        if close > open_p and prev_close is not None and close > prev_close:
            return False, "adverse_momentum_candle"

        return True, "ok"

    return False, "candle_failed_entry_zone_reclaim"


def _close_holds_entry_zone(market: dict[str, Any], order: dict[str, Any], max_penetration_r: float = 0.5) -> bool:
    """DEPRECATED: Replaced by _validate_limit_fill_candle.

    Kept for backward compatibility only. New code should use _validate_limit_fill_candle.
    """
    from plugins.crypto_guard.paper.paper_broker import _validate_limit_fill_candle as _v
    return _v(market, order)[0]


def _revalidate_pending_before_fill(repo: CryptoGuardRepository, order: dict[str, Any], market: dict[str, Any], *, event_time: int | None = None) -> dict[str, Any]:
    """BTC#9 fix: fill 前复核最新 GA + K 线健康。

    返回 dict：
    - {"proceed": True} 继续成交
    - {"proceed": False, "skip_reason": "ga_conflict_cancelled"} 取消订单
    - {"proceed": False, "skip_reason": "unhealthy_kline"} 保持 pending
    - {"proceed": False, "skip_reason": "close_penetrated_entry_zone"} 保持 pending
    - {"proceed": False, "skip_reason": "missing_event_time"} 缺少事件时间（fail-closed）

    BTC#9 fix: event_time (candle.close_time) is used as the upper bound
    for GA recheck. For limit orders, missing event_time is fail-closed.

    开关关闭时直接 proceed。
    """
    from plugins.crypto_guard.config.loader import load_config
    cfg = load_config().trading_mode
    rev_cfg = cfg.get("pending_order_revalidation", {})
    if not rev_cfg.get("enabled", True):
        return {"proceed": True}

    side = str(order.get("side") or "").upper()
    symbol = order.get("symbol") or ""

    # BTC#9 fix: limit orders require event_time for GA recheck upper bound
    order_type = str(order.get("order_type") or "").lower()
    if order_type == "limit" and event_time is None:
        return {"proceed": False, "skip_reason": "missing_event_time"}

    # 1. 复核最新 GA：方向冲突则取消
    # Section 五: GA recheck must be fail-closed — exceptions return ga_recheck_unavailable
    # Section 六 (C2): write idempotent audit log before returning ga_recheck_unavailable
    def _log_ga_recheck_unavailable(detail: str, latest_ga: dict | None = None) -> None:
        """C2: idempotent audit log for ga_recheck_unavailable events."""
        try:
            dedupe_key = f"ga_recheck_unavailable:{order['id']}:{event_time or 0}"
            existing = repo.conn.execute(
                "SELECT id FROM paper_trade_logs WHERE event_json ->> 'dedupe_key' = %s LIMIT 1",
                (dedupe_key,),
            ).fetchone()
            if existing:
                return
            latest_ga_id = latest_ga.get("id") if latest_ga else None
            latest_bias = str(latest_ga.get("market_bias") or "").lower() if latest_ga else None
            latest_grade = str(latest_ga.get("signal_grade") or "").upper() if latest_ga else None
            repo.log_paper_trade_event(
                position_id=None,
                event_type="pending_order_ga_recheck_unavailable",
                symbol=symbol,
                side=side,
                price=0.0,
                quantity=order.get("quantity"),
                pnl=0.0,
                pnl_pct=0.0,
                reason=f"GA recheck unavailable: {detail}",
                event={
                    "order_id": order["id"],
                    "order_side": side,
                    "latest_ga_decision_id": latest_ga_id,
                    "latest_bias": latest_bias,
                    "latest_grade": latest_grade,
                    "event_time": event_time,
                    "detail": detail,
                    "dedupe_key": dedupe_key,
                },
            )
        except Exception:
            # Audit log failure must not mask the original ga_recheck_unavailable
            pass

    try:
        from plugins.crypto_guard.paper.pending_revalidator import _latest_ga_decision
        latest_ga = _latest_ga_decision(repo, symbol, max_analysis_time=event_time)
    except Exception:
        _log_ga_recheck_unavailable("exception during _latest_ga_decision")
        return {"proceed": False, "skip_reason": "ga_recheck_unavailable"}
    if latest_ga is None:
        _log_ga_recheck_unavailable("no GA decision found", latest_ga=None)
        return {"proceed": False, "skip_reason": "ga_recheck_unavailable"}
    if latest_ga:
        # BTC#9 P1-1 fix: time-pinned GA recheck — only cancel if latest GA is NEWER than order's baseline.
        # R3-D: baseline = order's GA analysis_time if ga_decision_id exists, else order.created_at (ms).
        # R3-D: all SQL reads must be inside exception boundary; baseline read failure
        # returns ga_recheck_baseline_unavailable (distinct from ga_recheck_unavailable).
        order_ga_id = order.get("ga_decision_id")
        baseline_time: int | None = None
        try:
            if order_ga_id:
                order_ga_row = repo.conn.execute(
                    "SELECT analysis_time FROM ga_decisions WHERE id=%s", (int(order_ga_id),)
                ).fetchone()
                if order_ga_row and order_ga_row["analysis_time"]:
                    baseline_time = int(order_ga_row["analysis_time"])
            else:
                # R3-D: no ga_decision_id — use order.created_at as baseline
                raw_created = order.get("created_at")
                if raw_created:
                    text = str(raw_created).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(text)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    baseline_time = int(dt.timestamp() * 1000)
        except Exception:
            _log_ga_recheck_unavailable("exception during baseline time resolution", latest_ga)
            return {"proceed": False, "skip_reason": "ga_recheck_baseline_unavailable"}

        if baseline_time is None:
            _log_ga_recheck_unavailable("baseline time unresolvable (no ga_decision_id and no created_at)", latest_ga)
            return {"proceed": False, "skip_reason": "ga_recheck_baseline_unavailable"}

        # Also fetch latest GA's analysis_time (not returned by _latest_ga_decision)
        # R3-D: this read must also be inside the exception boundary
        latest_ga_analysis_time = None
        try:
            if latest_ga.get("id"):
                latest_row = repo.conn.execute(
                    "SELECT analysis_time FROM ga_decisions WHERE id=%s", (int(latest_ga["id"]),)
                ).fetchone()
                if latest_row and latest_row["analysis_time"]:
                    latest_ga_analysis_time = int(latest_row["analysis_time"])
        except Exception:
            _log_ga_recheck_unavailable("exception during latest GA analysis_time read", latest_ga)
            return {"proceed": False, "skip_reason": "ga_recheck_unavailable"}

        if latest_ga_analysis_time is not None and latest_ga_analysis_time <= baseline_time:
            # Latest GA predates or equals the order's baseline — don't cancel
            pass
        else:
            # Only proceed with cancel if latest GA is definitely newer than baseline
            # and within the event_time upper bound (already enforced by _latest_ga_decision).
            bias = str(latest_ga.get("market_bias") or "neutral").lower()
            grade = str(latest_ga.get("signal_grade") or "D").upper()
            conflict = (
                (side == "LONG" and bias == "bearish" and grade in {"S", "A", "B"})
                or (side == "SHORT" and bias == "bullish" and grade in {"S", "A", "B"})
            )
            if conflict:
                # R3-D: historical conflict cancellation must use candle event_time for cancelled_at,
                # not wall-clock utc_iso(). For live mode (event_time None), fall back to utc_iso().
                if event_time is not None and int(event_time) > 0:
                    from plugins.crypto_guard.utils import iso_utc_from_ms
                    cancel_ts_iso = iso_utc_from_ms(int(event_time))
                else:
                    cancel_ts_iso = utc_iso()
                reason = f"fill 前复核：方向冲突 {side} vs GA#{latest_ga['id']} bias={bias} grade={grade}"
                # Section 六: SAVEPOINT/CAS - update first, then audit log on success.
                # psycopg3: ``with conn.transaction():`` opens a SAVEPOINT when nested
                # in an outer transaction (matches the prior SQLite SAVEPOINT scope) and
                # rolls it back automatically on exception, so the race-lost UPDATE
                # and the audit-log write both revert without disturbing the caller's
                # outer transaction.
                conflict_dedupe_key = f"conflict_cancel:{order['id']}:{latest_ga['id']}"
                try:
                    with repo.conn.transaction():
                        cur = repo.conn.execute(
                            "UPDATE paper_orders SET status='revalidator_cancelled', cancelled_at=%s, cancel_reason=%s, invalidated_by_ga_decision_id=%s WHERE id=%s AND status IN ('pending', 'needs_recheck')",
                            (cancel_ts_iso, reason, latest_ga["id"], order["id"]),
                        )
                        if cur.rowcount == 0:
                            # Race lost - another worker already changed the order.
                            # Raise to roll back this savepoint (reverts the no-op
                            # UPDATE) without touching the caller's outer transaction.
                            raise _ConflictCancelRaceLost()
                        # C3: Audit log - position_id=None for pending orders (no trade yet).
                        # event_json enriched with order_id / original_ga_decision_id /
                        # invalidated_by_ga_decision_id / order_side / latest_bias /
                        # latest_grade / event_time / dedupe_key for full traceability.
                        repo.log_paper_trade_event(
                            position_id=None,
                            event_type="pending_order_invalidated_by_new_ga_decision",
                            symbol=symbol,
                            side=side,
                            price=0.0,
                            quantity=order.get("quantity"),
                            pnl=0.0,
                            pnl_pct=0.0,
                            reason=reason,
                            event={
                                "order_id": order["id"],
                                "original_ga_decision_id": order.get("ga_decision_id"),
                                "invalidated_by_ga_decision_id": latest_ga["id"],
                                "order_side": side,
                                "latest_bias": bias,
                                "latest_grade": grade,
                                "event_time": event_time,
                                "reason": reason,
                                "dedupe_key": conflict_dedupe_key,
                            },
                            event_time=event_time if (event_time is not None and int(event_time) > 0) else None,
                        )
                except _ConflictCancelRaceLost:
                    return {"proceed": False, "skip_reason": "cancel_race_lost", "ga_decision_id": latest_ga["id"]}
                except Exception:
                    _log_ga_recheck_unavailable("exception during conflict cancel", latest_ga)
                    return {"proceed": False, "skip_reason": "ga_recheck_unavailable"}
                return {"proceed": False, "skip_reason": "ga_conflict_cancelled", "ga_decision_id": latest_ga["id"]}

    # 2. limit 订单 K 线健康检查
    order_type = str(order.get("order_type") or "").lower()
    if order_type == "limit":
        if rev_cfg.get("require_healthy_kline_for_limit", True):
            wick_ratio = float(rev_cfg.get("unhealthy_kline_wick_ratio", 2.0))
            if _is_unhealthy_pullback_bar(market, side, wick_ratio=wick_ratio):
                return {"proceed": False, "skip_reason": "unhealthy_kline"}

        # BTC#9 P0-2: structured candle confirmation for limit orders
        if rev_cfg.get("require_structured_candle_confirmation", True):
            # Use prev_close from market if available (for per-candle processing),
            # otherwise default to open price as best approximation
            prev_close = float(market.get("prev_close") or market.get("open") or 0)
            if prev_close <= 0:
                prev_close = None
            pass_candle, candle_reason = _validate_limit_fill_candle(market, order, prev_close=prev_close)
            if not pass_candle:
                return {"proceed": False, "skip_reason": candle_reason}

    return {"proceed": True}


def _should_check_market_data_health_for_fill() -> bool:
    """P0-4: read the broker config flag for the market-data-health gate.

    Default is True (fail-closed). Returns False only when the config
    explicitly sets ``require_market_data_health_for_fill: false``.

    P0-4 R2: the hidden env-var bypass has been REMOVED from production
    code. It was a silent escape hatch that could disable the safety gate
    in production. Tests that need to bypass the gate must use
    ``unittest.mock.patch`` to monkey-patch this function, or seed the DB
    with full contiguous data so the gate passes naturally.
    """
    try:
        from plugins.crypto_guard.config.loader import load_config
        rev_cfg = load_config().trading_mode.get("pending_order_revalidation") or {}
        return bool(rev_cfg.get("require_market_data_health_for_fill", True))
    except Exception:
        # Config load failure — fail-closed (check health).
        return True


def _check_market_data_health_for_fill(
    repo: CryptoGuardRepository,
    order: dict[str, Any],
    *,
    event_time: int | None = None,
) -> dict[str, Any] | None:
    """P0-4: assess market data health before filling a pending order.

    Returns None when the data is healthy (fill should proceed). Returns a
    skip dict ``{"ok": True, "filled": False, "skip_reason": ...}`` when the
    data is not ready — the order stays pending.

    P0-2 R2: iterate over ALL required intervals from
    ``market_data.required_samples`` (1d/4h/1h/15m/5m), not just
    ``order["primary_interval"]``. If ANY interval is not ready, block the
    fill with ``skip_reason=market_data_not_ready`` and include
    ``health_reason`` listing which interval(s) failed.

    The analysis_time defaults to ``event_time`` when provided (the candle
    close_time that triggered the fill check), else ``utc_ms()``.
    """
    try:
        from plugins.crypto_guard.data.market_data_health import assess_health
        from plugins.crypto_guard.config.loader import load_config
        from plugins.crypto_guard.utils import utc_ms

        symbol = order.get("symbol") or ""
        if not symbol:
            return None  # cannot check without a symbol — let other gates handle

        cfg = load_config()
        required_samples = (cfg.market_data.get("required_samples") or {})
        if not required_samples:
            # Config missing — fall back to primary_interval only.
            required_samples = {order.get("primary_interval") or "15m": 200}

        analysis_time = int(event_time) if (event_time is not None and int(event_time) > 0) else utc_ms()

        # P0-2: check ALL required intervals, not just primary_interval.
        failed_intervals: list[str] = []
        for interval, required_count in required_samples.items():
            required_count = int(required_count)
            health = assess_health(
                repo, symbol, interval,
                analysis_time_utc=analysis_time, required_count=required_count,
            )
            if not health.get("ready"):
                reason = health.get("reason", "unknown")
                failed_intervals.append(f"{interval}: {reason}")

        if failed_intervals:
            return {
                "ok": True,
                "filled": False,
                "skip_reason": "market_data_not_ready",
                "health_reason": ", ".join(failed_intervals),
            }
        return None
    except Exception as exc:
        # Fail-closed on unexpected errors — don't fill if we can't verify
        # data health. Log the exception for debugging.
        return {
            "ok": True,
            "filled": False,
            "skip_reason": "market_data_health_check_error",
            "health_reason": str(exc)[:200],
        }


def fill_order_if_triggered(repo: CryptoGuardRepository, order: dict[str, Any], price: float | dict[str, Any], *, event_time: int | None = None) -> dict[str, Any]:
    market = price if isinstance(price, dict) else market_from_price(order["symbol"], float(price))
    last_price = float(market["close"])
    high = float(market["high"])
    low = float(market["low"])
    # 08-04 contract A5: reference candle open for the fill push's slippage
    # field (price-level slippage, not the market-order slippage pct).
    open_price_ref = float(market.get("open", last_price))
    order_type = order["order_type"]
    side = order["side"]
    should_fill = False
    entry_price = order.get("entry_price") or last_price
    fill_method = order.get("fill_method")
    # Calculate position size based on risk
    stop = float(order.get("stop_loss") or 0)
    risk_pct = float(order.get("risk_percent") or 0.5)
    # BTC#9 fix: convert event_time to ISO once for all downstream timestamps
    # event_time is candle.close_time in ms; if missing, fall back to utc_iso()
    if event_time is not None and int(event_time) > 0:
        from plugins.crypto_guard.utils import iso_utc_from_ms
        fill_ts_iso = iso_utc_from_ms(int(event_time))
        fill_event_time = int(event_time)
        fill_allow_wall = False
    else:
        fill_ts_iso = utc_iso()
        fill_event_time = None
        fill_allow_wall = True  # live mode: explicit wall-clock fallback
    if order_type == "market":
        should_fill = True
        open_price = float(market.get("open", last_price))
        slippage = float(market.get("market_slippage_pct", DEFAULT_SLIPPAGE_PCT))
        entry_price = compute_fill_price(open_price, side, slippage_pct=slippage)
        fill_method = "next_candle_open_with_slippage"
    elif order_type == "limit":
        should_fill = bool(entry_price is not None and low <= float(entry_price) <= high)
        fill_method = "limit_range_touch" if should_fill else fill_method
    elif order_type == "trigger":
        trigger = order.get("trigger_price")
        should_fill = bool(trigger is not None and (high >= trigger if side == "LONG" else low <= trigger))
        entry_price = trigger or last_price
        fill_method = "trigger_touch" if should_fill else fill_method
    if not should_fill:
        return {"ok": True, "filled": False}
    # BTC#9 fix: fill 前复核最新 GA + K 线健康
    rev = _revalidate_pending_before_fill(repo, order, market, event_time=event_time)
    if not rev.get("proceed"):
        return {"ok": True, "filled": False, "skip_reason": rev.get("skip_reason")}
    # P0-4: third fail-closed gate — assess_health before create_paper_trade.
    # The generation gate (P0-3) and risk_engine second gate already enforce
    # this at decision time, but the broker must independently verify at fill
    # time because (a) data may have degraded between decision and fill, and
    # (b) AC44 was previously a test-name lie — the body only called
    # risk_engine, never the broker. Now the broker actually checks.
    # Config flag ``require_market_data_health_for_fill`` (default true) lets
    # isolated unit tests bypass this gate when they need to.
    if _should_check_market_data_health_for_fill():
        health_skip = _check_market_data_health_for_fill(repo, order, event_time=event_time)
        if health_skip is not None:
            return health_skip
    # Size AFTER fill price is determined (slippage applied for market orders)
    sizing = compute_position_size(float(entry_price), stop, risk_percent=risk_pct)
    if sizing is not None:
        order["quantity"] = sizing[0]
    # Guard: don't create duplicate trades for the same order
    existing_trade = repo.get_open_trade_for_order(order["id"])
    if existing_trade:
        return {"ok": True, "filled": False, "existing_trade_id": existing_trade["id"],
                "reason": "order already has an open trade"}
    trade_id = repo.create_paper_trade(order, float(entry_price), fill_method=fill_method, event_time=fill_event_time, allow_wall_clock=fill_allow_wall)
    repo.update_paper_order_status(order["id"], "open", filled_at=fill_ts_iso)
    repo.enqueue_job(
        "paper_event_alert",
        3,
        "paper_worker",
        f"system:paper:filled:{order['id']}",
        {
            "event_type": "paper_order_filled",
            "symbol": order["symbol"],
            "order_id": order["id"],
            "trade_id": trade_id,
            "entry_price": float(entry_price),
            "fill_method": fill_method,
            "side": order.get("side"),
            "stop_loss": order.get("stop_loss"),
            "take_profits": _safe_json(order.get("take_profit_json"), []) if order.get("take_profit_json") else [],
            "filled_at": fill_ts_iso,
            "event_time": fill_event_time,
            "quantity": order.get("quantity"),
            "order_type": order.get("order_type"),
            # 08-04 contract A5: the fill push must carry the source decision
            # id, the price-level slippage (signed cost; positive = worse) and
            # the resulting position so the push is traceable and complete.
            "source_decision_id": order.get("ga_decision_id"),
            "slippage": round(
                (float(entry_price) - open_price_ref)
                * (1 if str(side).upper() == "LONG" else -1),
                8,
            ),
            "position": {
                "side": side,
                "quantity": order.get("quantity"),
                "avg_price": float(entry_price),
            },
        },
    )
    return {"ok": True, "filled": True, "trade_id": trade_id, "entry_price": float(entry_price), "fill_method": fill_method}


def close_trade_if_needed(repo: CryptoGuardRepository, order: dict[str, Any], trade: dict[str, Any], price: float | dict[str, Any], *, event_time: int | None = None) -> dict[str, Any]:
    market = price if isinstance(price, dict) else market_from_price(order["symbol"], float(price))
    path_metrics = update_trade_path_metrics(trade, market)
    repo.update_paper_trade_quality(
        trade["id"],
        mfe=path_metrics["max_favorable_excursion"],
        mae=path_metrics["max_adverse_excursion"],
        stop_take_path=path_metrics["stop_take_path"],
    )
    trade = dict(trade)
    trade["max_favorable_excursion"] = path_metrics["max_favorable_excursion"]
    trade["max_adverse_excursion"] = path_metrics["max_adverse_excursion"]
    trade["stop_take_path_json"] = json.dumps(path_metrics["stop_take_path"], ensure_ascii=False)

    # BTC#9 Phase B: evaluate_exit handles same-candle SL+TP ambiguity with
    # conservative SL priority (see execution_quality.py lines 78-81). When both
    # SL and TP are hit on the same candle, the trade closes at stop_loss with
    # {"ambiguous_intrabar": True} recorded in the path.
    exit_result = evaluate_exit(order, trade, market)
    if not exit_result["should_close"]:
        return {"ok": True, "closed": False, "mfe": path_metrics["max_favorable_excursion"], "mae": path_metrics["max_adverse_excursion"]}

    close_reason = str(exit_result["reason"])
    exit_price = float(exit_result["exit_price"])
    quality = close_quality_metrics(order, trade, market, exit_price=exit_price, close_reason=close_reason)
    stop_take_path = quality["stop_take_path"]
    if exit_result.get("hit"):
        stop_take_path.append({"event": "exit_hit", "reason": close_reason, "exit_price": exit_price, "details": exit_result["hit"]})
    # R3-B: determine close timestamp from event_time
    if event_time is not None and int(event_time) > 0:
        from plugins.crypto_guard.utils import iso_utc_from_ms
        close_ts_iso = iso_utc_from_ms(int(event_time))
        close_event_time = int(event_time)
        close_allow_wall = False
    else:
        close_ts_iso = utc_iso()
        close_event_time = None
        close_allow_wall = True
    closed = repo.close_paper_trade(
        trade["id"],
        exit_price=exit_price,
        close_reason=close_reason,
        pnl=quality["pnl"],
        pnl_percent=quality["pnl_percent"],
        pnl_r=quality["pnl_r"],
        mfe=quality["max_favorable_excursion"],
        mae=quality["max_adverse_excursion"],
        entry_efficiency=quality["entry_efficiency"],
        exit_efficiency=quality["exit_efficiency"],
        signal_decay_score=quality["signal_decay_score"],
        stop_take_path=stop_take_path,
        event_time=close_event_time,
        allow_wall_clock=close_allow_wall,
    )
    if not closed:
        return {"ok": True, "closed": False, "skip_reason": "concurrent_close"}
    repo.update_paper_order_status(order["id"], "closed", closed_at=close_ts_iso)
    # Backfill real pnl_r to active strategy_evaluations for this trade.
    # Shadow evaluations get PnL exclusively from their independent virtual_trade lifecycle.
    repo.backfill_active_evaluation_pnl_r(trade, quality["pnl_r"])
    repo.upsert_paper_position_from_trade(
        account_id=int(repo.ensure_paper_account()["id"]),
        trade={**trade, "current_price": exit_price},
        status="closed",
        current_price=exit_price,
        unrealized_pnl=0.0,
        unrealized_pnl_pct=0.0,
        event_time=close_event_time,
        allow_wall_clock=close_allow_wall,
    )
    repo.log_paper_trade_event(
        position_id=int(trade["id"]),
        event_type="close_position",
        symbol=order["symbol"],
        side=order["side"],
        price=exit_price,
        quantity=trade.get("quantity"),
        pnl=quality["pnl"],
        pnl_pct=quality["pnl_percent"],
        reason=close_reason,
        event={"order_id": order["id"], "trade_id": trade["id"], "pnl_r": quality["pnl_r"]},
        event_time=close_event_time,
    )
    repo.enqueue_job("trade_review", 4, "paper_worker", f"system:review:{trade['id']}", {"trade_id": trade["id"]})
    event_type = "take_profit_hit" if close_reason == "take_profit" else "stop_loss_hit" if close_reason == "stop_loss" else "close_position"
    repo.enqueue_job(
        "paper_event_alert",
        3,
        "paper_worker",
        f"system:paper:closed:{trade['id']}",
        {
            "event_type": event_type,
            "symbol": order["symbol"],
            "order_id": order["id"],
            "trade_id": trade["id"],
            "exit_price": exit_price,
            "close_reason": close_reason,
            "pnl_r": quality["pnl_r"],
            "side": order.get("side"),
            "entry_price": order.get("entry_price"),
            "stop_loss": order.get("stop_loss"),
            "take_profits": _safe_json(order.get("take_profit_json"), []) if order.get("take_profit_json") else [],
            "filled_at": order.get("filled_at"),
            "closed_at": close_ts_iso,
            "event_time": close_ts_iso,
            "quantity": trade.get("quantity"),
            "order_type": order.get("order_type"),
        },
    )
    return {
        "ok": True,
        "closed": True,
        "trade_id": trade["id"],
        "close_reason": close_reason,
        "exit_price": exit_price,
        "pnl_r": quality["pnl_r"],
        "mfe": quality["max_favorable_excursion"],
        "mae": quality["max_adverse_excursion"],
        "entry_efficiency": quality["entry_efficiency"],
        "exit_efficiency": quality["exit_efficiency"],
        "signal_decay_score": quality["signal_decay_score"],
    }


def _extract_entry_quality(trade_plan: dict[str, Any]) -> float | None:
    """Extract entry quality score from trade_plan if available."""
    # Check for entry_confirmation_quality field
    quality = trade_plan.get("entry_confirmation_quality")
    if quality is not None:
        try:
            return float(quality)
        except (ValueError, TypeError):
            pass

    # Check for entry_quality in metrics
    metrics = trade_plan.get("metrics") or {}
    quality = metrics.get("entry_quality")
    if quality is not None:
        try:
            return float(quality)
        except (ValueError, TypeError):
            pass

    return None


def _create_opportunity_watch_from_gate(
    repo: CryptoGuardRepository,
    symbol: str,
    side: str,
    ga_decision_id: int | None,
    gate_result: dict[str, Any],
) -> int | None:
    """Idempotent opportunity watch creation when gate downgrades to watch.

    Uses dedupe_key + UPSERT (ON CONFLICT) to prevent duplicate active watches
    on retry/re-entry. No manual transaction needed.

    The ON CONFLICT predicate matches the P0-2 partial unique index
    ``idx_opportunity_watches_dedupe`` (``WHERE dedupe_key IS NOT NULL AND
    status = 'active'``): a conflict only when an ACTIVE watch holds the key,
    so a terminal watch (triggered/invalidated/expired) releases it and a
    fresh active watch can be re-created with the same key.

    Stores a structured account_feedback_recheck watch condition so the
    opportunity watcher can evaluate it deterministically.

    Returns watch_id or None on failure.
    """
    from datetime import datetime as _datetime

    if not ga_decision_id:
        return None

    dedupe_key = f"account_feedback_gate:{ga_decision_id}"

    # Store structured account_feedback_recheck condition for deterministic watcher evaluation
    watch_condition = json.dumps({
        "type": "account_feedback_recheck",
        "source": "account_feedback_gate",
        "symbol": symbol,
        "side": side,
        "original_confidence": gate_result.get("actual", {}).get("confidence"),
        "original_entry_quality": gate_result.get("actual", {}).get("entry_quality"),
        "min_confidence": gate_result.get("required", {}).get("min_confidence"),
        "min_entry_quality": gate_result.get("required", {}).get("min_entry_quality"),
        "gate_decision": gate_result.get("would_decide", ""),
        "gate_reason": gate_result.get("reason", ""),
        "created_at": _datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False)

    watch_reason = f"account_feedback_gate: {gate_result.get('reason', '')}"

    # 24-hour TTL for gate-downgraded watches
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat().replace("+00:00", "Z")

    try:
        repo.conn.execute(
            """
            INSERT INTO opportunity_watches
            (symbol, direction, watch_reason, watch_condition_json, status, ga_decision_id, expires_at, dedupe_key)
            VALUES (%s, %s, %s, %s, 'active', %s, %s, %s)
            ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL AND status = 'active' DO UPDATE SET
                watch_condition_json = excluded.watch_condition_json,
                expires_at = excluded.expires_at,
                watch_reason = excluded.watch_reason,
                updated_at = CURRENT_TIMESTAMP
            """,
            (symbol, side, watch_reason, watch_condition, int(ga_decision_id), expires_at, dedupe_key),
        )
        # No commit here — caller owns the transaction
        # Return the ID of the upserted row
        row = repo.conn.execute(
            "SELECT id FROM opportunity_watches WHERE dedupe_key = %s", (dedupe_key,)
        ).fetchone()
        return int(row["id"]) if row else None
    except Exception:
        import logging
        logging.getLogger("crypto_guard.paper_broker").warning(
            "Failed to create/update opportunity watch from gate: ga_decision_id=%s", ga_decision_id,
            exc_info=True,
        )
        return None


def _save_gate_result_to_ga_decision(
    repo: CryptoGuardRepository,
    ga_decision_id: int,
    gate_result: dict[str, Any],
) -> None:
    """Save account feedback gate result to GA decision."""
    try:
        repo.conn.execute(
            "UPDATE ga_decisions SET account_feedback_gate_json = %s WHERE id = %s",
            (json.dumps(gate_result, ensure_ascii=False), ga_decision_id),
        )
    except Exception as exc:
        import logging
        logging.getLogger("crypto_guard.account_feedback_gate").warning(
            "Failed to save gate result to GA decision %d: %s", ga_decision_id, exc
        )


def _apply_regime_gate_if_enabled(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    side: str,
    signal_grade: str,
    confidence: float,
    analysis_time_utc: int,
    order_type: str = "",
    time_source: str = "",
) -> dict[str, Any] | None:
    """Apply market regime gate if enabled in config."""
    from plugins.crypto_guard.config.loader import load_config
    cfg = load_config().trading_mode
    regime_cfg = cfg.get("market_regime", {})
    if not regime_cfg.get("enabled", True):
        return None
    from plugins.crypto_guard.risk.risk_engine import apply_regime_gate
    result = apply_regime_gate(
        repo,
        symbol=symbol,
        side=side,
        signal_grade=signal_grade,
        confidence=confidence,
        analysis_time_utc=analysis_time_utc,
        order_type=order_type,
    )
    # Add time_source to the result for audit
    if result is not None:
        result["time_source"] = time_source or "fallback_now"
    return result


def _save_regime_gate_to_ga_decision(
    repo: CryptoGuardRepository,
    ga_decision_id: int,
    regime_gate: dict[str, Any],
) -> None:
    """Save market regime gate result to GA decision for audit trail."""
    try:
        repo.conn.execute(
            "UPDATE ga_decisions SET market_regime_gate_json = %s WHERE id = %s",
            (json.dumps(regime_gate, ensure_ascii=False), ga_decision_id),
        )
    except Exception as exc:
        import logging
        logging.getLogger("crypto_guard.market_regime").warning(
            "Failed to save regime gate result to GA decision %d: %s", ga_decision_id, exc
        )


def _apply_regime_adjustments(
    trade_plan: dict[str, Any],
    adjustments: dict[str, Any],
) -> dict[str, Any]:
    """Apply regime gate adjustments to trade_plan in controlled mode.

    Returns a modified copy of trade_plan with:
    - risk_percent scaled by risk_multiplier
    - min_rr and allowed_order_types set as audit fields (enforced by caller)
    - confidence and grade updated for downstream audit
    - require_stronger_confirmation raises effective thresholds for min_confidence and min_entry_quality
    """
    plan = dict(trade_plan)

    # Apply risk_multiplier
    risk_mult = float(adjustments.get("risk_multiplier", 1.0))
    if risk_mult != 1.0:
        original_risk = float(plan.get("risk_percent", 0.5))
        plan["risk_percent"] = round(original_risk * risk_mult, 4)
        plan["regime_risk_multiplier_applied"] = risk_mult

    # Apply effective_confidence (for audit only — doesn't change trade_plan)
    effective_conf = adjustments.get("effective_confidence")
    if effective_conf is not None:
        plan["regime_effective_confidence"] = effective_conf

    # Apply effective_grade (for audit only)
    effective_grade = adjustments.get("effective_grade")
    if effective_grade:
        plan["regime_effective_grade"] = effective_grade

    # Store min_rr and allowed_order_types as audit fields
    min_rr = adjustments.get("min_rr")
    if min_rr is not None:
        plan["regime_min_rr"] = min_rr
    allowed_types = adjustments.get("allowed_order_types")
    if allowed_types is not None:
        plan["regime_allowed_order_types"] = allowed_types

    # Require stronger confirmation: raise effective thresholds
    if adjustments.get("require_stronger_confirmation"):
        from plugins.crypto_guard.config.loader import load_config
        cfg = load_config().trading_mode
        market_regime_cfg = cfg.get("market_regime", {})
        stronger_cfg = market_regime_cfg.get("require_stronger_confirmation")
        if not isinstance(stronger_cfg, dict):
            stronger_cfg = (cfg.get("account_feedback_rules", {}).get("actions", {}).get("require_stronger_confirmation", {}))
        config_min_conf = float(stronger_cfg.get("min_confidence", 0.80))
        config_min_quality = float(stronger_cfg.get("min_entry_quality", 0.70))

        # Raise effective min confidence to at least config_min_conf
        current_min_conf = float(trade_plan.get("min_confidence") or 0)
        plan["regime_require_stronger_confirmation"] = True
        plan["regime_effective_min_confidence"] = max(current_min_conf, config_min_conf)

        # Raise effective min entry quality to at least config_min_quality
        current_min_quality = float(trade_plan.get("min_entry_quality") or 0)
        plan["regime_effective_min_entry_quality"] = max(current_min_quality, config_min_quality)

    return plan


def _signal_decision_confidence(decision: dict[str, Any], signal: dict[str, Any]) -> float:
    """Read confidence from GA decision JSON with legacy signal fallback."""
    value = decision.get("confidence")
    if value is None or value == "":
        value = signal.get("confidence")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _get_effective_regime_confidence(adjustments: dict[str, Any]) -> float:
    """Read the authoritative effective confidence after regime adjustments.

    Prefers effective_confidence_after_regime (risk_engine's canonical field),
    falls back to effective_confidence, then 0.
    """
    value = adjustments.get("effective_confidence_after_regime")
    if value is None:
        value = adjustments.get("effective_confidence")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _should_downgrade_to_watch_by_regime(
    trade_plan: dict[str, Any],
    adjustments: dict[str, Any],
) -> str | None:
    """Check if regime adjustments require downgrading to opportunity watch.

    Returns reason string if downgrade needed, None otherwise.
    Checks: allowed_order_types and min_rr.
    """
    # Check allowed_order_types
    allowed_types = adjustments.get("allowed_order_types")
    entry_type = str(trade_plan.get("entry_type") or "").lower()
    if allowed_types is not None and entry_type:
        if not allowed_types or entry_type not in [t.lower() for t in allowed_types]:
            return f"entry_type={entry_type} not in allowed_order_types={allowed_types}"

    # Check min_rr
    min_rr = float(adjustments.get("min_rr", 0))
    if min_rr > 0:
        from plugins.crypto_guard.risk.risk_engine import _risk_reward
        rr = _risk_reward(trade_plan)
        if rr is not None and rr < min_rr:
            return f"RR={rr:.2f} below regime min_rr={min_rr:.1f}"

    return None


def _check_stronger_confirmation(
    trade_plan: dict[str, Any],
    adjustments: dict[str, Any],
    *,
    confidence: float = 0.0,
    entry_quality: float | None = None,
) -> str | None:
    """Check if require_stronger_confirmation thresholds are met.

    Reads raised thresholds from trade_plan (set by _apply_regime_adjustments)
    and compares against actual confidence/entry_quality values passed by caller.

    Returns reason string if thresholds not met, None if OK or not applicable.
    """
    if not adjustments.get("require_stronger_confirmation"):
        return None

    min_conf = trade_plan.get("regime_effective_min_confidence")
    min_quality = trade_plan.get("regime_effective_min_entry_quality")

    failures = []
    if min_conf is not None and confidence < min_conf:
        failures.append(f"confidence={confidence:.2f} < required={min_conf:.2f}")
    if min_quality is not None and (entry_quality is None or entry_quality < min_quality):
        label = f"{entry_quality:.2f}" if entry_quality is not None else "missing"
        failures.append(f"entry_quality={label} < required={min_quality:.2f}")

    if failures:
        return f"require_stronger_confirmation: {'; '.join(failures)}"
    return None


def _resolve_analysis_time(
    repo: CryptoGuardRepository,
    signal: dict[str, Any],
    ga_decision_id: int | None,
) -> tuple[int, str]:
    """Resolve the original analysis_time for a signal to avoid lookahead bias.

    Tries: ga_decision.analysis_time -> signal.created_at -> utc_ms() fallback.
    Returns (analysis_time_ms, time_source) tuple.
    time_source is "original_analysis_time", "signal_created_at", or "fallback_now".
    """
    # Try GA decision first
    if ga_decision_id:
        try:
            row = repo.conn.execute(
                "SELECT analysis_time FROM ga_decisions WHERE id=%s",
                (int(ga_decision_id),),
            ).fetchone()
            if row and row["analysis_time"]:
                return int(row["analysis_time"]), "original_analysis_time"
        except Exception:
            pass

    # Try signal created_at as a better approximation than current time
    if signal:
        created_at = signal.get("created_at")
        if created_at:
            try:
                from datetime import datetime as _dt, timezone as _tz
                text = str(created_at).replace("Z", "+00:00")
                dt = _dt.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                return int(dt.timestamp() * 1000), "signal_created_at"
            except Exception:
                pass

    # Fallback to current time
    return utc_ms(), "fallback_now"
