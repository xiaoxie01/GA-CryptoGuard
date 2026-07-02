from __future__ import annotations

from typing import Any

from plugins.crypto_guard.data.binance_rest import fetch_klines, fetch_mark_price
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.paper.execution_quality import equity_snapshot, market_from_price
from plugins.crypto_guard.paper.mark_price import fetch_binance_mark_price, get_mark_price_with_fallback, clear_cycle_cache
from plugins.crypto_guard.paper.paper_broker import close_trade_if_needed, fill_order_if_triggered
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.redis_adapter import RedisAdapter
from plugins.crypto_guard.utils import utc_ms

LOGGER = get_logger("crypto_guard.paper")


def _log_retryable_skip_audit(
    repo: CryptoGuardRepository,
    order: dict[str, Any],
    candle_close_time: int,
    skip_reason: str,
    fill_result: dict[str, Any],
) -> None:
    """R3-C: Write ONE idempotent audit record per (order_id, candle_close_time, skip_reason).

    R4-D4: Uses direct INSERT with UNIQUE constraint on dedupe_key column
    instead of SELECT-then-INSERT race window. When a duplicate INSERT is
    attempted, sqlite3.IntegrityError is caught and silently ignored —
    the row already exists from a concurrent/earlier call.
    Audit log failure must not mask the original skip_reason.
    """
    import sqlite3

    try:
        dedupe_key = f"retryable_skip:{order['id']}:{candle_close_time}:{skip_reason}"
        repo.log_paper_trade_event(
            position_id=None,
            event_type="pending_order_retryable_skip",
            symbol=order.get("symbol", ""),
            side=order.get("side", ""),
            price=0.0,
            quantity=order.get("quantity"),
            pnl=0.0,
            pnl_pct=0.0,
            reason=f"Retryable skip: {skip_reason} at candle close_time={candle_close_time}",
            event={
                "order_id": order["id"],
                "candle_close_time": candle_close_time,
                "skip_reason": skip_reason,
                "ga_decision_id": fill_result.get("ga_decision_id"),
                "dedupe_key": dedupe_key,
            },
            event_time=candle_close_time if candle_close_time > 0 else None,
            dedupe_key=dedupe_key,
        )
    except sqlite3.IntegrityError:
        # R4-D4: UNIQUE constraint on dedupe_key — row already exists, idempotent.
        return
    except Exception:
        # Audit log failure must not mask the original skip_reason
        pass


def _dedupe_and_validate_monotonic(
    candles: list[dict[str, Any]],
    cursor_close_time: int | None = None,
) -> list[dict[str, Any]]:
    """R4-D2: Deduplicate candles by close_time and enforce strict-monotonic ordering.

    Page-boundary fetches can return overlapping candles (same close_time appearing
    as the last of page N and the first of page N+1). This helper:
    1. Deduplicates by close_time, keeping the first occurrence.
    2. Validates strict-monotonic close_time ordering. If a candle with a
       close_time <= a previously seen close_time is encountered (non-duplicate
       out-of-order), it is dropped and a warning is logged.
    3. R5-D2: Detects gaps in the candle sequence. If the gap between consecutive
       candles exceeds EXPECTED_INTERVAL_MS (60000ms for 1m candles), the result
       is truncated at the last safe candle (before the gap). The gap candle and
       all subsequent candles are excluded — the caller must not process past a
       gap because missing candles may contain fill/SL/TP events.
    4. R6-D1: When cursor_close_time is provided, the FIRST candle is checked
       against the cursor for gap detection. This closes the hole where the
       first returned candle was far ahead of the cursor (missing candles
       between cursor and first result). The prior code initialized
       last_close_time=0, so the gap check (which requires last_close_time > 0)
       was skipped for the first candle.
    5. R6-D1: Invalid candles (close_time <= 0, out-of-order, duplicate) now
       STOP processing instead of being skipped. The previous `continue`
       behavior silently dropped bad candles and kept processing subsequent
       candles, which could hide data-integrity issues and process candles
       after a corrupted one. Now we `break` to preserve the cursor at the
       last safe position.
    Returns a new list of unique, strictly-monotonic, gap-free candles.
    """
    EXPECTED_INTERVAL_MS = 60000  # 1-minute candles
    if not candles:
        return []
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    last_close_time = cursor_close_time or 0
    for c in candles:
        ct = int(c.get("close_time", 0))
        if ct <= 0:
            LOGGER.warning("R6-D1: stopping at candle with invalid close_time=%s", ct)
            break
        if ct in seen:
            # Duplicate — skip (expected at page boundaries)
            continue
        if ct <= last_close_time:
            # Out-of-order or equal (non-duplicate) — stop and warn
            LOGGER.warning(
                "R6-D1: stopping at out-of-order candle close_time=%s (last=%s)", ct, last_close_time,
            )
            break
        # R5-D2 / R6-D1: Gap detection — if the gap exceeds the expected interval,
        # truncate at the last safe candle. Do not process past a gap.
        # When cursor_close_time is provided, this also checks the first candle
        # against the cursor (closing the first-candle gap hole).
        if last_close_time > 0 and (ct - last_close_time) > EXPECTED_INTERVAL_MS:
            LOGGER.warning(
                "R5-D2/R6-D1: gap detected: last_close_time=%s next_close_time=%s "
                "gap=%sms (expected=%sms). Truncating at last safe candle.",
                last_close_time, ct, ct - last_close_time, EXPECTED_INTERVAL_MS,
            )
            break
        seen.add(ct)
        last_close_time = ct
        result.append(c)
    return result


