"""Account feedback gate: controlled execution for account-level risk rules.

Checks recent account-level feedback patterns (consecutive_stop_losses,
daily_loss_threshold) and applies quality gates before paper order creation.

Current mode: shadow/annotate_only — records gate results but does not block orders.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.migrations import check_schema_health
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.account_feedback_gate")


def check_account_feedback_gate(
    repo: CryptoGuardRepository,
    symbol: str,
    side: str,
    confidence: float,
    entry_quality: float | None = None,
) -> dict[str, Any]:
    """Check account feedback gate before paper order creation.

    Args:
        repo: Database repository
        symbol: Trading symbol (e.g., "BTCUSDT")
        side: Trade side ("LONG" or "SHORT")
        confidence: Decision confidence (0-1)
        entry_quality: Entry quality score (0-1, optional)

    Returns:
        {
            "ok": bool,
            "active": bool,  # Whether gate is active (pattern detected)
            "action": str,   # Action being checked
            "required": {"min_confidence": float, "min_entry_quality": float},
            "actual": {"confidence": float, "entry_quality": float},
            "passed": bool,  # Whether current trade passes the gate
            "decision": str, # "annotate_only" / "downgrade_to_watch" / "block_order"
            "reason": str,
            "lookback_hours": int,
            "events_matched": int,
        }
    """
    # Schema health guard
    schema = check_schema_health()
    if not schema["ok"]:
        return {"ok": False, "error": "schema_unhealthy", "active": False, "passed": True}

    # Load config
    cfg = load_config().trading_mode
    gate_cfg = cfg.get("account_feedback_rules", {})

    if not gate_cfg.get("enabled", False):
        return {"ok": True, "active": False, "passed": True, "decision": "disabled"}

    mode = gate_cfg.get("mode", "shadow")
    lookback_hours = int(gate_cfg.get("lookback_hours", 24))

    # Get action config
    actions_cfg = gate_cfg.get("actions", {})
    confirm_cfg = actions_cfg.get("require_stronger_confirmation", {})

    if not confirm_cfg.get("enabled", False):
        return {"ok": True, "active": False, "passed": True, "decision": "action_disabled"}

    min_confidence = float(confirm_cfg.get("min_confidence", 0.80))
    min_entry_quality = float(confirm_cfg.get("min_entry_quality", 0.70))
    on_fail = confirm_cfg.get("on_fail", "annotate_only")

    # Query recent consecutive_stop_losses events
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat().replace("+00:00", "Z")

    # Check if there are recent consecutive_stop_losses events affecting this symbol/side
    events = repo.conn.execute(
        """
        SELECT sfm.id, sfm.pattern_type, sfm.created_at,
               sp.candidate_version, et.related_trade_ids
        FROM skill_feedback_memory sfm
        LEFT JOIN strategy_patches sp ON sp.id = json_extract(sfm.suggested_adjustment_json, '$.candidate_patch_id')
        LEFT JOIN evolution_triggers et ON et.id = sp.trigger_id
        WHERE sfm.source_type = 'evolution_trigger'
          AND sfm.pattern_type = 'consecutive_stop_losses'
          AND datetime(sfm.created_at) >= datetime(?)
        ORDER BY sfm.created_at DESC
        """,
        (cutoff,),
    ).fetchall()

    if not events:
        return {
            "ok": True,
            "active": False,
            "passed": True,
            "decision": "no_recent_pattern",
            "events_matched": 0,
        }

    # Check if affected trades include current symbol/side
    affected_symbols, affected_sides = _get_affected_symbols_sides(repo, events)

    # Gate is active if:
    # 1. There are recent consecutive_stop_losses events
    # 2. Current symbol/side is in affected scope (or scope is "all")
    scope = confirm_cfg.get("affected_scope", "trigger_related_symbols")
    is_affected = (
        scope == "all"
        or (symbol in affected_symbols and side in affected_sides)
        or (symbol in affected_symbols and not affected_sides)
        or (not affected_symbols and side in affected_sides)
    )

    if not is_affected:
        return {
            "ok": True,
            "active": False,
            "passed": True,
            "decision": "not_affected",
            "events_matched": len(events),
            "affected_symbols": affected_symbols,
            "affected_sides": affected_sides,
        }

    # Gate is active - check thresholds
    actual = {
        "confidence": confidence,
        "entry_quality": entry_quality,
    }
    required = {
        "min_confidence": min_confidence,
        "min_entry_quality": min_entry_quality,
    }

    # Check if current trade passes the gate
    confidence_ok = confidence >= min_confidence
    quality_ok = entry_quality is None or entry_quality >= min_entry_quality
    passed = confidence_ok and quality_ok

    # Build reason
    reasons = []
    if not confidence_ok:
        reasons.append(f"confidence {confidence:.2f} < {min_confidence:.2f}")
    if not quality_ok:
        reasons.append(f"entry_quality {entry_quality:.2f} < {min_entry_quality:.2f}")

    result = {
        "ok": True,
        "active": True,
        "action": "require_stronger_confirmation",
        "required": required,
        "actual": actual,
        "passed": passed,
        "decision": on_fail if not passed else "passed",
        "reason": "; ".join(reasons) if reasons else "thresholds met",
        "lookback_hours": lookback_hours,
        "events_matched": len(events),
        "affected_symbols": affected_symbols,
        "affected_sides": affected_sides,
        "mode": mode,
    }

    # Log based on mode
    if mode == "shadow":
        LOGGER.info(
            "account_feedback_gate [shadow]: symbol=%s side=%s passed=%s events=%d reason=%s",
            symbol, side, passed, len(events), result["reason"],
        )
    elif mode == "controlled" and not passed:
        LOGGER.warning(
            "account_feedback_gate [controlled]: symbol=%s side=%s decision=%s reason=%s",
            symbol, side, on_fail, result["reason"],
        )

    return result


def _get_affected_symbols_sides(
    repo: CryptoGuardRepository,
    events: list[Any],
) -> tuple[list[str], list[str]]:
    """Get affected symbols and sides from event-related trades."""
    symbols: set[str] = set()
    sides: set[str] = set()

    for event in events:
        trade_ids_str = event["related_trade_ids"] if "related_trade_ids" in event.keys() else None
        if not trade_ids_str:
            continue

        try:
            trade_ids = json.loads(trade_ids_str)
        except (json.JSONDecodeError, TypeError):
            continue

        if not trade_ids:
            continue

        # Get symbols/sides from paper_trades
        placeholders = ",".join("?" for _ in trade_ids[:50])
        try:
            rows = repo.conn.execute(
                f"""
                SELECT DISTINCT symbol, side
                FROM paper_trades
                WHERE id IN ({placeholders})
                """,
                trade_ids[:50],
            ).fetchall()

            for r in rows:
                if r["symbol"]:
                    symbols.add(r["symbol"])
                if r["side"]:
                    sides.add(r["side"])
        except Exception:
            pass

    return sorted(symbols), sorted(sides)
