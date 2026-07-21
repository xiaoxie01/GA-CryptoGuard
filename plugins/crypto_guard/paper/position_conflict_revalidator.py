from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.paper.mark_price import get_mark_price_with_fallback, clear_cycle_cache
from plugins.crypto_guard.notify.time_utils import format_event_time_cst, format_event_time_cst_compact
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.position_conflict")


def run_position_conflict_revalidation(
    repo: CryptoGuardRepository,
    *,
    symbol: str | None = None,
    ga_decision_id: int | None = None,
    send_message: Any = None,
) -> dict[str, Any]:
    """Revalidate open paper trades against latest GA decisions for direction conflicts.

    Unlike the passive alert in run_ga_workers.py, this function takes conservative,
    auditable actions on open positions when the latest GA decision conflicts with
    the position direction.

    Returns:
        {ok, checked_count, conflict_count, closed_count, stop_adjusted_count,
         recheck_count, skipped_count, actions: [...]}
    """
    cfg = load_config()
    pos_conflict_cfg = (cfg.trading_mode.get("position_conflict") or {})
    if not pos_conflict_cfg.get("enabled", True):
        return {
            "ok": True, "checked_count": 0, "conflict_count": 0,
            "closed_count": 0, "stop_adjusted_count": 0,
            "recheck_count": 0, "skipped_count": 0,
            "actions": [], "disabled": True,
        }

    min_confidence = pos_conflict_cfg.get("min_confidence", {})
    s_confidence = float(min_confidence.get("S", 0.85))
    a_confidence = float(min_confidence.get("A", 0.78))
    b_confidence = float(min_confidence.get("B", 0.72))
    strong_confirmations = int(pos_conflict_cfg.get("strong_conflict_confirmations", 2))
    early_exit_min_adverse_r = float(pos_conflict_cfg.get("early_exit_min_adverse_r", -0.30))
    signal_decay_exit_threshold = float(pos_conflict_cfg.get("signal_decay_exit_threshold", 0.70))
    breakeven_mfe_r = float(pos_conflict_cfg.get("breakeven_on_conflict_if_mfe_r", 0.10))
    min_hold_minutes = int(pos_conflict_cfg.get("min_hold_minutes", 15))
    min_current_r_for_breakeven = float(pos_conflict_cfg.get("min_current_r_for_breakeven", 0.50))
    min_mfe_r_for_breakeven = float(pos_conflict_cfg.get("min_mfe_r_for_breakeven", 0.75))
    reverse_confirmations_for_tighten = int(pos_conflict_cfg.get("reverse_confirmations_for_tighten", 2))
    notify_actions = bool(pos_conflict_cfg.get("notify_actions", True))

    open_trades = repo.list_open_paper_trades()
    if symbol:
        open_trades = [t for t in open_trades if t.get("symbol") == symbol]

    actions: list[dict[str, Any]] = []
    checked_count = 0
    conflict_count = 0
    closed_count = 0
    stop_adjusted_count = 0
    recheck_count = 0
    skipped_count = 0

    # Per-cycle mark price cache
    clear_cycle_cache()
    mark_price_cache: dict[str, dict[str, Any]] = {}

    for trade in open_trades:
        trade = dict(trade)
        trade_id = int(trade["id"])
        trade_symbol = str(trade["symbol"])
        pos_side = str(trade["side"] or "").upper()

        # Get GA decision for this symbol — prefer the passed ga_decision_id
        if ga_decision_id is not None:
            latest_decision_row = repo.conn.execute(
                "SELECT * FROM ga_decisions WHERE id=%s AND symbol=%s",
                (ga_decision_id, trade_symbol),
            ).fetchone()
        else:
            latest_decision_row = None

        if latest_decision_row is None:
            # R13 P1 defense-in-depth: order by ``analysis_time`` (INTEGER
            # NOT NULL) instead of ``analysis_time_utc`` (TEXT) for the
            # same reason as ``pending_order_manager.py:122`` — canonical
            # chronological key, immune to ``analysis_time_utc`` regressions.
            latest_decision_row = repo.conn.execute(
                "SELECT * FROM ga_decisions WHERE symbol=%s ORDER BY analysis_time DESC, id DESC LIMIT 1",
                (trade_symbol,),
            ).fetchone()

        if not latest_decision_row:
            continue

        checked_count += 1
        latest_decision = dict(latest_decision_row)
        bias = str(latest_decision.get("market_bias") or "neutral").lower()
        grade = str(latest_decision.get("signal_grade") or "D").upper()
        confidence = float(latest_decision.get("confidence") or 0)
        ga_dec_id = int(latest_decision.get("id", 0))

        # Determine if direction conflict exists
        is_conflict = False
        if pos_side == "SHORT" and bias == "bullish":
            is_conflict = True
        elif pos_side == "LONG" and bias == "bearish":
            is_conflict = True

        if not is_conflict:
            skipped_count += 1
            action = _build_skipped_action(trade, latest_decision, "no_conflict")
            actions.append(action)
            continue

        # Conflict exists — check if grade is strong enough
        if grade not in {"S", "A", "B"}:
            skipped_count += 1
            action = _build_skipped_action(trade, latest_decision, f"grade_too_low:{grade}")
            actions.append(action)
            continue

        # Check confidence threshold per grade
        grade_thresholds = {"S": s_confidence, "A": a_confidence, "B": b_confidence}
        required_conf = grade_thresholds.get(grade, 0.80)
        if confidence < required_conf:
            skipped_count += 1
            action = _build_skipped_action(
                trade, latest_decision,
                f"confidence_below_threshold:{confidence:.2f}<{required_conf:.2f}",
            )
            actions.append(action)
            continue

        conflict_count += 1

        # Get current price once for all downstream checks — retain full quote
        # metadata (mark_price/price_source/price_as_of/price_age_seconds) so
        # it can be threaded into conflict action logs and notifications.
        mp_result = get_mark_price_with_fallback(trade_symbol, repo=repo, cache=mark_price_cache)
        if mp_result.get("ok"):
            current_price = float(mp_result["mark_price"])
            price_meta = {
                "mark_price": float(mp_result["mark_price"]),
                "price_source": mp_result.get("price_source", "binance_usdm_mark"),
                "price_as_of": mp_result.get("price_as_of"),
                "price_age_seconds": mp_result.get("price_age_seconds"),
            }
        else:
            current_price = None
            price_meta = None

        # ── Pre-gate: passive decisions must not adjust position ──
        is_passive = _is_passive_decision(latest_decision)
        if is_passive:
            # S-grade strong conflict at deep adverse R still gets emergency exit
            grade = str(latest_decision.get("signal_grade") or "").upper()
            current_r = _compute_current_r_for_trade(trade, current_price)
            if grade == "S" and current_r is not None and current_r <= early_exit_min_adverse_r:
                action = _execute_early_exit(repo, trade, latest_decision, ga_dec_id, current_price, price_meta=price_meta)
                if action.get("status") == "executed":
                    closed_count += 1
            else:
                action = _execute_recheck_mark(repo, trade, latest_decision, ga_dec_id, current_price)
                recheck_count += 1
            actions.append(action)
            if notify_actions:
                action_type = action.get("action", "")
                if action_type not in ("conflict_exit", "stop_adjusted", "profit_protection"):
                    _notify_action(repo, action, send_message)
            continue

        # --- P0: Profit Protection (runs BEFORE early exit / tighten) ---
        profit_protection_result = _evaluate_profit_protection_inline(
            repo, trade, latest_decision, ga_dec_id,
            mark_price_cache=mark_price_cache,
        )
        if profit_protection_result is not None:
            if profit_protection_result.get("status") == "executed":
                closed_count += 1
            elif profit_protection_result.get("status") in ("needs_position_recheck", "marked"):
                recheck_count += 1
            actions.append(profit_protection_result)
            if notify_actions and profit_protection_result.get("action") != "profit_protection":
                _notify_action(repo, profit_protection_result, send_message)
            continue

        # --- P0: Strong conflict early exit ---
        if _should_early_exit(
            repo, trade, latest_decision, current_price,
            early_exit_min_adverse_r=early_exit_min_adverse_r,
            signal_decay_exit_threshold=signal_decay_exit_threshold,
            strong_confirmations=strong_confirmations,
        ):
            action = _execute_early_exit(repo, trade, latest_decision, ga_dec_id, current_price, price_meta=price_meta)
            if action.get("status") == "executed":
                closed_count += 1
            elif action.get("status") in ("already_closed", "duplicate"):
                pass  # Don't double count
            elif action.get("status") == "no_change":
                skipped_count += 1
            elif action.get("status") == "marked":
                # Early exit bounced to recheck (e.g. missing current_price)
                recheck_count += 1
        elif _should_tighten_stop(
            repo, trade, latest_decision, current_price,
            min_hold_minutes=min_hold_minutes,
            min_current_r_for_breakeven=min_current_r_for_breakeven,
            min_mfe_r_for_breakeven=min_mfe_r_for_breakeven,
            reverse_confirmations_for_tighten=reverse_confirmations_for_tighten,
        ):
            action = _execute_stop_tighten(repo, trade, latest_decision, ga_dec_id, current_price, price_meta=price_meta)
            if action.get("status") == "executed":
                stop_adjusted_count += 1
            elif action.get("status") == "duplicate":
                pass  # Don't double count
            else:
                # no_change — route to skipped or its own category
                skipped_count += 1
        else:
            action = _execute_recheck_mark(repo, trade, latest_decision, ga_dec_id, current_price)
            recheck_count += 1

        actions.append(action)
        if notify_actions:
            action_type = action.get("action", "")
            if action_type not in ("conflict_exit", "stop_adjusted", "profit_protection"):
                _notify_action(repo, action, send_message)

    return {
        "ok": True,
        "checked_count": checked_count,
        "conflict_count": conflict_count,
        "closed_count": closed_count,
        "stop_adjusted_count": stop_adjusted_count,
        "recheck_count": recheck_count,
        "skipped_count": skipped_count,
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# Decision helpers
# ---------------------------------------------------------------------------


def _is_passive_decision(ga_decision: dict[str, Any]) -> bool:
    """Check if a GA decision is passive (should NOT trigger position adjustments).

    Passive decisions: opportunity_watch, monitor_only, risk_check.ok=false,
    or decisions without a trade_plan.
    """
    decision = str(ga_decision.get("decision") or "").lower()
    if decision in ("opportunity_watch", "monitor_only"):
        return True

    # Check risk_check
    risk = ga_decision.get("risk_check_json")
    if risk:
        if isinstance(risk, str):
            try:
                risk = json.loads(risk)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(risk, dict) and risk.get("ok") is False:
            return True

    # No trade_plan
    trade_plan = ga_decision.get("trade_plan_json")
    if trade_plan:
        if isinstance(trade_plan, str):
            try:
                trade_plan = json.loads(trade_plan)
            except (json.JSONDecodeError, TypeError):
                trade_plan = None
    if not trade_plan:
        return True

    return False


def _should_early_exit(
    repo: CryptoGuardRepository,
    trade: dict[str, Any],
    latest_decision: dict[str, Any],
    current_price: float | None,
    *,
    early_exit_min_adverse_r: float,
    signal_decay_exit_threshold: float,
    strong_confirmations: int,
) -> bool:
    """Check if this conflict warrants immediate position close (P0 early exit).

    Requires signal_grade == 'S' and confidence >= 0.85, plus at least one of:
    a. Same trade has been confirmed by 2+ consecutive reverse GA decisions
    b. Current unrealized PnL <= early_exit_min_adverse_r (e.g. -0.30R)
    c. signal_decay_score >= signal_decay_exit_threshold (e.g. 0.70)
    """
    grade = str(latest_decision.get("signal_grade") or "").upper()
    confidence = float(latest_decision.get("confidence") or 0)

    if grade != "S" or confidence < 0.85:
        return False

    # Condition a: consecutive reverse confirmations
    if _count_consecutive_reverse_confirmations(repo, trade) >= strong_confirmations:
        return True

    # Condition b: running loss >= threshold
    current_pnl_r = _compute_current_r_for_trade(trade, current_price)
    if current_pnl_r is not None and current_pnl_r <= early_exit_min_adverse_r:
        return True

    # Condition c: signal decay
    signal_decay = trade.get("signal_decay_score")
    if signal_decay is not None and float(signal_decay) >= signal_decay_exit_threshold:
        return True

    return False


def _count_consecutive_reverse_confirmations(
    repo: CryptoGuardRepository, trade: dict[str, Any]
) -> int:
    """Count consecutive GA decisions that reverse the trade direction since open.

    Only counts actionable decisions: risk_check.ok=true AND has trade_plan.
    Passive decisions (opportunity_watch, monitor_only, no trade_plan, risk_check failed)
    are SKIPPED (not counted) but do NOT break the consecutive chain — only truly
    non-reverse decisions break it.
    """
    import json as _json
    trade_symbol = str(trade["symbol"])
    pos_side = str(trade["side"] or "").upper()
    created_at = trade.get("created_at")
    if not created_at:
        return 0

    # Parse created_at to ISO string for proper comparison with analysis_time_utc (TEXT)
    from datetime import datetime, timezone
    try:
        if isinstance(created_at, str):
            dt = datetime.fromisoformat(created_at)
        else:
            dt = created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        created_at_iso = dt.isoformat()
    except (ValueError, TypeError):
        return 0

    # R13 P1 defense-in-depth: ``ORDER BY analysis_time DESC, id DESC``
    # uses the INTEGER NOT NULL column (schema.sql:148) as the canonical
    # chronological key, immune to any future regression in
    # ``analysis_time_utc`` shape. R13 P0 ensures ``analysis_time_utc``
    # is always an ISO string in production, but if a future regression
    # writes an integer, the ``ORDER BY analysis_time_utc`` would
    # misorder mixed populations (integer-text sorts AFTER ISO-text
    # because ``'1' < '2'``).
    #
    # The ``replace(replace(analysis_time_utc, 'T', ' '), 'Z', ''))``
    # wrapper on the ``WHERE`` clause matches the pattern used by 16+
    # SQL consumers in ``diagnostics/state_consistency.py``. NOTE: this
    # wrapper is NOT itself defense-in-depth against integer input —
    # SQLite ``datetime(replace(replace('1750000000000', 'T', ' '),
    # 'Z', ''))`` also returns NULL (verified by direct reproduction),
    # so an integer ``analysis_time_utc`` would silently exclude rows
    # from this WHERE clause regardless of the wrapper. The wrapper is
    # retained purely for stylistic consistency with the rest of the
    # codebase; the actual defense-in-depth is the ``ORDER BY
    # analysis_time DESC`` change above.
    rows = repo.conn.execute(
        """SELECT market_bias, signal_grade, risk_check_json, trade_plan_json FROM ga_decisions
           WHERE symbol=%s AND analysis_time_utc::timestamptz >= %s::timestamptz
           ORDER BY analysis_time DESC, id DESC""",
        (trade_symbol, created_at_iso),
    ).fetchall()

    count = 0
    for row in rows:
        bias = str(row["market_bias"] or "neutral").lower()
        grade = str(row["signal_grade"] or "").upper()

        # Skip non-reverse decisions — but also check actionability
        is_reverse = (pos_side == "SHORT" and bias == "bullish" and grade in {"S", "A", "B"}) or \
                     (pos_side == "LONG" and bias == "bearish" and grade in {"S", "A", "B"})
        if not is_reverse:
            break  # consecutive chain broken

        # Check actionability: must have risk_check.ok=true and trade_plan
        is_actionable = True
        try:
            risk = _json.loads(row["risk_check_json"] or "{}") if isinstance(row["risk_check_json"], str) else (row["risk_check_json"] or {})
            if isinstance(risk, dict) and risk.get("ok") is False:
                is_actionable = False
            trade_plan = _json.loads(row["trade_plan_json"] or "null") if isinstance(row["trade_plan_json"], str) else row["trade_plan_json"]
            if not trade_plan or not isinstance(trade_plan, dict):
                is_actionable = False
        except (_json.JSONDecodeError, TypeError):
            is_actionable = False

        if is_actionable:
            count += 1
        else:
            # Passive decisions (no trade_plan, risk_check failed) are skipped
            # but do NOT break the consecutive chain — only non-reverse decisions break it
            continue

    return count


def _should_tighten_stop(
    repo: CryptoGuardRepository,
    trade: dict[str, Any],
    latest_decision: dict[str, Any],
    current_price: float | None,
    *,
    min_hold_minutes: int,
    min_current_r_for_breakeven: float,
    min_mfe_r_for_breakeven: float,
    reverse_confirmations_for_tighten: int,
) -> bool:
    """Check if stop should be tightened to breakeven on direction conflict.

    All 5 gates must pass:
    1. Holding time >= min_hold_minutes (15 min)
    2. 2+ consecutive reverse GA confirmations
    3. current_r >= min_current_r_for_breakeven (0.50)
    4. MFE/R >= min_mfe_r_for_breakeven (0.75)
    5. Not a passive decision (checked upstream by caller)
    """
    if current_price is None or current_price <= 0:
        return False

    pos_side = str(trade["side"] or "").upper()
    entry_price = float(trade.get("entry_price") or 0)
    if entry_price <= 0:
        return False

    # Gate 1: holding time >= min_hold_minutes — fail-closed on missing created_at
    created_at = trade.get("created_at")
    if not created_at:
        return False
    try:
        from datetime import datetime, timezone
        if isinstance(created_at, str):
            open_time = datetime.fromisoformat(created_at)
        else:
            open_time = created_at
        if open_time.tzinfo is None:
            open_time = open_time.replace(tzinfo=timezone.utc)
        holding_minutes = (datetime.now(timezone.utc) - open_time).total_seconds() / 60
        if holding_minutes < min_hold_minutes:
            return False
    except (ValueError, TypeError):
        return False

    # Gate 2: 2+ consecutive reverse GA confirmations
    confirmations = _count_consecutive_reverse_confirmations(repo, trade)
    if confirmations < reverse_confirmations_for_tighten:
        return False

    # Gate 3: current_r >= min_current_r_for_breakeven
    current_r = _compute_current_r_for_trade(trade, current_price)
    if current_r is None or current_r < min_current_r_for_breakeven:
        return False

    # Gate 4: MFE/R >= min_mfe_r_for_breakeven
    mfe_r = _compute_mfe_r_for_trade(trade, current_price)
    if mfe_r is None or mfe_r < min_mfe_r_for_breakeven:
        return False

    # Gate 5: Not passive (checked by caller — _is_passive_decision)
    return True


# ---------------------------------------------------------------------------
# Price & PnL helpers
# ---------------------------------------------------------------------------


def _compute_current_r_for_trade(
    trade: dict[str, Any],
    current_price: float | None,
) -> float | None:
    """Compute current unrealized R using initial_risk_usdt if available.

    Fail-closed: returns None if initial_risk_usdt is unavailable and
    cannot be computed from entry_price/initial_stop_loss/quantity.
    """
    if current_price is None or current_price <= 0:
        return None

    pos_side = str(trade["side"] or "").upper()
    entry_price = float(trade.get("entry_price") or 0)
    if entry_price <= 0:
        return None

    # Use initial_risk_usdt if available
    initial_risk = float(trade.get("initial_risk_usdt") or 0)
    if initial_risk <= 0:
        # Fallback: compute from entry_price and initial_stop_loss (NOT stop_loss)
        stop_loss = float(trade.get("initial_stop_loss") or 0)
        quantity = float(trade.get("quantity") or 0)
        if stop_loss <= 0 or quantity <= 0:
            return None  # fail-closed
        initial_risk = abs(entry_price - stop_loss) * quantity
    if initial_risk <= 0:
        return None

    quantity = float(trade.get("quantity") or 0)
    if pos_side == "LONG":
        return (current_price - entry_price) * quantity / initial_risk
    else:
        return (entry_price - current_price) * quantity / initial_risk


def _compute_mfe_r_for_trade(
    trade: dict[str, Any],
    current_price: float,
) -> float | None:
    """Compute MFE in R-terms from historical max_favorable_excursion.

    Uses the stored max_favorable_excursion (absolute USD distance from entry
    to best price) divided by initial_risk_usdt. This is the proper MFE/R —
    NOT current unrealized R (which would retrace from the best price).

    Fail-closed: returns None if initial_risk_usdt is 0/unavailable.
    """
    entry_price = float(trade.get("entry_price") or 0)
    if entry_price <= 0:
        return None

    initial_risk_usdt = float(trade.get("initial_risk_usdt") or 0)
    if initial_risk_usdt <= 0:
        # Fallback: compute from entry_price and initial_stop_loss (NOT stop_loss)
        stop_loss = float(trade.get("initial_stop_loss") or 0)
        quantity = float(trade.get("quantity") or 0)
        if stop_loss <= 0 or quantity <= 0:
            return None  # fail-closed
        initial_risk_usdt = abs(entry_price - stop_loss) * quantity
    if initial_risk_usdt <= 0:
        return None

    mfe_usdt = float(trade.get("max_favorable_excursion") or 0)
    return mfe_usdt / initial_risk_usdt


def _compute_signal_decay_for_trade(
    trade: dict[str, Any],
    current_price: float,
) -> float:
    """Compute signal_decay_score including current state."""
    created_at = trade.get("created_at")
    minutes = 0.0
    if created_at:
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            minutes = max(0.0, (now - dt).total_seconds() / 60.0)
        except Exception:
            pass

    time_decay = min(0.6, minutes / 1440.0)

    pos_side = str(trade["side"] or "").upper()
    entry_price = float(trade.get("entry_price") or 0)
    stop_loss = float(trade.get("stop_loss") or entry_price or 0)
    risk = abs(entry_price - stop_loss) or 1.0
    if pos_side == "LONG":
        pnl_r = (current_price - entry_price) / risk
    else:
        pnl_r = (entry_price - current_price) / risk
    performance_decay = max(0.0, -pnl_r) * 0.4

    return max(0.0, min(1.0, time_decay + performance_decay))


def _evaluate_profit_protection_inline(
    repo: CryptoGuardRepository,
    trade: dict[str, Any],
    ga_decision: dict[str, Any],
    ga_decision_id: int,
    *,
    mark_price_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Inline profit protection evaluation for the conflict revalidator path.

    Called BEFORE early exit and stop tighten. Evaluates whether a profitable
    position with high MFE should be closed due to a strong reverse S-grade signal.

    Returns an action dict if profit protection triggers, None if it doesn't.
    """
    from plugins.crypto_guard.paper.paper_position_updater import _evaluate_profit_protection, _execute_profit_protection_close

    # Get the order for profit protection evaluation
    order_id = trade.get("order_id")
    if not order_id:
        return None

    order = repo.conn.execute(
        "SELECT * FROM paper_orders WHERE id=%s",
        (int(order_id),),
    ).fetchone()
    if not order or order["status"] != "open":
        return None

    order_dict = dict(order)

    # Use the shared _evaluate_profit_protection from paper_position_updater
    result = _evaluate_profit_protection(
        repo, order_dict, trade, ga_decision,
        mark_price_cache=mark_price_cache,
    )

    if result is None:
        return None

    if result.get("action") == "needs_position_recheck":
        # Close the loop: route the recheck through the standard
        # _execute_recheck_mark path so it writes paper_trade_logs, enqueues
        # paper_event_alert, and is countable by the main loop's recheck_count.
        return _execute_recheck_mark(
            repo, trade, ga_decision, ga_decision_id, None,
            reason="mark_price_unavailable_for_profit_protection",
        )

    # Build action dict in the conflict revalidator format
    return {
        "action": "profit_protection",
        "trade_id": int(trade["id"]),
        "symbol": str(trade["symbol"]),
        "side": result.get("side", str(trade.get("side", ""))),
        "entry_price": result.get("entry_price"),
        "exit_price": result.get("exit_price"),
        "pnl_r": result.get("pnl_r"),
        "pnl": result.get("pnl"),
        "mfe_r": result.get("mfe_r"),
        "current_r": result.get("current_r"),
        "retracement_r": result.get("retracement_r"),
        "signal_grade": ga_decision.get("signal_grade"),
        "market_bias": ga_decision.get("market_bias"),
        "ga_decision_id": ga_decision_id,
        "event_time": result.get("event_time"),
        "exit_time": result.get("event_time"),
        "reason": f"利润保护：+{result.get('mfe_r', 0):.2f}R MFE 回撤至 +{result.get('current_r', 0):.2f}R",
        "status": result.get("status", "executed"),
    }


# ---------------------------------------------------------------------------
# Action executors
# ---------------------------------------------------------------------------


def _execute_early_exit(
    repo: CryptoGuardRepository,
    trade: dict[str, Any],
    latest_decision: dict[str, Any],
    ga_decision_id: int,
    current_price: float | None,
    *,
    price_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Close the paper trade due to strong conflict signal.

    ``price_meta`` carries fresh quote metadata (mark_price, price_source,
    price_as_of, price_age_seconds) so it can be merged into the close log
    and the notification payload. None when the mark fetch failed upstream.
    """
    trade_id = int(trade["id"])
    trade_symbol = str(trade["symbol"])
    order_id = trade.get("order_id")
    pos_side = str(trade["side"] or "").upper()

    # Dedupe: don't close already closed trades
    existing = repo.conn.execute(
        "SELECT id, close_reason FROM paper_trades WHERE id=%s AND closed_at IS NOT NULL",
        (trade_id,),
    ).fetchone()
    if existing:
        return {
            "action": "conflict_exit", "trade_id": trade_id,
            "status": "already_closed",
            "reason": f"Trade already closed with reason: {existing['close_reason']}",
        }

    # Dedupe: don't re-execute same action for same ga_decision
    dedupe_key = f"position_conflict:{trade_id}:{ga_decision_id}:conflict_exit"
    if _was_action_executed(repo, dedupe_key):
        return {
            "action": "conflict_exit", "trade_id": trade_id,
            "status": "duplicate",
            "reason": "Action already executed for this GA decision",
        }

    entry_price = float(trade.get("entry_price") or 0)
    stop_loss = float(trade.get("stop_loss") or entry_price or 0)
    risk = abs(entry_price - stop_loss) or 1.0
    quantity = float(trade.get("quantity") or 1)
    order_row = None
    if order_id:
        order_row = repo.conn.execute(
            "SELECT filled_at, order_type, fill_method FROM paper_orders WHERE id=%s",
            (int(order_id),),
        ).fetchone()

    if current_price is None or current_price <= 0:
        return _execute_recheck_mark(
            repo, trade, latest_decision, ga_decision_id, current_price,
            reason="missing_current_price",
        )

    # Compute PnL
    if pos_side == "LONG":
        pnl = (current_price - entry_price) * quantity
        pnl_r = (current_price - entry_price) / risk
    else:
        pnl = (entry_price - current_price) * quantity
        pnl_r = (entry_price - current_price) / risk

    pnl_percent = (pnl / (entry_price * quantity)) * 100 if entry_price * quantity != 0 else 0.0

    # Quality metrics
    mfe = float(trade.get("max_favorable_excursion") or 0)
    mae = float(trade.get("max_adverse_excursion") or 0)
    if pos_side == "LONG":
        if current_price < entry_price:
            mae = max(mae, (entry_price - current_price) * quantity)
        else:
            mfe = max(mfe, (current_price - entry_price) * quantity)
    else:
        if current_price > entry_price:
            mae = max(mae, (current_price - entry_price) * quantity)
        else:
            mfe = max(mfe, (entry_price - current_price) * quantity)

    signal_decay = _compute_signal_decay_for_trade(trade, current_price)
    stop_take_path = _build_stop_take_path(trade, "conflict_exit")

    # Close the trade — atomic guard: only the winner of a concurrent close
    # proceeds with side effects. If close_paper_trade returns False, another
    # writer already closed this trade; bail out without backfill / order
    # update / position upsert / logs / enqueues / commit.
    now = datetime.now(timezone.utc).isoformat()
    closed = repo.close_paper_trade(
        trade_id=trade_id,
        exit_price=current_price,
        close_reason="conflict_exit",
        pnl=pnl,
        pnl_percent=pnl_percent,
        pnl_r=pnl_r,
        mfe=mfe,
        mae=mae,
        signal_decay_score=signal_decay,
        stop_take_path=stop_take_path,
        allow_wall_clock=True,
    )
    if not closed:
        return {
            "action": "conflict_exit",
            "trade_id": trade_id,
            "status": "already_closed",
            "reason": "concurrent close",
        }

    # Backfill real PnL to active evaluations.
    # Shadow evaluations get PnL exclusively from their independent virtual_trade lifecycle.
    repo.backfill_active_evaluation_pnl_r(trade, pnl_r)

    # Update paper_orders using proper repository method
    if order_id:
        repo.update_paper_order_status(int(order_id), "closed", closed_at=now)
        with repo.conn.transaction():
            repo.conn.execute(
                "UPDATE paper_orders SET cancel_reason=%s, invalidated_by_ga_decision_id=%s WHERE id=%s",
                (f"conflict_exit: GA#{ga_decision_id} direction conflict", ga_decision_id, int(order_id)),
            )

    # Update paper_positions to closed using proper repository method
    account = repo.ensure_paper_account()
    repo.upsert_paper_position_from_trade(
        account_id=int(account["id"]),
        trade=trade,
        status="closed",
        current_price=current_price,
        unrealized_pnl=0.0,
        unrealized_pnl_pct=0.0,
        allow_wall_clock=True,
    )

    # Record action
    _record_action(repo, dedupe_key, trade_id, trade_symbol)

    # Log conflict_exit event
    event_payload = {
        "trade_id": trade_id,
        "ga_decision_id": ga_decision_id,
        "signal_grade": latest_decision.get("signal_grade"),
        "market_bias": latest_decision.get("market_bias"),
        "confidence": latest_decision.get("confidence"),
        "pnl_r": round(pnl_r, 4),
        "close_reason": "conflict_exit",
        "dedupe_key": dedupe_key,
    }
    if price_meta:
        event_payload.update({
            "mark_price": price_meta.get("mark_price"),
            "price_source": price_meta.get("price_source"),
            "price_as_of": price_meta.get("price_as_of"),
            "price_age_seconds": price_meta.get("price_age_seconds"),
        })
    repo.log_paper_trade_event(
        event_type="conflict_exit",
        symbol=trade_symbol,
        side=pos_side,
        price=current_price,
        quantity=quantity,
        pnl=pnl,
        pnl_pct=pnl_percent,
        reason=f"Position conflict: GA#{ga_decision_id} {latest_decision.get('signal_grade')} {latest_decision.get('market_bias')} vs {pos_side}",
        event=event_payload,
    )

    # Log standard close_position event (side effect paper_broker does)
    repo.log_paper_trade_event(
        position_id=trade_id,
        event_type="close_position",
        symbol=trade_symbol,
        side=pos_side,
        price=current_price,
        quantity=quantity,
        pnl=pnl,
        pnl_pct=pnl_percent,
        reason="conflict_exit",
        event={"order_id": order_id, "trade_id": trade_id, "pnl_r": pnl_r},
    )

    # Enqueue trade_review job (side effect paper_broker does)
    repo.enqueue_job("trade_review", 4, "position_conflict", f"system:review:{trade_id}", {"trade_id": trade_id})

    # Enqueue paper_event_alert job (side effect paper_broker does)
    alert_payload = {
        "event_type": "close_position",
        "symbol": trade_symbol,
        "order_id": order_id,
        "trade_id": trade_id,
        "exit_price": current_price,
        "close_reason": "conflict_exit",
        "pnl_r": pnl_r,
        "side": pos_side,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profits": trade.get("take_profit_json") or [],
        "filled_at": (order_row["filled_at"] if order_row and order_row["filled_at"] else trade.get("created_at")),
        "closed_at": now,
        "event_time": now,
        "quantity": quantity,
        "order_type": (order_row["order_type"] if order_row and order_row["order_type"] else trade.get("fill_method") or "market"),
        "fill_method": (order_row["fill_method"] if order_row and order_row["fill_method"] else trade.get("fill_method")),
    }
    if price_meta:
        alert_payload.update({
            "mark_price": price_meta.get("mark_price"),
            "price_source": price_meta.get("price_source"),
            "price_as_of": price_meta.get("price_as_of"),
            "price_age_seconds": price_meta.get("price_age_seconds"),
        })
    repo.enqueue_job_once(
        "paper_event_alert",
        3,
        "position_conflict",
        f"system:paper:conflict_exit:{trade_id}:{ga_decision_id}",
        alert_payload,
    )

    LOGGER.info(
        "conflict_exit: trade_id=%s symbol=%s side=%s exit_price=%s pnl_r=%.2f ga=%s",
        trade_id, trade_symbol, pos_side, current_price, pnl_r, ga_decision_id,
    )

    return {
        "action": "conflict_exit",
        "trade_id": trade_id,
        "symbol": trade_symbol,
        "side": pos_side,
        "entry_price": entry_price,
        "exit_price": current_price,
        "pnl_r": round(pnl_r, 4),
        "pnl": round(pnl, 4),
        "signal_grade": latest_decision.get("signal_grade"),
        "market_bias": latest_decision.get("market_bias"),
        "ga_decision_id": ga_decision_id,
        "event_time": now,
        "exit_time": now,
        "reason": f"强反向{latest_decision.get('signal_grade')}级信号触发提前退出",
        "status": "executed",
    }


def _execute_stop_tighten(
    repo: CryptoGuardRepository,
    trade: dict[str, Any],
    latest_decision: dict[str, Any],
    ga_decision_id: int,
    current_price: float | None,
    *,
    price_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tighten stop loss on conflict — move to breakeven if profitable.

    ``price_meta`` carries fresh quote metadata (mark_price, price_source,
    price_as_of, price_age_seconds) so it can be merged into the adjustment
    log. None when the mark fetch failed upstream.
    """
    trade_id = int(trade["id"])
    trade_symbol = str(trade["symbol"])
    order_id = trade.get("order_id")
    pos_side = str(trade["side"] or "").upper()
    entry_price = float(trade.get("entry_price") or 0)
    old_stop = float(trade.get("stop_loss") or 0)

    # Dedupe
    dedupe_key = f"position_conflict:{trade_id}:{ga_decision_id}:stop_adjusted"
    if _was_action_executed(repo, dedupe_key):
        return {
            "action": "stop_adjusted", "trade_id": trade_id,
            "status": "duplicate",
            "reason": "Stop adjustment already executed for this GA decision",
        }

    current_r = _compute_current_r_for_trade(trade, current_price)

    # Tighten stop to breakeven
    new_stop = old_stop
    tighten_reason = ""
    if pos_side == "LONG":
        candidate_stop = max(old_stop, entry_price)
        if candidate_stop > old_stop:
            new_stop = candidate_stop
            tighten_reason = f"止损收紧: {old_stop} → {new_stop} (保本)"
    elif pos_side == "SHORT":
        candidate_stop = min(old_stop, entry_price)
        if candidate_stop < old_stop:
            new_stop = candidate_stop
            tighten_reason = f"止损收紧: {old_stop} → {new_stop} (保本)"

    if new_stop != old_stop:
        # Compute audit info
        from datetime import datetime, timezone
        tz_utc = timezone.utc
        open_time = trade.get("created_at")
        holding_minutes = None
        if open_time:
            try:
                if isinstance(open_time, str):
                    ot = datetime.fromisoformat(open_time)
                else:
                    ot = open_time
                if ot.tzinfo is None:
                    ot = ot.replace(tzinfo=tz_utc)
                holding_minutes = round((datetime.now(tz_utc) - ot).total_seconds() / 60, 1)
            except (ValueError, TypeError):
                pass

        mfe_r = _compute_mfe_r_for_trade(trade, current_price)

        # PG greenfield: ``created_at`` is a TIMESTAMPTZ column, so under the
        # psycopg dict_row factory ``trade["created_at"]`` is a ``datetime``
        # object (SQLite returned an ISO string). A raw datetime stored in
        # ``enriched_meta`` flows into ``log_paper_trade_event`` -> ``json.dumps``
        # and raises ``TypeError: Object of type datetime is not JSON serializable``
        # *before* the INSERT executes (so the error is invisible to cursor
        # tracing and swallowed by the update-stop-loss ``except Exception``
        # path), which made the stop tighten silently no-op. Normalize to an
        # ISO string for the audit payload - ``holding_minutes`` computation
        # above already proved ``open_time`` parses cleanly when non-None.
        if isinstance(open_time, datetime):
            open_time_iso = open_time.isoformat()
        elif open_time:
            open_time_iso = str(open_time)
        else:
            open_time_iso = None

        # Build enriched price_meta with audit details for the atomic log
        enriched_meta = dict(price_meta) if price_meta else {}
        enriched_meta.update({
            "ga_decision_id": ga_decision_id,
            "trigger": "position_conflict",
            "dedupe_key": dedupe_key,
            "audit": {
                "open_time": open_time_iso,
                "action_time": datetime.now(tz_utc).isoformat(),
                "holding_minutes": holding_minutes,
                "current_r": round(current_r, 4) if current_r is not None else None,
                "mfe_r": round(mfe_r, 4) if mfe_r is not None else None,
                "decision_executable": True,
                "gate_result": "all_passed",
            },
        })

        # Atomic stop tighten: use CAS across all three tables
        updated = repo.update_stop_loss_across_tables(
            trade_id, int(order_id) if order_id else 0, new_stop,
            old_stop=old_stop,
            reason=f"Position conflict stop tighten: GA#{ga_decision_id}",
            price_meta=enriched_meta,
        )
        if not updated:
            return {
                "action": "stop_adjusted",
                "trade_id": trade_id,
                "symbol": trade_symbol,
                "side": pos_side,
                "old_stop_loss": old_stop,
                "new_stop_loss": new_stop,
                "status": "duplicate",
                "reason": "Concurrent writer changed stop_loss first",
            }

        # Enqueue paper_event_alert so notifications carry quote metadata
        alert_payload = {
            "event_type": "stop_loss_adjustment",
            "symbol": trade_symbol,
            "order_id": order_id,
            "trade_id": trade_id,
            "new_stop_loss": new_stop,
            "old_stop_loss": old_stop,
            "side": pos_side,
            "entry_price": entry_price,
            "current_r": round(current_r, 4) if current_r is not None else None,
            "mfe_r": round(mfe_r, 4) if mfe_r is not None else None,
            "reason": tighten_reason,
            "event_time": datetime.now(tz_utc).isoformat(),
            "trigger": "position_conflict",
            "ga_decision_id": ga_decision_id,
        }
        if price_meta:
            alert_payload.update({
                "mark_price": price_meta.get("mark_price"),
                "price_source": price_meta.get("price_source"),
                "price_as_of": price_meta.get("price_as_of"),
                "price_age_seconds": price_meta.get("price_age_seconds"),
            })
        repo.enqueue_job_once(
            "paper_event_alert",
            3,
            "position_conflict",
            f"system:paper:stop_adjust:{trade_id}:{ga_decision_id}",
            alert_payload,
        )

        LOGGER.info(
            "stop_adjusted: trade_id=%s symbol=%s old=%s new=%s r=%.2f ga=%s",
            trade_id, trade_symbol, old_stop, new_stop, current_r or 0, ga_decision_id,
        )

        _record_action(repo, dedupe_key, trade_id, trade_symbol)

        return {
            "action": "stop_adjusted",
            "trade_id": trade_id,
            "symbol": trade_symbol,
            "side": pos_side,
            "old_stop_loss": old_stop,
            "new_stop_loss": new_stop,
            "current_r": round(current_r, 4) if current_r is not None else None,
            "signal_grade": latest_decision.get("signal_grade"),
            "market_bias": latest_decision.get("market_bias"),
            "ga_decision_id": ga_decision_id,
            "reason": tighten_reason,
            "status": "executed",
        }

    # Stop already at or past breakeven — no action taken, don't write ledger
    return {
        "action": "stop_adjusted",
        "trade_id": trade_id,
        "symbol": trade_symbol,
        "side": pos_side,
        "old_stop_loss": old_stop,
        "new_stop_loss": new_stop,
        "current_r": round(current_r, 4) if current_r is not None else None,
        "signal_grade": latest_decision.get("signal_grade"),
        "market_bias": latest_decision.get("market_bias"),
        "ga_decision_id": ga_decision_id,
        "reason": "止损已在保本或更优位置 — 无需调整",
        "status": "no_change",
    }


def _execute_recheck_mark(
    repo: CryptoGuardRepository,
    trade: dict[str, Any],
    latest_decision: dict[str, Any],
    ga_decision_id: int,
    current_price: float | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark trade as needing recheck — conflict exists but doesn't meet exit/tighten criteria."""
    trade_id = int(trade["id"])
    trade_symbol = str(trade["symbol"])

    reason_suffix = f":{reason}" if reason else ""
    dedupe_key = f"position_conflict:{trade_id}:{ga_decision_id}:needs_position_recheck{reason_suffix}"
    if _was_action_executed(repo, dedupe_key):
        return {
            "action": "needs_position_recheck", "trade_id": trade_id,
            "status": "duplicate",
            "reason": "Recheck already marked for this GA decision",
        }

    current_r = _compute_current_r_for_trade(trade, current_price)
    _record_action(repo, dedupe_key, trade_id, trade_symbol)

    repo.log_paper_trade_event(
        event_type="needs_position_recheck",
        symbol=trade_symbol,
        side=trade.get("side"),
        price=current_price,
        quantity=trade.get("quantity"),
        reason=f"Position conflict recheck: GA#{ga_decision_id} {latest_decision.get('signal_grade')} {latest_decision.get('market_bias')} vs {trade.get('side')}",
        position_id=trade_id,
        event={
            "trade_id": trade_id,
            "ga_decision_id": ga_decision_id,
            "current_r": round(current_r, 4) if current_r is not None else None,
            "dedupe_key": dedupe_key,
            "trigger": "position_conflict",
            "reason": reason or "方向冲突但未满足提前退出或收紧止损条件，进入复核",
        },
    )

    LOGGER.info(
        "needs_position_recheck: trade_id=%s symbol=%s side=%s grade=%s r=%.2f ga=%s reason=%s",
        trade_id, trade_symbol, trade.get("side"),
        latest_decision.get("signal_grade"), current_r or 0, ga_decision_id,
        reason or "方向冲突",
    )

    return {
        "action": "needs_position_recheck",
        "trade_id": trade_id,
        "symbol": trade_symbol,
        "side": trade.get("side"),
        "current_r": round(current_r, 4) if current_r is not None else None,
        "signal_grade": latest_decision.get("signal_grade"),
        "market_bias": latest_decision.get("market_bias"),
        "ga_decision_id": ga_decision_id,
        "reason": reason or "方向冲突但未满足提前退出或收紧止损条件，进入复核",
        "status": "marked",
    }


# ---------------------------------------------------------------------------
# Deduplication & audit
# ---------------------------------------------------------------------------


def _was_action_executed(repo: CryptoGuardRepository, dedupe_key: str) -> bool:
    """Check if an action was already executed by its dedupe key."""
    row = repo.conn.execute(
        "SELECT id FROM paper_trade_logs WHERE event_json ->> 'dedupe_key'=%s LIMIT 1",
        (dedupe_key,),
    ).fetchone()
    return row is not None


def _record_action(
    repo: CryptoGuardRepository,
    dedupe_key: str,
    trade_id: int,
    symbol: str,
) -> None:
    """Record that an action was executed to prevent duplicates."""
    now = datetime.now(timezone.utc).isoformat()
    repo.log_paper_trade_event(
        event_type="position_conflict_action",
        symbol=symbol,
        position_id=trade_id,
        event={
            "dedupe_key": dedupe_key,
            "trade_id": trade_id,
            "trigger": "position_conflict",
            "recorded_at": now,
        },
    )


# ---------------------------------------------------------------------------
# Action builders
# ---------------------------------------------------------------------------


def _build_skipped_action(
    trade: dict[str, Any],
    latest_decision: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "action": "skipped",
        "trade_id": int(trade["id"]),
        "symbol": str(trade["symbol"]),
        "side": str(trade.get("side") or ""),
        "bias": str(latest_decision.get("market_bias") or "neutral"),
        "grade": str(latest_decision.get("signal_grade") or ""),
        "confidence": float(latest_decision.get("confidence") or 0),
        "reason": reason,
        "status": "skipped",
    }


def _build_stop_take_path(
    trade: dict[str, Any],
    exit_reason: str,
) -> list[dict[str, Any]]:
    """Build or extend the stop_take_path_json for a conflict exit."""
    existing_path = trade.get("stop_take_path_json")
    if existing_path:
        try:
            if isinstance(existing_path, str):
                path = json.loads(existing_path)
            else:
                path = list(existing_path)
        except (json.JSONDecodeError, TypeError):
            path = []
    else:
        path = []

    path.append({
        "event": exit_reason,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    return path


def _format_utc8_timestamp(value: Any) -> str | None:
    """Format a timestamp to UTC+8 display string (compact, no seconds).

    Delegates to the shared formatter in notify/time_utils.py.
    Returns None if the value is None/unparseable, so callers can
    conditionally include the time line.
    """
    if not value:
        return None
    result = format_event_time_cst_compact(value)
    if result == "不可用":
        return None
    return result


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def _notify_action(
    repo: CryptoGuardRepository,
    action: dict[str, Any],
    send_message: Any,
) -> dict[str, Any]:
    """Send or enqueue a notification about a position conflict action."""
    from plugins.crypto_guard.notify.alert_delivery import send_markdown_alert
    from plugins.crypto_guard.notify.hourly_report import resolve_report_target

    action_type = action.get("action", "")
    status = action.get("status", "")

    # Only notify on actual executed/marked actions
    allowed = {
        ("conflict_exit", "executed"),
        ("stop_adjusted", "executed"),
        ("needs_position_recheck", "marked"),
        ("profit_protection", "executed"),
    }
    if (action_type, status) not in allowed:
        return {"ok": True, "sent": False, "queued": False, "reason": f"not_notifiable:{action_type}:{status}"}

    symbol = action.get("symbol", "-")
    side = str(action.get("side") or "").upper()
    side_cn = {"LONG": "做多", "SHORT": "做空"}.get(side, side)

    if action_type == "conflict_exit":
        exit_price = action.get("exit_price", "-")
        pnl_r = action.get("pnl_r", 0)
        ga_id = action.get("ga_decision_id", "?")
        grade = action.get("signal_grade", "?")
        event_time = _format_utc8_timestamp(action.get("event_time") or action.get("exit_time"))
        lines = [
            "**模拟盘持仓冲突 - 已提前退出**",
            "",
            f"- 产品：{symbol}",
            f"- 方向：{side_cn}",
            f"- 时间：{event_time}" if event_time else "",
            f"- 退出价格：{exit_price}",
            f"- 盈亏 R：{pnl_r}",
            f"- 触发信号：GA#{ga_id} {grade}级",
            f"- 原因：{action.get('reason', '方向冲突提前退出')}",
            "",
            "不构成实盘建议，仅用于模拟盘与策略研究。",
        ]
        alert_type = "close_position"
    elif action_type == "profit_protection":
        exit_price = action.get("exit_price", "-")
        pnl_r = action.get("pnl_r", 0)
        ga_id = action.get("ga_decision_id", "?")
        grade = action.get("signal_grade", "?")
        mfe_r = action.get("mfe_r", 0)
        current_r = action.get("current_r", 0)
        retracement_r = action.get("retracement_r", 0)
        event_time = _format_utc8_timestamp(action.get("event_time") or action.get("exit_time"))
        lines = [
            "**模拟盘利润保护 - 已平仓锁定利润**",
            "",
            f"- 产品：{symbol}",
            f"- 方向：{side_cn}",
            f"- 时间：{event_time}" if event_time else "",
            f"- 退出价格：{exit_price}",
            f"- 盈亏 R：{pnl_r}",
            f"- MFE：+{mfe_r:.2f}R → 当前：+{current_r:.2f}R（回撤 {retracement_r:.2f}R）",
            f"- 触发信号：GA#{ga_id} {grade}级强反向",
            f"- 原因：{action.get('reason', '利润保护')}",
            "",
            "不构成实盘建议，仅用于模拟盘与策略研究。",
        ]
        alert_type = "close_position"
    elif action_type == "stop_adjusted":
        old_stop = action.get("old_stop_loss", "-")
        new_stop = action.get("new_stop_loss", "-")
        grade = action.get("signal_grade", "?")
        event_time = _format_utc8_timestamp(action.get("event_time") or datetime.now(timezone.utc).isoformat())
        lines = [
            "**模拟盘持仓冲突 - 已收紧止损**",
            "",
            f"- 产品：{symbol}",
            f"- 方向：{side_cn}",
            f"- 时间：{event_time}",
            f"- 旧止损：{old_stop}",
            f"- 新止损：{new_stop}",
            f"- 当前 R：{action.get('current_r', '-')}",
            f"- 触发信号：{grade}级",
            f"- 原因：{action.get('reason', '方向冲突收紧止损')}",
            "",
            "不构成实盘建议，仅用于模拟盘与策略研究。",
        ]
        alert_type = "stop_loss_adjustment"
    elif action_type == "needs_position_recheck":
        grade = action.get("signal_grade", "?")
        current_r = action.get("current_r", "-")
        event_time = _format_utc8_timestamp(datetime.now(timezone.utc).isoformat())
        lines = [
            "**模拟盘持仓冲突 - 进入复核**",
            "",
            f"- 产品：{symbol}",
            f"- 方向：{side_cn}",
            f"- 时间：{event_time}",
            f"- 当前 R：{current_r}",
            f"- 触发信号：{grade}级",
            f"- 原因：{action.get('reason', '方向冲突进入复核队列')}",
            "",
            "系统将持续监控此持仓，不构成实盘建议。",
        ]
        alert_type = "risk_alert"
    else:
        return {"ok": True, "sent": False, "queued": False, "reason": "unknown_action_type"}

    text = "\n".join(lines)
    target = resolve_report_target(repo)
    if not target:
        return {"ok": True, "sent": False, "queued": False, "reason": "no_target"}

    dedupe_key = f"position_conflict:{action.get('trade_id')}:{action.get('ga_decision_id')}:{action_type}"

    sent = send_markdown_alert(
        repo,
        send_message,
        receive_id=target["receive_id"],
        receive_id_type=target.get("receive_id_type", "chat_id"),
        text=text,
        alert_type=alert_type,
        symbol=symbol,
        priority=3,
        dedupe_key=dedupe_key,
    )
    return {
        "ok": True,
        "sent": bool(sent.get("sent")),
        "queued": bool(sent.get("queued")),
        "text": text,
    }
