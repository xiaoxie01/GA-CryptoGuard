

<!-- FILE: README.md -->

# GA CryptoGuard：GenericAgent 币安合约预警与模拟盘自进化系统

本项目目标是在 **不接入实盘交易** 的前提下，将 GenericAgent 改造成一个面向 Binance USDⓈ-M Futures 的 **合约市场研究 Agent + 飞书交互式预警系统 + 模拟盘验证系统 + 自我学习迭代系统**。

> 核心边界：只做行情分析、预警、模拟盘、复盘、策略迭代；禁止真实下单，禁止保存具备交易或提现权限的 API Key。

## 给 Codex 的执行原则

Codex 在实现时必须遵守：

1. **不要把所有逻辑塞进 GenericAgent 主对话 Loop。**
   - 飞书用户消息必须优先响应。
   - 定时任务、行情缓存、模拟盘更新、复盘任务必须走独立 worker / queue。
2. **LLM/GA 不直接逐 tick 判断行情。**
   - 代码负责数据获取、K 线缓存、指标和结构特征识别。
   - GA 负责 SOP 推理、语境综合、解释、策略迭代建议。
3. **不接实盘。**
   - 交易模块只允许 paper trading。
   - 配置中必须显式 `live_trading_enabled: false`。
4. **所有分析必须可复盘。**
   - 每次信号必须保存 market snapshot、模块输出、策略评分、GA 判断、飞书消息、用户反馈、模拟盘结果。
5. **自进化不能直接覆盖策略。**
   - GA 可以生成 candidate patch。
   - 需要 shadow testing 后才能升级为 active。
6. **所有调度使用 UTC-0。**
   - K 线时间、任务时间、数据库时间统一使用 UTC timestamp / ISO UTC。
7. **避免未来函数。**
   - 所有分析函数必须接收 `analysis_time_utc`。
   - 数据查询必须限制 `close_time <= analysis_time_utc`。

## 文档结构

```text
.
├── README.md
├── docs/
│   ├── 01_PRODUCT_REQUIREMENTS.md
│   ├── 02_SYSTEM_ARCHITECTURE.md
│   ├── 03_MODULE_DESIGN.md
│   ├── 04_SCHEDULER_AND_QUEUES.md
│   ├── 05_MARKET_ANALYSIS_SOPS.md
│   ├── 06_STRATEGY_AND_EVOLUTION.md
│   ├── 07_FEISHU_INTERACTION.md
│   ├── 08_STORAGE_AND_CACHE.md
│   ├── 09_IMPLEMENTATION_PLAN.md
│   └── 10_ACCEPTANCE_CRITERIA.md
├── sql/
│   └── schema.sql
├── configs/
│   ├── trading_mode.yaml
│   ├── symbols.yaml
│   ├── scheduler.yaml
│   └── strategies.yaml
├── schemas/
│   ├── market_state_snapshot.schema.json
│   ├── ga_decision.schema.json
│   └── trade_review.schema.json
└── prompts/
    └── CODEX_EXECUTION_PROMPT.md
```

## 推荐最终进程

```text
1. frontends/fsapp.py
   飞书消息接收、用户自然语言交互、按钮回调。

2. plugins/crypto_guard/run_scheduler.py
   UTC 定时任务：K 线获取、行情分析触发、复盘触发。

3. plugins/crypto_guard/run_market_worker.py
   Binance WebSocket / REST 数据接入，K 线和订单流缓存。

4. plugins/crypto_guard/run_paper_worker.py
   模拟盘订单推进、止盈止损、权益曲线更新。

5. plugins/crypto_guard/run_ga_workers.py
   ga_user_worker + ga_background_worker，用户任务和后台任务分离。
```

MVP 可以合并 worker，但逻辑必须保持分层和队列隔离。

## 关键目录建议

```text
GenericAgent/
  plugins/
    crypto_guard/
      config/
      data/
      analysis/
      sop/
      strategy/
      reasoning/
      scheduler/
      queue/
      paper/
      review/
      notify/
      storage/
      tools/
      run_scheduler.py
      run_market_worker.py
      run_paper_worker.py
      run_ga_workers.py
```

## MVP 优先级

1. SQLite 主库 + schema 初始化。
2. symbols 产品池管理。
3. Binance K 线缓存，按 UTC 获取已收盘 K 线。
4. 独立 scheduler worker，不能阻塞飞书用户消息。
5. 价格行为、动能、趋势阶段初版 SOP。
6. 飞书自然语言：添加/移除/临时分析产品。
7. GA 分析输出标准 JSON。
8. 临时分析后用户选择：加入模拟盘 / 加入机会监控 / 加入长期监控。
9. 模拟盘执行和收益更新。
10. 每日复盘与策略 candidate patch。


<!-- FILE: docs/01_PRODUCT_REQUIREMENTS.md -->

# 01. 产品需求文档

## 1. 项目定位

GA CryptoGuard 是一个基于 GenericAgent 的加密货币合约市场研究系统。它不接入实盘，只做：

- Binance USDⓈ-M Futures 行情获取。
- 多周期 K 线缓存。
- 缠论 / 价格行为 / SMC / 订单流 / 动能 / 趋势阶段分析。
- 飞书自然语言交互。
- 合约预警。
- 模拟盘订单创建、收益更新和复盘。
- 策略版本管理、影子测试和自进化。

## 2. 非目标

明确禁止：

- 不做真实下单。
- 不接实盘交易 API。
- 不保存具备交易权限的 API Key。
- 不做收益承诺。
- 不把 GA/LLM 当作逐 tick 实时风控引擎。

## 3. 核心用户故事

### 3.1 用户管理产品池

用户可以在飞书自然语言输入：

```text
把 WIFUSDT 加入监控，重点看 15m 和 1h
暂停 DOGE 的分析
列出当前监控币种
只临时分析一下 SUIUSDT，不加入长期监控
```

