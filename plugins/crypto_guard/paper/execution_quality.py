from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def market_from_price(symbol: str, price: float, *, ts: int | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "open_time": ts,
        "close_time": ts,
        "open": float(price),
        "high": float(price),
        "low": float(price),
        "close": float(price),
        "source": "mark_price",
    }


def update_trade_path_metrics(
    trade: dict[str, Any],
    market: dict[str, Any],
    *,
    event: str = "mark",
    quantity: float | None = None,
) -> dict[str, Any]:
    entry = float(trade["entry_price"])
    side = str(trade["side"]).upper()
    if quantity is None:
        quantity = _quantity(trade)
    favorable_price = float(market["high"]) if side == "LONG" else float(market["low"])
    adverse_price = float(market["low"]) if side == "LONG" else float(market["high"])
    favorable = max(0.0, _pnl(side, entry, favorable_price, quantity))
    adverse = min(0.0, _pnl(side, entry, adverse_price, quantity))
    current_mfe = float(trade.get("max_favorable_excursion") or 0.0)
    current_mae = float(trade.get("max_adverse_excursion") or 0.0)
    mfe = max(current_mfe, favorable)
    mae = min(current_mae, adverse)
    path = _load_path(trade)
    path.append(
        {
            "event": event,
            "ts": market.get("close_time"),
            "open": market.get("open"),
            "high": market.get("high"),
            "low": market.get("low"),
            "close": market.get("close"),
            "mfe": mfe,
            "mae": mae,
        }
    )
    return {
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "stop_take_path": path,
    }


