"""Fresh Binance USDⓈ-M Futures mark price fetcher.

Provides a single source of truth for execution prices in paper trading.
All financial actions (close, stop adjustment, profit protection) must use
this module to get the current mark price.

Contracts:
- Never use 1h candle close as execution price.
- Never use entry_price as current price.
- Fail-closed: if live fetch fails and cached price is stale, return error.
- Runner-level cache: within a single update_paper_positions() call,
  the same symbol is only fetched once.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from plugins.crypto_guard.data.binance_rest import fetch_mark_price
from plugins.crypto_guard.logging_utils import get_logger

LOGGER = get_logger("crypto_guard.mark_price")

# Module-level cache: cleared per cycle by the caller.
# Keyed by symbol, stores the last fetch result for the current cycle.
_cycle_cache: dict[str, dict[str, Any]] = {}


def clear_cycle_cache() -> None:
    """Clear the per-cycle mark price cache. Call at the start of each update cycle."""
    _cycle_cache.clear()


def fetch_binance_mark_price(
    symbol: str,
    *,
    config: Any = None,
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch current mark price from Binance USDⓈ-M Futures.

    Uses a runner-level cache so the same symbol is only fetched once per
    update_paper_positions() call. Pass a dict as `cache` to share across
    multiple calls within the same cycle, or rely on the module-level cache.

    Args:
        symbol: Trading pair symbol (e.g. "XRPUSDT").
        config: Optional config object (reserved for future use).
        cache: Optional shared cache dict for the current cycle.

    Returns:
        {"ok": True, "mark_price": float, "price_source": "binance_usdm_mark",
         "price_as_of": str(ISO8601), "price_age_seconds": 0.0}
        or {"ok": False, "error": str, "price_age_seconds": float}
    """
    effective_cache = cache if cache is not None else _cycle_cache

    # Check runner-level cache first
    if symbol in effective_cache:
        cached = effective_cache[symbol]
        if cached.get("ok"):
            return cached

    try:
        result = fetch_mark_price(symbol)
        mark_price = float(result["markPrice"])
        if mark_price <= 0:
            raise ValueError(f"mark_price must be positive, got {mark_price}")

        now_utc = datetime.now(timezone.utc)
        # Use Binance server time if available, otherwise fall back to now
        binance_time = result.get("time")
        if binance_time is not None:
            # Binance returns Unix ms timestamp
            server_dt = datetime.fromtimestamp(float(binance_time) / 1000, timezone.utc)
            price_as_of = server_dt.isoformat()
            price_age_seconds = (now_utc - server_dt).total_seconds()
        else:
            # Binance server time missing — unverifiable freshness, fail-closed
            LOGGER.warning(
                "fetch_binance_mark_price: missing Binance server time for %s; returning error",
                symbol,
            )
            return {
                "ok": False,
                "error": "missing_binance_time",
                "price_age_seconds": -1.0,
            }

        # Reject clearly future server time (beyond 10s clock drift) — untrustworthy.
        # Do NOT cache as success so the next call can retry.
        if price_age_seconds < -10:
            LOGGER.warning(
                "fetch_binance_mark_price: future Binance time for %s (age=%.2fs, as_of=%s)",
                symbol, price_age_seconds, price_as_of,
            )
            return {
                "ok": False,
                "error": "future_binance_time",
                "price_as_of": price_as_of,
                "price_age_seconds": price_age_seconds,
            }

        # Stale-but-unacceptable: fail-closed. Live price with a server timestamp
        # over 90s old is not trustworthy for financial actions.
        if price_age_seconds > 90:
            LOGGER.warning(
                "fetch_binance_mark_price: stale Binance time for %s (age=%.2fs > 90s); returning error",
                symbol, price_age_seconds,
            )
            return {
                "ok": False,
                "error": "stale_binance_time",
                "price_as_of": price_as_of,
                "price_age_seconds": price_age_seconds,
            }

        response = {
            "ok": True,
            "mark_price": mark_price,
            "price_source": "binance_usdm_mark",
            "price_as_of": price_as_of,
            "price_age_seconds": price_age_seconds,
        }
        effective_cache[symbol] = response
        return response

    except Exception as exc:
        LOGGER.warning("fetch_binance_mark_price failed for %s: %s", symbol, exc)
        error_response = {
            "ok": False,
            "error": str(exc),
            "price_age_seconds": -1.0,
        }
        effective_cache[symbol] = error_response
        return error_response


def get_mark_price_with_fallback(
    symbol: str,
    *,
    repo: Any = None,
    cache: dict[str, dict[str, Any]] | None = None,
    max_cache_age_seconds: float = 90.0,
) -> dict[str, Any]:
    """Fetch mark price with fallback to paper_positions.current_price.

    Priority:
    1. Live Binance mark price (fresh fetch).
    2. paper_positions.current_price if price_as_of is within max_cache_age_seconds.
    3. Fail-closed: return error, caller must set needs_position_recheck.

    Args:
        symbol: Trading pair symbol.
        repo: CryptoGuardRepository instance (needed for fallback).
        cache: Optional shared cache dict for the current cycle.
        max_cache_age_seconds: Maximum age of cached price to accept.

    Returns:
        Same shape as fetch_binance_mark_price, with additional fields on fallback.
    """
    # Try live fetch first
    result = fetch_binance_mark_price(symbol, cache=cache)
    if result.get("ok"):
        return result

    # Fallback: check paper_positions.current_price
    if repo is not None:
        try:
            pos_row = repo.conn.execute(
                """SELECT current_price, updated_at
                   FROM paper_positions
                   WHERE symbol=? AND status='open'
                   ORDER BY updated_at DESC LIMIT 1""",
                (symbol,),
            ).fetchone()

            if pos_row and pos_row["current_price"] is not None:
                price_as_of = pos_row["updated_at"]
                if price_as_of:
                    try:
                        dt = datetime.fromisoformat(str(price_as_of).replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        age = (datetime.now(timezone.utc) - dt).total_seconds()
                        if age <= max_cache_age_seconds:
                            return {
                                "ok": True,
                                "mark_price": float(pos_row["current_price"]),
                                "price_source": "paper_position_cache",
                                "price_as_of": str(price_as_of),
                                "price_age_seconds": age,
                            }
                    except (ValueError, TypeError):
                        pass
        except Exception as exc:
            LOGGER.debug("mark_price fallback query failed for %s: %s", symbol, exc)

    # Fail-closed: no fresh price available
    return {
        "ok": False,
        "error": "stale_price",
        "price_age_seconds": -1.0,
    }