系统需要：

- 识别用户意图。
- 标准化 symbol，例如 `wif` → `WIFUSDT`。
- 校验 Binance 合约是否存在。
- 写入 `symbols` 表。
- 让后续 UTC 定时任务自动覆盖新增产品。

### 3.2 用户临时分析产品

用户输入：

```text
分析一下 SOLUSDT 现在有没有机会
```

系统需要：

1. 如果该产品未在长期池中，临时补齐必要 K 线。
2. 构建多周期 market snapshot。
3. 执行 SOP 分析。
4. 返回飞书卡片。
5. 根据分析结果提供按钮：
   - 加入模拟盘。
   - 加入机会监控。
   - 加入长期产品池。
   - 忽略。

### 3.3 用户使用机会监控

当行情有方向倾向但没有入场点时，系统输出 `wait_for_setup`，用户可以加入机会监控。

机会监控适用场景：

- 等回踩。
- 等突破。
- 等扫流动性后回收。
- 等 CVD 转强。
- 等高周期确认。
- 当前行情不确定但值得跟踪。

### 3.4 用户创建模拟盘

只有 GA 输出完整交易计划时，才允许加入模拟盘。完整交易计划必须包含：

- symbol。
- side: LONG / SHORT。
- entry_type: market / limit / trigger。
- entry_price 或触发条件。
- stop_loss。
- take_profit 至少一个。
- invalid_condition。
- risk_percent。
- reason。

### 3.5 系统自我学习

每笔模拟盘交易平仓后，系统执行复盘 SOP：

- 读取开单时快照。
- 读取持仓路径。
- 计算 MFE / MAE / R 值。
- 判断亏损原因。
- 生成策略补丁。
- 进入 candidate version。
- 通过 shadow testing 验证后再升级。

## 4. 产品池分类

系统必须支持四种产品状态：

| 类型 | 说明 |
|---|---|
| default_universe | 默认主流产品池，例如 BTCUSDT、ETHUSDT、SOLUSDT |
| user_watchlist | 用户通过飞书添加的长期监控品种 |
| ad_hoc_analysis | 临时分析品种，不自动长期监控 |
| opportunity_watch | 当前等待机会触发的品种 |

定时任务扫描范围：

```text
enabled symbols
+ active opportunity watches
+ open paper positions 涉及的 symbols
```

## 5. 信号分级

GA 输出信号必须分级：

| 等级 | 含义 | 动作 |
|---|---|---|
| S | 强信号，交易计划完整 | 飞书推送，可加入模拟盘 |
| A | 高质量机会 | 飞书推送，可加入模拟盘或机会监控 |
| B | 有倾向但需等待 | 飞书推送，可加入机会监控 |
| C | 观察 | 仅入库，不推送或聚合推送 |
| D | 无优势 / 混乱 | 忽略，仅记录 |

## 6. 风险语言规范

飞书文案禁止使用：

- 一定上涨。
- 必涨。
- 稳赚。
- 放心持有。
- 强烈开多/开空。

统一使用：

- 模拟盘候选。
- 结构倾向。
- 观察机会。
- 风险点。
- 失效条件。
- 不构成实盘建议。


<!-- FILE: docs/02_SYSTEM_ARCHITECTURE.md -->

# 02. 系统架构设计

## 1. 总体架构

```text
Feishu 用户 / 群聊
        ↓
GenericAgent Feishu Frontend
        ↓
User Interaction Queue，高优先级
        ↓
ga_user_worker
        ↓
crypto_guard tools
        ↓
SQLite / Redis / Market Data Cache
```

后台任务链路：

```text
UTC Scheduler Worker
        ↓
K 线获取 / 市场画像 / 模拟盘更新 / 复盘触发
        ↓
需要 GA 推理时，写入 Background Queue
        ↓
ga_background_worker
        ↓
写 signals / reviews / strategy_memory
        ↓
Feishu 通知
```

## 2. 为什么不能把定时任务全部放进 GA 主 Loop

用户已经遇到过：GA 定时任务覆盖用户消息，导致飞书消息没有回应。

根本原因是：

- 定时任务和用户对话共用 session / context。
- 定时任务长推理阻塞主 Loop。
- 后台任务污染用户短期上下文。
- 多个任务并发时状态串线。

必须使用：

```text
独立 scheduler worker
+ 独立 queue
+ 独立 user/background GA workers
+ 独立 session_id
```

## 3. 进程划分

### 3.1 feishu_agent_server

职责：

- 接收飞书事件。
- 快速 ACK。
- 解析基础 event。
- 写入 `agent_jobs` 或 Redis user stream。
- 不执行长任务。

### 3.2 crypto_scheduler_worker

职责：

- 基于 UTC cron 触发任务。
- 记录 `scheduler_runs`。
- 拉取已收盘 K 线。
- 构建 market profile。
- 触发 15m 分析。
- 触发 daily review。
- 不直接占用用户对话 Loop。

### 3.3 market_data_worker

职责：

- Binance REST / WebSocket。
- mark price、kline、aggTrade。
- 实时价格缓存。
- 订单流窗口缓存。
- K 线增量写库。

### 3.4 paper_trade_worker

职责：

- 每 3/5 分钟更新模拟盘。
- 计算浮盈亏。
- 检查止盈止损。
- 写 equity snapshots。
- 触发平仓复盘 job。

### 3.5 ga_user_worker

职责：

- 处理飞书用户消息。
- 自然语言意图识别。
- 产品池管理。
- 临时分析。
- 用户按钮回调。
- 优先级最高。

### 3.6 ga_background_worker

职责：

- 处理定时市场分析。
- 日线/4H总结。
- 交易复盘。
- 策略补丁生成。
- 影子测试结果总结。

## 4. Session 隔离

必须使用不同 session_id：