def evaluate_exit(order: dict[str, Any], trade: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    side = str(order["side"]).upper()
    stop = float(order["stop_loss"])
    take_profits = _take_profits(order)
    high = float(market["high"])
    low = float(market["low"])
    close = float(market["close"])

    if side == "LONG":
        hit_sl = low <= stop
        hit_tps = [tp for tp in take_profits if high >= float(tp["price"])]
    else:
        hit_sl = high >= stop
        hit_tps = [tp for tp in take_profits if low <= float(tp["price"])]

    if not hit_sl and not hit_tps:
        return {"should_close": False, "reason": None, "exit_price": close, "hit": None}

    first_tp = _first_take_profit(side, hit_tps)
    if hit_sl and first_tp:
        # Intrabar order is unknowable from OHLC alone. Use conservative stop-first handling
        # and record ambiguity in the path for later review.
        return {"should_close": True, "reason": "stop_loss", "exit_price": stop, "hit": {"ambiguous_intrabar": True, "tp": first_tp}}
    if hit_sl:
        return {"should_close": True, "reason": "stop_loss", "exit_price": stop, "hit": {"stop_loss": stop}}
    return {"should_close": True, "reason": "take_profit", "exit_price": float(first_tp["price"]), "hit": {"take_profit": first_tp}}


def close_quality_metrics(
    order: dict[str, Any],
    trade: dict[str, Any],
    market: dict[str, Any],
    *,
    exit_price: float,
    close_reason: str,
) -> dict[str, Any]:
    side = str(order["side"]).upper()
    entry = float(trade["entry_price"])
    stop = float(order["stop_loss"])
    # Calculate actual position size based on risk
    risk_pct = float(order.get("risk_percent") or 0.5) / 100.0
    account_balance = 10000.0  # starting equity
    risk_usdt = account_balance * risk_pct  # e.g. 10000 * 0.5% = 50U
    risk_per_unit = abs(entry - stop)
    quantity = risk_usdt / risk_per_unit if risk_per_unit > 0 else 1.0
    risk = risk_usdt  # actual USDT risk
    pnl = _pnl(side, entry, exit_price, quantity)
    pnl_r = pnl / risk
    pnl_percent = ((exit_price - entry) * (1 if side == "LONG" else -1)) / entry * 100 if entry else 0.0
    path_metrics = update_trade_path_metrics(trade, market, event=close_reason, quantity=quantity)
    mfe = float(path_metrics["max_favorable_excursion"])
    mae = float(path_metrics["max_adverse_excursion"])
    entry_efficiency = _entry_efficiency(side, entry, stop, market)
    exit_efficiency = _exit_efficiency(pnl, mfe, risk)
    signal_decay = _signal_decay_score(trade, market, pnl_r)
    return {
        "pnl": pnl,
        "pnl_percent": pnl_percent,
        "pnl_r": pnl_r,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "entry_efficiency": entry_efficiency,
        "exit_efficiency": exit_efficiency,
        "signal_decay_score": signal_decay,
        "stop_take_path": path_metrics["stop_take_path"],
    }


def equity_snapshot(
    *,
    ts: int,
    closed_realized_pnl: float,
    open_trades: list[dict[str, Any]],
    latest_prices: dict[str, float],
    events: list[dict[str, Any]],
    starting_equity: float = 10_000.0,
) -> dict[str, Any]:
    unrealized = 0.0
    for trade in open_trades:
        price = latest_prices.get(trade["symbol"])
        if price is None:
            continue
        # Use stored quantity (calculated at fill time) or recalculate
        qty = _quantity(trade)
        if qty <= 1.0:
            # Recalculate from risk parameters
            entry = float(trade.get("entry_price") or 0)
            stop = float(trade.get("stop_loss") or 0)
            risk_per_unit = abs(entry - stop) if entry and stop else 0
            if risk_per_unit > 0:
                qty = (starting_equity * 0.005) / risk_per_unit  # 0.5% risk
        unrealized += _pnl(str(trade["side"]).upper(), float(trade["entry_price"]), float(price), qty)
    equity = starting_equity + closed_realized_pnl + unrealized
    drawdown_percent = min(0.0, (equity - starting_equity) / starting_equity * 100) if starting_equity else 0.0
    return {
        "ts": int(ts),
        "account_equity": equity,
        "unrealized_pnl": unrealized,
        "realized_pnl": closed_realized_pnl,
        "margin_used": None,
        "open_position_count": len(open_trades),
        "drawdown_percent": drawdown_percent,
        "drawdown_alert": drawdown_percent <= -5.0,
        "events": events,
    }


def _pnl(side: str, entry: float, price: float, quantity: float) -> float:
    direction = 1 if side == "LONG" else -1
    return (float(price) - float(entry)) * direction * quantity


def _quantity(row: dict[str, Any]) -> float:
    raw = row.get("quantity")
    if raw in (None, "", 0):
        return 1.0
    return float(raw)


def _take_profits(order: dict[str, Any]) -> list[dict[str, Any]]:
    import json

    raw = order.get("take_profit_json") or "[]"
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return [x for x in values if isinstance(x, dict) and x.get("price") is not None]


def _first_take_profit(side: str, take_profits: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not take_profits:
        return None
    reverse = side == "SHORT"
    return sorted(take_profits, key=lambda tp: float(tp["price"]), reverse=reverse)[0]


def _load_path(trade: dict[str, Any]) -> list[dict[str, Any]]:
    import json

    raw = trade.get("stop_take_path_json")
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except Exception:
        return []
    return values if isinstance(values, list) else []


def _entry_efficiency(side: str, entry: float, stop: float, market: dict[str, Any]) -> float:
    adverse_price = float(market["low"]) if side == "LONG" else float(market["high"])
    risk = abs(entry - stop) or 1.0
    adverse_move = abs(entry - adverse_price)
    return max(0.0, min(1.0, 1.0 - adverse_move / risk))


def _exit_efficiency(pnl: float, mfe: float, risk: float) -> float:
    if pnl > 0 and mfe > 0:
        return max(0.0, min(1.0, pnl / mfe))
    if pnl < 0:
        return 0.0
    return max(0.0, min(1.0, 0.5 + pnl / (2 * risk)))


def _signal_decay_score(trade: dict[str, Any], market: dict[str, Any], pnl_r: float) -> float:
    minutes = _holding_minutes(trade.get("created_at"), market.get("close_time"))
    time_decay = min(0.6, (minutes or 0) / 1440.0)
    performance_decay = max(0.0, -pnl_r) * 0.4
    return max(0.0, min(1.0, time_decay + performance_decay))


def _holding_minutes(created_at: Any, close_time_ms: Any) -> int | None:
    if not created_at or close_time_ms is None:
        return None
    start = _parse_dt(created_at)
    if not start:
        return None
    end = datetime.fromtimestamp(int(close_time_ms) / 1000, timezone.utc)
    return max(0, int((end - start).total_seconds() // 60))


def _parse_dt(value: Any) -> datetime | None:
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
