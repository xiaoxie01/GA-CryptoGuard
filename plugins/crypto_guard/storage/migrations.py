from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from plugins.crypto_guard.config.loader import PLUGIN_ROOT, CryptoGuardConfig, load_config
from plugins.crypto_guard.storage.sqlite_db import connect_db


SCHEMA_PATH = PLUGIN_ROOT / "storage" / "schema.sql"


def initialize_database(config: CryptoGuardConfig | None = None) -> dict[str, Any]:
    """执行 schema，并写入默认 symbol 与策略版本。"""

    cfg = config or load_config()
    conn = connect_db(cfg.database_path)
    try:
        with SCHEMA_PATH.open("r", encoding="utf-8") as f:
            conn.executescript(f.read())
        _apply_phase_01_02_migrations(conn)
        _seed_symbols(conn, cfg.symbols)
        _seed_strategies(conn, cfg.strategies)
        _apply_phase_13_migrations(conn)
        _apply_phase_14_15_migrations(conn)
        _apply_decision_supplement_migrations(conn)
        _apply_v2_migrations(conn)
        _apply_ga_master_migrations(conn)
        _apply_pending_order_lifecycle_migrations(conn)
        _apply_p1_structured_feedback_migrations(conn)
        _apply_account_feedback_gate_migration(conn)
        _apply_daily_review_idempotency_migration(conn)
        return {"ok": True, "database_path": str(cfg.database_path)}
    finally:
        conn.close()


