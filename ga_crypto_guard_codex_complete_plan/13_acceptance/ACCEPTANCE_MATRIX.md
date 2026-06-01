# 最终验收矩阵

## A. GA 主控验收

- [ ] 飞书用户分析请求进入 GA Master Controller。
- [ ] 定时分析请求进入 GA Master Controller。
- [ ] 每次最终分析写入 `ga_decisions`。
- [ ] `paper_order` 只能由 `ga_decision_id` 创建。
- [ ] `opportunity_watch` 只能由 `ga_decision_id` + 用户按钮确认创建。
- [ ] 工具层不直接输出最终交易决策。
- [ ] 策略层不绕过 GA 创建交易动作。

## B. Skill 验收

- [ ] 五大 Skill 均有 `skill.yaml`。
- [ ] 五大 Skill 均有 `prompt.md`。
- [ ] 五大 Skill 均有 `tools.py`。
- [ ] 五大 Skill 均有 `schema.json`。
- [ ] 五大 Skill 均有 `feedback_rules.yaml`。
- [ ] Skill 执行写入 `skill_execution_logs`。
- [ ] Tool 只输出事实，不输出交易动作。
- [ ] 每日复盘可写入 `skill_feedback_memory`。

## C. 飞书交互验收

- [ ] D/C 级只显示 `[加入长期产品池] [忽略]`。
- [ ] B 级显示 `[加入机会监控] [加入长期产品池] [忽略]`。
- [ ] A/S 且 trade_plan 完整并风控通过，显示 `[加入模拟盘]`。
- [ ] 无 trade_plan 不显示 `[加入模拟盘]`。
- [ ] D 级不建议机会监控。
- [ ] 用户未点击按钮不会自动加入观察列表。
- [ ] 每小时播报为摘要，不展示逐币长篇模块明细。
- [ ] `详细分析 XXX` 才展开模块明细。

## D. Redis 验收

- [ ] `/status` 显示 Redis 状态。
- [ ] Redis 可见 `queue:user:feishu` 或对应队列 key。
- [ ] Redis 可见 `latest_price:{symbol}`。
- [ ] Redis 可见 `lock:job:*`。
- [ ] Redis 可见 `quiet:{symbol}:{alert_type}`。
- [ ] Redis 可见 `dedupe:feishu_event:*`。
- [ ] Redis 断开时系统 fallback SQLite，并显示 degraded。

## E. Parquet 验收

- [ ] closed candle 写入后生成 Parquet 文件。
- [ ] 文件路径符合 `data/parquet/klines/binance_um/{symbol}/{interval}/{yyyy-mm}.parquet`。
- [ ] 重复归档不会产生重复 K 线。
- [ ] `parquet_archive_runs` 有记录。
- [ ] `/status` 显示最近 Parquet 写入时间。

## F. DuckDB 验收

- [ ] DuckDB 数据库位于 `D:/Program Files/duckdb/crypto_guard_analytics.duckdb`。
- [ ] `/status` 执行轻量 DuckDB 查询并返回 ok/degraded。
- [ ] DuckDB 可以读取 Parquet。
- [ ] 每小时播报中的等级分布来自 DuckDB 聚合或明确 fallback。
- [ ] 每日复盘基础统计可由 DuckDB 查询。

## G. 分析流程验收

- [ ] 每次分析写入 `analysis_states`。
- [ ] 下一次分析读取 previous analysis_state。
- [ ] 4H 找方向，1H/15M 找趋势结构，5M/1M 找入场。
- [ ] 5M 不能单独推翻 4H。
- [ ] 未收盘大周期 K 线不能作为确认依据。
- [ ] next_analysis_time 对齐 K 线收盘后。

## H. 风控与模拟盘验收

- [ ] RR < 2 拒绝模拟盘。
- [ ] confidence < 0.72 拒绝模拟盘。
- [ ] 手动飞书开仓不能绕过风控。
- [ ] 每 3 分钟更新最新价格。
- [ ] 市价单使用下一根 K 线开盘价 ±0.1% 滑点。
- [ ] 挂单使用 high/low 区间成交判断。
- [ ] 开仓/平仓/止损/止盈/调整止损必须推送飞书。

## I. 自进化验收

- [ ] 连续 3 次止损创建 `evolution_triggers`。
- [ ] 总回撤 >10% 创建 `evolution_triggers`。
- [ ] 极端行情暂停自动生成策略补丁。
- [ ] patch 进入 candidate / shadow_testing。
- [ ] 不自动覆盖 active。
- [ ] 用户确认后才能升级。

## J. 安全验收

- [ ] 不接实盘。
- [ ] 不调用 Binance 下单接口。
- [ ] 不保存交易权限或提现权限 API Key。
- [ ] 飞书文案不出现“稳赚”“必涨”“确定盈利”等承诺。