```text
用户会话：feishu:user:{open_id}
群聊会话：feishu:chat:{chat_id}
15m 定时分析：system:scheduled:15m:{symbol}
日线总结：system:scheduled:daily:{symbol}
复盘任务：system:review:{trade_id}
影子测试：system:shadow:{strategy_name}:{version}
```

后台任务不得写入用户短期记忆。它只能写：

- market_profiles。
- signals。
- trade_reviews。
- strategy_memory。
- strategy_versions。

## 5. 数据流

### 5.1 定时 K 线数据流

```text
scheduler tick
  → calculate expected closed candle time
  → fetch missing candles
  → upsert candles
  → update module_analysis_results
  → update market_profiles
```

### 5.2 15m 分析数据流

```text
fetch latest closed 15m candle
  → read 1D/4H/1H profiles
  → read latest 15m/5m candles
  → read orderflow cache
  → build MarketStateSnapshot
  → pre-score
  → if score high, enqueue GA background job
  → GA outputs decision JSON
  → save signal / opportunity_watch / paper candidate
  → notify Feishu when needed
```

### 5.3 用户临时分析数据流

```text
Feishu message
  → user queue
  → ga_user_worker
  → parse intent
  → normalize symbol
  → ensure data available
  → build snapshot
  → run SOP
  → return Feishu card with actions
```

### 5.4 模拟盘数据流

```text
paper_order pending/open
  → paper worker reads latest price
  → update fills / PnL / MFE / MAE
  → hit SL/TP/timeout/invalid condition
  → close trade
  → enqueue review job
```

## 6. GenericAgent 改造原则

优先新增插件，不修改核心：

```text
plugins/crypto_guard/*
frontends/fsapp.py 仅做必要接入
```

尽量不要改：

```text
agent_loop.py
llmcore.py
memory/*
```

除非确实需要给工具注册和 session 隔离做最小改动。


<!-- FILE: docs/03_MODULE_DESIGN.md -->

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


<!-- FILE: docs/04_SCHEDULER_AND_QUEUES.md -->

# 04. 定时任务与队列设计

## 1. 设计目标

定时任务必须：

- 使用 UTC-0。
- 不阻塞飞书用户消息。
- 不污染用户对话上下文。
- 可幂等重试。
- 可跳过重复执行。
- 用户消息优先级最高。

## 2. 推荐任务表

| 任务 | UTC 时间 | 职责 | 是否需要 GA |
|---|---:|---|---|
| fetch_1d_klines | 每日 00:01 | 获取上一根完整日线，更新日线画像 | 可选，总结时需要 |
| fetch_4h_klines | 00:01/04:01/08:01/12:01/16:01/20:01 | 更新 4H 画像 | 结构变化时需要 |
| fetch_1h_klines | 每小时 00:01 | 更新 1H 画像 | 通常不需要 |
| analyze_market_15m | 每 15m + 1m | 多周期综合分析 | 高评分时需要 |
| update_paper_positions | 每 3m 或 5m | 更新模拟盘收益 | 平仓/异常时需要 |
| daily_review | 每日 00:08 | 昨日模拟盘复盘 | 需要 |
| strategy_shadow_report | 每日/每周 | 候选策略影子测试统计 | 汇总时需要 |

## 3. 不要直接用 GA cron 执行长任务

允许：

```text
GA 定时任务 → 创建轻量 job
```

禁止：

```text
GA 定时任务 → 直接分析 20 个币 → 多轮工具调用 → 推送大量飞书消息
```

正确链路：

```text
cron_scheduler
  → scheduler_runs 幂等检查
  → 确定性数据任务
  → 必要时 enqueue agent_jobs
  → ga_background_worker 消费
```

## 4. 队列优先级

| priority | 类型 |
|---:|---|
| 1 | 飞书用户消息 |
| 2 | 飞书按钮回调 |
| 3 | 重要预警解释 |
| 4 | 模拟盘平仓复盘 |
| 5 | 15m 定时分析 |
| 7 | 1D/4H 总结 |
| 9 | 历史回放 / 影子测试统计 |

后台 worker 每次处理任务前必须检查是否存在 pending user jobs。若有，则延迟后台任务。

## 5. agent_jobs 表

见 `sql/schema.sql`。核心字段：

- job_type。
- priority。
- source。
- session_id。
- payload_json。
- status。
- scheduled_at。
- started_at。
- finished_at。

## 6. task_locks

每类任务必须有锁，避免重叠：

```text
lock:fetch_klines:BTCUSDT:15m
lock:analyze:BTCUSDT:15m
lock:daily_review
lock:paper_update
```

锁必须带 `locked_until`，防止进程挂掉后永远锁死。

## 7. 幂等规则

### 7.1 scheduler_runs 幂等

同一个 `job_name + scheduled_time` 成功后不得重复执行。

### 7.2 candles 幂等

`UNIQUE(symbol, interval, open_time)`，使用 upsert。

### 7.3 Feishu event 幂等

飞书 event_id 需要缓存或写库，避免重复处理。

### 7.4 Paper order 幂等

同一个 `signal_id` 用户重复点击“加入模拟盘”时，不得创建重复订单。需要唯一约束或业务检查。

## 8. 调度伪代码

```python
def run_scheduled_job(job_name: str, scheduled_time: int, task_fn, **kwargs):
    if repo.scheduler_run_success_exists(job_name, scheduled_time):
        return {"ok": True, "skipped": True}

    if not lock.acquire(f"scheduler:{job_name}", ttl_seconds=600):
        return {"ok": True, "skipped": True, "reason": "locked"}

    run_id = repo.create_scheduler_run(job_name, scheduled_time, status="running")
    try:
        result = task_fn(**kwargs)
        repo.finish_scheduler_run(run_id, status="success", result=result)
        return result
    except Exception as exc:
        repo.finish_scheduler_run(run_id, status="failed", error=str(exc))
        raise
    finally:
        lock.release(f"scheduler:{job_name}")
```

