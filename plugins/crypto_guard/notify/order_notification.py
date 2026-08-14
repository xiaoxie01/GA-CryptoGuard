"""08-10 order notification builder (design.md §11, prd.md P2-1).

``build_order_notification`` renders a paper-order notification that carries
the FULL risk-governance context the operator needs to audit a fill:

  - original (candidate) vs adjusted (post-verifier) entry/stop geometry,
  - effective risk percent + the TP list,
  - computed quantity from account equity and effective risk,
  - confirmation source / timeframe / remaining TTL,
  - the final risk-committee result.

FAILS CLOSED: without ``verification_ok`` AND ``final_risk_check_ok`` it raises
``ValueError`` instead of emitting an order notification. An order that never
cleared the verifier must never be announced as one.
"""

from __future__ import annotations

import json
from typing import Any


def _json_take_profits(raw: Any) -> list[float]:
    """Decode a take-profit payload (JSON string or already-decoded list)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        entries = raw
    else:
        try:
            entries = json.loads(raw) if isinstance(raw, str) else []
        except (TypeError, ValueError):
            entries = []
    prices: list[float] = []
    for tp in entries:
        if isinstance(tp, dict):
            price = tp.get("price")
        else:
            price = tp
        try:
            prices.append(float(price))
        except (TypeError, ValueError):
            continue
    return prices


def _quantity(*, equity: float, risk_percent: float,
              entry: float, stop: float) -> float:
    """Fixed-fractional position size: risk_pct of equity / price risk."""
    risk_amount = equity * (risk_percent / 100.0)
    price_risk = abs(float(stop) - float(entry))
    if price_risk <= 0:
        return 0.0
    return risk_amount / price_risk


def build_order_notification(
    *, order: dict[str, Any], candidate_plan: dict[str, Any],
    adjusted_plan: dict[str, Any], verification: dict[str, Any],
    lifecycle: dict[str, Any] | None, account: dict[str, Any],
) -> str:
    """Render the order notification, or raise ValueError when the final risk
    committee did not pass the order (fail closed)."""
    if not (verification.get("verification_ok")
            and verification.get("final_risk_check_ok")):
        raise ValueError(
            "order_notification.build_order_notification FAIL CLOSED: final "
            f"risk committee did not pass the order "
            f"(verification_ok={verification.get('verification_ok')}, "
            f"final_risk_check_ok={verification.get('final_risk_check_ok')})"
        )

    symbol = order.get("symbol", "?")
    side = order.get("side", "?")
    order_type = order.get("order_type", "?")
    entry = float(order.get("entry_price", 0.0))
    stop = float(order.get("stop_loss", 0.0))

    cand_entry = float(candidate_plan.get("entry_price", entry))
    cand_stop = float(candidate_plan.get("stop_loss", stop))
    adj_entry = float(adjusted_plan.get("entry_price", entry))
    adj_stop = float(adjusted_plan.get("stop_loss", stop))
    risk_percent = float(adjusted_plan.get("risk_percent", 0.0))

    tps = _json_take_profits(order.get("take_profit_json")
                             or adjusted_plan.get("take_profits"))

    equity = float(account.get("equity", 0.0) or 0.0)
    qty = _quantity(equity=equity, risk_percent=risk_percent,
                    entry=entry, stop=stop)

    source = (lifecycle or {}).get("source", "?")
    timeframe = (lifecycle or {}).get("timeframe", "?")
    age_bars = (lifecycle or {}).get("age_bars", 0) or 0
    ttl_bars = (lifecycle or {}).get("ttl_bars")
    remaining = ""
    if ttl_bars is not None:
        remaining = f" · 已延续 {age_bars} · 剩余 {max(0, int(ttl_bars) - int(age_bars))}"

    lines = [
        f"**订单** {symbol} {side} {order_type} · 数量 {qty:.2f}",
        f"- 入场 {entry:.2f} · 止损 {stop:.2f}",
        f"- 原始 {cand_entry:.2f}/{cand_stop:.2f} · 调整 {adj_entry:.2f}/{adj_stop:.2f}",
        f"- 有效风险 {risk_percent:.2f}% · 止盈 {' · '.join(f'{p:.2f}' for p in tps)}",
        f"- 入场确认 {source} {timeframe}{remaining} · 最终风控：通过",
    ]
    return "\n".join(lines)
