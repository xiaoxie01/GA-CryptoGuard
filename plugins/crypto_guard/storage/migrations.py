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
        # Run dedup cleanup BEFORE executescript: schema.sql defines the partial
        # unique index on alert_outbox(dedupe_key) WHERE status='pending'. If a
        # dirty DB has duplicate pending rows, the executescript would fail.
        # Dedup first, then apply schema. The migration is table-guarded so it
        # is a no-op on a fresh DB.
        # P0-1: Run hourly_report_accuracy migration BEFORE executescript so that
        # _add_column(batch_id, previous_grade, rendered_summary) completes before
        # schema.sql tries CREATE INDEX ON ga_decisions(batch_id).  Old DBs that
        # lack the column would otherwise crash with OperationalError.
        _apply_stop_loss_adjustment_dedup(conn)
        _ensure_profit_protection_cutoff_marker(conn)
        _apply_hourly_report_accuracy_migration(conn)
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
        _apply_legacy_fuzzy_migration(conn)
        _apply_phase_shadow_vt_v2_migration(conn)
        _apply_candidate_cap_cleanup(conn)
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
    _add_column(conn, "evolution_triggers", "latest_trigger_value", "REAL")
    _add_column(conn, "evolution_triggers", "latest_triggered_at", "TEXT")
    _add_column(conn, "evolution_triggers", "original_related_trade_ids", "TEXT")
    _add_column(conn, "evolution_triggers", "latest_related_trade_ids", "TEXT")
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
    _add_column(conn, "strategy_evaluations", "ga_decision_id", "INTEGER")
    _add_column(conn, "strategy_evaluations", "paper_trade_id", "INTEGER")
    _add_column(conn, "strategy_evaluations", "outcome_source", "TEXT")
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
    _add_column(conn, "ga_decisions", "market_regime_gate_json", "TEXT")
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
    _add_column(conn, "paper_positions", "updated_at", "TEXT")
    _cleanup_agent_job_duplicates(conn)
    _cleanup_orphan_patches(conn)
    _cleanup_noisy_auto_analysis(conn)
    _cleanup_duplicate_open_trades(conn)
    _backfill_historical_shadow_pnl_r(conn)
    _cleanup_stale_empty_watches(conn)
    _add_column(conn, "paper_orders", "initial_stop_loss", "REAL")
    _add_column(conn, "paper_trades", "initial_stop_loss", "REAL")
    _add_column(conn, "paper_trades", "initial_risk_usdt", "REAL")
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


def _cleanup_stale_empty_watches(conn: sqlite3.Connection) -> dict[str, int]:
    """Clean up stale opportunity_watches with empty conditions and no TTL.

    Old watches (pre-TTL era) have watch_condition_json='{}' and expires_at=NULL.
    Without expires_at, evaluate_watch() can't expire them, so they stay active forever.

    Real data: created_at is ISO format (e.g. '2026-06-18T00:15:18+00:00').
    SQLite datetime('now') returns 'YYYY-MM-DD HH:MM:SS' (no T, no timezone).
    We MUST use SQLite's built-in datetime() on created_at for consistent comparison,
    and write expires_at in ISO UTC via strftime() so _is_expired() can parse it.
    """
    # Expire old watches (>24h since creation)
    # datetime(created_at) normalizes ISO→SQLite format for consistent comparison
    expired = conn.execute(
        """
        UPDATE opportunity_watches
        SET status = 'expired', updated_at = CURRENT_TIMESTAMP
        WHERE status = 'active'
          AND (watch_condition_json IS NULL OR watch_condition_json = '{}')
          AND (expires_at IS NULL OR expires_at = '')
          AND datetime(created_at) < datetime('now', '-1 day')
        """
    )
    expired_count = expired.rowcount if hasattr(expired, 'rowcount') else 0

    # Set TTL for recent watches — write ISO UTC so _is_expired() can parse it
    set_ttl = conn.execute(
        """
        UPDATE opportunity_watches
        SET expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', datetime(created_at), '+1 day'),
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'active'
          AND (watch_condition_json IS NULL OR watch_condition_json = '{}')
          AND (expires_at IS NULL OR expires_at = '')
          AND datetime(created_at) >= datetime('now', '-1 day')
        """
    )
    ttl_count = set_ttl.rowcount if hasattr(set_ttl, 'rowcount') else 0

    if expired_count or ttl_count:
        conn.commit()

    return {"stale_watches_expired": expired_count, "stale_watches_ttl_set": ttl_count}