## 9. 15m 分析伪代码

```python
def analyze_market_15m():
    symbols = repo.get_active_analysis_symbols()
    for symbol in symbols:
        snapshot = build_market_state_snapshot(
            symbol=symbol,
            analysis_time_utc=get_latest_closed_time("15m"),
            mode="scheduled",
        )
        repo.save_market_snapshot(snapshot)

        pre_score = deterministic_pre_score(snapshot)
        if pre_score >= 0.65:
            queue.enqueue(
                job_type="scheduled_market_analysis",
                priority=5,
                source="scheduler",
                session_id=f"system:scheduled:15m:{symbol}",
                payload_json=snapshot,
            )
```

## 10. 模拟盘更新伪代码

```python
def update_paper_positions():
    positions = repo.get_open_paper_positions()
    for pos in positions:
        price = price_cache.get_mark_or_last_price(pos.symbol)
        result = paper_broker.update_position(pos, price)
        repo.save_equity_snapshot()
        if result.closed:
            queue.enqueue(
                job_type="trade_review",
                priority=4,
                source="paper_worker",
                session_id=f"system:review:{result.trade_id}",
                payload_json={"trade_id": result.trade_id},
            )
```


<!-- FILE: docs/05_MARKET_ANALYSIS_SOPS.md -->

# 05. 市场分析 SOP 设计

## 1. SOP 总原则

GA 采用 SOP 自动化，因此交易能力也必须 SOP 化。

每个分析能力拆成：

```text
数据输入 → 确定性工具识别特征 → SOP 步骤 → 标准 JSON 输出 → 策略评分 → 复盘反馈
```

代码负责看见事实，GA 负责组织事实、交叉验证、解释和迭代。

## 2. 总 SOP：多周期市场分析

```text
SOP_MULTI_TIMEFRAME_MARKET_ANALYSIS

Input:
- symbol
- analysis_time_utc
- mode: scheduled / ad_hoc / opportunity_watch / paper_review
- timeframes: 1D, 4H, 1H, 15m, 5m/3m

Step 1: 数据完整性检查
- 检查 K 线是否完整。
- 检查是否只使用已收盘 K 线。
- 检查是否有缺口。
- 检查是否有未来函数风险。

Step 2: 高周期背景分析
- 读取 1D profile。
- 读取 4H profile。
- 判断大方向、趋势阶段、关键区域。

Step 3: 主周期结构分析
- 对 1H / 15m 执行：
  - 价格行为 SOP。
  - SMC SOP。
  - 动能 SOP。
  - 缠论 SOP，第二阶段实现。

Step 4: 低周期触发分析
- 对 5m / 3m 执行：
  - 订单流 SOP。
  - 入场触发判断。
  - 是否追价。
  - 是否等待回踩。

Step 5: 反向证据检查
- 主动寻找反对做多/做空的证据。
- 输出 contradiction_level。

Step 6: 趋势阶段判断
- early / middle / late / range / transition。

Step 7: 策略匹配
- 匹配 strategy templates。
- 输出策略评分。

Step 8: 动作决策
- trade_plan_available。
- wait_for_pullback。
- wait_for_breakout。
- monitor_only。
- no_edge。

Step 9: 输出标准 GA decision JSON。
```

## 3. 价格行为 SOP

### 3.1 工具输出

- swing_highs。
- swing_lows。
- HH / HL / LH / LL。
- BOS。
- CHoCH。
- range。
- breakout。
- retest。
- fake breakout。
- support / resistance。

### 3.2 SOP

```text
SOP_PRICE_ACTION_ANALYSIS

1. 获取指定周期 K 线。
2. 识别 swing highs / lows。
3. 判断结构序列：HH/HL、LH/LL 或无序震荡。
4. 判断最近结构事件：BOS、CHoCH、fakeout、breakout retest。
5. 标记关键价格区。
6. 判断当前价格位置。
7. 输出结构方向、失效点和置信度。
```

### 3.3 输出示例

```json
{
  "module": "price_action",
  "market_structure": "bullish",
  "swing_sequence": "HH_HL",
  "last_event": "bullish_bos",
  "range_status": "breakout_retest",
  "key_levels": {
    "support": [68120, 67680],
    "resistance": [69200, 69850]
  },
  "entry_context": "waiting_for_retest",
  "invalid_level": 68120,
  "confidence": 0.72
}
```

## 4. SMC SOP

### 4.1 工具输出

- liquidity sweep。
- equal high / equal low。
- FVG。
- order block。
- premium / discount。
- mitigation。
- BOS / CHoCH。

### 4.2 SOP

```text
SOP_SMC_ANALYSIS

1. 识别外部流动性：前高、前低、等高、等低。
2. 检查是否发生 sweep high / sweep low。
3. 检查扫流动性后是否回收。
4. 检查是否出现 CHoCH / BOS。
5. 查找 FVG / OB 区域。
6. 判断价格处于 premium 还是 discount。
7. 判断是否存在可等待的回踩区域。
8. 输出 SMC 方向倾向和等待条件。
```

### 4.3 输出示例

```json
{
  "module": "smc",
  "liquidity": {
    "last_event": "sell_side_liquidity_sweep",
    "reclaimed": true
  },
  "structure_shift": {
    "choch": true,
    "bos": false,
    "direction": "bullish"
  },
  "fvg": {
    "exists": true,
    "direction": "bullish",
    "range": [68200, 68480],
    "status": "unfilled"
  },
  "order_block": {
    "exists": true,
    "direction": "bullish",
    "range": [67880, 68150],
    "mitigated": false
  },
  "premium_discount": "discount",
  "setup": "bullish_reversal_after_sweep",
  "confidence": 0.74
}
```

## 5. 订单流 SOP

### 5.1 工具输出

- CVD。
- CVD slope。
- aggressive_buy_ratio。
- aggressive_sell_ratio。
- large_trade_bias。
- delta divergence。
- volume impulse。

