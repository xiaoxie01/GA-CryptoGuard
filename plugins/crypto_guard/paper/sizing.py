from __future__ import annotations

DEFAULT_ACCOUNT_BALANCE = 10000.0
DEFAULT_RISK_PERCENT = 0.5
DEFAULT_SLIPPAGE_PCT = 0.001


def compute_position_size(
    entry_price: float,
    stop_loss: float,
    *,
    risk_percent: float = DEFAULT_RISK_PERCENT,
    account_balance: float = DEFAULT_ACCOUNT_BALANCE,
) -> tuple[float, float] | None:
    """Compute quantity and initial_risk_usdt from risk% sizing formula."""
    risk_pct = risk_percent / 100.0
    risk_usdt = account_balance * risk_pct
    risk_per_unit = abs(entry_price - stop_loss)
    if risk_per_unit <= 0:
        return None
    return (risk_usdt / risk_per_unit, risk_usdt)


def compute_fill_price(
    entry_price: float,
    side: str,
    *,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    order_type: str = "market",
) -> float:
    """Apply market-order slippage while leaving passive orders at entry price."""
    if str(order_type).lower() != "market":
        return entry_price
    side_upper = str(side).upper()
    if side_upper == "SHORT":
        return entry_price * (1 - slippage_pct)
    return entry_price * (1 + slippage_pct)
