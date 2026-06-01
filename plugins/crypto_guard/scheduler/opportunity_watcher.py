from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.utils import latest_closed_close_time_ms, utc_ms

LOGGER = get_logger("crypto_guard.opportunity_watcher")


def update_opportunity_watches(repo: CryptoGuardRepository, *, analysis_time_utc: int | None = None) -> dict[str, Any]:
    analysis_time = int(analysis_time_utc or latest_closed_close_time_ms("15m", utc_ms()))
    checked = triggered = invalidated = expired = 0
    results: list[dict[str, Any]] = []

    for watch in repo.list_active_opportunity_watches():
        checked += 1
        result = evaluate_watch(repo, watch, analysis_time_utc=analysis_time)
        result = _agent_review_watch_result(watch, result)
        result["analysis_time_utc"] = analysis_time
        results.append(result)
        status = result["status"]
        if status == "expired":
            if repo.update_opportunity_watch_status(watch["id"], "expired", invalidated_reason="expired"):
                expired += 1
        elif status == "invalidated":
            if repo.update_opportunity_watch_status(watch["id"], "invalidated", invalidated_reason=result.get("reason")):
                invalidated += 1
        elif status == "triggered":
            triggered_at = _utc_iso_from_ms(analysis_time)
            if repo.update_opportunity_watch_status(watch["id"], "triggered", triggered_at=triggered_at):
                alert_job_id = repo.enqueue_job(
                    "opportunity_watch_alert",
                    3,
                    "opportunity_watcher",
                    f"system:opportunity_watch:{watch['id']}",
                    {"watch_id": watch["id"], "result": result, "analysis_time_utc": analysis_time},
                )
                result["alert_job_id"] = alert_job_id
                triggered += 1
        else:
            repo.touch_opportunity_watch(watch["id"])

    LOGGER.info(
        "update_opportunity_watches checked=%s triggered=%s invalidated=%s expired=%s",
        checked,
        triggered,
        invalidated,
        expired,
    )
    return {
        "ok": True,
        "analysis_time_utc": analysis_time,
        "checked": checked,
        "triggered": triggered,
        "invalidated": invalidated,
        "expired": expired,
        "results": results,
    }


def evaluate_watch(repo: CryptoGuardRepository, watch: dict[str, Any], *, analysis_time_utc: int) -> dict[str, Any]:
    if _is_expired(watch.get("expires_at")):
        return _result(watch, "expired", "监控已过期")

    timeframe = _watch_timeframe(watch)
    candles = repo.get_candles(watch["symbol"], timeframe, analysis_time_utc=analysis_time_utc, limit=3)
    if not candles:
        return _result(watch, "waiting", f"{timeframe} 无已收盘 K 线")

    latest = candles[-1]
    previous = candles[-2] if len(candles) > 1 else None
    invalid = _load_json(watch.get("invalid_condition_json"), None)
    invalid_hit = _condition_hit(invalid, latest, previous, watch)
    if invalid_hit["hit"]:
        return _result(watch, "invalidated", invalid_hit["reason"])

    conditions = _load_json(watch.get("watch_condition_json"), [])
    if isinstance(conditions, dict):
        conditions = [conditions]
    if not isinstance(conditions, list) or not conditions:
        return _result(watch, "waiting", "监控条件为空或无法解析")

    hits = [_condition_hit(condition, latest, previous, watch) for condition in conditions]
    triggered = any(item["hit"] for item in hits)
    if triggered:
        reasons = [item["reason"] for item in hits if item["hit"]]
        return _result(watch, "triggered", "；".join(reasons), condition_results=hits, latest_candle=latest)
    return _result(watch, "waiting", "尚未满足触发条件", condition_results=hits, latest_candle=latest)


def render_watch_alert_text(watch: dict[str, Any], result: dict[str, Any]) -> str:
    agent_reason = ((result.get("agent_review") or {}).get("summary") if isinstance(result.get("agent_review"), dict) else None) or result.get("reason")
    return "\n".join(
        [
            "**CryptoGuard 机会监控触发**",
            "",
            f"- 监控：#{watch['id']} {watch['symbol']} {watch.get('direction') or '-'}",
            f"- 原因：{watch.get('watch_reason') or '-'}",
            f"- GA/LLM 触发研判：{agent_reason or '-'}",
            f"- 分析时间 UTC：{result.get('analysis_time_utc') or '-'}",
            "",
            "不构成实盘建议，仅用于模拟盘与策略研究。",
        ]
    )


