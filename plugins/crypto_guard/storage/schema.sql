PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    market TEXT DEFAULT 'binance_um_futures',
    base_asset TEXT,
    quote_asset TEXT DEFAULT 'USDT',
    category TEXT DEFAULT 'custom',
    enabled INTEGER DEFAULT 1,
    source TEXT DEFAULT 'user',
    risk_profile TEXT DEFAULT 'auto',
    default_timeframes TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    close_time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    quote_volume REAL,
    taker_buy_volume REAL,
    taker_buy_quote_volume REAL,
    trade_count INTEGER,
    is_closed INTEGER DEFAULT 1,
    source TEXT DEFAULT 'binance',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_candles_symbol_interval_time ON candles(symbol, interval, open_time);
CREATE INDEX IF NOT EXISTS idx_candles_close_time ON candles(symbol, interval, close_time);

CREATE TABLE IF NOT EXISTS market_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    profile_time INTEGER NOT NULL,
    trend_direction TEXT,
    trend_stage TEXT,
    volatility_state TEXT,
    momentum_state TEXT,
    structure_state TEXT,
    smc_state TEXT,
    chanlun_state TEXT,
    profile_json TEXT NOT NULL,
    ga_summary TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, interval, profile_time)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    analysis_time INTEGER NOT NULL,
    mode TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, analysis_time, mode)
);

CREATE TABLE IF NOT EXISTS module_analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    analysis_time INTEGER NOT NULL,
    module TEXT NOT NULL,
    result_json TEXT NOT NULL,
    confidence REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, timeframe, analysis_time, module)
);

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
    ga_decision_id INTEGER,
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
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skill_logs_symbol_time ON skill_execution_logs(symbol, timeframe, analysis_time, skill_name);

CREATE TABLE IF NOT EXISTS skill_feedback_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    pattern_type TEXT,
    affected_symbols TEXT,
    affected_sides TEXT,
    finding TEXT NOT NULL,
    suggested_adjustment_json TEXT,
    status TEXT DEFAULT 'candidate',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skill_feedback_status ON skill_feedback_memory(skill_name, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_skill_feedback_pattern ON skill_feedback_memory(pattern_type, status);

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
    account_feedback_gate_json TEXT,
    created_by TEXT DEFAULT 'ga_master_controller',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ga_decisions_symbol_time ON ga_decisions(symbol, analysis_time);
CREATE INDEX IF NOT EXISTS idx_ga_decisions_grade_time ON ga_decisions(signal_grade, analysis_time);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT,
    direction TEXT,
    trend_stage TEXT,
    strategy_name TEXT,
    strategy_version TEXT,
    strategy_tags TEXT,
    confidence REAL,
    score REAL,
    signal_grade TEXT,
    alert_level TEXT,
    decision TEXT,
    market_snapshot_id INTEGER,
    trade_plan_json TEXT,
    opportunity_watch_json TEXT,
    ga_reason TEXT,
    risk_notes TEXT,
    status TEXT DEFAULT 'created',
    ga_decision_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(market_snapshot_id) REFERENCES market_snapshots(id)
);

CREATE TABLE IF NOT EXISTS ad_hoc_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    requested_by TEXT,
    request_text TEXT,
    timeframes TEXT,
    analysis_result_json TEXT NOT NULL,
    ga_summary TEXT,
    has_trade_plan INTEGER DEFAULT 0,
    signal_id INTEGER,
    status TEXT DEFAULT 'created',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS opportunity_watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT,
    watch_reason TEXT,
    watch_condition_json TEXT NOT NULL,
    invalid_condition_json TEXT,
    source_analysis_id INTEGER,
    source_signal_id INTEGER,
    status TEXT DEFAULT 'active',
    expires_at TEXT,
    triggered_at TEXT,
    invalidated_reason TEXT,
    last_checked_at TEXT,
    ga_decision_id INTEGER,
    created_by_user_action INTEGER DEFAULT 0,
    source_button_action TEXT,
    dedupe_key TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_opportunity_status_symbol ON opportunity_watches(status, symbol);

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
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    ga_decision_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    entry_price REAL,
    trigger_price REAL,
    stop_loss REAL,
    take_profit_json TEXT,
    quantity REAL,
    risk_percent REAL,
    status TEXT DEFAULT 'pending',
    reason TEXT,
    fill_method TEXT,
    source TEXT DEFAULT 'signal_compat',
    risk_check_passed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    filled_at TEXT,
    closed_at TEXT,
    expires_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    invalidated_by_ga_decision_id INTEGER,
    UNIQUE(signal_id),
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    stop_loss REAL,
    take_profit_json TEXT,
    quantity REAL,
    pnl REAL,
    pnl_percent REAL,
    pnl_r REAL,
    max_favorable_excursion REAL,
    max_adverse_excursion REAL,
    entry_efficiency REAL,
    exit_efficiency REAL,
    signal_decay_score REAL,
    stop_take_path_json TEXT,
    fill_method TEXT,
    close_reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    FOREIGN KEY(order_id) REFERENCES paper_orders(id)
);

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
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_account_status ON paper_positions(account_id, status, symbol);

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
);
CREATE INDEX IF NOT EXISTS idx_paper_trade_logs_symbol_time ON paper_trade_logs(symbol, created_at);

