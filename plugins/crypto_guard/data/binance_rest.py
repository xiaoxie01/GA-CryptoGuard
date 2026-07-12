from __future__ import annotations

import re
import os
import random
import threading
import time
from typing import Any

import requests

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.utils import latest_closed_close_time_ms

BASE_URL = "https://bnapi.01010909.xyz"
_SYMBOL_CACHE: set[str] | None = None
LOGGER = get_logger("crypto_guard.binance")
_HTTP = requests.Session()
_REQ_LOCK = threading.Lock()
_LAST_REQ_AT = 0.0


class MarketDataError(RuntimeError):
    """行情接口异常，供上层返回用户可读提示。"""


def normalize_symbol(input_text: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9]", "", input_text or "").upper()
    if not raw:
        raise ValueError("symbol 不能为空")
    if raw.endswith("USDT"):
        return raw
    return f"{raw}USDT"


def _public_get(path: str, params: dict[str, Any] | None = None) -> Any:
    last_error: Exception | None = None
    url = f"{BASE_URL}{path}"
    max_attempts = int(os.environ.get("CRYPTO_GUARD_BINANCE_RETRIES", "5"))
    timeout = float(os.environ.get("CRYPTO_GUARD_BINANCE_TIMEOUT", "20"))
    for attempt in range(1, max_attempts + 1):
        try:
            _throttle_public_request()
            response = _HTTP.get(url, params=params or {}, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            LOGGER.warning("Binance public request failed attempt=%s path=%s params=%s error=%s", attempt, path, params, exc)
            if attempt < max_attempts:
                time.sleep(_retry_delay(attempt))
    raise MarketDataError(f"Binance public 行情请求失败：{last_error}") from last_error


def _throttle_public_request() -> None:
    """Global process-level throttle for Binance public endpoints."""

    global _LAST_REQ_AT
    min_interval = float(os.environ.get("CRYPTO_GUARD_BINANCE_MIN_INTERVAL", "0.25"))
    with _REQ_LOCK:
        now = time.monotonic()
        wait = _LAST_REQ_AT + min_interval - now
        if wait > 0:
            time.sleep(wait)
        _LAST_REQ_AT = time.monotonic()


def _retry_delay(attempt: int) -> float:
    base = float(os.environ.get("CRYPTO_GUARD_BINANCE_RETRY_BASE", "0.8"))
    cap = float(os.environ.get("CRYPTO_GUARD_BINANCE_RETRY_CAP", "8.0"))
    return min(cap, base * (2 ** (attempt - 1))) + random.uniform(0.0, 0.4)


def exchange_symbols(force_refresh: bool = False) -> set[str]:
    global _SYMBOL_CACHE
    if _SYMBOL_CACHE is not None and not force_refresh:
        return _SYMBOL_CACHE
    data = _public_get("/fapi/v1/exchangeInfo")
    symbols = set()
    for item in data.get("symbols", []):
        if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT" and item.get("status") == "TRADING":
            symbols.add(str(item["symbol"]))
    _SYMBOL_CACHE = symbols
    return symbols


def validate_um_futures_symbol(symbol: str) -> bool:
    return normalize_symbol(symbol) in exchange_symbols()


def fetch_klines(
    symbol: str,
    interval: str,
    start_time: int | None = None,
    end_time: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    norm = normalize_symbol(symbol)
    params: dict[str, Any] = {"symbol": norm, "interval": interval, "limit": min(int(limit), 1500)}
    if start_time is not None:
        params["startTime"] = int(start_time)
    if end_time is not None:
        params["endTime"] = int(end_time)
    rows = _public_get("/fapi/v1/klines", params)
    closed_cutoff = end_time if end_time is not None else latest_closed_close_time_ms(interval)
    candles = []
    for row in rows:
        close_time = int(row[6])
        candles.append(
            {
                "symbol": norm,
                "interval": interval,
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": close_time,
                "quote_volume": float(row[7]),
                "trade_count": int(row[8]),
                "taker_buy_volume": float(row[9]),
                "taker_buy_quote_volume": float(row[10]),
                "is_closed": close_time <= int(closed_cutoff),
                "source": "binance",
            }
        )
    return candles


def fetch_closed_klines(symbol: str, interval: str, *, analysis_time_utc: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
    # analysis_time_utc 可能已经是某根 K 线 close_time，不能再向前取一根。
    end_time = int(analysis_time_utc) if analysis_time_utc is not None else latest_closed_close_time_ms(interval)
    return [c for c in fetch_klines(symbol, interval, end_time=end_time, limit=limit) if c["is_closed"]]


def fetch_mark_price(symbol: str) -> dict[str, Any]:
    norm = normalize_symbol(symbol)
    return _public_get("/fapi/v1/premiumIndex", {"symbol": norm})


def fetch_funding_rate(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    norm = normalize_symbol(symbol)
    return _public_get("/fapi/v1/fundingRate", {"symbol": norm, "limit": limit})
