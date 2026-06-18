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
