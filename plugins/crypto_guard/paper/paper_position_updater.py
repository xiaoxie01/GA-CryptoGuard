from __future__ import annotations

from typing import Any

from plugins.crypto_guard.data.binance_rest import fetch_mark_price
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.paper.execution_quality import equity_snapshot, market_from_price
from plugins.crypto_guard.paper.paper_broker import close_trade_if_needed, fill_order_if_triggered
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.redis_adapter import RedisAdapter
from plugins.crypto_guard.utils import utc_ms

LOGGER = get_logger("crypto_guard.paper")


def update_paper_positions(repo: CryptoGuardRepository, *, prices: dict[str, float | dict[str, Any]] | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    price_map = prices or {}
    latest_prices: dict[str, float] = {}
    redis = RedisAdapter()
    for order in repo.list_open_paper_orders():
        LOGGER.info("paper update order_id=%s symbol=%s status=%s", order.get("id"), order.get("symbol"), order.get("status"))
        market_or_price = price_map.get(order["symbol"])
        if market_or_price is None:
            market_or_price = float(fetch_mark_price(order["symbol"])["markPrice"])
        market = market_or_price if isinstance(market_or_price, dict) else market_from_price(order["symbol"], float(market_or_price))
        latest_prices[order["symbol"]] = float(market["close"])
        redis.set_latest_price(order["symbol"], float(market["close"]))
        if order["status"] == "pending":
            results.append(fill_order_if_triggered(repo, order, market))
        elif order["status"] == "open":
            trade = repo.get_open_trade_for_order(order["id"])
            if trade:
                close_result = close_trade_if_needed(repo, order, trade, market)
                results.append(close_result)
                adjustment = None if close_result.get("closed") else _maybe_adjust_stop_to_breakeven(repo, order, trade, market)
                if adjustment:
                    results.append(adjustment)
    snapshot = equity_snapshot(
        ts=utc_ms(),
        closed_realized_pnl=repo.sum_closed_realized_pnl(),
        open_trades=repo.list_open_paper_trades(),
        latest_prices=latest_prices,
        events=results,
    )
    previous_snapshot = repo.latest_equity_snapshot()
    snapshot_id = repo.save_equity_snapshot(snapshot)
    account = repo.update_paper_account_from_snapshot(snapshot)
    _sync_open_positions(repo, latest_prices)
    snapshot["id"] = snapshot_id
    snapshot["paper_account"] = account
    alert_job_id = _maybe_enqueue_drawdown_alert(repo, snapshot, previous_snapshot)
    if alert_job_id:
        snapshot["drawdown_alert_job_id"] = alert_job_id
    evolution = evaluate_evolution_triggers(repo, snapshot=snapshot)
    snapshot["evolution"] = evolution
    agent_execution_review = None
    if results or snapshot.get("drawdown_alert"):
        agent_execution_review = run_agent_json_task(
            task_name="paper_execution_quality_update",
            payload={"events": results, "equity_snapshot": snapshot},
            fallback={
                "summary": "模拟盘执行状态已更新。",
                "quality_findings": [],
                "risk_actions": ["继续按模拟盘风控观察"],
            },
            instructions=[
                "总结模拟盘成交、止盈止损、MFE/MAE、回撤和执行质量。",
                "只允许模拟盘/复盘建议，不得输出实盘下单建议。",
            ],
        )
    if results:
        LOGGER.info("paper update completed results=%s", results)
    return {"ok": True, "results": results, "equity_snapshot": snapshot, "agent_execution_review": agent_execution_review}


def _maybe_adjust_stop_to_breakeven(repo: CryptoGuardRepository, order: dict[str, Any], trade: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
    try:
        entry = float(trade["entry_price"])
        stop = float(order["stop_loss"])
        quantity = float(trade.get("quantity") or order.get("quantity") or 1)
    except (TypeError, ValueError):
        return None
    side = str(order["side"]).upper()
    risk_value = abs(entry - stop) * quantity
    mfe = float(trade.get("max_favorable_excursion") or 0)
    already_safe = stop >= entry if side == "LONG" else stop <= entry
    if already_safe or risk_value <= 0 or mfe < risk_value:
        return None
    repo.update_paper_order_stop_loss(order["id"], entry, reason="小级别趋势演化确认，止损移动到保本")
    repo.enqueue_job(
        "paper_event_alert",
        3,
        "paper_worker",
        f"system:paper:stop_adjust:{order['id']}",
        {
            "event_type": "stop_loss_adjustment",
            "symbol": order["symbol"],
            "order_id": order["id"],
            "trade_id": trade["id"],
            "entry_price": entry,
            "new_stop_loss": entry,
            "reason": "小级别走势向更大级别趋势演化，模拟盘止损移至保本。",
        },
    )
    return {"ok": True, "stop_loss_adjusted": True, "order_id": order["id"], "new_stop_loss": entry}


def _sync_open_positions(repo: CryptoGuardRepository, latest_prices: dict[str, float]) -> None:
    account = repo.ensure_paper_account()
    for trade in repo.list_open_paper_trades():
        price = latest_prices.get(trade["symbol"])
        if price is None:
            continue
        side = str(trade["side"]).upper()
        quantity = float(trade.get("quantity") or 1)
        pnl = (float(price) - float(trade["entry_price"])) * (1 if side == "LONG" else -1) * quantity
        pnl_pct = ((float(price) - float(trade["entry_price"])) * (1 if side == "LONG" else -1)) / float(trade["entry_price"]) * 100 if trade.get("entry_price") else 0.0
        repo.upsert_paper_position_from_trade(
            account_id=int(account["id"]),
            trade={**trade, "current_price": price},
            status="open",
            current_price=price,
            unrealized_pnl=pnl,
            unrealized_pnl_pct=pnl_pct,
        )


def _maybe_enqueue_drawdown_alert(repo: CryptoGuardRepository, snapshot: dict[str, Any], previous: dict[str, Any] | None) -> int | None:
    if not snapshot.get("drawdown_alert"):
        return None
    previous_alert = False
    if previous:
        import json

        try:
            previous_alert = bool(json.loads(previous.get("snapshot_json") or "{}").get("drawdown_alert"))
        except Exception:
            previous_alert = False
    if previous_alert:
        return None
    return repo.enqueue_job(
        "paper_drawdown_alert",
        3,
        "paper_worker",
        "system:paper:drawdown",
        {"snapshot": snapshot},
    )