### 5.2 SOP

```text
SOP_ORDER_FLOW_ANALYSIS

1. 读取最近 3m / 5m / 15m aggTrade 数据。
2. 计算主动买入和主动卖出比例。
3. 计算 CVD 方向和斜率。
4. 检查价格-CVD 背离。
5. 判断成交冲击方向。
6. 与 SMC / 价格行为结构交叉验证。
7. 输出订单流确认程度。
```

### 5.3 输出示例

```json
{
  "module": "order_flow",
  "cvd_slope": "up",
  "aggressive_buy_ratio": 0.62,
  "large_trade_bias": "buy",
  "delta_divergence": false,
  "volume_impulse": true,
  "flow_confirmation": "supports_long",
  "confidence": 0.69
}
```

## 6. 动能 SOP

### 6.1 工具输出

- ROC。
- RSI slope。
- MACD histogram expansion。
- ATR expansion。
- volume impulse。
- candle body expansion。
- pullback strength。
- CVD slope。

### 6.2 SOP

```text
SOP_MOMENTUM_ANALYSIS

1. 判断价格动能。
2. 判断成交量动能。
3. 判断波动动能。
4. 判断指标动能。
5. 判断订单流动能。
6. 判断动能质量：healthy / extended / exhausted / divergent。
7. 输出 momentum score。
```

### 6.3 输出示例

```json
{
  "module": "momentum",
  "direction": "bullish",
  "momentum_score": 78,
  "quality": "strong_but_extended",
  "price_momentum": "expanding",
  "volume_confirmed": true,
  "atr_state": "expanding",
  "divergence": false,
  "risk": "short_term_overextended"
}
```

## 7. 趋势阶段 SOP

### 7.1 状态

- early：趋势初期。
- middle：趋势中期。
- late：趋势末期。
- range：震荡。
- transition：转换期。

### 7.2 SOP

```text
SOP_TREND_STAGE_ANALYSIS

1. 读取 1D / 4H / 1H 背景。
2. 判断是否从震荡进入方向性突破。
3. 判断结构是否形成稳定 HH/HL 或 LH/LL。
4. 判断动能状态：刚开始扩张、健康延续、加速、衰竭。
5. 判断末端特征：背驰、放量滞涨、资金费率极端、liquidity grab 失败。
6. 输出趋势阶段和主风险。
```

## 8. 缠论 SOP

缠论模块建议第二阶段实现。

### 8.1 工具输出

- K 线包含关系处理结果。
- 顶底分型。
- 笔。
- 线段。
- 中枢。
- 背驰。
- 买卖点候选。

### 8.2 SOP

```text
SOP_CHANLUN_ANALYSIS

1. 读取指定周期 K 线。
2. 处理包含关系。
3. 识别顶底分型。
4. 生成笔。
5. 生成线段。
6. 识别中枢。
7. 判断背驰。
8. 判断一买/二买/三买或一卖/二卖/三卖候选。
9. 输出结构化结论。
```

### 8.3 输出示例

```json
{
  "module": "chanlun",
  "trend_direction": "up",
  "current_structure": "pullback_after_zhongshu_breakout",
  "zhongshu": {
    "exists": true,
    "range_low": 68000,
    "range_high": 68800,
    "position_of_price": "above"
  },
  "bi": {
    "current_direction": "down",
    "strength": "weak"
  },
  "divergence": {
    "exists": false,
    "type": null
  },
  "signal": "class_2_buy_candidate",
  "confidence": 0.68
}
```

## 9. 反向证据机制

每次分析必须输出：

```json
{
  "bullish_evidence": [],
  "bearish_evidence": [],
  "neutral_or_risk_evidence": [],
  "contradiction_level": "low|medium|high"
}
```

如果 contradiction_level 为 high，则不得直接创建模拟盘，只能输出 monitor_only 或 no_edge。


<!-- FILE: docs/06_STRATEGY_AND_EVOLUTION.md -->

# 06. 策略与自进化设计

## 1. 自进化定义

本系统中的自进化不是让 GA 随意修改策略，而是：

```text
SOP 执行 → 结构化记录 → 模拟盘验证 → 复盘归因 → 策略补丁 → 候选版本 → 影子测试 → 达标后升级
```

GA 可以提出建议，但不能直接覆盖 active 策略。

## 2. 策略模板

策略模板是：

```text
适用市场状态 + 必要证据 + 可选证据 + 风控过滤 + 权重评分 + 输出动作
```

示例见 `configs/strategies.yaml`。

## 3. 策略评分

每个策略输出：

```json
{
  "strategy_name": "smc_pullback_long",
  "strategy_version": "1.0",
  "score": 0.76,
  "decision": "paper_trade_candidate",
  "evidence": [],
  "counter_evidence": [],
  "risk_filters_passed": true
}
```

动作映射：

| score | 动作 |
|---:|---|
| >= 0.80 | S 级，可创建模拟盘候选 |
| 0.72 - 0.79 | A 级，推送并允许用户选择 |
| 0.65 - 0.71 | B 级，机会监控 |
| 0.50 - 0.64 | C 级，仅记录 |
| < 0.50 | D 级，忽略 |

## 4. 复盘 SOP

```text
SOP_TRADE_REVIEW

1. 读取开单时 market_snapshot。
2. 读取策略评分和 GA 原始判断。
3. 读取持仓过程价格路径。
4. 读取出场原因。
5. 计算：
   - pnl_r
   - MFE
   - MAE
   - entry_efficiency
   - exit_efficiency
   - holding_minutes
6. 对照开单理由逐条检查：
   - 哪条证据有效？
   - 哪条证据失效？
   - 是否忽略反向证据？
   - 趋势阶段是否判断错误？
   - 是否追价？
   - 止损是否过窄？
   - 是否 BTC 环境不支持？
7. 输出归因 JSON。
```

