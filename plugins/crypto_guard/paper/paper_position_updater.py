from __future__ import annotations

from typing import Any

from plugins.crypto_guard.data.binance_rest import fetch_klines, fetch_mark_price
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.paper.execution_quality import equity_snapshot, market_from_price
from plugins.crypto_guard.paper.paper_broker import close_trade_if_needed, fill_order_if_triggered
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.redis_adapter import RedisAdapter
from plugins.crypto_guard.utils import utc_ms

LOGGER = get_logger("crypto_guard.paper")


def _fetch_last_candle_market(symbol: str, mark_price: float) -> dict[str, Any]:
    """Fetch recent 1m candles OHLC for accurate stop loss/take profit checks.

    Uses the widest high/low across last 5 candles (including current) to detect
    intrabar price touches that mark price alone would miss.
    Falls back to mark_price-based market if candle fetch fails.
    """
    try:
        candles = fetch_klines(symbol, "1m", limit=5)
        if candles:
            # Use the widest range across all recent candles
            high = max(float(c["high"]) for c in candles)
            low = min(float(c["low"]) for c in candles)
            last = candles[-1]
            return {
                "symbol": symbol,
                "open_time": last.get("open_time"),
                "close_time": last.get("close_time"),
                "open": float(last["open"]),
                "high": high,
                "low": low,
                "close": mark_price,  # use mark price as current close
                "source": "1m_candle_range_with_mark",
            }
    except Exception as exc:
        LOGGER.debug("fetch last candle failed for %s: %s", symbol, exc)
    return market_from_price(symbol, mark_price)


def update_paper_positions(repo: CryptoGuardRepository, *, prices: dict[str, float | dict[str, Any]] | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    price_map = prices or {}
    latest_prices: dict[str, float] = {}
    redis = RedisAdapter()
    # Track orders filled in this batch — skip TP/SL check for them (defer to next batch)
    filled_order_ids: set[int] = set()
    for order in repo.list_open_paper_orders():
        LOGGER.info("paper update order_id=%s symbol=%s status=%s", order.get("id"), order.get("symbol"), order.get("status"))
        symbol = order["symbol"]
        market_or_price = price_map.get(symbol)
        if market_or_price is None:
            mark_price = float(fetch_mark_price(symbol)["markPrice"])
            candle_market = _fetch_last_candle_market(symbol, mark_price)
            market = candle_market
        else:
            market = market_or_price if isinstance(market_or_price, dict) else market_from_price(symbol, float(market_or_price))
        latest_prices[symbol] = float(market["close"])
        redis.set_latest_price(order["symbol"], float(market["close"]))
        if order["status"] == "pending":
            fill_result = fill_order_if_triggered(repo, order, market)
            results.append(fill_result)
            if fill_result.get("filled"):
                filled_order_ids.add(order["id"])
        elif order["status"] == "open":
            # Skip TP/SL check for orders just filled in this batch
            if order["id"] in filled_order_ids:
                continue
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
        _check_daily_loss_trigger(repo, results)
    # Fallback: ensure daily review runs even if scheduler missed the window
    _ensure_daily_review(repo)
    return {"ok": True, "results": results, "equity_snapshot": snapshot, "agent_execution_review": agent_execution_review}


def _ensure_daily_review(repo: CryptoGuardRepository) -> None:
    """Ensure daily review runs for yesterday if not already done."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Only check after 01:00 UTC (09:00 Beijing) to give scheduler a chance
    if now.hour < 1:
        return
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    # Check if daily_review_reports already exists for yesterday (source of truth)
    existing = repo.conn.execute(
        "SELECT id FROM daily_review_reports WHERE review_date=? LIMIT 1",
        (yesterday,),
    ).fetchone()
    if existing:
        return
    # Also check scheduler_runs for success
    existing_run = repo.conn.execute(
        "SELECT id FROM scheduler_runs WHERE job_name='daily_review' AND status='success' AND started_at >= ? AND started_at < ? LIMIT 1",
        (yesterday, now.strftime("%Y-%m-%d")),
    ).fetchone()
    if existing_run:
        return
    LOGGER.info("daily review fallback: enqueuing for %s", yesterday)
    repo.enqueue_job_once(
        "daily_review",
        7,
        "paper_worker",
        f"system:paper:daily_fallback:{yesterday}",
        {"day_utc": yesterday},
    )


def _check_daily_loss_trigger(repo: CryptoGuardRepository, results: list[dict[str, Any]]) -> None:
    """Trigger daily review when 3-5 losses occur in a single day."""
    # Count new stop losses from this batch
    new_sl_count = sum(1 for r in results if r.get("closed") and r.get("close_reason") == "stop_loss")
    if new_sl_count == 0:
        return
    # Count total losses today
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = repo.conn.execute(
        "SELECT COUNT(*) AS cnt FROM paper_trades WHERE close_reason='stop_loss' AND DATE(COALESCE(closed_at, datetime('now')))=?",
        (today,),
    ).fetchone()
    daily_losses = int(row["cnt"]) if row else 0
    LOGGER.info("daily loss check: today=%s losses=%s", today, daily_losses)
    if 3 <= daily_losses <= 5:
        # Check if we already triggered today
        existing = repo.conn.execute(
            "SELECT id FROM agent_jobs WHERE job_type='intraday_loss_review' AND session_id LIKE ? AND created_at >= ?",
            (f"system:paper:intraday_loss:{today}:%", today),
        ).fetchone()
        if not existing:
            repo.enqueue_job_once(
                "intraday_loss_review",
                5,  # high priority
                "paper_worker",
                f"system:paper:intraday_loss:{today}:{daily_losses}",
                {"day_utc": today, "trigger": "daily_loss_threshold", "loss_count": daily_losses},
            )
            LOGGER.info("daily review triggered by loss threshold: %s losses today", daily_losses)
            # Enqueue evolution trigger notification
            repo.enqueue_job_once(
                "evolution_trigger_alert",
                4,  # high priority
                "paper_worker",
                f"system:paper:evolution:{today}",
                {"trigger_type": "daily_loss_threshold", "loss_count": daily_losses, "day_utc": today},
            )


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
    # Read breakeven threshold from config (default 2.0R)
    from plugins.crypto_guard.config.loader import load_config
    risk_cfg = load_config().trading_mode.get("risk", {})
    breakeven_rr = float(risk_cfg.get("breakeven_after_rr", 2.0))
    if already_safe or risk_value <= 0 or mfe < risk_value * breakeven_rr:
        return None
    repo.update_paper_order_stop_loss(order["id"], entry, reason=f"价格已运行 {breakeven_rr}R，止损移动到保本")
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
            "side": order.get("side"),
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