CREATE TABLE IF NOT EXISTS paper_equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    account_equity REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    margin_used REAL,
    open_position_count INTEGER,
    snapshot_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trade_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    result TEXT,
    primary_reason TEXT,
    secondary_reasons_json TEXT,
    market_context TEXT,
    mistake_tags TEXT,
    improvement_suggestion TEXT,
    ga_review_json TEXT,
    market_regime_at_loss TEXT,
    evolution_trigger_allowed INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(trade_id) REFERENCES paper_trades(id)
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT DEFAULT 'candidate',
    config_json TEXT NOT NULL,
    change_reason TEXT,
    created_from_review_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(strategy_name, version)
);

CREATE TABLE IF NOT EXISTS strategy_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT,
    analysis_time INTEGER NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    score REAL,
    decision TEXT,
    evidence_json TEXT,
    counter_evidence_json TEXT,
    is_shadow INTEGER DEFAULT 0,
    snapshot_id INTEGER,
    pnl_r REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS strategy_patches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    from_version TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    patch_json TEXT NOT NULL,
    reason TEXT,
    evidence_json TEXT,
    trigger_id INTEGER,
    status TEXT DEFAULT 'candidate',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

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
);

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
);

CREATE TABLE IF NOT EXISTS self_evolution_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_evolution_triggers_status ON evolution_triggers(status, trigger_type, created_at);

CREATE TABLE IF NOT EXISTS strategy_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT,
    condition_hash TEXT,
    sample_count INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    avg_rr REAL,
    avg_pnl_percent REAL,
    notes TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
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

CREATE TABLE IF NOT EXISTS scheduler_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    scheduled_time INTEGER NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    result_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(job_name, scheduled_time)
);

CREATE TABLE IF NOT EXISTS agent_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    priority INTEGER DEFAULT 5,
    source TEXT NOT NULL,
    session_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    scheduled_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT,
    result_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_status_priority ON agent_jobs(status, priority, scheduled_at);

CREATE TABLE IF NOT EXISTS task_locks (
    lock_name TEXT PRIMARY KEY,
    owner TEXT,
    locked_until TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feishu_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT,
    received_at TEXT DEFAULT CURRENT_TIMESTAMP,
    payload_json TEXT
);

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
);
CREATE INDEX IF NOT EXISTS idx_alert_outbox_status_retry ON alert_outbox(status, next_retry_at, priority);
CREATE INDEX IF NOT EXISTS idx_alert_outbox_dedupe ON alert_outbox(dedupe_key, created_at);

CREATE TABLE IF NOT EXISTS alert_failure_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_outbox_id INTEGER,
    alert_type TEXT,
    symbol TEXT,
    error_message TEXT,
    retry_count INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
);
CREATE INDEX IF NOT EXISTS idx_parquet_archive_runs_recent ON parquet_archive_runs(created_at, symbol, interval);

CREATE TABLE IF NOT EXISTS runtime_config (
    config_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT,
    target_id INTEGER,
    feedback_type TEXT,
    feedback_text TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sop_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sop_name TEXT NOT NULL,
    version TEXT NOT NULL,
    module TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    output_schema_json TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sop_name, version)
);