## 5. 亏损原因分类

`primary_loss_reason` 必须从以下枚举中选择：

```text
wrong_direction
trend_stage_misclassified
late_trend_chasing
range_misread_as_trend
ignored_counter_evidence
ignored_btc_context
entry_chasing
entry_too_early
entry_too_late
stop_loss_too_tight
take_profit_too_far
orderflow_not_confirmed
smc_false_signal
chanlun_divergence_missed
volatility_spike
news_like_move
```

## 6. 策略补丁

复盘后 GA 可以生成策略补丁：

```json
{
  "strategy_name": "smc_pullback_long",
  "from_version": "1.0",
  "candidate_version": "1.1-candidate",
  "change_reason": "连续亏损集中在 1H late trend + BTC risk_off 背景",
  "patch": {
    "score_adjustments": {
      "late_trend_penalty": -0.2,
      "btc_risk_off_penalty": -0.15
    },
    "risk_filters": [
      "disallow_if btc_context == risk_off and trend_stage == late"
    ]
  }
}
```

状态必须为 `candidate`。

## 7. 策略版本状态

```text
candidate       新生成，尚未验证
shadow_testing  影子测试中
active          正式启用
deprecated      已废弃
disabled        禁用
```

## 8. 影子测试

active 策略正常推送和创建模拟盘候选。

candidate 策略只记录假设信号：

- 不推送飞书。
- 不创建模拟盘订单。
- 只保存 shadow evaluations。

升级条件建议：

```text
sample_count >= 30
candidate_avg_r > active_avg_r
candidate_max_drawdown <= active_max_drawdown
candidate 不明显减少优质机会
不同品种表现不过度集中
```

## 9. 学习对象

GA 不应该学习模糊句子，而应该学习结构化经验：

```json
{
  "lesson_type": "strategy_weight_adjustment",
  "condition": {
    "strategy": "smc_pullback_long",
    "symbol_category": "high_beta",
    "higher_tf_trend_stage": "late",
    "btc_context": "risk_off"
  },
  "finding": "long setups have poor follow-through",
  "adjustment": {
    "score_penalty": -0.18,
    "preferred_action": "opportunity_watch"
  },
  "evidence": {
    "sample_count": 42,
    "avg_r": -0.22,
    "loss_rate": 0.64
  }
}
```

## 10. 用户反馈学习

飞书按钮可写入 `user_feedback`：

- 有用。
- 误报。
- 错过机会。
- 方向错了。
- 入场太晚。
- 止损太近。
- 解释不清楚。
- 推送太频繁。

用户反馈不直接改策略，但可作为复盘证据。


<!-- FILE: docs/07_FEISHU_INTERACTION.md -->

# 07. 飞书交互设计

## 1. 交互原则

- 飞书入口快速 ACK，不执行长任务。
- 用户消息 priority=1。
- 按钮回调 priority=2。
- 后台任务不得阻塞用户响应。
- 所有用户输入通过自然语言意图识别转为结构化命令。

## 2. 支持的自然语言命令

### 2.1 产品池管理

```text
把 WIFUSDT 加入监控
以后也分析 ORDIUSDT
暂停分析 DOGE
移除 LTCUSDT
列出当前监控品种
今天重点分析 BTC ETH SOL
```

### 2.2 临时分析

```text
分析一下 SUIUSDT 现在有没有机会
只临时看一下 WIF，不要加入监控
帮我看 BTC 现在能不能做多
```

### 2.3 机会监控

```text
SOL 如果回踩 168 附近再提醒我
这个币不确定，先帮我盯着
如果 BTC 重新站回 68000 并且 CVD 转强提醒我
```

### 2.4 模拟盘

```text
把刚才那个 SOL 计划加入模拟盘
列出当前模拟盘持仓
复盘最近 10 笔模拟单
```

## 3. Intent Schema

```json
{
  "intent": "add_symbol|remove_symbol|pause_symbol|resume_symbol|list_symbols|analyze_once|create_opportunity_watch|create_paper_order|list_paper_positions|review_trades",
  "symbol": "SOLUSDT",
  "timeframes": ["4h", "1h", "15m", "5m"],
  "scope": "long_term_watchlist|temporary|opportunity_watch",
  "raw_text": "用户原始消息"
}
```

## 4. 临时分析卡片

### 4.1 有完整交易计划

卡片内容：

```text
📊 SOLUSDT 临时分析

方向：模拟多单候选
趋势阶段：1H 多头中期，15m 回踩确认
信号等级：A
置信度：76%

理由：
- 1H 高周期偏多
- 15m bullish BOS
- SMC 回踩 FVG
- CVD 转强

风险：
- 5m 短线动能偏热
- 跌破 165.90 结构失效

模拟计划：
Entry: 168.40
SL: 165.90
TP1: 173.20
TP2: 177.60
RR: 2.1

不构成实盘建议。
```

按钮：

```text
[加入模拟盘] [加入机会监控] [加入长期产品池] [忽略]
```

### 4.2 无完整交易计划但有等待条件

```text
📊 WIFUSDT 临时分析

当前结论：做多观察，但不适合追价
趋势阶段：15m 初期启动，1H 仍在震荡上沿
信号等级：B

等待条件：
- 回踩 2.31-2.36 区域
- 5m 形成 higher low
- CVD 转强

失效条件：
跌破 2.24

不构成实盘建议。
```

按钮：

```text
[加入机会监控] [加入长期产品池] [忽略]
```

### 4.3 无优势

```text
📊 DOGEUSDT 分析

当前结论：无明显优势，不建议模拟盘
原因：
- 1H 震荡
- 15m 信号互相矛盾
- 订单流无确认

系统仅记录本次分析。
```

按钮：

```text
[加入长期产品池] [忽略]
```

## 5. 按钮回调逻辑

### 5.1 加入模拟盘

前置条件：

