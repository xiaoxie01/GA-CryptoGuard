from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

from plugins.crypto_guard.data.binance_rest import fetch_klines, fetch_mark_price
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.paper.execution_quality import market_from_price
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.shadow_virtual_trade")

# ── defaults ──────────────────────────────────────────────────────────────
DEFAULT_MAX_PENDING_MINUTES = 120       # 2h for pending_entry expiry
DEFAULT_MAX_HOLD_MINUTES = 4320         # 72h max holding time
DEFAULT_MAX_STALE_MINUTES = 15          # skip update if latest candle is older
DEFAULT_GAP_CATCHUP_LIMIT = 500         # max candles to fetch for gap catch-up
MAX_PAGINATION_PAGES = 10               # safety cap: max pages to fetch per trade per update


def _iso_to_unix_ms(iso_str: str | None) -> int | None:
    """Convert ISO datetime string to unix milliseconds. Returns None on failure."""
    if not iso_str:
        return None
    try:
        if isinstance(iso_str, str):
            dt = datetime.fromisoformat(iso_str)
        else:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _iso_to_dt(iso_str: str | None) -> datetime | None:
    """Convert ISO datetime string to timezone-aware datetime. Returns None on failure."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _fetch_candles_from(
    symbol: str,
    start_time_ms: int,
    *,
    limit: int = DEFAULT_GAP_CATCHUP_LIMIT,
) -> list[dict[str, Any]]:
    """Fetch 1m candles from start_time_ms to now, up to limit candles.

    Returns candles sorted by open_time ascending.
    Returns empty list only when there are genuinely no candles in range
    (gap is caught up). Raises on network/API errors so the caller can
    distinguish "no data" from "fetch failed" and preserve the cursor.
    """
    candles = fetch_klines(symbol, "1m", start_time=start_time_ms, limit=limit)
    if candles:
        candles.sort(key=lambda c: int(c.get("open_time", 0)))
    return candles


def _single_candle_from_mark(symbol: str, mark_price: float) -> dict[str, Any]:
    """Build a single-candle dict from mark price with current timestamp.

    Marked as is_closed=False — it's a point-in-time snapshot, not a finalized candle.
    Processing it updates unrealized PnL but does NOT advance the cursor.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "symbol": symbol,
        "open_time": now_ms,
        "close_time": now_ms,
        "open": mark_price,
        "high": mark_price,
        "low": mark_price,
        "close": mark_price,
        "is_closed": False,
        "source": "mark_price",
    }


def _minutes_since(iso_str: str | None) -> float | None:
    """Return minutes elapsed since iso_str (ISO string or None)."""
    if not iso_str:
        return None
    try:
        if isinstance(iso_str, str):
            dt = datetime.fromisoformat(iso_str)
        else:
            dt = iso_str
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def _is_candle_stale(candle: dict[str, Any], max_stale_minutes: int) -> bool:
    """Check if the latest candle's close_time is too old."""
    close_time = candle.get("close_time")
    if close_time is None:
        return True
    try:
        ct = datetime.fromtimestamp(int(close_time) / 1000, tz=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - ct).total_seconds() / 60.0
        return age_minutes > max_stale_minutes
    except (ValueError, TypeError, OSError):
        return True