def _agent_review_watch_result(watch: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    fallback = {
        "summary": result.get("reason") or "机会监控状态已更新。",
        "status": result.get("status"),
        "action": "notify" if result.get("status") == "triggered" else "keep_waiting",
        "risk_notes": [],
    }
    agent = run_agent_json_task(
        task_name="opportunity_watch_review",
        payload={"watch": watch, "rule_result": result},
        fallback=fallback,
        instructions=[
            "复核机会监控条件是否真的值得提醒，解释触发/失效/继续等待原因。",
            "只能输出观察、提醒、失效等模拟盘研究建议，不得输出实盘建议。",
        ],
    )
    enriched = dict(result)
    enriched["agent_review"] = agent
    if agent.get("summary"):
        enriched["reason"] = str(agent["summary"])
    return enriched


def _condition_hit(condition: Any, latest: dict[str, Any], previous: dict[str, Any] | None, watch: dict[str, Any]) -> dict[str, Any]:
    if isinstance(condition, str):
        return {"hit": False, "reason": f"文本条件等待人工或后续结构化确认：{condition}"}
    if not isinstance(condition, dict):
        return {"hit": False, "reason": "条件格式不支持"}

    kind = str(condition.get("type") or condition.get("kind") or "").lower()
    side = str(condition.get("side") or condition.get("direction") or watch.get("direction") or "").upper()
    level = _float_or_none(condition.get("level") or condition.get("price"))
    close = float(latest["close"])
    high = float(latest["high"])
    low = float(latest["low"])

    if kind in {"price_below", "close_below"} and level is not None:
        return {"hit": close < level, "reason": f"收盘价 {close} 跌破 {level}"}
    if kind in {"price_above", "close_above"} and level is not None:
        return {"hit": close > level, "reason": f"收盘价 {close} 站上 {level}"}
    if kind == "pullback" and level is not None:
        tolerance = float(condition.get("tolerance_pct") or 0.003)
        if side in {"LONG", "BULLISH"}:
            hit = low <= level * (1 + tolerance) and close >= level
            return {"hit": hit, "reason": f"回踩 {level} 后收回"}
        if side in {"SHORT", "BEARISH"}:
            hit = high >= level * (1 - tolerance) and close <= level
            return {"hit": hit, "reason": f"反抽 {level} 后回落"}
    if kind == "breakout" and level is not None:
        if side in {"LONG", "BULLISH"}:
            return {"hit": close > level, "reason": f"收盘突破 {level}"}
        if side in {"SHORT", "BEARISH"}:
            return {"hit": close < level, "reason": f"收盘跌破 {level}"}
    if kind == "reclaim" and level is not None and previous:
        previous_close = float(previous["close"])
        if side in {"LONG", "BULLISH"}:
            hit = previous_close < level and close > level
            return {"hit": hit, "reason": f"从 {level} 下方重新站回"}
        if side in {"SHORT", "BEARISH"}:
            hit = previous_close > level and close < level
            return {"hit": hit, "reason": f"从 {level} 上方重新跌回"}
    if kind == "cvd_confirmation":
        flow = str(condition.get("flow_confirmation") or condition.get("value") or "").lower()
        expected = "supports_long" if side in {"LONG", "BULLISH"} else "supports_short"
        return {"hit": flow == expected, "reason": f"CVD/order-flow 确认为 {flow or 'not_available'}"}
    return {"hit": False, "reason": f"未知或未满足条件：{condition}"}


def _result(
    watch: dict[str, Any],
    status: str,
    reason: str,
    *,
    condition_results: list[dict[str, Any]] | None = None,
    latest_candle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "watch_id": watch["id"],
        "symbol": watch["symbol"],
        "status": status,
        "reason": reason,
        "condition_results": condition_results or [],
        "latest_candle": _candle_summary(latest_candle),
    }


def _watch_timeframe(watch: dict[str, Any]) -> str:
    conditions = _load_json(watch.get("watch_condition_json"), [])
    if isinstance(conditions, dict):
        conditions = [conditions]
    if isinstance(conditions, list):
        for condition in conditions:
            if isinstance(condition, dict) and condition.get("timeframe"):
                return str(condition["timeframe"])
    return "15m"


def _is_expired(expires_at: Any) -> bool:
    if not expires_at:
        return False
    try:
        raw = str(expires_at).replace("Z", "+00:00")
        return datetime.fromisoformat(raw) <= datetime.now(timezone.utc)
    except Exception:
        return False


def _load_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _candle_summary(candle: dict[str, Any] | None) -> dict[str, Any] | None:
    if not candle:
        return None
    return {
        "open_time": candle.get("open_time"),
        "close_time": candle.get("close_time"),
        "open": candle.get("open"),
        "high": candle.get("high"),
        "low": candle.get("low"),
        "close": candle.get("close"),
    }


def _utc_iso_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