def _backfill_historical_shadow_pnl_r(conn: sqlite3.Connection) -> dict[str, int]:
    """One-shot backfill: copy pnl_r from closed paper_trades to active evaluations.

    Uses exact ga_decision_id matching (no ±1h fuzzy match).
    Each trade backfills at most one active evaluation (is_shadow=0).
    Shadow evaluations are NOT backfilled — they get PnL exclusively from
    their independent shadow_virtual_trades lifecycle.
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

        gd_id = int(order["ga_decision_id"])

        # Update active evaluation with exact ga_decision_id match
        conn.execute(
            """
            UPDATE strategy_evaluations
            SET pnl_r=?, ga_decision_id=?, paper_trade_id=?, outcome_source='real_pnl'
            WHERE ga_decision_id=? AND is_shadow=0 AND pnl_r IS NULL
            """,
            (pnl_r, gd_id, int(row["id"]), gd_id),
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


def _apply_legacy_fuzzy_migration(conn: sqlite3.Connection) -> None:
    """Mark legacy strategy_evaluations as outcome_source='legacy_fuzzy'.

    - All rows WHERE ga_decision_id IS NULL → legacy_fuzzy
    - All rows WHERE paper_trade_id IS NULL AND outcome_source IS NULL → legacy_fuzzy
    - Clean stalled momentum_continuation_long candidate (Item 12)
    """
    # Mark ga_decision_id IS NULL rows
    cur = conn.execute(
        """
        UPDATE strategy_evaluations
        SET outcome_source='legacy_fuzzy'
        WHERE ga_decision_id IS NULL AND outcome_source IS NULL
        """,
    )
    marked_null_ga = int(cur.rowcount or 0)
    # Mark paper_trade_id IS NULL AND outcome_source IS NULL rows
    cur = conn.execute(
        """
        UPDATE strategy_evaluations
        SET outcome_source='legacy_fuzzy'
        WHERE paper_trade_id IS NULL AND outcome_source IS NULL AND ga_decision_id IS NOT NULL
        """,
    )
    marked_pending = int(cur.rowcount or 0)

    # Item 12: Clean stalled momentum_continuation_long candidate
    stalled = conn.execute(
        """
        SELECT sv.id AS version_id, sv.version, sv.created_at
        FROM strategy_versions sv
        WHERE sv.strategy_name = 'momentum_continuation_long'
          AND sv.status = 'candidate'
          AND datetime(sv.created_at) < datetime('now', '-48 hours')
        """
    ).fetchall()

    stalled_cleaned = 0
    for row in stalled:
        cur = conn.execute(
            "UPDATE strategy_versions SET status='rejected', change_reason=? WHERE id=?",
            ("stalled_candidate_cleanup:超过48小时未进入shadow_testing", int(row["version_id"])),
        )
        stalled_cleaned += int(cur.rowcount or 0)
        conn.execute(
            "UPDATE strategy_patches SET status='rejected' WHERE candidate_version=? AND status NOT IN ('rejected','duplicate')",
            (row["version"],),
        )

    marked = marked_null_ga + marked_pending
    if marked or stalled_cleaned:
        conn.commit()

    LOGGER = __import__("logging", fromlist=["getLogger"]).getLogger("crypto_guard.migrations")
    log = LOGGER.info if (marked or stalled_cleaned) else LOGGER.debug
    log(
        "legacy_fuzzy_migration: marked %d evaluations as legacy_fuzzy, cleaned %d stalled candidates",
        marked, stalled_cleaned,
    )


def _apply_phase_shadow_vt_v2_migration(conn: sqlite3.Connection) -> None:
    """Phase shadow_vt_v2: entry_type, opened_at, expires_at + strategy_name + shadow_virtual_trade_id."""
    # 1.0: shadow_virtual_trades add entry_type, opened_at, expires_at
    _add_column(conn, "shadow_virtual_trades", "entry_type", "TEXT NOT NULL DEFAULT 'market'")
    _add_column(conn, "shadow_virtual_trades", "opened_at", "TEXT")
    _add_column(conn, "shadow_virtual_trades", "expires_at", "TEXT")

    # 1.1: shadow_virtual_trades add strategy_name + rebuild unique index
    _add_column(conn, "shadow_virtual_trades", "strategy_name", "TEXT NOT NULL DEFAULT 'smc_pullback_long'")

    # 1.1a: Soft-mark duplicate shadow_virtual_trades before creating unique index.
    # Priority: closed with pnl_r > open > pending_entry; has initial_risk_usdt+quantity > bare;
    # id DESC (most recent wins tiebreak).
    conn.execute(
        """
        UPDATE shadow_virtual_trades SET status = 'duplicate'
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY strategy_name, candidate_version, ga_decision_id
                           ORDER BY
                               CASE WHEN status = 'closed' AND pnl_r IS NOT NULL THEN 0 ELSE 1 END,
                               CASE WHEN status = 'open' THEN 0 ELSE 1 END,
                               CASE WHEN initial_risk_usdt > 0 AND quantity > 0 THEN 0 ELSE 1 END,
                               id DESC
                       ) AS rn
                FROM shadow_virtual_trades
                WHERE COALESCE(status, '') != 'duplicate'
            ) WHERE rn > 1
        )
        """
    )
    # Drop any stale plain index (pre-dedup era) before creating the partial unique index.
    # IF EXISTS makes this idempotent: no index, old plain index, or current partial index
    # are all handled correctly.
    conn.execute("DROP INDEX IF EXISTS idx_shadow_vt_unique")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_vt_unique
        ON shadow_virtual_trades(strategy_name, candidate_version, ga_decision_id)
        WHERE COALESCE(status, '') != 'duplicate'
        """
    )

    # 1.2: strategy_evaluations add shadow_virtual_trade_id
    _add_column(conn, "strategy_evaluations", "shadow_virtual_trade_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_evals_shadow_vt ON strategy_evaluations(shadow_virtual_trade_id)")

    # 1.3: Backfill opened_at for existing open trades that have no opened_at
    conn.execute(
        "UPDATE shadow_virtual_trades SET opened_at=created_at WHERE opened_at IS NULL AND status='open'"
    )
    # Backfill entry_type to 'market' for existing trades without it (already covered by DEFAULT)

    # 1.4: shadow_virtual_trades add last_processed_candle_time for per-candle replay cursor
    _add_column(conn, "shadow_virtual_trades", "last_processed_candle_time", "INTEGER")

    # 1.5: Partial unique index on strategy_evaluations for shadow dedup
    # Soft-mark duplicate shadow evaluations (outcome_source='duplicate'),
    # keeping the best row per group (VT-linked > has pnl_r > most recent).
    # This preserves the audit trail instead of hard-deleting.
    # NULL outcome_source rows are included: COALESCE handles the NULL != 'duplicate' gap.
    conn.execute(
        """
        UPDATE strategy_evaluations SET outcome_source = 'duplicate'
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY strategy_name, strategy_version, ga_decision_id
                           ORDER BY
                               CASE WHEN shadow_virtual_trade_id IS NOT NULL THEN 0 ELSE 1 END,
                               CASE WHEN pnl_r IS NOT NULL THEN 0 ELSE 1 END,
                               id DESC
                       ) AS rn
                FROM strategy_evaluations
                WHERE is_shadow = 1
                  AND COALESCE(outcome_source, '') != 'duplicate'
            ) WHERE rn > 1
        )
        """
    )
    # Drop old index if it exists (may be a plain index, not partial)
    conn.execute("DROP INDEX IF EXISTS idx_strategy_evals_shadow_unique")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_evals_shadow_unique
        ON strategy_evaluations(strategy_name, strategy_version, ga_decision_id)
        WHERE is_shadow = 1 AND COALESCE(outcome_source, '') != 'duplicate'
        """
    )


def _apply_candidate_cap_cleanup(conn: sqlite3.Connection) -> None:
    """Reject excess candidates beyond 5 per strategy_name.

    For each strategy_name with more than 5 candidate+shadow_testing versions,
    reject the excess candidates, sorted by real_pnl_count DESC, created_at ASC
    (meaning the weakest candidates are rejected first).

    Idempotent: no-op if cap is already satisfied.
    Atomic: all rejections happen in a single transaction.
    """
    # Find all strategy_names that have candidates
    strategy_names = conn.execute(
        """
        SELECT DISTINCT sv.strategy_name
        FROM strategy_versions sv
        WHERE sv.status IN ('candidate', 'shadow_testing')
        """
    ).fetchall()

    for row in strategy_names:
        strategy_name = str(row["strategy_name"])

        # Get all candidate+shadow_testing versions sorted by real_pnl_count DESC, created_at ASC
        candidates = conn.execute(
            """
            SELECT sv.id, sv.version, sv.created_at, sv.status,
                   (SELECT COUNT(*) FROM strategy_evaluations se
                    WHERE se.strategy_name=sv.strategy_name AND se.strategy_version=sv.version
                      AND se.is_shadow=1 AND se.outcome_source='real_pnl' AND se.pnl_r IS NOT NULL) as real_pnl_count
            FROM strategy_versions sv
            WHERE sv.strategy_name=? AND sv.status IN ('candidate', 'shadow_testing')
            ORDER BY real_pnl_count DESC, sv.created_at ASC
            """,
            (strategy_name,),
        ).fetchall()

        if len(candidates) <= 5:
            continue

        # Reject the excess: weakest candidates (fewest real_pnl samples, newest first)
        excess = list(candidates[5:])
        for cand in excess:
            # Reject strategy_versions
            conn.execute(
                "UPDATE strategy_versions SET status='rejected', change_reason=? WHERE id=?",
                ("候选上限 5 已满，自动拒绝旧候选", int(cand["id"])),
            )
            # Sync strategy_patches
            conn.execute(
                "UPDATE strategy_patches SET status='rejected' WHERE candidate_version=? AND status NOT IN ('rejected','duplicate')",
                (cand["version"],),
            )
            # Sync evolution_triggers via strategy_patches
            conn.execute(
                "UPDATE evolution_triggers SET status='rejected' WHERE id IN (SELECT trigger_id FROM strategy_patches WHERE candidate_version=? AND trigger_id IS NOT NULL)",
                (cand["version"],),
            )

    conn.commit()


def _ensure_profit_protection_cutoff_marker(conn: sqlite3.Connection) -> None:
    """Write the profit_protection_mark_price_contract_v1 marker to _migration_state.

    This marker is used by state_consistency._profit_protection_cutoff() to
    determine the effective cutoff timestamp for profit-protection-era
    diagnostics checks. Idempotent: no-op if the marker already exists.
    """
    # Ensure _migration_state table exists (created by _apply_stop_loss_adjustment_dedup
    # or schema.sql, but may not exist yet on a fresh DB).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migration_state (
            key TEXT PRIMARY KEY,
            applied_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO _migration_state(key, applied_at) VALUES (?, CURRENT_TIMESTAMP)",
        ("profit_protection_mark_price_contract_v1",),
    )
    conn.commit()


def _apply_stop_loss_adjustment_dedup(conn: sqlite3.Connection) -> None:
    """Soft-mark duplicate stop_loss_adjustment paper_trade_logs entries.

    For each (order_id, old_stop_loss, new_stop_loss) combination, keep the
    earliest entry and mark the rest with event_json.is_duplicate=true.

    Also adds the partial unique index on alert_outbox(dedupe_key) restricted
    to status='pending' (mirrors schema.sql). Sent alerts keep their full
    history so a future enqueue can reuse the same dedupe_key.

    Idempotent: no-op if no duplicates exist or already marked. Safe to call
    before executescript — guards on required tables existing.

    Migration state guard: worker startup calls initialize_database() at high
    frequency. The expensive scan-and-clean below is only needed once per
    database to tidy historical dirty data; subsequent runs skip the entire
    function via the _migration_state marker table.
    """
    # Lightweight migration-state table — self-contained so the marker works
    # even before schema.sql is executed on a brand-new DB. IF NOT EXISTS
    # makes this harmless on re-runs.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migration_state (
            key TEXT PRIMARY KEY,
            applied_at TEXT
        )
        """
    )

    # 0a. Migration marker: skip the expensive scan-and-clean once it has run
    # successfully on this database. HOWEVER, we must still verify the partial
    # unique index definition matches the current contract (pending-only). If
    # the database was migrated under an older version that used the over-broad
    # 'pending OR sent' scope, the marker alone does NOT guarantee correctness.
    marker_row = conn.execute(
        "SELECT key FROM _migration_state WHERE key=?",
        ("stop_loss_adjustment_dedup_v1",),
    ).fetchone()
    if marker_row:
        # Verify the index is pending-only. If it still has the old scope
        # (status IN ('pending','sent')), drop it and let the IF NOT EXISTS
        # below recreate it with the correct pending-only scope.
        old_index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_alert_outbox_dedupe_unique'"
        ).fetchone()
        if old_index and "sent" in (old_index["sql"] or ""):
            conn.execute("DROP INDEX IF EXISTS idx_alert_outbox_dedupe_unique")
            conn.commit()
            # Fall through to the IF NOT EXISTS CREATE below — marker stays
            # set so we skip the heavy scan, but the index gets corrected.
        else:
            # Index is already pending-only — fully applied, nothing to do.
            return

    # 0b. Guard: required tables must exist. Called early in initialize_database
    # (before executescript), so on a fresh DB some tables may not yet exist.
    required = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name IN ('alert_outbox', 'paper_trade_logs', 'agent_jobs')
        """
    ).fetchall()
    existing = {row["name"] for row in required}
    if not {"alert_outbox", "paper_trade_logs", "agent_jobs"}.issubset(existing):
        # Not all required tables exist yet — nothing to dedupe. Do NOT mark
        # the migration as applied, so the next run can attempt cleanup once
        # the schema is in place.
        return

    # 1. Clean existing duplicate dedupe_keys before creating unique index.
    # Only pending rows are constrained by the unique index; sent rows keep
    # their history. So we only need to collapse duplicate pending rows:
    # keep the earliest pending id per dedupe_key and mark the rest duplicate.
    dup_keys = conn.execute(
        """
        SELECT dedupe_key FROM (
            SELECT dedupe_key, COUNT(*) AS cnt
            FROM alert_outbox
            WHERE dedupe_key IS NOT NULL AND status='pending'
            GROUP BY dedupe_key
            HAVING COUNT(*) > 1
        )
        """
    ).fetchall()
    for (dk,) in dup_keys:
        conn.execute(
            """
            UPDATE alert_outbox SET status='duplicate'
            WHERE dedupe_key=? AND status='pending'
              AND id > (SELECT MIN(id) FROM alert_outbox WHERE dedupe_key=? AND status='pending')
            """,
            (dk, dk),
        )
    if dup_keys:
        conn.commit()

    # 2. Add partial unique index on alert_outbox (idempotent). Mirrors
    # schema.sql: dedupe_key unique only among status='pending' rows. Use
    # IF NOT EXISTS so we never DROP/CREATE on already-applied databases.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_outbox_dedupe_unique
        ON alert_outbox(dedupe_key)
        WHERE dedupe_key IS NOT NULL AND status='pending'
        """
    )

    # 3. Soft-mark duplicate stop_loss_adjustment logs
    dupes = conn.execute(
        """
        SELECT id, order_id, event_json
        FROM (
            SELECT id,
                   json_extract(event_json, '$.order_id') AS order_id,
                   json_extract(event_json, '$.old_stop_loss') AS old_stop,
                   json_extract(event_json, '$.new_stop_loss') AS new_stop,
                   event_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY json_extract(event_json, '$.order_id'),
                                    json_extract(event_json, '$.old_stop_loss'),
                                    json_extract(event_json, '$.new_stop_loss')
                       ORDER BY created_at ASC
                   ) AS rn
            FROM paper_trade_logs
            WHERE event_type='stop_loss_adjustment'
              AND json_extract(event_json, '$.is_duplicate') IS NULL
        ) WHERE rn > 1
        """
    ).fetchall()

    for row in dupes:
        try:
            event = json.loads(row["event_json"])
            event["is_duplicate"] = True
            conn.execute(
                "UPDATE paper_trade_logs SET event_json=? WHERE id=?",
                (json.dumps(event, ensure_ascii=False), int(row["id"])),
            )
        except (json.JSONDecodeError, TypeError):
            pass

    if dupes:
        conn.commit()

    # 4. Soft-mark duplicate agent_jobs for paper_event_alert stop_loss_adjustment.
    # Key the partition on (order_id, event_type, normalized_new_stop) so that
    # two LEGITIMATE stop adjustments on the same order (different new_stop)
    # are NOT marked as duplicates of each other.
    dup_jobs_pending_rows = conn.execute(
        """
        SELECT id, payload_json
        FROM (
            SELECT id, payload_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY json_extract(payload_json, '$.order_id'),
                                    json_extract(payload_json, '$.event_type'),
                                    ROUND(json_extract(payload_json, '$.new_stop_loss'), 8)
                       ORDER BY created_at ASC
                   ) AS rn
            FROM agent_jobs
            WHERE job_type='paper_event_alert'
              AND json_extract(payload_json, '$.event_type')='stop_loss_adjustment'
              AND status IN ('pending', 'success')
        ) WHERE rn > 1
        """
    ).fetchall()

    for row in dup_jobs_pending_rows:
        conn.execute(
            "UPDATE agent_jobs SET status='duplicate' WHERE id=?",
            (int(row["id"]),),
        )

    if dup_jobs_pending_rows:
        conn.commit()

    # 5. Record the migration marker LAST, after all cleanup has committed.
    # This ensures a partial failure does not silently skip future retries.
    conn.execute(
        "INSERT OR IGNORE INTO _migration_state(key, applied_at) VALUES (?, CURRENT_TIMESTAMP)",
        ("stop_loss_adjustment_dedup_v1",),
    )
    conn.commit()


