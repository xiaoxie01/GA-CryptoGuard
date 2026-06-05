"""Account feedback gate: controlled execution for account-level risk rules.

Checks recent account-level feedback patterns (consecutive_stop_losses,
daily_loss_threshold) and applies quality gates before paper order creation.

Modes:
- shadow: records gate results but does not block orders
- annotate_only: records and annotates but no behavior change
- downgrade_to_watch: blocks order and creates opportunity watch
- block_order: blocks order with explicit audit reason
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
            "active": bool,
            "action": str,
            "required": {"min_confidence": float, "min_entry_quality": float},
            "actual": {"confidence": float, "entry_quality": float},
            "passed": bool,
            "decision": str,
            "would_decide": str,  # shadow mode: what controlled mode would decide
            "reason": str,
            "lookback_hours": int,
            "events_matched": int,
            "affected_pairs": [{"symbol": str, "side": str}],
            "entry_quality_status": str,  # "ok" / "below_threshold" / "data_quality_insufficient"
            "mode": str,
        }
    """
    # Schema health guard
    schema = check_schema_health()
    if not schema["ok"]:
        return {"ok": False, "error": "schema_unhealthy", "active": False, "passed": True, "decision": "error", "would_decide": "error"}

    # Load config
    cfg = load_config().trading_mode
    gate_cfg = cfg.get("account_feedback_rules", {})

    if not gate_cfg.get("enabled", False):
        return {"ok": True, "active": False, "passed": True, "decision": "disabled", "would_decide": "disabled"}

    mode = gate_cfg.get("mode", "shadow")
    lookback_hours = int(gate_cfg.get("lookback_hours", 24))
    scope = gate_cfg.get("affected_scope", "trigger_related_symbols")

    # Get action config
    actions_cfg = gate_cfg.get("actions", {})
    confirm_cfg = actions_cfg.get("require_stronger_confirmation", {})

    if not confirm_cfg.get("enabled", False):
        return {"ok": True, "active": False, "passed": True, "decision": "action_disabled", "would_decide": "action_disabled"}

    min_confidence = float(confirm_cfg.get("min_confidence", 0.80))
    min_entry_quality = float(confirm_cfg.get("min_entry_quality", 0.70))
    on_fail = confirm_cfg.get("on_fail", "annotate_only")

    # Query recent consecutive_stop_losses events
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat().replace("+00:00", "Z")

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
            "would_decide": "no_recent_pattern",
            "events_matched": 0,
            "mode": mode,
        }

    # Check if affected trades include current symbol/side (paired, not cross-product)
    affected_pairs = _get_affected_symbol_side_pairs(repo, events)

    # Gate is active if scope matches
    is_affected = (
        scope == "all"
        or {"symbol": symbol, "side": side} in affected_pairs
        or (not affected_pairs and scope == "trigger_related_symbols")
    )

    if not is_affected:
        return {
            "ok": True,
            "active": False,
            "passed": True,
            "decision": "not_affected",
            "would_decide": "not_affected",
            "events_matched": len(events),
            "affected_pairs": affected_pairs,
            "mode": mode,
        }

    # Gate is active — check thresholds
    actual = {
        "confidence": confidence,
        "entry_quality": entry_quality,
    }
    required = {
        "min_confidence": min_confidence,
        "min_entry_quality": min_entry_quality,
    }

    # Check confidence threshold
    confidence_ok = confidence >= min_confidence

    # Check entry quality — missing quality fails closed in controlled mode
    entry_quality_status = "ok"
    if entry_quality is None:
        if mode == "shadow":
            quality_ok = True
            entry_quality_status = "data_quality_insufficient"
        else:
            quality_ok = False
            entry_quality_status = "data_quality_insufficient"
    elif entry_quality < min_entry_quality:
        quality_ok = False
        entry_quality_status = "below_threshold"
    else:
        quality_ok = True

    passed = confidence_ok and quality_ok

    # Build reason
    reasons = []
    if not confidence_ok:
        reasons.append(f"confidence {confidence:.2f} < {min_confidence:.2f}")
    if entry_quality_status == "data_quality_insufficient":
        reasons.append("entry_quality missing (data_quality_insufficient)")
    elif not quality_ok:
        reasons.append(f"entry_quality {entry_quality:.2f} < {min_entry_quality:.2f}")

    # Determine what controlled mode would decide
    would_decide = on_fail if not passed else "passed"

    # In shadow mode, record what would happen but don't enforce
    # In controlled mode, the decision IS the enforcement
    decision = would_decide if mode != "shadow" else ("shadow_" + would_decide if not passed else "passed")

    result = {
        "ok": True,
        "active": True,
        "action": "require_stronger_confirmation",
        "required": required,
        "actual": actual,
        "passed": passed,
        "decision": decision,
        "would_decide": would_decide,
        "reason": "; ".join(reasons) if reasons else "thresholds met",
        "lookback_hours": lookback_hours,
        "events_matched": len(events),
        "affected_pairs": affected_pairs,
        "entry_quality_status": entry_quality_status,
        "mode": mode,
    }

    # Log based on mode
    if mode == "shadow":
        LOGGER.info(
            "account_feedback_gate [shadow]: symbol=%s side=%s passed=%s would_decide=%s events=%d reason=%s",
            symbol, side, passed, would_decide, len(events), result["reason"],
        )
    elif mode == "controlled" and not passed:
        LOGGER.warning(
            "account_feedback_gate [controlled]: symbol=%s side=%s decision=%s reason=%s",
            symbol, side, would_decide, result["reason"],
        )

    return result


def _get_affected_symbol_side_pairs(
    repo: CryptoGuardRepository,
    events: list[Any],
) -> list[dict[str, str]]:
    """Get affected symbol-side pairs from event-related trades.

    Returns paired records [{"symbol": "BTCUSDT", "side": "LONG"}], not
    independent sets, to prevent cross-product false positives.
    """
    pairs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

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

        # Get symbol-side pairs from paper_trades
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
                sym = r["symbol"]
                sd = r["side"]
                if sym and sd:
                    key = (sym, sd)
                    if key not in seen:
                        seen.add(key)
                        pairs.append({"symbol": sym, "side": sd})
        except Exception:
            pass

    return pairs
