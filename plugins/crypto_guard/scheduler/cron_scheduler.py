from __future__ import annotations

from typing import Any

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.data.binance_rest import MarketDataError
from plugins.crypto_guard.data.candle_store import fetch_and_upsert_closed_klines
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db
from plugins.crypto_guard.utils import latest_closed_close_time_ms, utc_ms


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
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        analysis_time = latest_closed_close_time_ms(primary_interval, analysis_time_utc or utc_ms())
        job_ids: list[int] = []
        skipped_pending = 0
        priority = 6 if primary_interval == "5m" else 5
        for symbol in repo.active_analysis_symbols():
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
                continue
            snapshot = build_market_state_snapshot(repo, symbol=symbol, analysis_time_utc=analysis_time, mode=mode, timeframes=timeframes)
            snapshot_id = repo.save_market_snapshot(snapshot)
            job_id = repo.enqueue_job(
                "scheduled_market_analysis",
                priority,
                "scheduler",
                session_id,
                {"snapshot_id": snapshot_id, "snapshot": snapshot, "primary_interval": primary_interval},
            )
            job_ids.append(job_id)
        return {
            "ok": True,
            "primary_interval": primary_interval,
            "analysis_time_utc": analysis_time,
            "queued": len(job_ids),
            "skipped_pending": skipped_pending,
            "priority": priority,
            "job_ids": job_ids,
        }
    finally:
        conn.close()


def enqueue_15m_analysis(*, analysis_time_utc: int | None = None, mode: str = "scheduled") -> dict[str, Any]:
    return enqueue_market_analysis(analysis_time_utc=analysis_time_utc, mode=mode, primary_interval="15m")
