from __future__ import annotations

from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.data.binance_rest import MarketDataError
from plugins.crypto_guard.data.candle_backfill import backfill_symbol_interval
from plugins.crypto_guard.data.candle_store import fetch_and_upsert_closed_klines
from plugins.crypto_guard.data.market_data_health import assess_health
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db
from plugins.crypto_guard.utils import INTERVAL_MS, latest_closed_close_time_ms, utc_ms


LOGGER = get_logger("crypto_guard.scheduler")


def fetch_closed_klines_for_active_symbols(interval: str, lookback: int, *, analysis_time_utc: int | None = None) -> dict[str, Any]:
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        analysis_time = latest_closed_close_time_ms(interval, analysis_time_utc or utc_ms())
        results = []
        for symbol in repo.active_analysis_symbols():
            try:
                result = fetch_and_upsert_closed_klines(repo, symbol, interval, analysis_time_utc=analysis_time, lookback=lookback)
            except MarketDataError as exc:
                LOGGER.warning("fetch_closed_klines failed symbol=%s interval=%s error=%s", symbol, interval, exc)
                result = {"ok": False, "symbol": symbol, "interval": interval, "error": str(exc), "analysis_time_utc": analysis_time}
            if interval in {"1d", "4h", "1h"}:
                try:
                    result["agent_summary"] = summarize_higher_timeframe(repo, symbol, interval, analysis_time)
                except Exception as exc:
                    result["agent_summary"] = {"ok": False, "error": str(exc)}
            results.append(result)
        return {"ok": all(item.get("ok") for item in results), "interval": interval, "analysis_time_utc": analysis_time, "results": results}
    finally:
        conn.close()


def summarize_higher_timeframe(repo: CryptoGuardRepository, symbol: str, interval: str, analysis_time_utc: int) -> dict[str, Any]:
    candles = repo.get_candles(symbol, interval, analysis_time_utc=analysis_time_utc, limit=80)
    fallback = {
        "summary": f"{symbol} {interval} K 线已更新，等待后续多周期分析引用。",
        "trend_context": "unknown",
        "key_levels": [],
        "risk_notes": [],
    }
    agent = run_agent_json_task(
        task_name="higher_timeframe_kline_summary",
        payload={
            "symbol": symbol,
            "interval": interval,
            "analysis_time_utc": int(analysis_time_utc),
            "recent_candles": candles[-40:],
        },
        fallback=fallback,
        instructions=[
            "总结高周期 K 线背景，提取趋势状态、关键区域和风险，供低周期巡航复用。",
            "只基于已收盘 K 线，不得使用未来函数，不得输出实盘建议。",
        ],
    )
    repo.save_module_result(symbol, interval, analysis_time_utc, "ga_higher_timeframe_summary", agent, None)
    return agent


