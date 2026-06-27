from __future__ import annotations

import json
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


def create_paper_order_from_signal(repo: CryptoGuardRepository, signal_id: int) -> dict[str, Any]:
    signal = repo.get_signal(signal_id)
    if not signal:
        return {"ok": False, "error": "signal 不存在", "signal_id": signal_id}
    if not signal.get("trade_plan_json"):
        return {"ok": False, "error": "该 signal 没有完整 trade_plan，不能加入模拟盘", "signal_id": signal_id}
    trade_plan = json.loads(signal["trade_plan_json"])
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
            snapshot = json.loads(row.get("snapshot_json") or "{}")
    decision = json.loads(signal.get("ga_decision_json") or "{}") if signal.get("ga_decision_json") else {"confidence": signal.get("confidence"), "trade_plan": trade_plan, "has_trade_plan": True}
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
            snapshot = json.loads(row.get("snapshot_json") or "{}")
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
    signal_row = repo.conn.execute("SELECT id FROM signals WHERE ga_decision_id=? ORDER BY id DESC LIMIT 1", (int(ga_decision_id),)).fetchone()
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
    legacy = {
        "symbol": signal["symbol"],
        "decision": signal.get("decision") or "trade_plan_available",
        "signal_grade": signal.get("signal_grade") or "D",
        "confidence": float(signal.get("confidence") or 0),
        "summary": signal.get("ga_reason") or "兼容旧 signal 创建的 GA decision。",
        "market_bias": signal.get("direction") or "neutral",
        "trend_stage": signal.get("trend_stage") or "unknown",
        "has_trade_plan": True,
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
    repo.conn.execute(
        "UPDATE signals SET ga_decision_id=?, ga_decision_json=? WHERE id=?",
        (ga_decision_id, json.dumps(legacy, ensure_ascii=False), int(signal["id"])),
    )
    repo.conn.commit()
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


def fill_order_if_triggered(repo: CryptoGuardRepository, order: dict[str, Any], price: float | dict[str, Any]) -> dict[str, Any]:
    market = price if isinstance(price, dict) else market_from_price(order["symbol"], float(price))
    last_price = float(market["close"])
    high = float(market["high"])
    low = float(market["low"])
    order_type = order["order_type"]
    side = order["side"]
    should_fill = False
    entry_price = order.get("entry_price") or last_price
    fill_method = order.get("fill_method")
    # Calculate position size based on risk
    stop = float(order.get("stop_loss") or 0)
    risk_pct = float(order.get("risk_percent") or 0.5)
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
    # Size AFTER fill price is determined (slippage applied for market orders)
    sizing = compute_position_size(float(entry_price), stop, risk_percent=risk_pct)
    if sizing is not None:
        order["quantity"] = sizing[0]
    # Guard: don't create duplicate trades for the same order
    existing_trade = repo.get_open_trade_for_order(order["id"])
    if existing_trade:
        return {"ok": True, "filled": False, "existing_trade_id": existing_trade["id"],
                "reason": "order already has an open trade"}
    trade_id = repo.create_paper_trade(order, float(entry_price), fill_method=fill_method)
    repo.update_paper_order_status(order["id"], "open", filled_at=utc_iso())
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
            "take_profits": json.loads(order.get("take_profit_json") or "[]") if order.get("take_profit_json") else [],
            "filled_at": utc_iso(),
            "quantity": order.get("quantity"),
            "order_type": order.get("order_type"),
        },
    )
    return {"ok": True, "filled": True, "trade_id": trade_id, "entry_price": float(entry_price), "fill_method": fill_method}


def close_trade_if_needed(repo: CryptoGuardRepository, order: dict[str, Any], trade: dict[str, Any], price: float | dict[str, Any]) -> dict[str, Any]:
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

    exit_result = evaluate_exit(order, trade, market)
    if not exit_result["should_close"]:
        return {"ok": True, "closed": False, "mfe": path_metrics["max_favorable_excursion"], "mae": path_metrics["max_adverse_excursion"]}

    close_reason = str(exit_result["reason"])
    exit_price = float(exit_result["exit_price"])
    quality = close_quality_metrics(order, trade, market, exit_price=exit_price, close_reason=close_reason)
    stop_take_path = quality["stop_take_path"]
    if exit_result.get("hit"):
        stop_take_path.append({"event": "exit_hit", "reason": close_reason, "exit_price": exit_price, "details": exit_result["hit"]})
    repo.close_paper_trade(
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
    )
    closed_at = utc_iso()
    repo.update_paper_order_status(order["id"], "closed", closed_at=closed_at)
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
            "take_profits": json.loads(order.get("take_profit_json") or "[]") if order.get("take_profit_json") else [],
            "filled_at": order.get("filled_at"),
            "closed_at": closed_at,
            "event_time": closed_at,
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
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
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
            "SELECT id FROM opportunity_watches WHERE dedupe_key = ?", (dedupe_key,)
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
            "UPDATE ga_decisions SET account_feedback_gate_json = ? WHERE id = ?",
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
            "UPDATE ga_decisions SET market_regime_gate_json = ? WHERE id = ?",
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
                "SELECT analysis_time FROM ga_decisions WHERE id=?",
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
