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
