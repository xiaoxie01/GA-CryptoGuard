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
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_opportunity_status_symbol ON opportunity_watches(status, symbol);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
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
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    filled_at TEXT,
    closed_at TEXT,
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
    close_reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    FOREIGN KEY(order_id) REFERENCES paper_orders(id)
);

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
    status TEXT DEFAULT 'candidate',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

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
