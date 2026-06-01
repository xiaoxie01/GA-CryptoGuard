# 03. 模块设计

## 1. 推荐目录结构

```text
plugins/crypto_guard/
  config/
    trading_mode.yaml
    symbols.yaml
    scheduler.yaml
    strategies.yaml

  data/
    binance_rest.py
    binance_ws.py
    candle_store.py
    kline_cache.py
    orderflow_cache.py
    symbol_registry.py

  analysis/
    price_action_engine.py
    smc_engine.py
    order_flow_engine.py
    momentum_engine.py
    trend_stage_engine.py
    chanlun_engine.py
    market_regime_engine.py
    counter_evidence_engine.py

  sop/
    sop_runner.py
    sop_definitions.py
    sop_outputs.py

  strategy/
    strategy_loader.py
    strategy_scorer.py
    strategy_versioning.py
    shadow_testing.py

  reasoning/
    market_state_builder.py
    ga_judge.py
    decision_schema.py
    prompt_templates.py

  scheduler/
    cron_scheduler.py
    job_registry.py
    job_runner.py
    task_locks.py

  queue/
    job_queue.py
    sqlite_queue.py
    redis_queue.py

  paper/
    paper_broker.py
    paper_account.py
    position_manager.py
    execution_simulator.py
    paper_position_updater.py

  review/
    trade_reviewer.py
    loss_classifier.py
    evolution_engine.py
    strategy_memory.py

  notify/
    feishu_cards.py
    feishu_notifier.py

  storage/
    sqlite_db.py
    repository.py
    migrations.py

  tools/
    ga_crypto_tools.py
```

## 2. 数据模块

### 2.1 binance_rest.py

职责：

- 获取 exchange info。
- 校验 symbol。
- 获取历史 K 线。
- 获取 mark price / funding rate。
- 获取 open interest 可选。

函数建议：

```python
def normalize_symbol(input_text: str) -> str: ...
def validate_um_futures_symbol(symbol: str) -> bool: ...
def fetch_klines(symbol: str, interval: str, start_time: int | None, end_time: int | None, limit: int) -> list[dict]: ...
def fetch_mark_price(symbol: str) -> dict: ...
def fetch_funding_rate(symbol: str) -> dict: ...
```

### 2.2 candle_store.py

职责：

- upsert closed candles。
- 查询指定 analysis_time 之前的 K 线。
- 发现缺口。
- 补齐缺口。

关键规则：

```text
所有查询必须 close_time <= analysis_time_utc
未收盘 K 线不得参与 15m/1H/4H/1D 分析
```

### 2.3 orderflow_cache.py

职责：

- 缓存 aggTrade。
- 计算 CVD。
- 计算主动买卖比例。
- 计算大单方向。
- 生成 3m/5m/15m 订单流窗口。

## 3. 分析模块

### 3.1 price_action_engine.py

输出：

- swing highs/lows。
- HH/HL/LH/LL。
- BOS。
- CHoCH。
- range。
- breakout / retest / fakeout。
- support / resistance。

### 3.2 smc_engine.py

输出：

- liquidity sweep。
- equal high / equal low。
- FVG。
- order block。
- breaker block 可选。
- premium / discount。
- mitigation 状态。

### 3.3 order_flow_engine.py

输出：

- CVD slope。
- aggressive buy/sell ratio。
- large trade bias。
- delta divergence。
- flow confirmation。

### 3.4 momentum_engine.py

输出：

- ROC。
- RSI slope。
- MACD histogram expansion。
- ATR expansion。
- volume impulse。
- momentum quality: healthy / extended / exhausted / divergent。

### 3.5 trend_stage_engine.py

输出：

- early。
- middle。
- late。
- range。
- transition。

判断依据：

- 价格行为。
- 动能。
- SMC。
- 缠论。
- 订单流。
- 高周期方向。
- funding / volatility。

### 3.6 chanlun_engine.py

缠论放在第二阶段以后实现。初版可以输出空值或简化结果。

最终能力：

- 包含关系处理。
- 分型。
- 笔。
- 线段。
- 中枢。
- 背驰。
- 一买 / 二买 / 三买候选。
- 一卖 / 二卖 / 三卖候选。

## 4. Reasoning 模块

### 4.1 market_state_builder.py

读取各模块结果，构建统一快照：

```python
def build_market_state_snapshot(symbol: str, analysis_time_utc: int, mode: str) -> dict: ...
```

### 4.2 ga_judge.py

职责：

- 接收 MarketStateSnapshot。
- 执行 SOP 总流程。
- 匹配策略模板。
- 输出标准 decision JSON。
- 验证 JSON schema。

## 5. Paper 模块

### 5.1 paper_broker.py

职责：

- 创建模拟订单。
- 模拟限价/市价/触发订单成交。
- 平仓。
- 记录交易。

### 5.2 paper_position_updater.py

职责：

- 每 3/5 分钟更新持仓。
- 计算 unrealized pnl。
- 检查 SL / TP / invalid condition。
- 记录 equity snapshot。

## 6. Review 模块

### 6.1 trade_reviewer.py

平仓后执行复盘 SOP。

### 6.2 evolution_engine.py

根据复盘结果生成 candidate patch，不直接启用。

### 6.3 shadow_testing.py

候选策略进入影子测试，只记录假设信号，不推送，不创建模拟盘。
