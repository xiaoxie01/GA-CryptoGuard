from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.review.evolution_engine import build_candidate_patch
from plugins.crypto_guard.review.loss_classifier import classify_trade
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def review_trade(repo: CryptoGuardRepository, trade_id: int) -> dict[str, Any]:
    existing = repo.get_trade_review_by_trade(trade_id)
    if existing:
        return {"ok": True, "review_id": existing["id"], "patch_id": None, "review": _load_review_json(existing), "idempotent": True}
    trade = repo.get_trade(trade_id)
    if not trade:
        return {"ok": False, "error": "trade 不存在", "trade_id": trade_id}
    pnl_r = float(trade.get("pnl_r") or 0)
    result = "win" if pnl_r > 0.05 else "loss" if pnl_r < -0.05 else "breakeven"
    primary = classify_trade(trade)
    patch = build_candidate_patch(trade, primary)
    metrics = _trade_metrics(trade)
    snapshot_context = _snapshot_context(repo, trade)
    evidence_checklist = _evidence_checklist(trade, snapshot_context)
    fallback_review = {
        "trade_id": int(trade_id),
        "result": result,
        "primary_reason": primary,
        "secondary_reasons": _secondary_reasons(trade, primary),
        "summary": _summary(trade, result, primary, pnl_r),
        "metrics": metrics,
        "source_snapshot": snapshot_context,
        "evidence_checklist": evidence_checklist,
        "improvement_suggestion": _improvement_suggestion(primary),
        "strategy_patch_candidate": patch,
        "market_regime_at_loss": snapshot_context.get("market_regime", "normal"),
        "evolution_trigger_allowed": bool(snapshot_context.get("evolution_trigger_allowed", True)),
    }
    review = run_agent_json_task(
        task_name="trade_review_attribution",
        payload={"trade": trade, "fallback_review": fallback_review, "snapshot_context": snapshot_context},
        fallback=fallback_review,
        schema_name="trade_review.schema.json",
        instructions=[
            "复盘昨日或单笔模拟盘交易，判断亏损/盈利是否来自方向、入场、趋势阶段、反向证据、执行质量或止盈止损设计。",
            "可以修正 primary_reason、summary、improvement_suggestion 和 candidate patch，但 patch 只能进入 candidate。",
            "不要输出实盘建议。",
        ],
    )
    ok, err = validate_json("trade_review.schema.json", review)
    if not ok:
        raise ValueError(f"TradeReview schema 校验失败: {err}")
    review_id = repo.save_trade_review(trade_id, review)
    review_patch = review.get("strategy_patch_candidate") if isinstance(review.get("strategy_patch_candidate"), dict) else patch
    patch_id = repo.save_strategy_patch_candidate(review_patch, {"trade_id": trade_id, "review_id": review_id}) if review_patch and review.get("evolution_trigger_allowed", True) else None
    if patch_id and review_patch:
        repo.save_strategy_version(
            strategy_name=review_patch["strategy_name"],
            version=review_patch["candidate_version"],
            status="shadow_testing",
            config=review_patch.get("patch", {}),
            change_reason=review_patch.get("change_reason", "trade_review"),
        )
    repo.update_strategy_memory_from_review(
        strategy_name=(review_patch or {}).get("strategy_name", "paper_trade_sop"),
        condition_hash=f"{trade.get('symbol')}:{primary}",
        result=result,
        pnl_r=pnl_r,
        notes=review["summary"],
    )
    return {"ok": True, "review_id": review_id, "patch_id": patch_id, "review": review}


def _load_review_json(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("ga_review_json") or "{}")
    except Exception:
        return {"trade_id": row.get("trade_id"), "result": row.get("result"), "primary_reason": row.get("primary_reason")}


def _trade_metrics(trade: dict[str, Any]) -> dict[str, Any]:
    entry = float(trade.get("entry_price") or 0)
    exit_price = float(trade.get("exit_price") or entry or 0)
    stop = float(trade.get("stop_loss") or entry or 0)
    risk = abs(entry - stop) or 1.0
    pnl_r = float(trade.get("pnl_r") or 0)
    mfe = float(trade.get("max_favorable_excursion") or 0)
    mae = float(trade.get("max_adverse_excursion") or 0)
    return {
        "pnl_r": pnl_r,
        "pnl_percent": trade.get("pnl_percent"),
        "mfe": mfe,
        "mae": mae,
        "mfe_r": mfe / risk,
        "mae_r": mae / risk,
        "entry_efficiency": trade.get("entry_efficiency") if trade.get("entry_efficiency") is not None else _bounded_efficiency(entry, stop, exit_price, trade.get("side")),
        "exit_efficiency": trade.get("exit_efficiency") if trade.get("exit_efficiency") is not None else 1.0 if pnl_r > 0 else 0.0 if pnl_r < 0 else 0.5,
        "signal_decay_score": trade.get("signal_decay_score"),
        "holding_minutes": _holding_minutes(trade.get("created_at"), trade.get("closed_at")),
        "close_reason": trade.get("close_reason"),
    }