- `has_trade_plan = true`。
- signal_id 存在。
- 同 signal_id 没有重复 paper_order。

动作：

```text
读取 signal.trade_plan
创建 paper_order
写入 paper_orders
通知 paper_worker
飞书确认
```

### 5.2 加入机会监控

前置条件：

- GA decision 中存在 opportunity_watch。
- watch_condition_json 有效。

动作：

```text
创建 opportunity_watches
设置 expires_at
后续 15m/5m 任务检查
```

### 5.3 加入长期产品池

动作：

```text
写入 symbols
source=user
enabled=1
后续 scheduler 自动覆盖
```

## 6. 飞书推送频率控制

推送：

- S/A 级信号。
- 用户关注品种的 B 级信号。
- 模拟盘开仓/平仓。
- 机会监控触发/失效。
- 4H/1D 重大结构变化。
- 每日复盘。

不推送或聚合：

- C/D 级普通分析。
- 高频模拟盘浮盈亏普通更新。
- shadow testing 普通信号。

## 7. GA 工具列表

```python
crypto_symbol_add(symbol, category="custom", timeframes=None, enabled=True)
crypto_symbol_remove(symbol)
crypto_symbol_pause(symbol)
crypto_symbol_resume(symbol)
crypto_symbol_list()
crypto_analyze_symbol_once(symbol, timeframes=None)
crypto_create_opportunity_watch(symbol, watch_condition, expire_minutes=240)
crypto_create_paper_order_from_signal(signal_id)
crypto_get_market_state(symbol, timeframes)
crypto_get_open_paper_positions()
crypto_review_trade(trade_id)
crypto_get_strategy_stats(strategy_name)
```


<!-- FILE: docs/08_STORAGE_AND_CACHE.md -->

# 08. 存储与缓存设计

## 1. 推荐组合

MVP：

```text
SQLite + WAL
```

稳定运行：

```text
SQLite + WAL + Redis
```

回测分析阶段：

```text
SQLite + Redis + Parquet + DuckDB
```

## 2. SQLite 用途

SQLite 作为主库，存储：

- symbols。
- candles 热数据。
- market_profiles。
- module_analysis_results。
- signals。
- opportunity_watches。
- paper_orders。
- paper_trades。
- trade_reviews。
- strategy_versions。
- strategy_patches。
- scheduler_runs。
- agent_jobs。
- user_feedback。

推荐 PRAGMA：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

## 3. Redis 用途，可选但推荐

Redis 用于：

- 用户消息队列。
- 后台任务队列。
- 实时价格缓存。
- mark price 缓存。
- orderflow 窗口缓存。
- 分布式锁。
- 飞书事件去重。
- 模拟盘实时浮盈亏缓存。

Key 示例：

```text
price:BTCUSDT
mark_price:BTCUSDT
funding:BTCUSDT
orderflow:BTCUSDT:5m
lock:analyze:BTCUSDT:15m
feishu:event:{event_id}
queue:ga:user
queue:ga:background
```

## 4. DuckDB + Parquet，用于第二阶段

长期 K 线归档不是 MVP 必须项，但对自进化有价值：

- 历史回放。
- 策略版本比较。
- 防未来函数检查。
- 多市场状态样本积累。
- 减少重复请求 Binance。

建议路径：

```text
data/klines/binance_um/BTCUSDT/15m/2026-05.parquet
data/klines/binance_um/BTCUSDT/1h/2026.parquet
```

SQLite 可保留热数据：

```text
1m: 最近 3-7 天
3m/5m: 最近 14 天
15m: 最近 60-90 天
1h/4h/1d: 可长期保留
```

## 5. 数据保留策略

MVP 不做归档也可以，但 schema 需要为后续保留扩展：

- `candles.source`。
- `candles.is_closed`。
- `candles.created_at` / `updated_at`。
- `market_profiles.profile_time`。
- `analysis_time`。

## 6. 未来函数防护

所有分析查询必须：

```sql
WHERE close_time <= :analysis_time_utc
```

禁止在 15m 分析时使用未收盘的 15m K 线。

允许低周期实时触发使用实时价格/订单流，但必须在 snapshot 中标记：

```json
{
  "data_type": "realtime_trigger",
  "is_closed_candle": false
}
```

## 7. 数据库迁移

实现时建议提供：

```text
plugins/crypto_guard/storage/migrations.py
plugins/crypto_guard/storage/schema.sql
```

启动时：

1. 连接 SQLite。
2. 设置 PRAGMA。
3. 执行 schema。
4. 检查必要配置。
5. 初始化默认 symbols 和策略版本。


<!-- FILE: docs/09_IMPLEMENTATION_PLAN.md -->

# 09. 实施路线图

## Phase 0：项目骨架

目标：在不破坏 GenericAgent 核心的前提下新增 crypto_guard 插件。

任务：

- 创建 `plugins/crypto_guard` 目录结构。
- 创建 config loader。
- 创建 SQLite storage。
- 执行 schema 初始化。
- 创建 repository 层。
- 配置 `live_trading_enabled=false`。

验收：

- 程序能初始化数据库。
- 默认 symbols 写入成功。
- 不需要 Binance API Key 即可启动。

## Phase 1：产品池与飞书自然语言

任务：

- 实现 `crypto_symbol_add/remove/pause/resume/list`。
- 实现 symbol normalize。
- 实现飞书 intent parser。
- 飞书用户消息写入 high priority job。
- user worker 处理产品管理消息。

验收：

- 用户可以飞书添加/暂停/移除/列出 symbol。
- 定时任务读取 active symbol 列表。
- 后台任务不会阻塞用户消息。

## Phase 2：K 线缓存与 UTC 调度

任务：

- 实现 Binance REST kline fetch。
- 实现 candles upsert。
- 实现 scheduler worker。
- 实现 fetch_1d/4h/1h/15m jobs。
- 实现 scheduler_runs 幂等。
- 实现 task_locks。

