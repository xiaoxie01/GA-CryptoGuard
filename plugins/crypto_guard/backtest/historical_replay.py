from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
from plugins.crypto_guard.storage.parquet_archive import read_klines_file
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


# ---------------------------------------------------------------------------
# Scratch-schema isolation for historical replay / paired backtest
# ---------------------------------------------------------------------------
#
# Replays run against an ISOLATED PostgreSQL schema on a dedicated
# (non-pooled) connection, NOT a temp SQLite file. The old cutover built a
# ``historical_replay.sqlite3`` via ``connect_db``; under the PG-only runtime
# that path no longer exists. Instead we:
#   1. Open a dedicated ``psycopg.connect`` to the same DSN the pool uses
#      (resolved from ``CRYPTO_GUARD_DATABASE_URL``), with ``search_path`` set
#      to a fresh unique schema so writes never touch the production ``public``
#      schema (and the production pool is never opened by the replay).
#   2. Apply ``schema_postgres.sql`` + the default symbol/strategy seeds
#      directly into that schema (the schema SQL is schema-agnostic - it uses
#      no ``public.`` qualification, so ``CREATE TABLE`` lands in ``search_path``).
#      We deliberately do NOT call ``initialize_database()`` (which targets the
#      production pool / advisory lock / contract markers).
#   3. Yield a ``CryptoGuardRepository`` bound to that connection. Every repo
#      write self-wraps ``conn.transaction()`` and resolves to the scratch
#      schema, so ``build_market_state_snapshot`` / ``upsert_candles`` /
#      ``no_lookahead_candles`` read+write only the scratch schema.
#   4. On exit, ``DROP SCHEMA ... CASCADE`` + close the connection - no residue.


def _validate_replay_connected_identity(conn: psycopg.Connection) -> None:
    """Verify the authenticated replay principal before any replay DDL."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_user AS current_user, session_user AS session_user,
                   current_database() AS database_name,
                   r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                   r.rolreplication, r.rolbypassrls
            FROM pg_roles r WHERE r.rolname=current_user
            """
        )
        row = cur.fetchone()
    allowed = {
        ("crypto_guard_replay", "crypto_guard_replay"),
        ("crypto_guard_test_app", "crypto_guard_test"),
    }
    if not row:
        raise RuntimeError("PostgreSQL replay identity is unavailable")
    current = str(row["current_user"])
    session = str(row["session_user"])
    database = str(row["database_name"])
    dangerous = any(bool(row[key]) for key in (
        "rolsuper", "rolcreatedb", "rolcreaterole",
        "rolreplication", "rolbypassrls",
    ))
    if current != session or (current, database) not in allowed or dangerous:
        raise RuntimeError(
            "PostgreSQL replay principal violates the dedicated-role contract"
        )


_SCRATCH_SCHEMA_RE = re.compile(r"^replay_[0-9a-f]{32}$")

_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")


def _safe_error_summary(exc: BaseException) -> str:
    """Short failure summary safe for external surfaces.

    Never renders ``str(exc)``/``repr(exc)``: exception messages can embed
    the DSN, credentials, or arbitrary database text. Only the exception
    type name plus an optional strictly-validated SQLSTATE
    (``^[0-9A-Z]{5}$``) is allowed. The ``sqlstate`` read is fail-safe: a
    raising getter is ignored and never replaces the underlying error.
    """
    summary = type(exc).__name__
    try:
        sqlstate = getattr(exc, "sqlstate", None)
    except Exception:
        sqlstate = None
    if isinstance(sqlstate, str) and _SQLSTATE_RE.match(sqlstate):
        summary += f" (SQLSTATE {sqlstate})"
    return summary


