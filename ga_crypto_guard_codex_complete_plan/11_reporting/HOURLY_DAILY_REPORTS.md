# 每小时播报与每日复盘

## 1. 每小时播报目标

每小时播报是管理摘要，不是完整分析报告。

## 2. 每小时播报结构

```text
【GA CryptoGuard 每小时摘要】
时间：UTC
系统状态：正常/降级

一、模拟盘
- 账户权益
- 本日盈亏
- 当前回撤
- 持仓摘要
- pending orders

二、高等级机会
- 只列 S/A/B
- 每个产品最多 3 行

三、机会监控
- symbol
- waiting condition
- invalid condition

四、无优势品种汇总
- C/D 数量
- symbol 列表
- 主要原因概览

五、风险事件
- 开仓/平仓/止损/止盈/调整止损/回撤警报

六、基础设施
- Redis
- SQLite
- Parquet last write
- DuckDB query
- Feishu queue
```

## 3. 不允许

- 逐币展开 D 级长篇分析。
- 把 `analysis_state` 原样推送。
- 把所有 SkillResult 明细放进每小时播报。

## 4. DuckDB 用途

每小时播报中的：

- analyzed_symbols 数量
- S/A/B/C/D 分布
- C/D 无优势列表
- 模拟盘当日 PnL
- 策略表现摘要

优先使用 DuckDB 聚合。

## 5. 每日复盘

UTC 00:05 执行。

必须包含：

- 当日订单总数
- 盈利/亏损
- 亏损归因
- 分析失效原因
- Skill 表现
- 自进化是否触发
- 明日注意事项

复盘必须写入：

- `daily_review_reports`
- `skill_feedback_memory`
- `trade_reviews`
