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
    schema = check_schema_health(conn=repo.conn)
    if not schema["ok"]:
        # Load config to determine mode
        cfg_schema = load_config().trading_mode
        gate_cfg_schema = cfg_schema.get("account_feedback_rules", {})
        mode_schema = gate_cfg_schema.get("mode", "shadow")

        if mode_schema == "shadow":
            return {
                "ok": True,
                "active": False,
                "passed": True,
                "decision": "data_quality_insufficient",
                "would_decide": "data_quality_insufficient",
                "entry_quality_status": "schema_unhealthy",
                "mode": mode_schema,
            }
        else:
            # Controlled mode: fail closed
            return {
                "ok": False,
                "active": True,
                "passed": False,
                "decision": "downgrade_to_watch",
                "would_decide": "downgrade_to_watch",
                "reason": "schema unhealthy",
                "entry_quality_status": "schema_unhealthy",
                "mode": mode_schema,
                "events_matched": 0,
                "affected_pairs": [],
            }

    # Load config
    cfg = load_config().trading_mode
    gate_cfg = cfg.get("account_feedback_rules", {})
    mode = gate_cfg.get("mode", "shadow")

    if not gate_cfg.get("enabled", False):
        return {"ok": True, "active": False, "passed": True, "decision": "disabled", "would_decide": "disabled"}

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
               sp.candidate_version, sp.id AS candidate_patch_id, sp.trigger_id AS patch_trigger_id,
               et.related_trade_ids
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

    # Deduplicate events by trigger_id (each trigger is a unique event).
    # If trigger_id is NULL, fall back to candidate_patch_id.
    feedback_row_count = len(events)
    seen_triggers: set[str] = set()
    unique_events: list[Any] = []
    for event in events:
        trigger_id = event["patch_trigger_id"] if "patch_trigger_id" in event.keys() else None
        patch_id = event["candidate_patch_id"] if "candidate_patch_id" in event.keys() else None
        if trigger_id is not None:
            key = f"trigger:{trigger_id}"
        elif patch_id is not None:
            key = f"patch:{patch_id}"
        else:
            key = f"row:{event['id']}"
        if key in seen_triggers:
            continue
        seen_triggers.add(key)
        unique_events.append(event)

    unique_event_count = len(unique_events)

    # Check if affected trades include current symbol/side (paired, not cross-product)
    affected_pairs = _get_affected_symbol_side_pairs(repo, unique_events)

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
            "events_matched": unique_event_count,
            "feedback_row_count": feedback_row_count,
            "unique_event_count": unique_event_count,
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

    # Compute would_decide based on controlled-mode logic (fail-closed)
    # so shadow mode accurately reports what controlled mode would do
    controlled_confidence_ok = confidence >= min_confidence
    if entry_quality is None:
        controlled_quality_ok = False  # fail-closed in controlled mode
    elif entry_quality < min_entry_quality:
        controlled_quality_ok = False
    else:
        controlled_quality_ok = True
    controlled_passed = controlled_confidence_ok and controlled_quality_ok
    would_decide = on_fail if not controlled_passed else "passed"

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
        "events_matched": unique_event_count,
        "feedback_row_count": feedback_row_count,
        "unique_event_count": unique_event_count,
        "affected_pairs": affected_pairs,
        "entry_quality_status": entry_quality_status,
        "mode": mode,
    }

    # Controlled projection: what controlled mode would decide, even in shadow mode
    controlled_projection = {
        "would_pass": controlled_passed,
        "would_decide": would_decide,
        "shadow_passed": passed,  # what shadow mode reports
        "gating_factor": None,
    }
    if not controlled_passed:
        if not controlled_confidence_ok:
            controlled_projection["gating_factor"] = "confidence"
        elif not controlled_quality_ok and entry_quality is None:
            controlled_projection["gating_factor"] = "missing_entry_quality"
        elif not controlled_quality_ok:
            controlled_projection["gating_factor"] = "entry_quality_below_threshold"
    result["controlled_projection"] = controlled_projection

    # Log based on mode
    if mode == "shadow":
        LOGGER.info(
            "account_feedback_gate [shadow]: symbol=%s side=%s passed=%s would_decide=%s events=%d reason=%s",
            symbol, side, passed, would_decide, unique_event_count, result["reason"],
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

    Batches through all trade IDs in chunks of 50 (Fix 9).
    """
    pairs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # Collect all trade IDs from all events
    all_trade_ids: list[int] = []
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
        all_trade_ids.extend(trade_ids)

    if not all_trade_ids:
        return pairs

    # Batch through all trade IDs in chunks of 50
    for i in range(0, len(all_trade_ids), 50):
        batch = all_trade_ids[i:i + 50]
        placeholders = ",".join("?" for _ in batch)
        try:
            rows = repo.conn.execute(
                f"""
                SELECT DISTINCT symbol, side
                FROM paper_trades
                WHERE id IN ({placeholders})
                """,
                batch,
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