def _fetch_unprocessed_closed_candles(
    symbol: str,
    start_time: int | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Fetch 1m closed candles from Binance REST API.

    BTC#9 Phase C Section 11: returns a result dict instead of swallowing
    exceptions into an empty list. Callers must distinguish network_error
    from no-data.

    Returns:
        {"ok": True, "error": None, "candles": [...]} on success.
        {"ok": False, "error": "network_error", "candles": []} on exception.
        {"ok": False, "error": "invalid_data", "candles": []} on bad data.

    Returns ONLY fully closed candles (close_time < now). The current
    unclosed candle is NEVER returned. Candles are sorted by open_time
    ascending. On network failure: candles list is empty (never fallback
    to fake candle).

    Args:
        symbol: Trading symbol (e.g. "BTCUSDT")
        start_time: Unix millis start time for candle fetch (candles with
                    close_time > start_time). If None, fetches most recent.
        limit: Max candles to fetch (default 500).
    """
    try:
        candles = fetch_klines(symbol, "1m", start_time=start_time, limit=int(limit))
    except Exception as exc:
        LOGGER.warning("fetch unprocessed closed candles failed for %s: %s", symbol, exc)
        return {"ok": False, "error": "network_error", "candles": []}

    try:
        # Filter to ONLY fully closed candles
        now_ms = utc_ms()
        closed = [c for c in candles if int(c.get("close_time", 0)) < now_ms]
        # Sort by open_time ascending
        closed.sort(key=lambda c: int(c.get("open_time", 0)))
        return {"ok": True, "error": None, "candles": closed}
    except Exception as exc:
        LOGGER.warning("parse closed candles failed for %s: %s", symbol, exc)
        return {"ok": False, "error": "invalid_data", "candles": []}


def update_paper_positions(repo: CryptoGuardRepository, *, prices: dict[str, float | dict[str, Any]] | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    price_map = prices or {}
    latest_prices: dict[str, float] = {}
    redis = RedisAdapter()
    # Clear per-cycle mark price cache
    clear_cycle_cache()
    # Shared mark price cache for this cycle
    mark_price_cache: dict[str, dict[str, Any]] = {}
    # Track orders filled in this batch — skip TP/SL check for them (defer to next batch)
    filled_order_ids: set[int] = set()
    # Load config for max_candles_per_batch
    from plugins.crypto_guard.config.loader import load_config as _load_config
    rev_cfg = _load_config().trading_mode.get("pending_order_revalidation", {})
    max_candles_per_batch = int(rev_cfg.get("max_candles_per_batch", 500))
    # C5: paged backfill config — per-page size and max pages per batch
    max_candles_per_page = int(rev_cfg.get("max_candles_per_page", 500))
    max_pages_per_batch = int(rev_cfg.get("max_pages_per_batch", 10))

    for order in repo.list_open_paper_orders():
        LOGGER.info("paper update order_id=%s symbol=%s status=%s", order.get("id"), order.get("symbol"), order.get("status"))
        symbol = order["symbol"]
        market_or_price = price_map.get(symbol)

        if order["status"] == "pending":
            # Per-candle 1m closed candle processing for pending orders
            created_at_ms = None
            if order.get("created_at"):
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    text = str(order["created_at"]).replace("Z", "+00:00")
                    dt = _dt.fromisoformat(text)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_tz.utc)
                    created_at_ms = int(dt.timestamp() * 1000)
                except Exception:
                    pass

            cursor = order.get("last_processed_candle_time")
            if cursor:
                start_time = cursor
            elif created_at_ms:
                start_time = created_at_ms
            else:
                start_time = None

            # C4: _fetch_unprocessed_closed_candles returns a result dict.
            # C5: paged backfill — fetch multiple pages until exhausted,
            # config cap, error, or order closed/filled.
            page = 0
            all_candles: list[dict[str, Any]] = []
            page_start_time = start_time
            fetch_error: str | None = None
            while page < max_pages_per_batch:
                result = _fetch_unprocessed_closed_candles(
                    symbol, start_time=page_start_time, limit=max_candles_per_page,
                )
                if not result.get("ok"):
                    fetch_error = result.get("error", "unknown")
                    # If we already have candles from prior pages, process them.
                    # Otherwise skip this cycle preserving the cursor.
                    if not all_candles:
                        results.append({"ok": True, "filled": False, "order_id": order["id"],
                                       "skip_reason": f"candle_fetch_{fetch_error}"})
                        break
                    else:
                        break
                page_candles = result.get("candles") or []
                if not page_candles:
                    break  # No more data
                all_candles.extend(page_candles)
                page += 1
                # C5: next page startTime must be strictly > last close_time
                last_page_close = int(page_candles[-1].get("close_time", 0))
                if last_page_close <= 0:
                    break
                page_start_time = last_page_close + 1
                # Stop if we've reached the batch cap
                if len(all_candles) >= max_candles_per_batch:
                    break

            # R4-D2/R6-D1: deduplicate page-boundary candles and enforce strict-monotonic close_time.
            # R6-D1: pass cursor_close_time so the first candle is checked against the
            # cursor for gap detection. Without this, a gap between the cursor and
            # the first returned candle would go undetected.
            cursor_close_time = 0
            if cursor:
                try:
                    cursor_close_time = int(cursor)
                except (TypeError, ValueError):
                    cursor_close_time = 0
            all_candles = _dedupe_and_validate_monotonic(
                all_candles,
                cursor_close_time=cursor_close_time if cursor_close_time > 0 else None,
            )

            # On first-page error with no candles, skip to next order
            if fetch_error and not all_candles:
                continue

            candles = all_candles
            if not candles:
                # No candles to process — use mark_price for equity tracking only
                if market_or_price is None:
                    try:
                        mark_price = float(fetch_mark_price(symbol)["markPrice"])
                    except Exception:
                        mark_price = 0.0
                else:
                    mark_price = float(market_or_price) if not isinstance(market_or_price, dict) else float(market_or_price.get("close", 0))
                latest_prices[symbol] = mark_price if mark_price > 0 else latest_prices.get(symbol, 0.0)
                redis.set_latest_price(symbol, latest_prices[symbol])
                continue

            # Process each candle sequentially.
            #
            # BTC#9 Phase B (Section 2): After a pending order is filled on
            # candle N, we must NOT break out of the loop. Instead we continue
            # processing remaining closed candles through the normal open-order
            # path (path metrics / SL/TP). The fill candle itself is conservative
            # — only the trade is created, SL/TP is deferred to the next candle
            # via filled_order_ids. Mark-price-dependent breakeven / profit
            # protection are skipped in historical replay (no mark price for
            # past times).
            last_candle_time = None
            prev_candle_close = None
            order_became_open = False  # track transition within this loop

            # R3-C: retryable skip_reasons — preserve cursor, stop processing later candles.
            # These represent transient failures where the fill opportunity was not consumed;
            # the next run must re-encounter the same candle.
            _RETRYABLE_SKIP_REASONS = frozenset({
                "ga_recheck_unavailable",
                "ga_recheck_baseline_unavailable",
                "cancel_race_lost",
                "missing_event_time",
            })

            for candle in candles:
                # Build single-candle market dict with prev_close from previous candle
                single_market = {
                    "symbol": symbol,
                    "open_time": candle.get("open_time"),
                    "close_time": candle.get("close_time"),
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "prev_close": prev_candle_close,
                    "source": "1m_closed_candle",
                }
                candle_event_time = int(candle.get("close_time", 0))
                candle_close_time = candle_event_time

                if not order_became_open:
                    # --- Pending path: try to fill ---
                    fill_result = fill_order_if_triggered(
                        repo, order, single_market, event_time=candle_event_time,
                    )
                    results.append(fill_result)

                    # R3-C: classify result — retryable skip_reasons must preserve cursor
                    # and stop processing later candles. The fill opportunity was not
                    # consumed; the next run must re-encounter this candle.
                    skip_reason = fill_result.get("skip_reason")
                    if skip_reason in _RETRYABLE_SKIP_REASONS:
                        # Write ONE idempotent audit record per (order_id, candle_close_time, skip_reason)
                        _log_retryable_skip_audit(
                            repo, order, candle_close_time, skip_reason, fill_result,
                        )
                        # Do NOT advance cursor — break immediately
                        break

                    last_candle_time = candle_close_time
                    prev_candle_close = float(candle["close"])

                    if fill_result.get("filled"):
                        filled_order_ids.add(order["id"])
                        # Update cursor to the candle that triggered the fill
                        repo.conn.execute(
                            "UPDATE paper_orders SET last_processed_candle_time=? WHERE id=?",
                            (last_candle_time, order["id"]),
                        )
                        # Order is now open — transition to open-order path
                        # for subsequent candles. Do NOT evaluate SL/TP on
                        # this same candle (conservative rule).
                        order_became_open = True
                        # Refresh order dict to reflect new status
                        order = dict(repo.conn.execute(
                            "SELECT * FROM paper_orders WHERE id=?", (order["id"],),
                        ).fetchone())
                    continue

                # --- Open path (post-fill): evaluate SL/TP on this candle ---
                # Skip if this order was just filled in this batch (already
                # handled above — filled_order_ids prevents same-candle SL/TP).
                # For post-fill candles, order["id"] is in filled_order_ids but
                # we must still process SL/TP. The filled_order_ids check in the
                # main `open` branch (line ~172) handles orders that were filled
                # in a PREVIOUS batch. Here, within the same candle loop, we
                # explicitly evaluate SL/TP for candles after the fill candle.
                trade = repo.get_open_trade_for_order(order["id"])
                if trade:
                    close_result = close_trade_if_needed(
                        repo, order, trade, single_market,
                        event_time=candle_event_time,
                    )
                    results.append(close_result)
                    if close_result.get("closed"):
                        # Trade closed on this candle — advance cursor and stop
                        last_candle_time = candle_close_time
                        repo.conn.execute(
                            "UPDATE paper_orders SET last_processed_candle_time=? WHERE id=?",
                            (last_candle_time, order["id"]),
                        )
                        prev_candle_close = float(candle["close"])
                        break  # Trade closed, no more processing needed
                    # Not closed: advance cursor to this candle
                    last_candle_time = candle_close_time
                    repo.conn.execute(
                        "UPDATE paper_orders SET last_processed_candle_time=? WHERE id=?",
                        (last_candle_time, order["id"]),
                    )
                else:
                    # No open trade (shouldn't happen, but handle gracefully)
                    last_candle_time = candle_close_time
                    repo.conn.execute(
                        "UPDATE paper_orders SET last_processed_candle_time=? WHERE id=?",
                        (last_candle_time, order["id"]),
                    )
                prev_candle_close = float(candle["close"])

            # After processing all candles, advance cursor for non-fill, non-close cases
            if last_candle_time is not None and not order_became_open:
                repo.conn.execute(
                    "UPDATE paper_orders SET last_processed_candle_time=? WHERE id=?",
                    (last_candle_time, order["id"]),
                )

            # Use the last candle's close as the latest price for equity tracking
            if last_candle_time is not None:
                last_candle_close = candles[-1]["close"]
                latest_prices[symbol] = float(last_candle_close)
                redis.set_latest_price(symbol, latest_prices[symbol])
            else:
                if market_or_price is None:
                    try:
                        mark_price = float(fetch_mark_price(symbol)["markPrice"])
                    except Exception:
                        mark_price = 0.0
                else:
                    mark_price = float(market_or_price) if not isinstance(market_or_price, dict) else float(market_or_price.get("close", 0))
                latest_prices[symbol] = mark_price if mark_price > 0 else latest_prices.get(symbol, 0.0)
                redis.set_latest_price(symbol, latest_prices[symbol])
        elif order["status"] == "open":
            # Skip TP/SL check for orders just filled in this batch
            if order["id"] in filled_order_ids:
                continue
            if market_or_price is None:
                try:
                    mark_price = float(fetch_mark_price(symbol)["markPrice"])
                except Exception:
                    mark_price = 0.0
                market = market_from_price(symbol, mark_price)
            else:
                market = market_or_price if isinstance(market_or_price, dict) else market_from_price(symbol, float(market_or_price))
            latest_prices[symbol] = float(market["close"])
            redis.set_latest_price(symbol, latest_prices[symbol])
            trade = repo.get_open_trade_for_order(order["id"])
            if trade:
                close_result = close_trade_if_needed(repo, order, trade, market)  # live mode: no event_time, wall-clock fallback
                results.append(close_result)
                adjustment = None if close_result.get("closed") else _maybe_adjust_stop_to_breakeven(repo, order, trade, market)
                if adjustment:
                    results.append(adjustment)

    repo.conn.commit()
    snapshot = equity_snapshot(
        ts=utc_ms(),
        closed_realized_pnl=repo.sum_closed_realized_pnl(),
        open_trades=repo.list_open_paper_trades(),
        latest_prices=latest_prices,
        events=results,
    )
    previous_snapshot = repo.latest_equity_snapshot()
    snapshot_id = repo.save_equity_snapshot(snapshot)
    account = repo.update_paper_account_from_snapshot(snapshot)
    _sync_open_positions(repo, latest_prices)
    snapshot["id"] = snapshot_id
    snapshot["paper_account"] = account
    alert_job_id = _maybe_enqueue_drawdown_alert(repo, snapshot, previous_snapshot)
    if alert_job_id:
        snapshot["drawdown_alert_job_id"] = alert_job_id
    evolution = evaluate_evolution_triggers(repo, snapshot=snapshot)
    snapshot["evolution"] = evolution
    agent_execution_review = None
    if results or snapshot.get("drawdown_alert"):
        agent_execution_review = run_agent_json_task(
            task_name="paper_execution_quality_update",
            payload={"events": results, "equity_snapshot": snapshot},
            fallback={
                "summary": "模拟盘执行状态已更新。",
                "quality_findings": [],
                "risk_actions": ["继续按模拟盘风控观察"],
            },
            instructions=[
                "总结模拟盘成交、止盈止损、MFE/MAE、回撤和执行质量。",
                "只允许模拟盘/复盘建议，不得输出实盘下单建议。",
            ],
        )
    if results:
        LOGGER.info("paper update completed results=%s", results)
        _check_daily_loss_trigger(repo, results)
    # Fallback: ensure daily review runs even if scheduler missed the window
    _ensure_daily_review(repo)
    return {"ok": True, "results": results, "equity_snapshot": snapshot, "agent_execution_review": agent_execution_review}


def _ensure_daily_review(repo: CryptoGuardRepository) -> None:
    """Ensure daily review runs for yesterday if not already done."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Only check after 01:00 UTC (09:00 Beijing) to give scheduler a chance
    if now.hour < 1:
        return
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    # Check if daily_review_reports already exists for yesterday (source of truth)
    existing = repo.conn.execute(
        "SELECT id FROM daily_review_reports WHERE review_date=? LIMIT 1",
        (yesterday,),
    ).fetchone()
    if existing:
        return
    # Also check scheduler_runs for success
    existing_run = repo.conn.execute(
        "SELECT id FROM scheduler_runs WHERE job_name='daily_review' AND status='success' AND started_at >= ? AND started_at < ? LIMIT 1",
        (yesterday, now.strftime("%Y-%m-%d")),
    ).fetchone()
    if existing_run:
        return
    LOGGER.info("daily review fallback: enqueuing for %s", yesterday)
    repo.enqueue_job_once(
        "daily_review",
        7,
        "paper_worker",
        f"system:paper:daily_fallback:{yesterday}",
        {"day_utc": yesterday},
    )


def _check_daily_loss_trigger(repo: CryptoGuardRepository, results: list[dict[str, Any]]) -> None:
    """Trigger daily review when 3-5 losses occur in a single day."""
    # Count new stop losses from this batch
    new_sl_count = sum(1 for r in results if r.get("closed") and r.get("close_reason") == "stop_loss")
    if new_sl_count == 0:
        return
    # Count total losses today
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = repo.conn.execute(
        "SELECT COUNT(*) AS cnt FROM paper_trades WHERE close_reason='stop_loss' AND DATE(COALESCE(closed_at, datetime('now')))=?",
        (today,),
    ).fetchone()
    daily_losses = int(row["cnt"]) if row else 0
    LOGGER.info("daily loss check: today=%s losses=%s", today, daily_losses)
    if 3 <= daily_losses <= 5:
        # Check if we already triggered today
        existing = repo.conn.execute(
            "SELECT id FROM agent_jobs WHERE job_type='intraday_loss_review' AND session_id LIKE ? AND created_at >= ?",
            (f"system:paper:intraday_loss:{today}:%", today),
        ).fetchone()
        if not existing:
            repo.enqueue_job_once(
                "intraday_loss_review",
                5,  # high priority
                "paper_worker",
                f"system:paper:intraday_loss:{today}:{daily_losses}",
                {"day_utc": today, "trigger": "daily_loss_threshold", "loss_count": daily_losses},
            )
            LOGGER.info("daily review triggered by loss threshold: %s losses today", daily_losses)
            # Enqueue evolution trigger notification
            repo.enqueue_job_once(
                "evolution_trigger_alert",
                4,  # high priority
                "paper_worker",
                f"system:paper:evolution:{today}",
                {"trigger_type": "daily_loss_threshold", "loss_count": daily_losses, "day_utc": today},
            )


def _maybe_adjust_stop_to_breakeven(repo: CryptoGuardRepository, order: dict[str, Any], trade: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
    """Unified breakeven logic using position_conflict config (P0-5).

    Replaces the old breakeven_after_rr: 2.0 threshold with the same gates
    used by the conflict path: min_hold_minutes, min_current_r_for_breakeven,
    min_mfe_r_for_breakeven. Does NOT require reverse_confirmations (routine
    breakeven doesn't need conflict confirmation).
    """
    from datetime import datetime, timezone
    from plugins.crypto_guard.config.loader import load_config

    try:
        entry = float(trade["entry_price"])
        stop = float(order.get("stop_loss") or order.get("initial_stop_loss") or 0)
        quantity = float(trade.get("quantity") or order.get("quantity") or 0)
    except (TypeError, ValueError):
        return None

    if entry <= 0 or stop <= 0:
        return None

    side = str(order["side"]).upper()
    already_safe = stop >= entry if side == "LONG" else stop <= entry
    if already_safe:
        return None

    # Load unified config from position_conflict section
    cfg = load_config().trading_mode.get("position_conflict") or {}
    min_hold_minutes = int(cfg.get("min_hold_minutes", 15))
    min_current_r_for_breakeven = float(cfg.get("min_current_r_for_breakeven", 0.50))
    min_mfe_r_for_breakeven = float(cfg.get("min_mfe_r_for_breakeven", 0.75))

    # Gate 1: holding time — fail-closed on missing created_at
    created_at = trade.get("created_at")
    if not created_at:
        return None
    holding_minutes = None
    try:
        if isinstance(created_at, str):
            open_time = datetime.fromisoformat(created_at)
        else:
            open_time = created_at
        if open_time.tzinfo is None:
            open_time = open_time.replace(tzinfo=timezone.utc)
        holding_minutes = (datetime.now(timezone.utc) - open_time).total_seconds() / 60
        if holding_minutes < min_hold_minutes:
            return None
    except (ValueError, TypeError):
        return None

    # Gate 2: current_r >= threshold — use fresh mark price
    symbol = order["symbol"]
    mp_result = get_mark_price_with_fallback(symbol, repo=repo)
    if mp_result.get("ok"):
        current_price = float(mp_result["mark_price"])
        price_source = mp_result.get("price_source", "binance_usdm_mark")
        price_as_of = mp_result.get("price_as_of", datetime.now(timezone.utc).isoformat())
        price_age_seconds = mp_result.get("price_age_seconds", -1.0)
    else:
        # Fail-closed: do NOT fall back to market.close. Skip the adjustment
        # so we never move a stop using a stale candle-derived price.
        LOGGER.info("breakeven: mark price fetch failed for %s, skipping adjustment (fail-closed)", symbol)
        return None
    if current_price <= 0:
        return None
    initial_risk_usdt = float(trade.get("initial_risk_usdt") or 0)
    if initial_risk_usdt <= 0:
        return None  # fail-closed: no initial_risk_usdt available
    if side == "LONG":
        current_r = (current_price - entry) * quantity / initial_risk_usdt
    else:
        current_r = (entry - current_price) * quantity / initial_risk_usdt
    if current_r < min_current_r_for_breakeven:
        return None

    # Gate 3: MFE/R >= threshold — MFE/R = max_favorable_excursion_usdt / initial_risk_usdt
    mfe_usdt = float(trade.get("max_favorable_excursion") or 0)
    mfe_r = mfe_usdt / initial_risk_usdt if initial_risk_usdt > 0 else 0
    if mfe_r < min_mfe_r_for_breakeven:
        return None

    # All gates passed — move stop to breakeven.
    # The stop update is atomic and only emits a log when the row actually
    # changed. We must only enqueue the paper_event_alert job when the
    # update happened, otherwise we'd spam duplicate alerts on every tick
    # (a concurrent writer, or a stop already at breakeven, returns False).
    changed = repo.update_paper_order_stop_loss(
        order["id"], entry,
        reason=f"统一保本门禁通过（持仓 {holding_minutes:.0f} 分钟，current_r={current_r:.2f}，MFE/R={mfe_r:.2f}）",
        price_meta={
            "mark_price": current_price,
            "price_source": price_source,
            "price_as_of": price_as_of,
            "price_age_seconds": price_age_seconds,
        },
    )
    if not changed:
        # The atomic UPDATE was rejected (order not open, concurrent writer
        # already moved the stop, or the new stop would widen risk). Do NOT
        # report a successful adjustment or enqueue an alert — that would
        # mislead callers/logs into believing a stop change happened.
        return {"ok": True, "stop_loss_adjusted": False, "order_id": order["id"], "new_stop_loss": entry, "skip_reason": "no_change", "action": "skip"}

    # Idempotency key is keyed on (order, entry) so that re-issuing the
    # SAME breakeven stop is deduped, but raising the stop to a new
    # (higher) breakeven price gets its own job.
    dedupe_session = f"system:paper:stop_adjust:breakeven:{order['id']}:{round(entry, 8)}"
    event_time = datetime.now(timezone.utc).isoformat()
    repo.enqueue_job_once(
        "paper_event_alert",
        3,
        "paper_worker",
        dedupe_session,
        {
            "event_type": "stop_loss_adjustment",
            "symbol": order["symbol"],
            "order_id": order["id"],
            "trade_id": trade["id"],
            "entry_price": entry,
            "new_stop_loss": entry,
            "mark_price": current_price,
            "price_source": price_source,
            "price_as_of": price_as_of,
            "event_time": event_time,
            "current_r": round(current_r, 4),
            "mfe_r": round(mfe_r, 4),
            "reason": "统一保本门禁通过",
            "side": order.get("side"),
            "audit": {
                "open_time": created_at,
                "action_time": event_time,
                "holding_minutes": round(holding_minutes, 1) if holding_minutes else None,
                "current_r": round(current_r, 4),
                "mfe_r": round(mfe_r, 4),
                "gate_result": "all_passed",
            },
        },
    )
    return {"ok": True, "stop_loss_adjusted": True, "order_id": order["id"], "new_stop_loss": entry}


def _evaluate_profit_protection(
    repo: CryptoGuardRepository,
    order: dict[str, Any],
    trade: dict[str, Any],
    ga_decision: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    mark_price_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Evaluate profit protection rule on a profitable position facing a strong reverse signal.

    Runs BEFORE breakeven adjustment. If triggered, closes the full position
    using fresh Binance mark price.

    Conditions (ALL must be true):
    1. ga_decision is actionable (not passive/watch/monitor_only)
    2. signal_grade == "S"
    3. confidence >= min_confidence (default 0.85)
    4. mfe_r >= min_mfe_r (default 1.00)
    5. current_r >= min_current_r (default 0.30)
    6. retracement_r = mfe_r - current_r >= min_retracement_r (default 0.50)

    Returns None if profit protection does not trigger, or a result dict if it does.
    """
    from datetime import datetime, timezone
    from plugins.crypto_guard.config.loader import load_config

    if config is None:
        cfg = load_config().trading_mode.get("position_conflict") or {}
        pp_cfg = cfg.get("profit_protection") or {}
    else:
        pp_cfg = config.get("profit_protection") or {}

    if not pp_cfg.get("enabled", True):
        return None

    # Condition 1: ga_decision is actionable
    decision = str(ga_decision.get("decision") or "").lower()
    if decision in ("opportunity_watch", "monitor_only", "no_edge", ""):
        return None
    # Check risk_check
    risk = ga_decision.get("risk_check_json")
    if risk:
        import json as _json
        if isinstance(risk, str):
            try:
                risk = _json.loads(risk)
            except (_json.JSONDecodeError, TypeError):
                pass
        if isinstance(risk, dict) and risk.get("ok") is False:
            return None
    # Check has trade_plan
    trade_plan = ga_decision.get("trade_plan_json")
    if trade_plan:
        import json as _json
        if isinstance(trade_plan, str):
            try:
                trade_plan = _json.loads(trade_plan)
            except (_json.JSONDecodeError, TypeError):
                trade_plan = None
    if not trade_plan:
        return None

    # Condition 2: signal_grade == "S"
    grade = str(ga_decision.get("signal_grade") or "").upper()
    min_grade = str(pp_cfg.get("min_grade", "S")).upper()
    if grade != min_grade:
        return None

    # Condition 2b: direction verification — profit protection only triggers on
    # EXPLICIT conflict (opposite signal). Same-direction OR neutral/unknown bias
    # must NOT trigger. This is a strict bidirectional gate:
    #   LONG  → require bias == "bearish"; anything else (bullish/neutral/unknown) → no trigger
    #   SHORT → require bias == "bullish";  anything else (bearish/neutral/unknown) → no trigger
    side = str(order["side"]).upper()
    bias = str(ga_decision.get("bias") or ga_decision.get("market_bias") or "neutral").lower()
    # Parse bias from decision field if not directly available
    decision_text = str(ga_decision.get("decision") or "").lower()
    if bias not in ("bullish", "bearish") and decision_text:
        if "bullish" in decision_text:
            bias = "bullish"
        elif "bearish" in decision_text:
            bias = "bearish"
        else:
            bias = "neutral"
    # Strict bidirectional: only explicit opposite-direction conflict triggers
    if side == "LONG":
        if bias != "bearish":
            return None
    elif side == "SHORT":
        if bias != "bullish":
            return None
    else:
        # Unknown side — fail-closed
        return None

    # Condition 3: confidence >= min_confidence
    confidence = float(ga_decision.get("confidence") or 0)
    min_confidence = float(pp_cfg.get("min_confidence", 0.85))
    if confidence < min_confidence:
        return None

    # Compute R-multiples using initial_risk_usdt
    try:
        entry = float(trade["entry_price"])
        quantity = float(trade.get("quantity") or order.get("quantity") or 0)
    except (TypeError, ValueError):
        return None

    if entry <= 0 or quantity <= 0:
        return None

    initial_risk_usdt = float(trade.get("initial_risk_usdt") or 0)
    if initial_risk_usdt <= 0:
        return None  # fail-closed

    side = str(order["side"]).upper()

    # Compute mfe_r from stored max_favorable_excursion
    mfe_usdt = float(trade.get("max_favorable_excursion") or 0)
    mfe_r = mfe_usdt / initial_risk_usdt if initial_risk_usdt > 0 else 0.0

    # Condition 4: mfe_r >= min_mfe_r
    min_mfe_r = float(pp_cfg.get("min_mfe_r", 1.00))
    if mfe_r < min_mfe_r:
        return None

    # Get fresh mark price for current_r computation
    symbol = order["symbol"]
    mp_result = get_mark_price_with_fallback(symbol, repo=repo, cache=mark_price_cache)
    if not mp_result.get("ok"):
        # Fail-closed: log warning and return needs_position_recheck without changing order status
        LOGGER.warning(
            "profit_protection: cannot get fresh mark price for %s order_id=%s, marking needs_position_recheck",
            symbol, order["id"],
        )
        return {
            "ok": False,
            "action": "needs_position_recheck",
            "order_id": order["id"],
            "trade_id": trade["id"],
            "reason": "mark_price_unavailable",
            "mark_price_result": mp_result,
        }

    mark_price = float(mp_result["mark_price"])
    price_source = mp_result.get("price_source", "unknown")
    price_as_of = mp_result.get("price_as_of", "")
    price_age_seconds = float(mp_result.get("price_age_seconds", 0))

    # Compute current_r
    if side == "LONG":
        current_r = (mark_price - entry) * quantity / initial_risk_usdt
    else:
        current_r = (entry - mark_price) * quantity / initial_risk_usdt

    # Condition 5: current_r >= min_current_r
    min_current_r = float(pp_cfg.get("min_current_r", 0.30))
    if current_r < min_current_r:
        return None

    # Condition 6: retracement_r >= min_retracement_r
    retracement_r = mfe_r - current_r
    min_retracement_r = float(pp_cfg.get("min_retracement_r", 0.50))
    if retracement_r < min_retracement_r:
        return None

    # All conditions met — execute profit protection close
    LOGGER.info(
        "profit_protection triggered: order_id=%s symbol=%s side=%s mfe_r=%.2f current_r=%.2f retracement_r=%.2f grade=%s confidence=%.2f",
        order["id"], symbol, side, mfe_r, current_r, retracement_r, grade, confidence,
    )

    return _execute_profit_protection_close(
        repo, order, trade, ga_decision, mark_price, price_source, price_as_of,
        price_age_seconds, current_r, mfe_r, retracement_r,
    )


def _execute_profit_protection_close(
    repo: CryptoGuardRepository,
    order: dict[str, Any],
    trade: dict[str, Any],
    ga_decision: dict[str, Any],
    mark_price: float,
    price_source: str,
    price_as_of: str,
    price_age_seconds: float,
    current_r: float,
    mfe_r: float,
    retracement_r: float,
) -> dict[str, Any]:
    """Execute profit protection close with full side effects.

    Reuses the existing close path: close_paper_trade, update_paper_order_status,
    backfill_active_evaluation_pnl_r, upsert_paper_position_from_trade,
    log_paper_trade_event, enqueue trade_review, enqueue paper_event_alert.
    """
    import json as _json
    from datetime import datetime, timezone

    trade_id = int(trade["id"])
    order_id = int(order["id"])
    symbol = str(order["symbol"])
    side = str(order["side"]).upper()
    ga_decision_id = int(ga_decision.get("id", 0))
    entry_price = float(trade["entry_price"])
    quantity = float(trade.get("quantity") or order.get("quantity") or 0)

    # Idempotent: check order is still open
    current_order = repo.conn.execute(
        "SELECT status FROM paper_orders WHERE id=?", (order_id,),
    ).fetchone()
    if not current_order or current_order["status"] != "open":
        return {
            "ok": False,
            "action": "profit_protection",
            "order_id": order_id,
            "trade_id": trade_id,
            "status": "order_not_open",
            "reason": f"Order status is {current_order['status'] if current_order else 'missing'}",
        }

    # Dedupe: check if already closed for this GA decision
    dedupe_key = f"profit_protection:{trade_id}:{ga_decision_id}"
    existing = repo.conn.execute(
        "SELECT id FROM paper_trade_logs WHERE json_extract(event_json, '$.dedupe_key')=? LIMIT 1",
        (dedupe_key,),
    ).fetchone()
    if existing:
        return {
            "ok": True,
            "action": "profit_protection",
            "order_id": order_id,
            "trade_id": trade_id,
            "status": "duplicate",
            "reason": "Already executed for this GA decision",
        }

    # Compute PnL
    if side == "LONG":
        pnl = (mark_price - entry_price) * quantity
        pnl_r = (mark_price - entry_price) * quantity / float(trade.get("initial_risk_usdt") or 1)
    else:
        pnl = (entry_price - mark_price) * quantity
        pnl_r = (entry_price - mark_price) * quantity / float(trade.get("initial_risk_usdt") or 1)

    pnl_percent = (pnl / (entry_price * quantity)) * 100 if entry_price * quantity != 0 else 0.0

    # Quality metrics
    mfe = float(trade.get("max_favorable_excursion") or 0)
    mae = float(trade.get("max_adverse_excursion") or 0)
    if side == "LONG":
        if mark_price < entry_price:
            mae = max(mae, (entry_price - mark_price) * quantity)
        else:
            mfe = max(mfe, (mark_price - entry_price) * quantity)
    else:
        if mark_price > entry_price:
            mae = max(mae, (mark_price - entry_price) * quantity)
        else:
            mfe = max(mfe, (entry_price - mark_price) * quantity)

    signal_decay = 0.0
    created_at = trade.get("created_at")
    if created_at:
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            minutes = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 60.0)
            signal_decay = min(0.6, minutes / 1440.0)
        except (ValueError, TypeError):
            pass

    # Build stop_take_path
    existing_path = trade.get("stop_take_path_json")
    if existing_path:
        try:
            if isinstance(existing_path, str):
                path = _json.loads(existing_path)
            else:
                path = list(existing_path)
        except (_json.JSONDecodeError, TypeError):
            path = []
    else:
        path = []
    path.append({"event": "profit_protection_close", "ts": datetime.now(timezone.utc).isoformat()})

    # Close the trade — atomic guard: only the winner of a concurrent close
    # proceeds with side effects. If close_paper_trade returns False, a
    # concurrent writer already closed this trade; bail out without
    # backfill / order update / position upsert / logs / enqueues / commit.
    close_reason = "strong_conflict_profit_protection"
    closed = repo.close_paper_trade(
        trade_id=trade_id,
        exit_price=mark_price,
        close_reason=close_reason,
        pnl=pnl,
        pnl_percent=pnl_percent,
        pnl_r=pnl_r,
        mfe=mfe,
        mae=mae,
        signal_decay_score=signal_decay,
        stop_take_path=path,
        allow_wall_clock=True,
    )
    if not closed:
        return {
            "ok": True,
            "action": "profit_protection",
            "order_id": order_id,
            "trade_id": trade_id,
            "status": "already_closed",
            "reason": "concurrent close",
        }

    # Backfill real PnL to active evaluations
    repo.backfill_active_evaluation_pnl_r(trade, pnl_r)

    # Update paper_orders
    now = datetime.now(timezone.utc).isoformat()
    repo.update_paper_order_status(order_id, "closed", closed_at=now)
    repo.conn.execute(
        "UPDATE paper_orders SET cancel_reason=?, invalidated_by_ga_decision_id=? WHERE id=?",
        (f"profit_protection: GA#{ga_decision_id} strong reverse signal", ga_decision_id, order_id),
    )

    # Update paper_positions
    account = repo.ensure_paper_account()
    repo.upsert_paper_position_from_trade(
        account_id=int(account["id"]),
        trade=trade,
        status="closed",
        current_price=mark_price,
        unrealized_pnl=0.0,
        unrealized_pnl_pct=0.0,
        allow_wall_clock=True,
    )

    # Log close_position event
    repo.log_paper_trade_event(
        position_id=trade_id,
        event_type="close_position",
        symbol=symbol,
        side=side,
        price=mark_price,
        quantity=quantity,
        pnl=pnl,
        pnl_pct=pnl_percent,
        reason=close_reason,
        event={
            "order_id": order_id,
            "trade_id": trade_id,
            "pnl_r": round(pnl_r, 4),
            "ga_decision_id": ga_decision_id,
            "mark_price": mark_price,
            "price_source": price_source,
            "price_as_of": price_as_of,
            "price_age_seconds": price_age_seconds,
            "current_r": round(current_r, 4),
            "mfe_r": round(mfe_r, 4),
            "retracement_r": round(retracement_r, 4),
            "dedupe_key": dedupe_key,
        },
    )

    # Log profit_protection event
    repo.log_paper_trade_event(
        event_type="profit_protection",
        symbol=symbol,
        side=side,
        price=mark_price,
        quantity=quantity,
        pnl=pnl,
        pnl_pct=pnl_percent,
        reason=f"Profit protection: GA#{ga_decision_id} S-grade reverse signal, MFE={mfe_r:.2f}R current={current_r:.2f}R retracement={retracement_r:.2f}R",
        event={
            "order_id": order_id,
            "trade_id": trade_id,
            "ga_decision_id": ga_decision_id,
            "signal_grade": ga_decision.get("signal_grade"),
            "confidence": ga_decision.get("confidence"),
            "mark_price": mark_price,
            "price_source": price_source,
            "price_as_of": price_as_of,
            "price_age_seconds": price_age_seconds,
            "current_r": round(current_r, 4),
            "mfe_r": round(mfe_r, 4),
            "retracement_r": round(retracement_r, 4),
            "close_reason": close_reason,
            "dedupe_key": dedupe_key,
        },
    )

    # Enqueue trade_review
    repo.enqueue_job("trade_review", 4, "paper_worker", f"system:review:{trade_id}", {"trade_id": trade_id})

    # Enqueue paper_event_alert — use enqueue_job_once to prevent duplicate notifications
    repo.enqueue_job_once(
        "paper_event_alert",
        3,
        "paper_worker",
        f"system:paper:profit_protection:{trade_id}:{ga_decision_id}",
        {
            "event_type": "close_position",
            "symbol": symbol,
            "order_id": order_id,
            "trade_id": trade_id,
            "exit_price": mark_price,
            "close_reason": close_reason,
            "pnl_r": round(pnl_r, 4),
            "side": side,
            "entry_price": entry_price,
            "stop_loss": order.get("stop_loss"),
            "mark_price": mark_price,
            "price_source": price_source,
            "price_as_of": price_as_of,
            "price_age_seconds": price_age_seconds,
            "current_r": round(current_r, 4),
            "mfe_r": round(mfe_r, 4),
            "retracement_r": round(retracement_r, 4),
            "take_profits": _json.loads(order.get("take_profit_json") or "[]") if order.get("take_profit_json") else [],
            "filled_at": order.get("filled_at"),
            "closed_at": now,
            "event_time": now,
            "quantity": quantity,
            "order_type": order.get("order_type"),
            "fill_method": order.get("fill_method"),
            "ga_decision_id": ga_decision_id,
        },
    )

    repo.conn.commit()

    LOGGER.info(
        "profit_protection_close: trade_id=%s symbol=%s side=%s exit_price=%s pnl_r=%.2f ga=%s",
        trade_id, symbol, side, mark_price, pnl_r, ga_decision_id,
    )

    return {
        "ok": True,
        "action": "profit_protection",
        "order_id": order_id,
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "exit_price": mark_price,
        "pnl_r": round(pnl_r, 4),
        "pnl": round(pnl, 4),
        "mfe_r": round(mfe_r, 4),
        "current_r": round(current_r, 4),
        "retracement_r": round(retracement_r, 4),
        "ga_decision_id": ga_decision_id,
        "event_time": now,
        "close_reason": close_reason,
        "status": "executed",
    }


def _sync_open_positions(repo: CryptoGuardRepository, latest_prices: dict[str, float]) -> None:
    account = repo.ensure_paper_account()
    for trade in repo.list_open_paper_trades():
        price = latest_prices.get(trade["symbol"])
        if price is None:
            continue
        side = str(trade["side"]).upper()
        quantity = float(trade.get("quantity") or 1)
        pnl = (float(price) - float(trade["entry_price"])) * (1 if side == "LONG" else -1) * quantity
        pnl_pct = ((float(price) - float(trade["entry_price"])) * (1 if side == "LONG" else -1)) / float(trade["entry_price"]) * 100 if trade.get("entry_price") else 0.0
        repo.upsert_paper_position_from_trade(
            account_id=int(account["id"]),
            trade={**trade, "current_price": price},
            status="open",
            current_price=price,
            unrealized_pnl=pnl,
            unrealized_pnl_pct=pnl_pct,
            allow_wall_clock=True,
        )


def _maybe_enqueue_drawdown_alert(repo: CryptoGuardRepository, snapshot: dict[str, Any], previous: dict[str, Any] | None) -> int | None:
    if not snapshot.get("drawdown_alert"):
        return None
    previous_alert = False
    if previous:
        import json

        try:
            previous_alert = bool(json.loads(previous.get("snapshot_json") or "{}").get("drawdown_alert"))
        except Exception:
            previous_alert = False
    if previous_alert:
        return None
    return repo.enqueue_job(
        "paper_drawdown_alert",
        3,
        "paper_worker",
        "system:paper:drawdown",
        {"snapshot": snapshot},
    )
