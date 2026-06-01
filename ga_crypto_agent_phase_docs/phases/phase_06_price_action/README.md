# Phase 06：价格行为模块

## 阶段目标

价格行为模块 是 GA CryptoGuard 在 MVP 后推进路线中的第 6 阶段。

本阶段只实现当前范围内的功能，不提前实现后续阶段高级能力。

## 前置条件

- 已阅读 `global/` 全局设计。
- 已执行 `global/schema.sql` 基础建表或对应迁移。
- 前置阶段已通过验收。
- 系统仍保持“不接实盘”的强制边界。

## 本阶段主要任务

- 实现 Swing High/Low
- 识别 HH/HL/LH/LL
- 识别 BOS/CHoCH
- 识别突破回踩/假突破/区间
- 输出 price_action JSON

## 本阶段完成后应达到

- 模块结果保存到 module_analysis_results
- 能解释最近结构事件
- 输出 invalid_level
- 对震荡行情不强行判断趋势