验收：

- 每个周期只获取上一根已收盘 K 线。
- 重复执行不会产生重复数据。
- 所有时间使用 UTC。

## Phase 3：基础分析引擎

先实现：

- price_action_engine。
- momentum_engine。
- trend_stage_engine 初版。
- counter_evidence_engine 初版。

暂缓：

- 完整缠论。
- 复杂 order block。

验收：

- 每个 symbol/timeframe 能输出 module_analysis_results。
- 能生成 MarketStateSnapshot。
- 所有输出符合 schema。

## Phase 4：GA SOP 决策

任务：

- 实现 SOP runner。
- 实现 GA decision prompt。
- 验证 GA 输出 JSON schema。
- 实现 signal grade。
- 实现 strategy scorer。

验收：

- 临时分析能返回标准决策。
- 有完整交易计划时显示“加入模拟盘”。
- 无交易计划但有等待条件时显示“加入机会监控”。

## Phase 5：飞书卡片与按钮闭环

任务：

- 生成飞书分析卡片。
- 实现按钮回调。
- 加入模拟盘。
- 加入机会监控。
- 加入长期产品池。

验收：

- 用户临时分析后可以一键操作。
- 重复点击不会创建重复订单。

## Phase 6：模拟盘

任务：

- paper_broker。
- position_manager。
- paper_position_updater。
- paper_equity_snapshots。
- SL/TP/timeout/invalid condition。

验收：

- 模拟盘订单能 pending → open → closed。
- 每 3/5 分钟更新收益。
- 平仓后自动创建 review job。

## Phase 7：复盘与自进化

任务：

- trade_review SOP。
- loss_classifier。
- strategy_patches。
- strategy_versions。
- shadow_testing。

验收：

- 平仓后生成复盘 JSON。
- 亏损原因结构化。
- 生成 candidate patch。
- candidate 进入 shadow_testing，不直接 active。

## Phase 8：SMC / 订单流 / 缠论增强

优先级：

1. SMC liquidity sweep / FVG。
2. CVD 和主动买卖比例。
3. 价格-CVD 背离。
4. 简化 order block。
5. 缠论分型 / 笔 / 中枢 / 背驰。

验收：

- 模块输出可以参与策略评分。
- 复盘能定位哪个模块证据失效。

## Phase 9：历史回放与长期归档

可选后续：

- Parquet 归档。
- DuckDB 查询。
- 历史回放。
- 策略版本对比。
- 防未来函数测试。

## Codex 实施建议

Codex 每次改动应该小步提交：

```text
1. 修改一个模块。
2. 补充最小测试。
3. 保证现有接口不破坏。
4. 更新 README 或配置示例。
5. 避免一次性生成过多不可运行代码。
```


<!-- FILE: docs/10_ACCEPTANCE_CRITERIA.md -->

# 10. 验收标准

## 1. 不接实盘验收

必须满足：

- 配置中 `live_trading_enabled=false`。
- 无真实下单代码路径。
- 无 `/order` 实盘交易调用。
- 不读取交易权限 API Key。
- 代码中所有交易相关类命名为 paper 或 simulation。

## 2. 用户消息不被定时任务覆盖

测试场景：

1. 手动触发 daily_review 长任务。
2. 同时在飞书发送“分析 BTC”。
3. 系统必须优先响应用户消息。
4. 用户 session 不得出现 daily_review 的上下文污染。

验收：

- 用户消息 job priority=1。
- daily_review priority>=7。
- session_id 不同。
- 后台 worker 检测到 user queue pending 时暂停或延迟。

## 3. UTC 定时任务

测试：

- 在 UTC 10:01 执行 1H 任务，只获取 09:00-09:59:59 的已收盘 K 线。
- 在 UTC 10:16 执行 15m 分析，只使用 10:00-10:14:59 或更早的已收盘 15m K 线。

验收：

- 不使用未收盘 K 线。
- candles `open_time` 唯一。
- scheduler_runs 幂等。

## 4. 产品池

测试命令：

```text
把 WIFUSDT 加入监控
暂停 WIFUSDT
恢复 WIFUSDT
移除 WIFUSDT
列出当前监控品种
```

验收：

- symbols 表正确更新。
- 定时任务扫描 active symbols。

## 5. 临时分析

测试命令：

```text
只临时分析一下 SUIUSDT，不加入监控
```

验收：

- 不写入长期 user_watchlist。
- 写入 ad_hoc_analyses。
- 返回决策卡片。
- 根据结果显示正确按钮。

## 6. 机会监控

验收：

- 创建 opportunity_watches。
- 支持 expires_at。
- 支持 invalid_condition。
- 触发后生成 signal。
- 失效后飞书通知。

## 7. 模拟盘

验收：

- 只有完整 trade_plan 才能创建 paper_order。
- 同一个 signal 不能重复创建订单。
- 按价格推进 pending/open/closed。
- 记录 MFE、MAE、PnL、R 值。
- 平仓后创建 trade_review job。

## 8. SOP 输出

每个模块必须输出标准 JSON：

- price_action。
- smc，可第二阶段为空但 schema 存在。
- order_flow，可第二阶段为空但 schema 存在。
- momentum。
- trend_stage。
- chanlun，可第二阶段为空但 schema 存在。

GA decision 必须通过 `ga_decision.schema.json` 校验。

## 9. 自进化

验收：

- trade_review 输出亏损/盈利归因。
- strategy_patch 状态默认为 candidate。
- candidate 策略不得直接 active。
- shadow testing 不推送飞书、不创建模拟盘。
- 满足样本条件后才允许人工或规则升级 active。

## 10. 防未来函数

测试：

- 用历史时间点 analysis_time 回放。
- 数据查询不得读取 close_time > analysis_time 的 K 线。

验收：

- 所有 repository 查询暴露 `analysis_time_utc` 参数。
- 所有策略评估记录 analysis_time。
