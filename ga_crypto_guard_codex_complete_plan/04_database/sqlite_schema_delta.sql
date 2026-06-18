-- GA CryptoGuard schema delta. All migrations must be idempotent.

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
    counter_evidence_json TEXT,
    risk_check_json TEXT NOT NULL,
    trade_plan_json TEXT,
    opportunity_watch_json TEXT,
    feishu_actions_json TEXT NOT NULL,
    final_summary TEXT NOT NULL,
    created_by TEXT DEFAULT 'ga_master_controller',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ga_decisions_symbol_time ON ga_decisions(symbol, analysis_time);
CREATE INDEX IF NOT EXISTS idx_ga_decisions_grade_time ON ga_decisions(signal_grade, analysis_time);

CREATE TABLE IF NOT EXISTS analysis_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    analysis_time INTEGER NOT NULL,
    analysis_time_utc TEXT NOT NULL,
    ga_decision_id INTEGER,
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
    trend_evolution_json TEXT,
    trade_plan_json TEXT,
    opportunity_watch_recommended INTEGER DEFAULT 0,
    paper_trade_allowed INTEGER DEFAULT 0,
    state_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_analysis_states_symbol_time ON analysis_states(symbol, analysis_time);

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
    degraded INTEGER DEFAULT 0,
    degraded_reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_skill_logs_symbol_time ON skill_execution_logs(symbol, analysis_time);
CREATE INDEX IF NOT EXISTS idx_skill_logs_skill_time ON skill_execution_logs(skill_name, analysis_time);

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
);

CREATE TABLE IF NOT EXISTS parquet_archive_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    file_path TEXT NOT NULL,
    rows_written INTEGER DEFAULT 0,
    min_open_time INTEGER,
    max_open_time INTEGER,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alert_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ga_decision_id INTEGER,
    alert_type TEXT NOT NULL,
    symbol TEXT,
    priority INTEGER DEFAULT 5,
    payload_json TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alert_failure_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_outbox_id INTEGER,
    alert_type TEXT,
    symbol TEXT,
    error_message TEXT,
    retry_count INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_review_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date TEXT NOT NULL UNIQUE,
    summary_json TEXT NOT NULL,
    ga_report TEXT NOT NULL,
    skill_updates_json TEXT,
    evolution_actions_json TEXT,
    pushed_to_feishu INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

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
);

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
);

-- Existing tables should be migrated with nullable ga_decision_id if present.
-- Codex should use ALTER TABLE guarded by pragma table_info in migration code.