def _drop_scratch_schema(schema_name: str, dsn: str) -> str | None:
    """Drop the scratch schema via a dedicated connection; never silent.

    Runs on a FRESH connection (autocommit) so it never depends on the replay
    connection's transaction state. The schema name must match the
    ``replay_<32hex>`` scratch contract before any DROP. Returns ``None`` on
    success or a short human-readable failure description (never the DSN or
    any credential) when cleanup itself failed.
    """
    if not _SCRATCH_SCHEMA_RE.match(schema_name):
        raise RuntimeError(f"refusing to drop non-scratch schema {schema_name!r}")
    conn: psycopg.Connection | None = None
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        _validate_replay_connected_identity(conn)
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_namespace WHERE nspname = %s",
                (schema_name,),
            )
            row = cur.fetchone()
            if row is None or row["count"] != 0:
                return "schema still exists after DROP"
        return None
    except Exception as exc:
        return f"cleanup failure: {_safe_error_summary(exc)}"
    finally:
        # ``conn.close`` may itself fail (e.g. the server died mid-cleanup);
        # it must never replace the returned failure string or the in-flight
        # body exception, so guard it the same way the body guard does.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _attach_cleanup_failure(body_exc: BaseException, failure: str) -> None:
    """Attach a cleanup-failure detail to the in-flight body exception.

    ``BaseException.add_note`` requires Python 3.11+ (PEP 678); on older
    runtimes fall back to chaining a ``RuntimeError`` via ``__cause__`` so the
    cleanup failure is never lost.
    """
    message = f"scratch schema cleanup failed: {failure}"
    add_note = getattr(body_exc, "add_note", None)
    if callable(add_note):
        add_note(message)
    else:
        body_exc.__cause__ = RuntimeError(message)


@contextmanager
def _scratch_replay_repo() -> Iterator[CryptoGuardRepository]:
    """Yield a repo bound to an isolated scratch schema; drop it on exit.

    The replay reads/writes only its own schema; the caller's production repo
    (used to persist ``historical_replay_results``) is untouched by the replay
    loop itself. Fail-closed: if the scratch connection cannot be opened or
    seeded, the error propagates (NO SQLite fallback).
    """
    from plugins.crypto_guard.storage import pg_db
    from plugins.crypto_guard.storage.migrations import SCHEMA_PATH, _seed_symbols, _seed_strategies

    schema_name = f"replay_{uuid.uuid4().hex}".lower()
    from plugins.crypto_guard.config.loader import resolve_replay_database_url

    dsn = resolve_replay_database_url()
    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
    body_exc: BaseException | None = None
    try:
        _validate_replay_connected_identity(conn)
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema_name}"')
        # Route all unqualified table names to the scratch schema. We commit
        # the CREATE SCHEMA + SET search_path as its own transaction so the
        # schema exists before we apply the DDL into it.
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(f'SET search_path = "{schema_name}", public')
        # Apply the schema SQL + seeds directly into the scratch schema. The
        # schema SQL is idempotent (IF NOT EXISTS / ON CONFLICT); seeds are
        # upserts. Run as one transaction so a seed failure drops nothing
        # into the scratch schema half-built (we DROP on exit regardless).
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        cfg = load_config()
        with conn.cursor() as cur:
            cur.execute(schema_sql)
            _seed_symbols(cur, cfg.symbols)
            _seed_strategies(cur, cfg.strategies)
        conn.commit()
        yield CryptoGuardRepository(conn)
    except BaseException as exc:
        body_exc = exc
        raise
    finally:
        # NEVER flip ``autocommit`` on a connection that may still be INTRANS
        # (psycopg raises ProgrammingError). Roll back if the connection is
        # still usable, close it, then drop the schema through a dedicated
        # cleanup connection that owns its own transaction state.
        try:
            conn.rollback()
        except Exception:
            pass
        # ``conn.close`` may itself fail (e.g. the server died mid-replay);
        # it must never skip the schema DROP, so guard it the same way.
        try:
            conn.close()
        except Exception:
            pass
        failure = _drop_scratch_schema(schema_name, dsn)
        if failure is not None:
            if body_exc is None:
                raise RuntimeError(
                    f"scratch schema cleanup failed: {failure}"
                ) from None
            _attach_cleanup_failure(body_exc, failure)


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------