def _apply_hourly_report_accuracy_migration(conn: sqlite3.Connection) -> None:
    """Hourly Report Accuracy (2026-06-28) schema migration.

    - ga_decisions gains batch_id, previous_grade, rendered_summary columns.
    - New analysis_batches table tracks scheduler analysis batch identity and
      per-symbol completion state, enabling the hourly report batch
      completion gate.
    - New batch_symbol_status table replaces JSON columns for atomic per-symbol
      completion tracking (P0-2: concurrent write safety).
    - Idempotent: ALTER TABLE is guarded by _add_column (which checks PRAGMA
      table_info first); CREATE TABLE is guarded by IF NOT EXISTS. Also guards
      that ga_decisions table exists before trying ALTER TABLE (called before
      executescript on fresh DBs).
    """
    # Guard: ga_decisions must exist before we ALTER it.
    ga_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ga_decisions'"
    ).fetchone()
    if ga_table:
        _add_column(conn, "ga_decisions", "batch_id", "TEXT")
        _add_column(conn, "ga_decisions", "previous_grade", "TEXT")
        _add_column(conn, "ga_decisions", "rendered_summary", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ga_decisions_batch "
            "ON ga_decisions(batch_id) WHERE batch_id IS NOT NULL"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL UNIQUE,
            primary_interval TEXT NOT NULL,
            analysis_time INTEGER NOT NULL,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            status TEXT DEFAULT 'running',
            enabled_symbols_json TEXT NOT NULL DEFAULT '[]',
            completed_symbols_json TEXT NOT NULL DEFAULT '[]',
            failed_symbols_json TEXT NOT NULL DEFAULT '[]',
            summary_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_batches_status_time "
        "ON analysis_batches(status, analysis_time)"
    )
    # P0-2: batch_symbol_status for atomic per-symbol completion tracking
    # P2-10 (Round 3): CHECK constraint on status column
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_symbol_status (
            batch_id  TEXT NOT NULL,
            symbol    TEXT NOT NULL,
            status    TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed', 'failed')),
            updated_at TEXT,
            PRIMARY KEY (batch_id, symbol)
        )
        """
    )
    # One-shot migration: populate batch_symbol_status from existing JSON columns
    _migrate_batch_json_to_symbol_status(conn)


def _migrate_batch_json_to_symbol_status(conn: sqlite3.Connection) -> None:
    """One-shot migration: populate batch_symbol_status from existing JSON columns.

    Reads completed_symbols_json and failed_symbols_json from each analysis_batches
    row and inserts them into batch_symbol_status. Idempotent: rows with existing
    batch_id+symbol pairs are skipped via INSERT OR IGNORE.
    """
    # Check if there's any data to migrate
    rows = conn.execute(
        "SELECT batch_id, completed_symbols_json, failed_symbols_json FROM analysis_batches"
    ).fetchall()
    for row in rows:
        bid = row["batch_id"]
        completed = _json_list_from_raw(row["completed_symbols_json"])
        failed = _json_list_from_raw(row["failed_symbols_json"])
        for sym in completed:
            conn.execute(
                "INSERT OR IGNORE INTO batch_symbol_status(batch_id, symbol, status, updated_at) VALUES (?, ?, 'completed', CURRENT_TIMESTAMP)",
                (bid, sym),
            )
        for sym in failed:
            conn.execute(
                "INSERT OR IGNORE INTO batch_symbol_status(batch_id, symbol, status, updated_at) VALUES (?, ?, 'failed', CURRENT_TIMESTAMP)",
                (bid, sym),
            )
    if rows:
        conn.commit()


def _json_list_from_raw(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        import json as _json
        data = _json.loads(raw) if isinstance(raw, str) else raw
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def check_schema_health(*, config: CryptoGuardConfig | None = None, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
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
        "ga_decisions": ["account_feedback_gate_json", "market_regime_gate_json", "batch_id", "previous_grade", "rendered_summary"],
        "opportunity_watches": ["dedupe_key"],
        "paper_positions": ["updated_at"],
        "strategy_evaluations": ["ga_decision_id", "paper_trade_id", "outcome_source", "shadow_virtual_trade_id"],
        "paper_orders": ["initial_stop_loss"],
        "paper_trades": ["initial_stop_loss", "initial_risk_usdt"],
        "shadow_virtual_trades": ["strategy_name", "status", "entry_type", "opened_at", "expires_at", "last_processed_candle_time"],
    }

    # Required indexes
    required_indexes = [
        "idx_opportunity_watches_dedupe",
        "idx_one_open_trade_per_order",
        "idx_shadow_vt_unique",
        "idx_strategy_evals_shadow_unique",
        "idx_alert_outbox_dedupe_unique",
    ]

    # Required tables
    required_tables = ["analysis_batches", "batch_symbol_status"]

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

        # Check required tables
        for tbl_name in required_tables:
            tbl_exists = _conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tbl_name,),
            ).fetchone()
            if not tbl_exists:
                missing.append({"table": tbl_name, "column": "(table)"})

        return {
            "ok": len(missing) == 0,
            "missing_columns": missing,
            "tables_checked": tables_checked,
        }
    finally:
        if own_conn is not None:
            own_conn.close()
