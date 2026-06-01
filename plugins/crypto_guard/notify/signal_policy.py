from __future__ import annotations

from typing import Any

from plugins.crypto_guard.strategy.grade_config import PUSH_GRADES, WATCH_GRADES, STORE_ONLY_GRADES, alert_level_for_grade


def should_push_signal(decision: dict[str, Any]) -> bool:
    return str(decision.get("signal_grade") or "").upper() in PUSH_GRADES


def can_create_opportunity_watch(decision: dict[str, Any]) -> bool:
    grade = str(decision.get("signal_grade") or "").upper()
    return grade in PUSH_GRADES | WATCH_GRADES and bool(decision.get("opportunity_watch"))