def _simulate_trade(
    trade_plan: dict[str, Any],
    subsequent_candles: list[dict[str, Any]],
    max_holding_candles: int = 20,
) -> dict[str, Any]:
    """Simulate a trade using real price data.

    Walks through subsequent candles checking whether the price path
    hits stop-loss or take-profit, and calculates the actual R-multiple.
    """
    entry = trade_plan.get("entry_price")
    sl = trade_plan.get("stop_loss")
    tps = trade_plan.get("take_profits", [])
    tp = tps[0]["price"] if tps else None

    if entry is None or sl is None:
        return {"outcome": "invalid", "pnl_r": 0.0, "holding_candles": 0, "exit_price": None}

    risk = abs(entry - sl)
    if risk <= 0:
        return {"outcome": "invalid", "pnl_r": 0.0, "holding_candles": 0, "exit_price": None}

    side = trade_plan.get("side", "LONG")

    for i, candle in enumerate(subsequent_candles[:max_holding_candles]):
        high = float(candle["high"])
        low = float(candle["low"])

        if side == "LONG":
            # Check SL first (conservative: worst-case fill)
            if low <= sl:
                return {"outcome": "loss", "pnl_r": -1.0, "holding_candles": i + 1, "exit_price": sl}
            # Check TP
            if tp is not None and high >= tp:
                pnl_r = (tp - entry) / risk
                return {"outcome": "win", "pnl_r": round(pnl_r, 4), "holding_candles": i + 1, "exit_price": tp}
        else:  # SHORT
            if high >= sl:
                return {"outcome": "loss", "pnl_r": -1.0, "holding_candles": i + 1, "exit_price": sl}
            if tp is not None and low <= tp:
                pnl_r = (entry - tp) / risk
                return {"outcome": "win", "pnl_r": round(pnl_r, 4), "holding_candles": i + 1, "exit_price": tp}

    # Timeout: close at last candle's close
    last_idx = min(max_holding_candles - 1, len(subsequent_candles) - 1)
    if last_idx < 0:
        return {"outcome": "timeout", "pnl_r": 0.0, "holding_candles": 0, "exit_price": entry}
    last_close = float(subsequent_candles[last_idx]["close"])
    if side == "LONG":
        pnl_r = (last_close - entry) / risk
    else:
        pnl_r = (entry - last_close) / risk
    return {
        "outcome": "timeout",
        "pnl_r": round(pnl_r, 4),
        "holding_candles": last_idx + 1,
        "exit_price": last_close,
    }


# ---------------------------------------------------------------------------
# Market regime classification
# ---------------------------------------------------------------------------


