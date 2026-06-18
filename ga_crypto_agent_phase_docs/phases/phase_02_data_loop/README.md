# Phase 02：数据闭环完善

## 阶段目标

数据闭环完善 是 GA CryptoGuard 在 MVP 后推进路线中的第 2 阶段。

本阶段只实现当前范围内的功能，不提前实现后续阶段高级能力。

## 前置条件

- 已阅读 `global/` 全局设计。
- 已执行 `global/schema.sql` 基础建表或对应迁移。
- 前置阶段已通过验收。
- 系统仍保持“不接实盘”的强制边界。

## 本阶段主要任务

- 确保 market_snapshots、module_analysis_results、signals、paper_orders、paper_trades、trade_reviews 串联
- 所有 GA 分析保存原始结构化 JSON
- 新增数据质量检查
- 补充 no-lookahead 查询工具

## 本阶段完成后应达到

- 任意 signal 可追溯到 snapshot
- 任意 paper_trade 可追溯到 signal 和 snapshot
- 分析只使用已收盘 K 线
- 缺失数据会标记 data_quality 而不是静默通过