def _secondary_reasons(trade: dict[str, Any], primary: str) -> list[str]:
    reasons: list[str] = []
    pnl_r = float(trade.get("pnl_r") or 0)
    if pnl_r < -1.0:
        reasons.append("risk_exceeded_expected_1r")
    if trade.get("close_reason") == "stop_loss" and primary != "wrong_direction":
        reasons.append("stop_loss_triggered")
    if trade.get("signal_decay_score") is not None and float(trade.get("signal_decay_score") or 0) >= 0.6:
        reasons.append("signal_decay")
    if not reasons:
        reasons.append("none")
    return reasons


def _improvement_suggestion(primary: str) -> dict[str, Any]:
    mapping = {
        "good_execution": "保持当前证据组合，继续观察样本量。",
        "wrong_direction": "降低同类方向信号权重，复查高周期和反向证据。",
        "entry_too_early": "等待 5m/15m 二次确认后再进入模拟盘。",
        "entry_chasing": "禁止远离 invalid 位后追价，增加 entry_distance 风险过滤。",
        "entry_too_late": "避免追价，要求 entry 距离 invalid 不超过计划风险。",
        "late_trend_chasing": "late 趋势阶段只允许观察或等待回踩，不生成追单计划。",
        "stop_loss_too_tight": "扩大或重算 invalid 位，避免正常回撤触发止损。",
        "take_profit_too_far": "缩短第一止盈距离，先验证 1R 附近兑现能力。",
        "unknown": "样本证据不足，先纳入 strategy_memory 观察。",
    }
    return {"action": "candidate_patch_or_memory_update", "note": mapping.get(primary, mapping["unknown"]), "patch_policy": "补丁只进入 candidate，不直接 active。"}


def _summary(trade: dict[str, Any], result: str, primary: str, pnl_r: float) -> str:
    return f"{trade.get('symbol')} 模拟盘交易已平仓，结果 {result}，R 值 {pnl_r:.2f}，主要归因：{primary}。"


def _snapshot_context(repo: CryptoGuardRepository, trade: dict[str, Any]) -> dict[str, Any]:
    snapshot_id = trade.get("market_snapshot_id")
    if not snapshot_id and trade.get("signal_id"):
        signal = repo.get_signal(int(trade["signal_id"]))
        snapshot_id = (signal or {}).get("market_snapshot_id") or (signal or {}).get("snapshot_id")
    if not snapshot_id:
        return {"available": False, "reason": "trade 未关联 snapshot"}
    row = repo.get_market_snapshot(int(snapshot_id))
    if not row:
        return {"available": False, "snapshot_id": int(snapshot_id), "reason": "snapshot 不存在"}
    try:
        snapshot = json.loads(row.get("snapshot_json") or "{}")
    except Exception as exc:
        return {"available": False, "snapshot_id": int(snapshot_id), "reason": f"snapshot_json 解析失败: {exc}"}
    modules = snapshot.get("modules") or {}
    regime = modules.get("market_regime") or {}
    return {
        "available": True,
        "snapshot_id": int(snapshot_id),
        "analysis_time_utc": snapshot.get("analysis_time_utc"),
        "trend_stage": (modules.get("trend_stage") or {}).get("trend_stage"),
        "price_action_event": (modules.get("price_action") or {}).get("last_event"),
        "momentum": (modules.get("momentum") or {}).get("direction"),
        "market_regime": regime.get("regime", "normal"),
        "evolution_trigger_allowed": bool(regime.get("evolution_trigger_allowed", True)),
        "counter_evidence": snapshot.get("counter_evidence") or {},
    }


def _evidence_checklist(trade: dict[str, Any], snapshot_context: dict[str, Any]) -> list[dict[str, Any]]:
    if not snapshot_context.get("available"):
        return [{"item": "snapshot_available", "status": "missing", "finding": snapshot_context.get("reason")}]
    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "item": "trend_stage",
            "status": "risk" if snapshot_context.get("trend_stage") == "late" else "ok",
            "finding": f"open_snapshot trend_stage={snapshot_context.get('trend_stage')}",
        }
    )
    counter = snapshot_context.get("counter_evidence") or {}
    risk_items = counter.get("neutral_or_risk_evidence") or []
    checks.append(
        {
            "item": "counter_evidence",
            "status": "risk" if risk_items else "ok",
            "finding": "；".join(str(x) for x in risk_items) if risk_items else "无显著反向证据",
        }
    )
    checks.append(
        {
            "item": "execution_path",
            "status": "risk" if float(trade.get("pnl_r") or 0) < -0.05 else "ok",
            "finding": f"pnl_r={float(trade.get('pnl_r') or 0):.2f}, close_reason={trade.get('close_reason')}",
        }
    )
    return checks


def _holding_minutes(created_at: Any, closed_at: Any) -> int | None:
    start = _parse_dt(created_at)
    end = _parse_dt(closed_at)
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds() // 60))


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bounded_efficiency(entry: float, stop: float, exit_price: float, side: Any) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.5
    direction = 1 if side == "LONG" else -1
    value = ((exit_price - entry) * direction) / risk
    return max(0.0, min(1.0, (value + 1.0) / 2.0))
