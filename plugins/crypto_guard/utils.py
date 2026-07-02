from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any


INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def utc_ms() -> int:
    return int(time.time() * 1000)


def iso_utc_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def latest_closed_close_time_ms(interval: str, now_ms: int | None = None) -> int:
    """返回最近一根已收盘 K 线的 Binance close_time，永远不含当前未收盘 K 线。"""

    span = INTERVAL_MS[interval]
    now = utc_ms() if now_ms is None else int(now_ms)
    current_open = math.floor(now / span) * span
    return current_open - 1


def _strict_positive_int_ms(value: Any) -> int | None:
    """R11/R12: strict positive integer parser — rejects float/bool/NaN/Infinity/string/None/non-positive.

    Only accepts a real int (not a bool subclass) that is > 0. All other
    types return None so callers can fail-closed.

    Key constraint: ``isinstance(True, int) is True`` in Python, so the
    bool check MUST come before the int check.

    R12: single source of truth — risk_engine, ga_judge, llm_agent_judge
    all import this function from here to prevent contract drift.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value


def latest_closed_open_time_ms(interval: str, now_ms: int | None = None) -> int:
    close_time = latest_closed_close_time_ms(interval, now_ms)
    return close_time - INTERVAL_MS[interval] + 1