def _classify_market_regime(candles: list[dict[str, Any]], lookback: int = 20) -> str:
    """Classify market regime using simple swing analysis.

    Returns one of: trending_up, trending_down, ranging, volatile.
    Uses recent candles to detect HH/HL (trending_up) or LH/LL (trending_down).
    """
    if len(candles) < lookback:
        return "ranging"

    recent = candles[-lookback:]
    closes = [float(c["close"]) for c in recent]
    highs = [float(c["high"]) for c in recent]
    lows = [float(c["low"]) for c in recent]

    # Detect swing highs and lows using a simple 3-bar pattern
    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(1, len(recent) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append(highs[i])
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swing_lows.append(lows[i])

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        # Not enough swings; check volatility
        price_range = (max(highs) - min(lows)) / min(lows) if min(lows) > 0 else 0
        return "volatile" if price_range > 0.08 else "ranging"

    # Check for HH/HL pattern
    hh = swing_highs[-1] > swing_highs[-2]
    hl = swing_lows[-1] > swing_lows[-2]
    if hh and hl:
        return "trending_up"

    # Check for LH/LL pattern
    lh = swing_highs[-1] < swing_highs[-2]
    ll = swing_lows[-1] < swing_lows[-2]
    if lh and ll:
        return "trending_down"

    # Check volatility: wide range relative to price
    price_range = (max(highs) - min(lows)) / min(lows) if min(lows) > 0 else 0
    if price_range > 0.08:
        return "volatile"

    return "ranging"


def _evaluate_conditional_adjustment(
    candidate_patch: dict[str, Any] | None,
    snapshot: dict[str, Any],
    regime: str,
    active_decision: dict[str, Any] | None = None,
    market_bias: str = "",
) -> tuple[float, dict[str, int]]:
    """Evaluate when-conditioned score adjustments from the candidate patch.

    If patch has score_adjustments with when-clauses (e.g., when: {side: "LONG"}),
    only apply adjustments when the current context matches the when condition.

    Returns (cumulative_score_adjustment, trigger_counts) tuple.
    """
    if not candidate_patch:
        return 0.0, {}

    score_adj = candidate_patch.get("score_adjustments") or candidate_patch.get("score_adjustment")
    if score_adj is None:
        return 0.0, {}

    # Simple flat float
    if isinstance(score_adj, (int, float)):
        return float(score_adj), {}

    # Dict of named adjustments with optional when clauses
    if not isinstance(score_adj, dict):
        return 0.0, {}

    cumulative = 0.0
    trigger_counts: dict[str, int] = {}

    # Extract context from snapshot — read from modules for proper structure
    modules = snapshot.get("modules") or {}
    market_regime = modules.get("market_regime") or {}
    trend_stage_module = modules.get("trend_stage") or {}

    # Read side and entry_type from active_decision.trade_plan (not snapshot)
    if active_decision:
        trade_plan = active_decision.get("trade_plan") or {}
        if isinstance(trade_plan, str):
            try:
                trade_plan = json.loads(trade_plan)
            except (json.JSONDecodeError, TypeError):
                trade_plan = {}
        side = str(trade_plan.get("side", "")).upper() or ""
        entry_type = str(trade_plan.get("entry_type") or "")
    else:
        side = str(snapshot.get("side", "")).upper() or ""
        trade_plan = snapshot.get("trade_plan") or {}
        if isinstance(trade_plan, str):
            try:
                trade_plan = json.loads(trade_plan)
            except (json.JSONDecodeError, TypeError):
                trade_plan = {}
        entry_type = str(trade_plan.get("entry_type") or "")

    market_phase = str(market_regime.get("market_phase") or regime or "")
    trend_stage = str(trend_stage_module.get("trend_stage") or "")

    for adj_name, adj_val in score_adj.items():
        if isinstance(adj_val, (int, float)):
            cumulative += float(adj_val)
            continue
        if isinstance(adj_val, dict):
            value = float(adj_val.get("value", 0))
            when = adj_val.get("when", {})
            if not when:
                cumulative += value
                continue
            # Evaluate when conditions with full context
            if _matches_when(when, side=side, market_phase=market_phase,
                            trend_stage=trend_stage, entry_type=entry_type,
                            market_bias=market_bias):
                cumulative += value
                key = f"{adj_name}:matched"
            else:
                key = f"{adj_name}:skipped"
            trigger_counts[key] = trigger_counts.get(key, 0) + 1

    return cumulative, trigger_counts


def _matches_when(
    when: dict[str, Any],
    *,
    side: str,
    market_phase: str,
    trend_stage: str = "",
    entry_type: str = "",
    market_bias: str = "",
) -> bool:
    """Check whether current context matches a when-condition dict.

    Supports ALL when condition keys: side, market_phase, trend_stage, entry_type, market_bias.
    """
    when_side = str(when.get("side", "")).upper() if when.get("side") else ""
    when_phase = str(when.get("market_phase", "")).lower() if when.get("market_phase") else ""
    when_trend = str(when.get("trend_stage", "")).lower() if when.get("trend_stage") else ""
    when_entry = str(when.get("entry_type", "")).lower() if when.get("entry_type") else ""
    when_bias = str(when.get("market_bias", "")).lower() if when.get("market_bias") else ""

    if when_side and when_side != side:
        return False
    if when_phase and when_phase != str(market_phase).lower():
        return False
    if when_trend and when_trend != str(trend_stage).lower():
        return False
    if when_entry and when_entry != str(entry_type).lower():
        return False
    if when_bias and when_bias != str(market_bias).lower():
        return False

    return True


def load_historical_klines(path: str | Path, *, symbol: str, interval: str) -> dict[str, Any]:
    return read_klines_file(path, symbol=symbol, interval=interval)


def run_historical_replay(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    candles: list[dict[str, Any]] | None = None,
    parquet_path: str | Path | None = None,
    strategy_versions: list[str] | None = None,
    export_path: str | Path | None = None,
    warmup: int = 30,
) -> dict[str, Any]:
    if candles is None:
        if parquet_path is None:
            return {"ok": False, "error": "candles or parquet_path required"}
        loaded = load_historical_klines(parquet_path, symbol=symbol, interval=interval)
        if not loaded.get("ok"):
            return loaded
        candles = loaded["rows"]
    rows = [
        _normalize_replay_candle(c, symbol=symbol, interval=interval)
        for c in candles
        if int(c["close_time"]) >= int(start_time) and int(c["close_time"]) <= int(end_time)
    ]
    rows.sort(key=lambda c: int(c["close_time"]))
    if len(rows) < warmup:
        result = _empty_result(symbol, interval, start_time, end_time, strategy_versions or [], "insufficient_candles")
        result["replay_result_id"] = repo.save_historical_replay_result(result)
        return result

    signals: list[dict[str, Any]] = []
    no_lookahead_violations = 0
    with _scratch_replay_repo() as replay_repo:
        for idx, candle in enumerate(rows):
            replay_repo.upsert_candles([candle])
            if idx + 1 < warmup:
                continue
            analysis_time = int(candle["close_time"])
            check = replay_repo.no_lookahead_candles(symbol, interval, analysis_time_utc=analysis_time, limit=500)
            no_lookahead_violations += int(check["violation_count"])

            # Classify market regime at this point
            regime = _classify_market_regime(rows[: idx + 1])

            snapshot = build_market_state_snapshot(
                replay_repo,
                symbol=symbol,
                analysis_time_utc=analysis_time,
                mode="shadow_test",
                timeframes=[interval],
            )
            decision = run_ga_sop_decision(snapshot)

            signal: dict[str, Any] = {
                "analysis_time_utc": analysis_time,
                "symbol": symbol,
                "close": candle["close"],
                "decision": decision["decision"],
                "signal_grade": decision["signal_grade"],
                "confidence": decision["confidence"],
                "market_bias": decision.get("market_bias"),
                "market_regime": regime,
            }

            # Real trade simulation for signals with a trade plan
            trade_plan = decision.get("trade_plan")
            if trade_plan and decision["decision"] == "trade_plan_available":
                subsequent = rows[idx + 1: idx + 51]  # up to 50 candles ahead
                sim = _simulate_trade(trade_plan, subsequent)
                signal["trade_simulation"] = sim
                signal["pnl_r"] = sim["pnl_r"]
                signal["trade_outcome"] = sim["outcome"]
            else:
                signal["trade_simulation"] = None
                signal["pnl_r"] = None
                signal["trade_outcome"] = None

            signals.append(signal)

    stats = _performance_stats(signals)
    comparisons = _compare_strategy_versions(signals, strategy_versions or [])
    regime_dist = _regime_distribution(signals)
    result = {
        "ok": no_lookahead_violations == 0,
        "symbol": symbol,
        "interval": interval,
        "start_time": int(start_time),
        "end_time": int(end_time),
        "strategy_versions": strategy_versions or [],
        "candles_replayed": len(rows),
        "signals": signals,
        "trades": _pseudo_trades(signals),
        "stats": stats,
        "strategy_comparison": comparisons,
        "regime_distribution": regime_dist,
        "no_lookahead": {"ok": no_lookahead_violations == 0, "violation_count": no_lookahead_violations},
        "export_path": str(export_path) if export_path else None,
    }
    result["agent_analysis"] = run_agent_json_task(
        task_name="historical_replay_backtest_analysis",
        payload={
            "symbol": symbol,
            "interval": interval,
            "start_time": int(start_time),
            "end_time": int(end_time),
            "stats": stats,
            "strategy_comparison": comparisons,
            "sample_signals": signals[:80],
            "sample_trades": _pseudo_trades(signals)[:80],
            "no_lookahead": result["no_lookahead"],
        },
        fallback={
            "summary": _fallback_replay_summary(symbol, interval, stats, comparisons),
            "regime_findings": [],
            "strategy_findings": [],
            "recommended_next_steps": ["继续扩大回放样本后再考虑 candidate 策略变更。"],
        },
        instructions=[
            "分析历史回放/回测结果，指出行情状态、策略版本表现、过拟合风险和下一步 shadow/candidate 建议。",
            "必须检查 no_lookahead 是否通过。",
            "不要输出实盘交易建议。",
        ],
    )
    if export_path:
        Path(export_path).parent.mkdir(parents=True, exist_ok=True)
        Path(export_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["replay_result_id"] = repo.save_historical_replay_result(result)
    return result


def run_paired_backtest(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    warmup: int = 30,
    candidate_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run paired backtest: same historical data, two strategy versions compared side-by-side.

    Runs deterministic SOP decision on each historical candle with both active and candidate
    configurations. Returns paired stats for backtest gate judgment.

    Args:
        candidate_patch: Full candidate patch dict with score_adjustments and when-conditions.
            If provided, conditional adjustments are evaluated per-candle based on market context.
    """
    from datetime import datetime, timezone

    # Load candles from repo
    candles = repo.get_candles(symbol, interval, analysis_time_utc=end_time, limit=10000)
    if not candles:
        return {"ok": False, "error": "no_candles", "symbol": symbol, "interval": interval}

    rows = [
        _normalize_replay_candle(c, symbol=symbol, interval=interval)
        for c in candles
        if int(c["close_time"]) >= int(start_time) and int(c["close_time"]) <= int(end_time)
    ]
    rows.sort(key=lambda c: int(c["close_time"]))
    if len(rows) < warmup:
        return {"ok": False, "error": "insufficient_candles", "candle_count": len(rows), "warmup_required": warmup}

    active_signals: list[dict[str, Any]] = []
    candidate_signals: list[dict[str, Any]] = []
    no_lookahead_violations = 0
    all_trigger_counts: dict[str, int] = {}

    with _scratch_replay_repo() as replay_repo:
        for idx, candle in enumerate(rows):
            replay_repo.upsert_candles([candle])
            if idx + 1 < warmup:
                continue
            analysis_time = int(candle["close_time"])
            check = replay_repo.no_lookahead_candles(symbol, interval, analysis_time_utc=analysis_time, limit=500)
            no_lookahead_violations += int(check["violation_count"])

            regime = _classify_market_regime(rows[: idx + 1])

            snapshot = build_market_state_snapshot(
                replay_repo,
                symbol=symbol,
                analysis_time_utc=analysis_time,
                mode="shadow_test",
                timeframes=[interval],
            )

            # Read real market_phase from snapshot modules
            market_phase = str(
                (snapshot.get("modules") or {}).get("market_regime", {}).get("market_phase", "")
            )

            # Run active decision (no adjustment)
            active_decision = run_ga_sop_decision(snapshot)
            market_bias = str(active_decision.get("market_bias", ""))
            active_signal = _build_signal(analysis_time, symbol, candle, active_decision, regime)

            # Run candidate decision with conditional adjustments from patch
            candidate_adjustment, trigger_counts = _evaluate_conditional_adjustment(
                candidate_patch, snapshot, regime,
                active_decision=active_decision,
                market_bias=market_bias,
            )
            for k, v in trigger_counts.items():
                all_trigger_counts[k] = all_trigger_counts.get(k, 0) + v
            candidate_decision = run_ga_sop_decision(snapshot, score_adjustment=candidate_adjustment)
            candidate_signal = _build_signal(analysis_time, symbol, candle, candidate_decision, regime)

            # Simulate trades for both if trade plan available
            for signal, decision in [(active_signal, active_decision), (candidate_signal, candidate_decision)]:
                trade_plan = decision.get("trade_plan")
                if trade_plan and decision["decision"] == "trade_plan_available":
                    subsequent = rows[idx + 1: idx + 51]
                    sim = _simulate_trade(trade_plan, subsequent)
                    signal["trade_simulation"] = sim
                    signal["pnl_r"] = sim["pnl_r"]
                    signal["trade_outcome"] = sim["outcome"]
                else:
                    signal["trade_simulation"] = None
                    signal["pnl_r"] = None
                    signal["trade_outcome"] = None

            active_signals.append(active_signal)
            candidate_signals.append(candidate_signal)

    active_stats = _performance_stats(active_signals)
    candidate_stats = _performance_stats(candidate_signals)
    paired_count = len(active_signals)

    # Compute candidate_score_adjustment from patch for result
    # Use the per-candle adjustments already computed during the loop
    candidate_score_adjustment = candidate_patch.get("score_adjustments") if candidate_patch else None

    # Extract raw R sequences for accurate aggregation
    active_real_rs = [s["pnl_r"] for s in active_signals if s.get("pnl_r") is not None]
    candidate_real_rs = [s["pnl_r"] for s in candidate_signals if s.get("pnl_r") is not None]
    active_trade_outcomes = [s["trade_outcome"] for s in active_signals if s.get("trade_outcome") is not None]
    candidate_trade_outcomes = [s["trade_outcome"] for s in candidate_signals if s.get("trade_outcome") is not None]

    return {
        "ok": no_lookahead_violations == 0,
        "symbol": symbol,
        "interval": interval,
        "start_time": int(start_time),
        "end_time": int(end_time),
        "paired_count": paired_count,
        "active_stats": active_stats,
        "candidate_stats": candidate_stats,
        "active_r_values": active_real_rs,
        "candidate_r_values": candidate_real_rs,
        "active_trade_outcomes": active_trade_outcomes,
        "candidate_trade_outcomes": candidate_trade_outcomes,
        "candidate_score_adjustment": candidate_score_adjustment,
        "no_lookahead": {"ok": no_lookahead_violations == 0, "violation_count": no_lookahead_violations},
        "regime_distribution": _regime_distribution(active_signals),
        "when_rule_stats": all_trigger_counts,
    }


def _build_signal(
    analysis_time: int,
    symbol: str,
    candle: dict[str, Any],
    decision: dict[str, Any],
    regime: str,
) -> dict[str, Any]:
    """Build a signal dict from decision result."""
    trade_plan = decision.get("trade_plan") or {}
    if isinstance(trade_plan, str):
        try:
            trade_plan = json.loads(trade_plan)
        except (json.JSONDecodeError, TypeError):
            trade_plan = {}
    return {
        "analysis_time_utc": analysis_time,
        "symbol": symbol,
        "close": candle["close"],
        "decision": decision["decision"],
        "signal_grade": decision["signal_grade"],
        "confidence": decision["confidence"],
        "market_bias": decision.get("market_bias"),
        "market_regime": regime,
        "side": str(trade_plan.get("side", "")).upper() or None,
        "entry_type": str(trade_plan.get("entry_type", "")).lower() or None,
        "trend_stage": str(decision.get("trend_stage", "")),
    }


def _normalize_replay_candle(candle: dict[str, Any], *, symbol: str, interval: str) -> dict[str, Any]:
    return {
        "symbol": str(candle.get("symbol") or symbol).upper(),
        "interval": str(candle.get("interval") or interval),
        "open_time": int(candle["open_time"]),
        "close_time": int(candle["close_time"]),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": float(candle["volume"]),
        "is_closed": True,
        "source": "historical_replay",
    }


def _performance_stats(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate performance stats from real trade simulations.

    Falls back to pseudo-R for signals without trade simulation (non-trade-plan signals).
    """
    real_rs = [s["pnl_r"] for s in signals if s.get("pnl_r") is not None]
    pseudo_rs = [_pseudo_r(s) for s in signals if s.get("pnl_r") is None]
    all_rs = real_rs + pseudo_rs

    sim_signals = [s for s in signals if s.get("trade_simulation") is not None]
    wins = len([s for s in sim_signals if s.get("trade_outcome") == "win"])
    losses = len([s for s in sim_signals if s.get("trade_outcome") == "loss"])
    timeouts = len([s for s in sim_signals if s.get("trade_outcome") == "timeout"])

    drawdown = _drawdown(all_rs)
    avg_r = sum(all_rs) / len(all_rs) if all_rs else 0.0
    win_rate = wins / len(sim_signals) if sim_signals else 0.0

    # Sharpe-like ratio: avg_r / std_r
    if len(real_rs) >= 2:
        mean_r = sum(real_rs) / len(real_rs)
        variance = sum((r - mean_r) ** 2 for r in real_rs) / (len(real_rs) - 1)
        std_r = variance ** 0.5
        sharpe = mean_r / std_r if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "signal_count": len(signals),
        "simulated_trades": len(sim_signals),
        "avg_r": round(avg_r, 4),
        "win_rate": round(win_rate, 4),
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "drawdown": round(drawdown, 4),
        "sharpe_ratio": round(sharpe, 4),
    }


def _pseudo_trades(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build trade list from signals, preferring real simulation results."""
    trades: list[dict[str, Any]] = []
    for s in signals:
        if s.get("decision") not in {"trade_plan_available", "wait_for_pullback"}:
            continue
        trade: dict[str, Any] = {
            "analysis_time_utc": s["analysis_time_utc"],
            "symbol": s.get("symbol"),
            "source": "historical_replay",
            "market_regime": s.get("market_regime"),
        }
        sim = s.get("trade_simulation")
        if sim:
            trade["pnl_r"] = sim["pnl_r"]
            trade["outcome"] = sim["outcome"]
            trade["holding_candles"] = sim.get("holding_candles")
            trade["exit_price"] = sim.get("exit_price")
            trade["simulated"] = True
        else:
            trade["pnl_r"] = _pseudo_r(s)
            trade["outcome"] = "pseudo"
            trade["simulated"] = False
        trades.append(trade)
    return trades


def _compare_strategy_versions(signals: list[dict[str, Any]], versions: list[str]) -> list[dict[str, Any]]:
    """Compare strategy versions using real performance metrics.

    When there are multiple versions, signals are split evenly across
    versions so each version's stats reflect its own slice. When only
    one version (or none) is given, the full signal set is attributed to it.
    """
    if not versions:
        return []

    base = _performance_stats(signals)
    n_versions = len(versions)
    chunk_size = max(1, len(signals) // n_versions)

    comparisons: list[dict[str, Any]] = []
    for idx, version in enumerate(versions):
        start = idx * chunk_size
        end = start + chunk_size if idx < n_versions - 1 else len(signals)
        chunk = signals[start:end]
        if chunk:
            chunk_stats = _performance_stats(chunk)
        else:
            chunk_stats = {
                "avg_r": 0.0, "win_rate": 0.0, "drawdown": 0.0,
                "signal_count": 0, "simulated_trades": 0,
                "wins": 0, "losses": 0, "timeouts": 0, "sharpe_ratio": 0.0,
            }
        comparisons.append({
            "version": version,
            "avg_r": chunk_stats["avg_r"],
            "win_rate": chunk_stats["win_rate"],
            "drawdown": chunk_stats["drawdown"],
            "sample_count": chunk_stats["signal_count"],
            "simulated_trades": chunk_stats["simulated_trades"],
            "wins": chunk_stats["wins"],
            "losses": chunk_stats["losses"],
            "timeouts": chunk_stats["timeouts"],
            "sharpe_ratio": chunk_stats["sharpe_ratio"],
        })
    return comparisons


def _pseudo_r(signal: dict[str, Any]) -> float:
    confidence = float(signal.get("confidence") or 0)
    grade_bonus = {"S": 0.25, "A": 0.15, "B": 0.05, "C": -0.05, "D": -0.12}.get(signal.get("signal_grade"), 0)
    decision_bonus = 0.1 if signal.get("decision") == "trade_plan_available" else 0.03 if str(signal.get("decision", "")).startswith("wait") else -0.03
    return (confidence - 0.5) + grade_bonus + decision_bonus


def _regime_distribution(signals: list[dict[str, Any]]) -> dict[str, int]:
    """Count signals per market regime."""
    dist: dict[str, int] = {}
    for s in signals:
        regime = s.get("market_regime", "unknown")
        dist[regime] = dist.get(regime, 0) + 1
    return dist


def _drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    return dd


def _fallback_replay_summary(symbol: str, interval: str, stats: dict[str, Any], comparisons: list[dict[str, Any]]) -> str:
    best = max(comparisons, key=lambda x: float(x.get("avg_r") or 0), default=None)
    best_text = f"，当前样本最优版本 {best.get('version')}" if best else ""
    return (
        f"{symbol} {interval} 历史回放完成：信号 {stats.get('signal_count')}，"
        f"平均R {float(stats.get('avg_r') or 0):.2f}，胜率 {float(stats.get('win_rate') or 0) * 100:.0f}%{best_text}。"
    )


def _empty_result(symbol: str, interval: str, start_time: int, end_time: int, versions: list[str], reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": reason,
        "symbol": symbol,
        "interval": interval,
        "start_time": int(start_time),
        "end_time": int(end_time),
        "strategy_versions": versions,
        "candles_replayed": 0,
        "signals": [],
        "trades": [],
        "stats": {"signal_count": 0, "avg_r": 0.0, "win_rate": 0.0, "drawdown": 0.0},
        "strategy_comparison": [],
        "no_lookahead": {"ok": True, "violation_count": 0},
        "export_path": None,
    }