def enqueue_market_analysis(
    *,
    analysis_time_utc: int | None = None,
    mode: str = "scheduled",
    primary_interval: str = "5m",
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    # P1-2 R4: Readiness gate — defer analysis when market-data warmup hasn't
    # reached the "ready" state. The state machine has 3 explicit states:
    #   "pending" — warmup started but not finished (defer)
    #   "ready"   — warmup succeeded AND not degraded (allow)
    #   "failed"  — warmup raised, returned degraded, or timed out (defer)
    # Only "ready" opens the gate. The next periodic warmup job can recover
    # from "failed" to "ready" on a subsequent successful run.
    # In tests/CLI (where start_all_services isn't called), state defaults to
    # "ready" so analysis proceeds normally.
    from plugins.crypto_guard.service_manager import is_warmup_complete, get_warmup_state
    if not is_warmup_complete():
        LOGGER.info(
            "enqueue_market_analysis: deferred — warmup state: %s (primary_interval=%s)",
            get_warmup_state(), primary_interval,
        )
        return {
            "ok": False,
            "deferred": True,
            "reason": "warmup_not_complete",
            "warmup_state": get_warmup_state(),
            "primary_interval": primary_interval,
            "analysis_time_utc": analysis_time_utc,
            "queued": 0,
        }

    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        analysis_time = latest_closed_close_time_ms(primary_interval, analysis_time_utc or utc_ms())
        # Hourly Report Accuracy: register a single analysis_batches row that
        # aggregates this scheduler tick across every enabled symbol. ga_decisions
        # reference this batch_id so the report renderer can gate on completion.
        batch_id = f"{primary_interval}:{analysis_time}"
        enabled_symbols = repo.active_analysis_symbols()
        repo.start_analysis_batch(
            batch_id=batch_id,
            primary_interval=primary_interval,
            analysis_time=analysis_time,
            enabled_symbols=enabled_symbols,
        )
        job_ids: list[int] = []
        skipped_pending = 0
        priority = 6 if primary_interval == "5m" else 5
        for symbol in enabled_symbols:
            session_id = f"system:scheduled:{primary_interval}:{symbol}:{analysis_time}"
            pending = conn.execute(
                """
                SELECT 1
                FROM agent_jobs
                WHERE job_type='scheduled_market_analysis'
                  AND session_id=?
                  AND status IN ('pending', 'running')
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if pending:
                skipped_pending += 1
                # The symbol already has a pending/running job for this tick;
                # P0-4: mark it as 'pending' (not 'completed') in batch_symbol_status
                # so _await_batch_completion won't count it as completed.
                repo.mark_batch_symbol_completed(batch_id=batch_id, symbol=symbol, status="pending")
                continue
            snapshot = build_market_state_snapshot(repo, symbol=symbol, analysis_time_utc=analysis_time, mode=mode, timeframes=timeframes)
            snapshot_id = repo.save_market_snapshot(snapshot)
            # Pass batch_id through the job payload so the controller can attach
            # it to the ga_decisions row it creates.
            payload = {
                "snapshot_id": snapshot_id,
                "snapshot": snapshot,
                "primary_interval": primary_interval,
                "batch_id": batch_id,
            }
            job_id = repo.enqueue_job(
                "scheduled_market_analysis",
                priority,
                "scheduler",
                session_id,
                payload,
            )
            job_ids.append(job_id)
        return {
            "ok": True,
            "primary_interval": primary_interval,
            "analysis_time_utc": analysis_time,
            "batch_id": batch_id,
            "queued": len(job_ids),
            "skipped_pending": skipped_pending,
            "priority": priority,
            "job_ids": job_ids,
        }
    finally:
        conn.close()


def enqueue_15m_analysis(*, analysis_time_utc: int | None = None, mode: str = "scheduled") -> dict[str, Any]:
    return enqueue_market_analysis(analysis_time_utc=analysis_time_utc, mode=mode, primary_interval="15m")


def market_data_warmup(*, analysis_time_utc: int | None = None) -> dict[str, Any]:
    """R5: Pre-analysis market-data warmup job.

    For each active symbol and each TF in ``cfg.market_data.required_samples``,
    check health; if not ready, run ``backfill_symbol_interval`` with the
    configured ``max_pages_per_run`` budget. Uses ``task_locks`` dedup so
    only one backfill per ``(symbol, interval)`` proceeds at a time.

    This job runs on cron (every 5 min) and once at startup before the
    scheduler loop begins. It must never raise — per-(symbol, TF) exceptions
    are caught and logged.

    P1-2 R4: This function transitions the warmup state machine:
      - On success (no degradation): ``_set_warmup_ready()``
      - On degraded result: ``_set_warmup_failed("degraded")``
      - On exception: ``_set_warmup_failed(str(exc))``
    This allows the periodic cron job to recover from "failed" to "ready"
    on a subsequent successful run.

    Returns a structured summary of per-TF status for diagnostics and the
    hourly report.
    """
    # P1-2 R4: Transition the warmup state machine. Wrap the entire body
    # so that any exception (even from load_config/initialize_database)
    # transitions to "failed" instead of leaving the gate in "pending".
    from plugins.crypto_guard.service_manager import _set_warmup_ready, _set_warmup_failed
    try:
        result = _market_data_warmup_impl(analysis_time_utc=analysis_time_utc)
        if result.get("degraded"):
            _set_warmup_failed("degraded")
        else:
            _set_warmup_ready()
        return result
    except Exception as exc:
        LOGGER.exception("market_data_warmup: unhandled exception — transitioning to failed")
        _set_warmup_failed(str(exc))
        return {
            "ok": False,
            "degraded": True,
            "error": str(exc),
            "symbols": {},
        }


def _market_data_warmup_impl(*, analysis_time_utc: int | None = None) -> dict[str, Any]:
    """Implementation of market_data_warmup — separated so the wrapper can
    catch top-level exceptions and transition the state machine.
    """
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        now = utc_ms()
        required_samples = cfg.market_data.get("required_samples", {})
        if not required_samples:
            return {"ok": True, "degraded": False, "symbols": {}, "reason": "no_required_samples"}

        symbols = repo.active_analysis_symbols()
        backfill_cfg = cfg.market_data.get("backfill", {})
        max_pages = int(backfill_cfg.get("max_pages_per_run", 50))
        backfill_enabled = bool(backfill_cfg.get("enabled", True))

        per_symbol: dict[str, dict[str, Any]] = {}
        any_degraded = False

        for symbol in symbols:
            per_tf: dict[str, Any] = {}
            for tf, required_count in required_samples.items():
                tf_required = int(required_count)
                # Use a TF-appropriate analysis_time: the latest closed candle
                # boundary for this interval at the current time.
                span = INTERVAL_MS.get(tf)
                if span is None:
                    per_tf[tf] = {"ready": False, "reason": "invalid_interval"}
                    any_degraded = True
                    continue
                tf_analysis_time = latest_closed_close_time_ms(tf, analysis_time_utc or now)

                try:
                    health = assess_health(
                        repo, symbol, tf,
                        analysis_time_utc=tf_analysis_time,
                        required_count=tf_required,
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "market_data_warmup: assess_health failed symbol=%s interval=%s error=%s",
                        symbol, tf, exc,
                    )
                    per_tf[tf] = {
                        "ready": False, "reason": "assess_error",
                        "contiguous_tail_count": 0, "required_count": tf_required,
                        "gap_count": 0, "largest_gap_bars": 0,
                        "last_close_time": None, "error": str(exc),
                    }
                    any_degraded = True
                    continue

                if not health["ready"] and backfill_enabled:
                    LOGGER.info(
                        "market_data_warmup: backfilling symbol=%s interval=%s "
                        "contiguous=%d required=%d reason=%s",
                        symbol, tf, health["contiguous_tail_count"],
                        tf_required, health["reason"],
                    )
                    try:
                        backfill_symbol_interval(
                            repo, symbol, tf,
                            analysis_time_utc=tf_analysis_time,
                            required_count=tf_required,
                            max_pages=max_pages,
                        )
                        # Re-assess after backfill.
                        health = assess_health(
                            repo, symbol, tf,
                            analysis_time_utc=tf_analysis_time,
                            required_count=tf_required,
                        )
                    except Exception as exc:
                        LOGGER.warning(
                            "market_data_warmup: backfill failed symbol=%s interval=%s error=%s",
                            symbol, tf, exc,
                        )
                        health = {
                            "ready": False, "reason": "backfill_error",
                            "contiguous_tail_count": health["contiguous_tail_count"],
                            "required_count": tf_required,
                            "gap_count": health.get("gap_count", 0),
                            "largest_gap_bars": health.get("largest_gap_bars", 0),
                            "last_close_time": health.get("last_close_time"),
                            "error": str(exc),
                        }

                per_tf[tf] = {
                    "ready": health["ready"],
                    "reason": health.get("reason", ""),
                    "contiguous_tail_count": health["contiguous_tail_count"],
                    "required_count": tf_required,
                    "gap_count": health.get("gap_count", 0),
                    "largest_gap_bars": health.get("largest_gap_bars", 0),
                    "last_close_time": health.get("last_close_time"),
                    "total_closed_count": health.get("total_closed_count", 0),
                }
                if not health["ready"]:
                    any_degraded = True

            per_symbol[symbol] = per_tf

        LOGGER.info(
            "market_data_warmup: symbols=%d degraded=%s",
            len(symbols), any_degraded,
        )

        return {
            "ok": True,
            "degraded": any_degraded,
            "symbols": per_symbol,
            "analysis_time_utc": now,
        }
    finally:
        conn.close()
