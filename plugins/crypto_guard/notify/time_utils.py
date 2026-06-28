"""Shared UTC+8 time formatting for all paper-trading notifications.

Every paper-trading notification must contain an explicit UTC+8 event time.
This module provides the single formatter — never inline strftime + (UTC+8)
manually, and never double-add (UTC+8).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))


def format_event_time_cst(dt: datetime | str | int | float | None) -> str:
    """Format a datetime to 'YYYY-MM-DD HH:mm:ss (UTC+8)'.

    - Naive datetimes are treated as UTC.
    - String inputs are parsed as ISO8601.
    - int/float inputs auto-detect seconds vs milliseconds (threshold 1e12).
    - Returns '不可用' if None or unparseable.
    """
    if dt is None:
        return "不可用"

    try:
        if isinstance(dt, datetime):
            parsed = dt
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        elif isinstance(dt, bool):
            return "不可用"
        elif isinstance(dt, (int, float)):
            # Auto-detect seconds vs milliseconds:
            # Values >= 1e12 are milliseconds; values < 1e12 are seconds.
            ts = float(dt)
            if ts >= 1_000_000_000_000:
                ts = ts / 1000
            parsed = datetime.fromtimestamp(ts, timezone.utc)
        elif isinstance(dt, str):
            text = dt.strip()
            if not text:
                return "不可用"
            if text.isdigit():
                ts = int(text)
                if ts >= 1_000_000_000_000:
                    ts = ts // 1000
                parsed = datetime.fromtimestamp(ts, timezone.utc)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            return "不可用"

        cst_time = parsed.astimezone(CST)
        return cst_time.strftime("%Y-%m-%d %H:%M:%S") + " (UTC+8)"
    except (ValueError, TypeError, OverflowError):
        return "不可用"


def format_event_time_cst_compact(dt: datetime | str | int | float | None) -> str:
    """Format to 'YYYY-MM-DD HH:MM (UTC+8)' (no seconds)."""
    if dt is None:
        return "不可用"

    try:
        if isinstance(dt, datetime):
            parsed = dt
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        elif isinstance(dt, bool):
            return "不可用"
        elif isinstance(dt, (int, float)):
            ts = float(dt)
            if ts >= 1_000_000_000_000:
                ts = ts / 1000
            parsed = datetime.fromtimestamp(ts, timezone.utc)
        elif isinstance(dt, str):
            text = dt.strip()
            if not text:
                return "不可用"
            if text.isdigit():
                ts = int(text)
                if ts >= 1_000_000_000_000:
                    ts = ts // 1000
                parsed = datetime.fromtimestamp(ts, timezone.utc)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            return "不可用"

        cst_time = parsed.astimezone(CST)
        return cst_time.strftime("%Y-%m-%d %H:%M") + " (UTC+8)"
    except (ValueError, TypeError, OverflowError):
        return "不可用"


def format_event_time_cst_for_line(dt: Any) -> str:
    """Format for a notification detail line: '时间：YYYY-MM-DD HH:mm:ss (UTC+8)'."""
    return f"时间：{format_event_time_cst(dt)}"


def now_cst_iso() -> str:
    """Return current UTC+8 time as ISO8601 string."""
    return datetime.now(timezone.utc).astimezone(CST).isoformat()