def _candle_event_dt(candle: dict[str, Any]) -> datetime | None:
    """Return the event time of a candle as a timezone-aware datetime."""
    open_time = candle.get("open_time")
    if open_time is None:
        return None
    try:
        return datetime.fromtimestamp(int(open_time) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


# ── entry activation ──────────────────────────────────────────────────────

def activate_pending_entry(
    repo: CryptoGuardRepository,
    trade: dict[str, Any],
    candle: dict[str, Any],
    *,
    event_time: str | None = None,
) -> bool:
    """Check if a pending_entry trade's entry condition is met and transition to open.

    For market orders: activates immediately and updates entry_price to the actual
    fill price (candle.open + slippage), recalculates quantity using
    compute_position_size while preserving the original initial_risk_usdt budget.

    For limit/trigger/stop orders: activates when price condition is met, using
    the planned entry_price (no slippage adjustment).

    Returns True if the trade was activated, False otherwise.
    """
    side = str(trade.get("side", "LONG")).upper()
    entry_price = float(trade["entry_price"])
    entry_type = str(trade.get("entry_type", "market")).lower()

    candle_high = float(candle.get("high", 0))
    candle_low = float(candle.get("low", 0))

    activated = False

    if side == "LONG":
        if entry_type == "market":
            activated = True
        elif entry_type in ("limit",):
            activated = candle_low <= entry_price
        elif entry_type in ("trigger", "stop"):
            activated = candle_high >= entry_price
    else:  # SHORT
        if entry_type == "market":
            activated = True
        elif entry_type in ("limit",):
            activated = candle_high >= entry_price
        elif entry_type in ("trigger", "stop"):
            activated = candle_low <= entry_price

    if activated:
        trade_id = int(trade["id"])

        if entry_type == "market":
            # Market entry: compute fill price from candle.open + slippage.
            # Inlined to avoid circular import (paper_broker -> controller -> updater).
            # Formula: LONG → open * (1 + slippage), SHORT → open * (1 - slippage)
            slippage_pct = 0.001  # DEFAULT_SLIPPAGE_PCT from paper_broker
            candle_open = float(candle.get("open", 0))
            if candle_open <= 0:
                LOGGER.warning(
                    "shadow virtual trade activation: candle.open is zero id=%s symbol=%s — skipping fill price calc",
                    trade_id, trade.get("symbol"),
                )
            else:
                if side == "SHORT":
                    fill_price = candle_open * (1 - slippage_pct)
                else:
                    fill_price = candle_open * (1 + slippage_pct)

                stop_loss = float(trade["stop_loss"])
                initial_risk_usdt = float(trade["initial_risk_usdt"])

                # Recalculate quantity against actual fill price, preserving risk budget.
                # compute_position_size inlined: risk_percent back-computed from initial_risk_usdt.
                account_balance = 10000.0  # DEFAULT_ACCOUNT_BALANCE
                risk_percent = (initial_risk_usdt / account_balance) * 100.0
                risk_pct = risk_percent / 100.0
                risk_usdt = account_balance * risk_pct
                risk_per_unit = abs(fill_price - stop_loss)
                if risk_per_unit > 0:
                    new_quantity = risk_usdt / risk_per_unit
                    new_risk = risk_usdt
                    # Update entry_price, quantity, and initial_risk_usdt inline
                    repo.conn.execute(
                        "UPDATE shadow_virtual_trades SET entry_price=?, quantity=?, initial_risk_usdt=?"
                        " WHERE id=?",
                        (fill_price, new_quantity, new_risk, trade_id),
                    )
                    LOGGER.info(
                        "shadow virtual trade market activation id=%s symbol=%s side=%s "
                        "planned_entry=%.4f fill_entry=%.4f qty=%.6f risk=%.4f",
                        trade_id, trade.get("symbol"), side, entry_price, fill_price,
                        new_quantity, new_risk,
                    )
                else:
                    LOGGER.warning(
                        "shadow virtual trade activation: risk_per_unit <= 0 id=%s — using planned values",
                        trade_id,
                    )

        repo.update_shadow_virtual_trade_status(trade_id, "open", event_time=event_time)
        LOGGER.info(
            "shadow virtual trade activated id=%s symbol=%s side=%s entry_type=%s entry=%.4f",
            trade_id, trade.get("symbol"), side, entry_type, entry_price,
        )
        return True

    return False


# ── SL / TP checks ────────────────────────────────────────────────────────

def check_sl_tp(
    trade: dict[str, Any],
    candle: dict[str, Any],
) -> tuple[str | None, float | None]:
    """Check if stop-loss or take-profit has been hit on a SINGLE candle.

    Returns a (close_reason, close_price) tuple.
    close_reason is one of: 'stop_loss', 'take_profit', 'ambiguous_path', or None.
    close_price is the actual trigger price (stop_loss price for SL, TP price for TP).

    Same-candle SL+TP: conservative rule — SL wins (risk management priority).
    Records 'ambiguous_path' as close_reason with stop_loss price.
    """
    side = str(trade.get("side", "LONG")).upper()
    stop_loss = float(trade.get("stop_loss") or 0)
    candle_high = float(candle.get("high", 0))
    candle_low = float(candle.get("low", 0))

    sl_hit = False
    tp_hit = False
    tp_hit_price: float | None = None

    if stop_loss > 0:
        if side == "LONG" and candle_low <= stop_loss:
            sl_hit = True
        if side == "SHORT" and candle_high >= stop_loss:
            sl_hit = True

    tp_json = trade.get("take_profit_json") or "[]"
    try:
        tp_levels = json.loads(tp_json) if isinstance(tp_json, str) else tp_json
    except (json.JSONDecodeError, TypeError):
        tp_levels = []

    if tp_levels:
        for tp in tp_levels:
            if isinstance(tp, dict):
                tp_price = float(tp.get("price", 0))
            else:
                tp_price = float(tp)
            if tp_price <= 0:
                continue
            if side == "LONG" and candle_high >= tp_price:
                tp_hit = True
                tp_hit_price = tp_price
                break
            if side == "SHORT" and candle_low <= tp_price:
                tp_hit = True
                tp_hit_price = tp_price
                break

    if sl_hit and tp_hit:
        return ("ambiguous_path", stop_loss)

    if sl_hit:
        return ("stop_loss", stop_loss)

    if tp_hit:
        return ("take_profit", tp_hit_price)

    return (None, None)


# ── per-candle replay helpers ─────────────────────────────────────────────

def _get_replay_start(trade: dict[str, Any]) -> int | None:
    """Determine the start time for candle replay.

    Priority: last_processed_candle_time > opened_at > created_at.
    Returns unix ms or None.
    """
    last_processed = trade.get("last_processed_candle_time")
    if last_processed is not None:
        try:
            return int(last_processed)
        except (ValueError, TypeError):
            pass

    return _iso_to_unix_ms(trade.get("opened_at") or trade.get("created_at"))


def _process_candle_for_trade(
    repo: CryptoGuardRepository,
    trade: dict[str, Any],
    candle: dict[str, Any],
    *,
    max_hold_minutes: int,
    max_pending_minutes: int = DEFAULT_MAX_PENDING_MINUTES,
    candle_dt: datetime | None = None,
) -> tuple[str, int, int]:
    """Process a single candle for a single trade.

    Returns (action, closed_delta, activated_delta) where action is one of:
      'none', 'updated', 'activated', 'closed_sl', 'closed_tp',
      'closed_ambiguous', 'closed_max_hold', 'expired_pending'

    closed_delta and activated_delta separate same-candle activation+close counts
    so the caller can track activated_count vs closed_count independently.
    For most actions, only one of the two deltas is non-zero.
    """
    trade_id = int(trade["id"])
    status = str(trade.get("status", "open"))
    symbol = str(trade["symbol"])

    if status == "pending_entry":
        # Use candle event time for expiry check, falling back to wall clock
        ref_dt = candle_dt if candle_dt is not None else datetime.now(timezone.utc)
        # Prefer expires_at if set, otherwise fall back to created_at + DEFAULT_MAX_PENDING_MINUTES
        expires_at_str = trade.get("expires_at")
        if expires_at_str:
            expires_dt = _iso_to_dt(expires_at_str)
            if expires_dt is not None and ref_dt >= expires_dt:
                repo.update_shadow_virtual_trade_status(trade_id, "expired")
                LOGGER.info(
                    "shadow virtual trade expired (pending) id=%s symbol=%s expires_at=%s ref=%s",
                    trade_id, symbol, expires_at_str, ref_dt.isoformat(),
                )
                return ("expired_pending", 0, 1)
        else:
            created_dt = _iso_to_dt(trade.get("created_at"))
            if created_dt is not None:
                age_minutes = (ref_dt - created_dt).total_seconds() / 60.0
                if age_minutes > max_pending_minutes:
                    repo.update_shadow_virtual_trade_status(trade_id, "expired")
                    LOGGER.info(
                        "shadow virtual trade expired (pending) id=%s symbol=%s age=%.0fm",
                        trade_id, symbol, age_minutes,
                    )
                    return ("expired_pending", 0, 1)

        if activate_pending_entry(repo, trade, candle, event_time=candle_dt.isoformat() if candle_dt else None):
            # P1-3: Same-candle SL/TP after activation — conservative rule
            # Re-read trade as open and check if this candle also hits SL/TP
            activated_trade = dict(repo.conn.execute(
                "SELECT * FROM shadow_virtual_trades WHERE id=?", (trade_id,)
            ).fetchone() or trade)
            if str(activated_trade.get("status")) == "open":
                sl_tp_reason, sl_tp_price = check_sl_tp(activated_trade, candle)
                if sl_tp_reason:
                    # Same-candle activation + SL/TP: can't determine order within the candle.
                    # SL+TP (ambiguous_path) or SL-only → record as stop_loss (conservative).
                    # TP-only → record as activation_ambiguous_path (not a valid profit sample).
                    if sl_tp_reason == "stop_loss":
                        close_reason = "stop_loss"
                    elif sl_tp_reason == "ambiguous_path":
                        close_reason = "stop_loss"  # SL+TP both hit → SL wins
                    else:
                        close_reason = "activation_ambiguous_path"  # TP-only: can't confirm order
                    result = repo.close_shadow_virtual_trade(trade_id, sl_tp_price, close_reason)
                    if result:
                        LOGGER.info(
                            "shadow virtual trade activated+closed same-candle id=%s symbol=%s reason=%s pnl_r=%.4f",
                            trade_id, symbol, close_reason, result.get("pnl_r", 0),
                        )
                        return ("closed_ambiguous", 1, 1)
            return ("activated", 0, 1)
        return ("none", 0, 0)

    if status == "open":
        close_price = float(candle.get("close", 0))
        if close_price <= 0:
            return ("none", 0, 0)

        # Update unrealized PnL
        repo.update_shadow_virtual_trade_prices(trade_id, close_price)

        # Check SL / TP
        sl_tp_reason, sl_tp_price = check_sl_tp(trade, candle)
        if sl_tp_reason:
            result = repo.close_shadow_virtual_trade(trade_id, sl_tp_price, sl_tp_reason)
            if result:
                LOGGER.info(
                    "shadow virtual trade closed id=%s symbol=%s reason=%s pnl_r=%.4f",
                    trade_id, symbol, sl_tp_reason, result.get("pnl_r", 0),
                )
                if sl_tp_reason == "stop_loss":
                    return ("closed_sl", 1, 0)
                elif sl_tp_reason == "take_profit":
                    return ("closed_tp", 1, 0)
                else:
                    return ("closed_ambiguous", 1, 0)
            return ("none", 0, 0)

        # Check max holding time — use candle event time, fall back to wall clock
        ref_dt = candle_dt if candle_dt is not None else datetime.now(timezone.utc)
        hold_since = _iso_to_dt(trade.get("opened_at") or trade.get("created_at"))
        if hold_since is not None:
            age_minutes = (ref_dt - hold_since).total_seconds() / 60.0
            if age_minutes > max_hold_minutes:
                result = repo.close_shadow_virtual_trade(trade_id, close_price, "max_hold_expired")
                if result:
                    LOGGER.info(
                        "shadow virtual trade expired (max hold) id=%s symbol=%s age=%.0fm",
                        trade_id, symbol, age_minutes,
                    )
                    return ("closed_max_hold", 1, 0)
                return ("none", 0, 0)

        return ("updated", 0, 0)

    return ("none", 0, 0)


def _persist_cursor(repo: CryptoGuardRepository, trade_id: int, candle: dict[str, Any]) -> None:
    """Persist last_processed_candle_time after processing a candle.

    Stores open_time + 60000ms (start of NEXT 1m candle) to prevent re-processing
    the same candle on the next update cycle. The loop uses < start_time comparison,
    so storing the next candle's timestamp ensures this candle won't match again.
    """
    open_time = candle.get("open_time")
    if open_time is not None:
        next_candle_time = int(open_time) + 60000
        repo.conn.execute(
            "UPDATE shadow_virtual_trades SET last_processed_candle_time=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (next_candle_time, trade_id),
        )
        repo.conn.commit()


# ── main entry point ──────────────────────────────────────────────────────

def update_shadow_virtual_trades(
    repo: CryptoGuardRepository,
    market_data_fetcher: Callable[[str], dict[str, Any]] | None = None,
    *,
    max_pending_minutes: int = DEFAULT_MAX_PENDING_MINUTES,
    max_hold_minutes: int = DEFAULT_MAX_HOLD_MINUTES,
    max_stale_minutes: int = DEFAULT_MAX_STALE_MINUTES,
) -> dict[str, Any]:
    """Main entry point: update all open/pending shadow virtual trades.

    Per-candle sequential replay: fetches candles from last_processed_candle_time
    (or opened_at/created_at) and processes each candle individually in time order.
    This preserves the actual price path and avoids misclassifying "TP then SL"
    as same-candle ambiguous.

    Gap catch-up: fetches up to 500 candles to recover from downtime.

    Returns a summary dict:
        {updated_count, activated_count, closed_count, expired_count, errors: [...]}
    """
    trades = repo.list_open_shadow_virtual_trades()
    if not trades:
        return {
            "updated_count": 0,
            "activated_count": 0,
            "closed_count": 0,
            "expired_count": 0,
            "errors": [],
        }

    updated_count = 0
    activated_count = 0
    closed_count = 0
    expired_count = 0
    errors: list[dict[str, Any]] = []

    # Fetch candles per symbol: keyed by (symbol, start_time_ms) for gap catch-up
    symbol_candle_queues: dict[tuple[str, int], list[dict[str, Any]]] = {}

    def _get_candles_for(symbol: str, start_time_ms: int) -> list[dict[str, Any]]:
        cache_key = (symbol, start_time_ms)
        if cache_key not in symbol_candle_queues:
            if market_data_fetcher is not None:
                try:
                    candle = market_data_fetcher(symbol)
                    symbol_candle_queues[cache_key] = [candle]
                except Exception as exc:
                    LOGGER.warning("market_data_fetcher failed for %s: %s", symbol, exc)
                    # fall through to real fetch
            if cache_key not in symbol_candle_queues:
                try:
                    candles = _fetch_candles_from(symbol, start_time_ms)
                except Exception as exc:
                    LOGGER.warning("fetch_klines failed for %s from %s: %s — preserving cursor for retry",
                                   symbol, start_time_ms, exc)
                    errors.append({"symbol": symbol, "error": str(exc), "stage": "fetch_klines"})
                    symbol_candle_queues[cache_key] = None  # sentinel: fetch failed, skip trade
                else:
                    if not candles:
                        # Gap is caught up — fallback to mark price for current state
                        try:
                            mark_price = float(fetch_mark_price(symbol)["markPrice"])
                        except Exception as exc:
                            LOGGER.warning("fetch_mark_price failed for %s: %s", symbol, exc)
                            errors.append({"symbol": symbol, "error": str(exc), "stage": "fetch_price"})
                            symbol_candle_queues[cache_key] = []
                        else:
                            if mark_price <= 0:
                                LOGGER.warning("mark_price zero or negative for %s: %s — skipping", symbol, mark_price)
                                errors.append({"symbol": symbol, "error": f"mark_price={mark_price}", "stage": "zero_mark_price"})
                                symbol_candle_queues[cache_key] = []
                            else:
                                symbol_candle_queues[cache_key] = [_single_candle_from_mark(symbol, mark_price)]
                    elif len(candles) >= DEFAULT_GAP_CATCHUP_LIMIT:
                        # Pagination: may be more candles to fetch.
                        # Only set what we have — the updater loop will re-enter
                        # _get_candles_for with the updated cursor after processing
                        # these, triggering the next page fetch.
                        symbol_candle_queues[cache_key] = candles
                    else:
                        symbol_candle_queues[cache_key] = candles
        return symbol_candle_queues[cache_key]

    for trade in trades:
        trade_id = int(trade["id"])
        symbol = str(trade["symbol"])
        status = str(trade.get("status", "open"))

        try:
            page_count = 0
            while page_count < MAX_PAGINATION_PAGES:
                start_time_ms = _get_replay_start(trade)
                if start_time_ms is None:
                    LOGGER.warning(
                        "shadow virtual trade has no replay start id=%s symbol=%s — skipping",
                        trade_id, symbol,
                    )
                    break

                candles = _get_candles_for(symbol, start_time_ms)

                if candles is None:
                    # fetch_klines failed — preserve cursor, skip this trade
                    LOGGER.debug(
                        "shadow virtual trade fetch failed id=%s symbol=%s start_time_ms=%s — skipping",
                        trade_id, symbol, start_time_ms,
                    )
                    break

                if not candles:
                    LOGGER.debug(
                        "shadow virtual trade no candles id=%s symbol=%s start_time_ms=%s — skipping",
                        trade_id, symbol, start_time_ms,
                    )
                    break

                for i, candle in enumerate(candles):
                    # Skip candles older than our cursor
                    candle_time = int(candle.get("open_time", 0))
                    if start_time_ms is not None and candle_time < start_time_ms:
                        continue

                    # Only process finalized (is_closed=True) candles from Binance.
                    # Unclosed candles may have their high/low updated later —
                    # processing them would permanently lose subsequent price data.
                    # Mark-price snapshots (is_closed=False) update unrealized PnL
                    # but do NOT advance the cursor.
                    is_closed = candle.get("is_closed", True)  # default True for mock/test candles
                    if not is_closed:
                        # Update unrealized PnL for open trades using mark price snapshot
                        current_status = str(trade.get("status", ""))
                        if current_status == "open":
                            close_price = float(candle.get("close", 0))
                            if close_price > 0:
                                repo.update_shadow_virtual_trade_prices(trade_id, close_price)
                        # Do NOT advance cursor — next update will re-fetch this candle
                        break

                    # Staleness check: only skip if this is the LAST candle (current time)
                    # AND it's stale. Historical candles in the replay queue are always valid.
                    is_last_candle = (i == len(candles) - 1)
                    if is_last_candle and _is_candle_stale(candle, max_stale_minutes):
                        LOGGER.debug(
                            "shadow virtual trade stale candle id=%s symbol=%s candle_time=%s",
                            trade_id, symbol, candle_time,
                        )
                        continue

                    candle_dt = _candle_event_dt(candle)
                    action, closed_delta, activated_delta = _process_candle_for_trade(
                        repo, trade, candle, max_hold_minutes=max_hold_minutes,
                        max_pending_minutes=max_pending_minutes,
                        candle_dt=candle_dt,
                    )

                    if action == "updated":
                        updated_count += 1
                    elif action == "activated":
                        activated_count += activated_delta
                    elif action in ("closed_sl", "closed_tp", "closed_ambiguous", "closed_max_hold"):
                        closed_count += closed_delta
                        activated_count += activated_delta
                    elif action == "expired_pending":
                        expired_count += activated_delta

                    # Persist cursor
                    _persist_cursor(repo, trade_id, candle)

                    # Re-read trade status after processing (may have changed)
                    trade = dict(repo.conn.execute(
                        "SELECT * FROM shadow_virtual_trades WHERE id=?", (trade_id,)
                    ).fetchone() or trade)

                    # Stop processing if trade is no longer open/pending
                    new_status = str(trade.get("status", ""))
                    if new_status not in ("open", "pending_entry"):
                        break

                # Re-read trade status after processing this page
                trade = dict(repo.conn.execute(
                    "SELECT * FROM shadow_virtual_trades WHERE id=?", (trade_id,)
                ).fetchone() or trade)
                new_status = str(trade.get("status", ""))
                if new_status not in ("open", "pending_entry"):
                    break

                # Pagination: if we got a full page, there may be more candles.
                # Clear the cache key so the next iteration re-fetches with the updated cursor.
                if len(candles) >= DEFAULT_GAP_CATCHUP_LIMIT:
                    cache_key = (symbol, start_time_ms)
                    symbol_candle_queues.pop(cache_key, None)
                    page_count += 1
                    continue
                else:
                    break

        except Exception as exc:
            LOGGER.exception(
                "shadow virtual trade update failed id=%s symbol=%s status=%s: %s",
                trade_id, symbol, status, exc,
            )
            errors.append({
                "trade_id": trade_id,
                "symbol": symbol,
                "status": status,
                "error": str(exc),
            })

    return {
        "updated_count": updated_count,
        "activated_count": activated_count,
        "closed_count": closed_count,
        "expired_count": expired_count,
        "errors": errors,
    }