def _apply_phase_01_02_migrations(conn: sqlite3.Connection) -> None:
    """Phase 01-02 兼容迁移，幂等执行，不破坏已有 MVP 数据。"""

    _add_column(conn, "market_snapshots", "data_quality_json", "TEXT")
    _add_column(conn, "module_analysis_results", "snapshot_id", "INTEGER")
    _add_column(conn, "strategy_evaluations", "snapshot_id", "INTEGER")
    _add_column(conn, "signals", "snapshot_id", "INTEGER")
    _add_column(conn, "signals", "ga_decision_json", "TEXT")
    _add_column(conn, "paper_trades", "signal_id", "INTEGER")
    _add_column(conn, "paper_trades", "market_snapshot_id", "INTEGER")
    _add_column(conn, "paper_trades", "signal_decay_score", "REAL")
    _add_column(conn, "paper_trades", "stop_take_path_json", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feishu_events_received_at ON feishu_events(received_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_snapshot_id ON signals(market_snapshot_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_signal_snapshot ON paper_trades(signal_id, market_snapshot_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_module_results_snapshot ON module_analysis_results(snapshot_id)")


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _apply_phase_13_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_test_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL,
            candidate_version TEXT NOT NULL,
            active_version TEXT,
            sample_count INTEGER DEFAULT 0,
            active_stats_json TEXT,
            candidate_stats_json TEXT,
            recommendation TEXT,
            status TEXT DEFAULT 'running',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_results_strategy ON shadow_test_results(strategy_name, candidate_version, status)")


def _apply_phase_14_15_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS historical_replay_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            start_time INTEGER NOT NULL,
            end_time INTEGER NOT NULL,
            strategy_versions_json TEXT,
            result_json TEXT NOT NULL,
            export_path TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS self_evolution_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_historical_replay_symbol_time ON historical_replay_results(symbol, interval, start_time, end_time)")


def _apply_decision_supplement_migrations(conn: sqlite3.Connection) -> None:
    _add_column(conn, "ad_hoc_analyses", "status", "TEXT DEFAULT 'created'")
    _add_column(conn, "paper_orders", "fill_method", "TEXT")
    _add_column(conn, "paper_trades", "fill_method", "TEXT")
    _add_column(conn, "trade_reviews", "market_regime_at_loss", "TEXT")
    _add_column(conn, "trade_reviews", "evolution_trigger_allowed", "INTEGER DEFAULT 1")
    _add_column(conn, "shadow_test_results", "verdict_runner_run", "INTEGER DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            symbol TEXT,
            priority INTEGER DEFAULT 5,
            payload_json TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            retry_count INTEGER DEFAULT 0,
            next_retry_at TEXT,
            last_error TEXT,
            dedupe_key TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_failure_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_outbox_id INTEGER,
            alert_type TEXT,
            symbol TEXT,
            error_message TEXT,
            retry_count INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config_hot_reload (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT NOT NULL,
            requested_by TEXT,
            request_text TEXT,
            confirmation_required INTEGER DEFAULT 1,
            confirmed INTEGER DEFAULT 0,
            confirmed_at TEXT,
            status TEXT DEFAULT 'pending',
            applied_at TEXT,
            audit_summary TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_config (
            config_key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_outbox_status_retry ON alert_outbox(status, next_retry_at, priority)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_outbox_dedupe ON alert_outbox(dedupe_key, created_at)")


def _apply_v2_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            analysis_time INTEGER NOT NULL,
            analysis_time_utc TEXT NOT NULL,
            analysis_mode TEXT NOT NULL,
            timeframes TEXT NOT NULL,
            market_structure_json TEXT NOT NULL,
            trend_clarity_json TEXT NOT NULL,
            no_trade_reason_json TEXT,
            key_levels_json TEXT,
            next_triggers_json TEXT,
            next_analysis_json TEXT,
            breakout_watch_json TEXT,
            trade_permission_json TEXT,
            trade_plan_json TEXT,
            opportunity_watch_recommended INTEGER DEFAULT 0,
            paper_trade_allowed INTEGER DEFAULT 0,
            state_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_states_symbol_time ON analysis_states(symbol, analysis_time)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            skill_version TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            analysis_time INTEGER NOT NULL,
            input_summary_json TEXT,
            tool_result_json TEXT NOT NULL,
            ga_interpretation_json TEXT NOT NULL,
            final_result_json TEXT NOT NULL,
            confidence REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_logs_symbol_time ON skill_execution_logs(symbol, timeframe, analysis_time, skill_name)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_feedback_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            skill_version TEXT NOT NULL,
            feedback_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id INTEGER,
            finding TEXT NOT NULL,
            suggested_adjustment_json TEXT,
            status TEXT DEFAULT 'candidate',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_feedback_status ON skill_feedback_memory(skill_name, status, updated_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL UNIQUE,
            initial_balance REAL NOT NULL,
            current_balance REAL NOT NULL,
            equity REAL NOT NULL,
            realized_pnl REAL DEFAULT 0,
            unrealized_pnl REAL DEFAULT 0,
            max_drawdown REAL DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            current_price REAL,
            quantity REAL NOT NULL,
            stop_loss REAL,
            take_profit_json TEXT,
            unrealized_pnl REAL DEFAULT 0,
            unrealized_pnl_pct REAL DEFAULT 0,
            max_favorable_excursion REAL DEFAULT 0,
            max_adverse_excursion REAL DEFAULT 0,
            status TEXT DEFAULT 'open',
            opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_positions_account_status ON paper_positions(account_id, status, symbol)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_trade_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            event_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT,
            price REAL,
            quantity REAL,
            pnl REAL,
            pnl_pct REAL,
            reason TEXT,
            event_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_trade_logs_symbol_time ON paper_trade_logs(symbol, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_type TEXT NOT NULL,
            strategy_name TEXT,
            symbol TEXT,
            trigger_value REAL,
            threshold_value REAL,
            related_trade_ids TEXT,
            market_regime TEXT,
            evolution_allowed INTEGER DEFAULT 1,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_triggers_status ON evolution_triggers(status, trigger_type, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_review_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_date TEXT NOT NULL UNIQUE,
            summary_json TEXT NOT NULL,
            ga_report TEXT NOT NULL,
            skill_updates_json TEXT,
            evolution_actions_json TEXT,
            pushed_to_feishu INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT INTO paper_accounts(account_name, initial_balance, current_balance, equity)
        VALUES ('default', 10000, 10000, 10000)
        ON CONFLICT(account_name) DO NOTHING
        """
    )


def _apply_ga_master_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ga_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            analysis_time INTEGER NOT NULL,
            analysis_time_utc TEXT NOT NULL,
            decision_type TEXT NOT NULL,
            signal_grade TEXT NOT NULL,
            confidence REAL NOT NULL,
            market_bias TEXT,
            trend_stage TEXT,
            decision TEXT NOT NULL,
            skill_result_refs_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            counter_evidence_json TEXT NOT NULL,
            risk_check_json TEXT NOT NULL,
            trade_plan_json TEXT,
            opportunity_watch_json TEXT,
            feishu_actions_json TEXT NOT NULL,
            final_summary TEXT NOT NULL,
            raw_decision_json TEXT NOT NULL,
            analysis_state_id INTEGER,
            snapshot_id INTEGER,
            created_by TEXT DEFAULT 'ga_master_controller',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ga_decisions_symbol_time ON ga_decisions(symbol, analysis_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ga_decisions_grade_time ON ga_decisions(signal_grade, analysis_time)")
    _add_column(conn, "signals", "ga_decision_id", "INTEGER")
    _add_column(conn, "analysis_states", "ga_decision_id", "INTEGER")
    _add_column(conn, "paper_orders", "ga_decision_id", "INTEGER")
    _add_column(conn, "paper_orders", "source", "TEXT DEFAULT 'signal_compat'")
    _add_column(conn, "paper_orders", "risk_check_passed", "INTEGER DEFAULT 0")
    _add_column(conn, "opportunity_watches", "ga_decision_id", "INTEGER")
    _add_column(conn, "opportunity_watches", "created_by_user_action", "INTEGER DEFAULT 0")
    _add_column(conn, "opportunity_watches", "source_button_action", "TEXT")
    _add_column(conn, "strategy_evaluations", "pnl_r", "REAL")
    _add_column(conn, "strategy_patches", "trigger_id", "INTEGER")
    _add_column(conn, "strategy_patches", "backtest_result_json", "TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_orders_ga_decision_unique ON paper_orders(ga_decision_id) WHERE ga_decision_id IS NOT NULL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS parquet_archive_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            year_month TEXT NOT NULL,
            path TEXT NOT NULL,
            rows_written INTEGER DEFAULT 0,
            status TEXT NOT NULL,
            error_message TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_parquet_archive_runs_recent ON parquet_archive_runs(created_at, symbol, interval)")


def _apply_pending_order_lifecycle_migrations(conn: sqlite3.Connection) -> None:
    """Add lifecycle columns for pending order TTL and conflict cancellation."""
    _add_column(conn, "paper_orders", "expires_at", "TEXT")
    _add_column(conn, "paper_orders", "cancelled_at", "TEXT")
    _add_column(conn, "paper_orders", "cancel_reason", "TEXT")
    _add_column(conn, "paper_orders", "invalidated_by_ga_decision_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status)")


def _seed_symbols(conn: sqlite3.Connection, symbols_cfg: dict[str, Any]) -> None:
    default = symbols_cfg.get("default_universe", {})
    profiles = symbols_cfg.get("symbol_profiles", {})
    if not default.get("enabled", True):
        return
    for symbol in default.get("symbols", []):
        profile = profiles.get(symbol, {})
        base_asset = symbol.removesuffix("USDT")
        timeframes = profile.get("default_timeframes") or symbols_cfg.get("user_symbol_defaults", {}).get("default_timeframes", [])
        conn.execute(
            """
            INSERT INTO symbols(symbol, base_asset, quote_asset, category, enabled, source, risk_profile, default_timeframes)
            VALUES (?, ?, 'USDT', ?, ?, 'default', ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                category=excluded.category,
                enabled=excluded.enabled,
                default_timeframes=excluded.default_timeframes,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                symbol,
                base_asset,
                profile.get("category", "default_universe"),
                1 if profile.get("enabled", True) else 0,
                profile.get("volatility_level", "auto"),
                json.dumps(timeframes, ensure_ascii=False),
            ),
        )


def _seed_strategies(conn: sqlite3.Connection, strategies_cfg: dict[str, Any]) -> None:
    for item in strategies_cfg.get("strategies", []):
        name = item.get("strategy_name")
        version = str(item.get("version", "1.0"))
        if not name:
            continue
        status = item.get("status", "candidate")
        # 自进化硬约束：只有配置里显式 active 的初始策略可以 active，补丁创建逻辑永远 candidate。
        conn.execute(
            """
            INSERT INTO strategy_versions(strategy_name, version, status, config_json, change_reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(strategy_name, version) DO NOTHING
            """,
            (name, version, status, json.dumps(item, ensure_ascii=False), "seed_from_config"),
        )


def _apply_p1_structured_feedback_migrations(conn: sqlite3.Connection) -> None:
    """Add structured fields to skill_feedback_memory for pattern matching."""
    _add_column(conn, "skill_feedback_memory", "pattern_type", "TEXT")
    _add_column(conn, "skill_feedback_memory", "affected_symbols", "TEXT")
    _add_column(conn, "skill_feedback_memory", "affected_sides", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_feedback_pattern ON skill_feedback_memory(pattern_type, status)")


def _apply_account_feedback_gate_migration(conn: sqlite3.Connection) -> None:
    """Add account_feedback_gate_json column to ga_decisions for gate results."""
    _add_column(conn, "ga_decisions", "account_feedback_gate_json", "TEXT")
    # dedupe_key for opportunity_watches (P0 hotfix: Fix 4)
    _add_column(conn, "opportunity_watches", "dedupe_key", "TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunity_watches_dedupe "
        "ON opportunity_watches(dedupe_key)"
    )


def _apply_daily_review_idempotency_migration(conn: sqlite3.Connection) -> None:
    """Cleanup duplicate agent_jobs from the pre-idempotency era.

    Idempotency is enforced at the application layer via enqueue_job_once()
    (SELECT-then-INSERT with IntegrityError catch), NOT via a DB-level UNIQUE
    index.  A global UNIQUE(job_type, session_id) would break event-queue
    callers like feishu_user_message / feishu_button_callback that legitimately
    reuse session_ids across events.

    The cleanup here soft-deduplicates historical duplicates so the data is
    tidy, but does not create a hard constraint.
    """
    _cleanup_agent_job_duplicates(conn)
    _cleanup_orphan_patches(conn)
    _cleanup_noisy_auto_analysis(conn)
    _cleanup_duplicate_open_trades(conn)
    _backfill_historical_shadow_pnl_r(conn)
    # Partial unique index: one order can only have one open trade.
    # Unlike the global UNIQUE on agent_jobs(job_type, session_id) which was
    # rejected because event-queue callers legitimately reuse session_ids,
    # this is scoped to open trades only — a genuine data integrity rule.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_trade_per_order
        ON paper_trades(order_id)
        WHERE closed_at IS NULL
        """
    )


def _cleanup_orphan_patches(conn: sqlite3.Connection) -> dict[str, int]:
    """Mark strategy_patches as rejected when they have no matching strategy_version."""
    orphans = conn.execute(
        """
        SELECT sp.id, sp.strategy_name, sp.candidate_version
        FROM strategy_patches sp
        LEFT JOIN strategy_versions sv ON sp.strategy_name = sv.strategy_name AND sp.candidate_version = sv.version
        WHERE sv.id IS NULL AND sp.status NOT IN ('duplicate', 'rejected')
        """
    ).fetchall()

    for row in orphans:
        conn.execute(
            "UPDATE strategy_patches SET status='rejected' WHERE id=?",
            (row["id"],),
        )

    if orphans:
        conn.commit()

    return {"orphans_cleaned": len(orphans)}


def _cleanup_noisy_auto_analysis(conn: sqlite3.Connection) -> dict[str, int]:
    """Dedup auto_analysis skill_feedback_memory: keep only the latest per (skill_name, finding) per day."""
    # Mark older duplicates as 'superseded' — keep the latest per group
    conn.execute(
        """
        UPDATE skill_feedback_memory
        SET status='superseded'
        WHERE feedback_type='auto_analysis'
          AND status='candidate'
          AND id NOT IN (
              SELECT MAX(id) FROM skill_feedback_memory
              WHERE feedback_type='auto_analysis' AND status='candidate'
              GROUP BY skill_name, finding, date(created_at)
          )
        """
    )
    cleaned = int(conn.execute("SELECT changes() AS c").fetchone()["c"])
    if cleaned:
        conn.commit()
    return {"auto_analysis_deduped": cleaned}


def _cleanup_duplicate_open_trades(conn: sqlite3.Connection) -> dict[str, int]:
    """Close duplicate open trades (same order_id, multiple open paper_trades).

    Keeps the oldest trade (lowest id), closes others with reason 'duplicate_cleanup'.
    Also marks duplicate paper_positions as closed.
    """
    # Find order_ids with multiple open trades
    dup_orders = conn.execute(
        """
        SELECT order_id, COUNT(*) as cnt
        FROM paper_trades
        WHERE closed_at IS NULL
        GROUP BY order_id
        HAVING cnt > 1
        """
    ).fetchall()

    trades_closed = 0
    positions_closed = 0

    for row in dup_orders:
        order_id = int(row["order_id"])
        # Find all open trades for this order, keep the oldest
        trades = conn.execute(
            "SELECT id FROM paper_trades WHERE order_id=? AND closed_at IS NULL ORDER BY id ASC",
            (order_id,),
        ).fetchall()

        keeper_id = int(trades[0]["id"])
        for trade in trades[1:]:
            dup_id = int(trade["id"])
            conn.execute(
                """
                UPDATE paper_trades
                SET closed_at=CURRENT_TIMESTAMP, close_reason='duplicate_cleanup',
                    pnl=NULL, pnl_percent=NULL, pnl_r=NULL
                WHERE id=?
                """,
                (dup_id,),
            )
            trades_closed += 1
            # Close matching paper_position (position id matches trade id)
            conn.execute(
                "UPDATE paper_positions SET status='closed', closed_at=CURRENT_TIMESTAMP WHERE id=? AND status='open'",
                (dup_id,),
            )
            positions_closed += conn.execute("SELECT changes() AS c").fetchone()["c"]

    if trades_closed:
        conn.commit()

    return {"duplicate_trades_closed": trades_closed, "duplicate_positions_closed": positions_closed}


def _backfill_historical_shadow_pnl_r(conn: sqlite3.Connection) -> dict[str, int]:
    """One-shot backfill: copy pnl_r from closed paper_trades to shadow evaluations.

    For each closed trade with real pnl_r, finds the ga_decision's analysis_time (integer ms)
    and strategy_name, then updates matching shadow strategy_evaluations (is_shadow=1).
    Excludes duplicate_cleanup trades (pnl_r=NULL, no real outcome).
    """
    import json

    trades = conn.execute(
        """
        SELECT pt.id, pt.order_id, pt.pnl_r
        FROM paper_trades pt
        WHERE pt.closed_at IS NOT NULL
          AND pt.pnl_r IS NOT NULL
          AND pt.close_reason != 'duplicate_cleanup'
        """
    ).fetchall()

    trades_processed = 0
    evals_updated = 0

    for row in trades:
        order_id = int(row["order_id"])
        pnl_r = float(row["pnl_r"])

        # Get order info
        order = conn.execute(
            "SELECT ga_decision_id, symbol FROM paper_orders WHERE id=?",
            (order_id,),
        ).fetchone()
        if not order or not order["ga_decision_id"]:
            continue

        gd = conn.execute(
            "SELECT analysis_time, raw_decision_json FROM ga_decisions WHERE id=?",
            (int(order["ga_decision_id"]),),
        ).fetchone()
        if not gd or not gd["analysis_time"]:
            continue

        try:
            analysis_time = int(gd["analysis_time"])
        except (ValueError, TypeError):
            continue

        strategy_name = None
        try:
            raw = json.loads(gd["raw_decision_json"] or "{}")
            # Real data: raw_decision_json.raw_legacy_decision.strategy_name
            strategy_name = raw.get("strategy_name")
            if not strategy_name:
                legacy = raw.get("raw_legacy_decision")
                if isinstance(legacy, dict):
                    strategy_name = legacy.get("strategy_name")
        except (json.JSONDecodeError, TypeError):
            pass

        if strategy_name:
            conn.execute(
                """
                UPDATE strategy_evaluations
                SET pnl_r=?
                WHERE symbol=? AND strategy_name=? AND is_shadow=1 AND pnl_r IS NULL
                  AND ABS(analysis_time - ?) < 3600000
                """,
                (pnl_r, order["symbol"], strategy_name, analysis_time),
            )
        else:
            conn.execute(
                """
                UPDATE strategy_evaluations
                SET pnl_r=?
                WHERE symbol=? AND is_shadow=1 AND pnl_r IS NULL
                  AND ABS(analysis_time - ?) < 3600000
                """,
                (pnl_r, order["symbol"], analysis_time),
            )

        updated = int(conn.execute("SELECT changes() AS c").fetchone()["c"])
        if updated > 0:
            trades_processed += 1
            evals_updated += updated

    if evals_updated:
        conn.commit()

    return {"trades_processed": trades_processed, "evaluations_updated": evals_updated}


# Job types that use enqueue_job_once() with idempotent session_ids.
# Cleanup only deduplicates these — event-queue callers (feishu_user_message,
# feishu_button_callback, scheduled_market_analysis) are intentionally excluded
# because they legitimately reuse session_ids across events.
IDEMPOTENT_JOB_TYPES = frozenset({
    "daily_review",
    "intraday_loss_review",
    "hourly_feishu_report",
    "alert_outbox_retry",
    "update_paper_positions",
    "pending_order_management",
    "pending_order_revalidation",
    "update_opportunity_watches",
})


def _cleanup_agent_job_duplicates(conn: sqlite3.Connection) -> dict[str, int]:
    """Soft-clean duplicate agent_jobs for idempotent job types only.

    Only deduplicates job types in IDEMPOTENT_JOB_TYPES — these use
    enqueue_job_once() and should have at most one active row per
    (job_type, session_id).  Event-queue job types (feishu_user_message,
    feishu_button_callback, etc.) are intentionally skipped because
    they legitimately reuse session_ids.

    Keeps the earliest success or the latest pending/running row,
    marks the rest as 'duplicate'.

    Returns cleanup stats for audit log.
    """
    result: dict[str, int] = {}
    placeholders = ",".join("?" * len(IDEMPOTENT_JOB_TYPES))
    params = tuple(IDEMPOTENT_JOB_TYPES)

    # 1. agent_jobs: keep earliest success per (job_type, session_id)
    dup_rows = conn.execute(
        f"""
        SELECT job_type, session_id, COUNT(*) as cnt
        FROM agent_jobs
        WHERE job_type IN ({placeholders})
          AND status NOT IN ('duplicate', 'superseded')
        GROUP BY job_type, session_id
        HAVING cnt > 1
        """,
        params,
    ).fetchall()
    agent_jobs_cleaned = 0
    for row in dup_rows:
        keeper = conn.execute(
            """
            SELECT id FROM agent_jobs
            WHERE job_type=? AND session_id=?
            ORDER BY CASE WHEN status='success' THEN 0 ELSE 1 END, id ASC
            LIMIT 1
            """,
            (row["job_type"], row["session_id"]),
        ).fetchone()
        if keeper:
            cur = conn.execute(
                """
                UPDATE agent_jobs
                SET status='duplicate',
                    session_id=session_id || '--dup-' || id,
                    error_message='deduped by agent_job_idempotency cleanup'
                WHERE job_type=? AND session_id=? AND id!=?
                """,
                (row["job_type"], row["session_id"], int(keeper["id"])),
            )
            agent_jobs_cleaned += cur.rowcount
    result["agent_jobs_duplicate"] = agent_jobs_cleaned

    # 2. skill_feedback_memory: archive repeated low-info "无平仓样本"/"无显著亏损" entries
    # Group by review_date (extracted from finding text pattern) + skill_name + finding
    skill_cleaned = 0
    low_info_patterns = (
        "每日复盘：今日无平仓样本%",
        "每日复盘：今日无显著亏损%",
    )
    for pattern in low_info_patterns:
        dup_skills = conn.execute(
            """
            SELECT skill_name, finding, COUNT(*) as cnt, MIN(id) as keeper_id
            FROM skill_feedback_memory
            WHERE source_type='daily_review' AND finding LIKE ?
            GROUP BY skill_name, finding
            HAVING cnt > 1
            """,
            (pattern,),
        ).fetchall()
        for row in dup_skills:
            cur = conn.execute(
                """
                UPDATE skill_feedback_memory
                SET status='archived'
                WHERE source_type='daily_review'
                  AND skill_name=? AND finding=? AND id!=?
                  AND status NOT IN ('archived', 'superseded')
                """,
                (row["skill_name"], row["finding"], int(row["keeper_id"])),
            )
            skill_cleaned += cur.rowcount
    result["skill_feedback_archived"] = skill_cleaned

    # 3. alert_outbox: mark duplicate daily_review alerts
    alert_dup_rows = conn.execute(
        """
        SELECT dedupe_key, COUNT(*) as cnt
        FROM alert_outbox
        WHERE alert_type='daily_review'
        GROUP BY dedupe_key
        HAVING cnt > 1
        """
    ).fetchall()
    alert_cleaned = 0
    for row in alert_dup_rows:
        keeper = conn.execute(
            """
            SELECT id FROM alert_outbox
            WHERE alert_type='daily_review' AND dedupe_key=?
            ORDER BY CASE WHEN status='sent' THEN 0 ELSE 1 END, id ASC
            LIMIT 1
            """,
            (row["dedupe_key"],),
        ).fetchone()
        if keeper:
            cur = conn.execute(
                """
                UPDATE alert_outbox
                SET status='duplicate'
                WHERE alert_type='daily_review' AND dedupe_key=? AND id!=?
                """,
                (row["dedupe_key"], int(keeper["id"])),
            )
            alert_cleaned += cur.rowcount
    result["alert_outbox_duplicate"] = alert_cleaned

    return result


def check_schema_health(config: CryptoGuardConfig | None = None, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Check production schema health - verify all required columns exist.

    Args:
        config: Optional config for database path. If conn is provided, config is ignored.
        conn: Optional existing connection. If provided, this is used instead of creating a new one.

    Returns:
        {
            ok: bool,
            missing_columns: [{table, column}],
            tables_checked: [str],
        }
    """
    if conn is not None:
        own_conn = None
        _conn = conn
    else:
        cfg = config or load_config()
        _conn = connect_db(cfg.database_path)
        own_conn = _conn

    # Required columns for skill_feedback_memory
    required_columns = {
        "skill_feedback_memory": ["pattern_type", "affected_symbols", "affected_sides"],
        "ga_decisions": ["account_feedback_gate_json"],
        "opportunity_watches": ["dedupe_key"],
    }

    # Required indexes
    required_indexes = [
        "idx_opportunity_watches_dedupe",
        "idx_one_open_trade_per_order",
    ]

    missing: list[dict[str, str]] = []
    tables_checked: list[str] = []

    try:
        for table, columns in required_columns.items():
            tables_checked.append(table)
            # Check if table exists
            table_exists = _conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            ).fetchone()

            if not table_exists:
                for col in columns:
                    missing.append({"table": table, "column": col})
                continue

            # Check columns
            existing_cols = {row["name"] for row in _conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col in columns:
                if col not in existing_cols:
                    missing.append({"table": table, "column": col})

        # Check required indexes
        for idx_name in required_indexes:
            idx_exists = _conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (idx_name,),
            ).fetchone()
            if not idx_exists:
                missing.append({"table": "(index)", "column": idx_name})

        return {
            "ok": len(missing) == 0,
            "missing_columns": missing,
            "tables_checked": tables_checked,
        }
    finally:
        if own_conn is not None:
            own_conn.close()
