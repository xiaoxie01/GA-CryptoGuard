# Phase 07：动能模块

## 阶段目标

动能模块 是 GA CryptoGuard 在 MVP 后推进路线中的第 7 阶段。

本阶段只实现当前范围内的功能，不提前实现后续阶段高级能力。

## 前置条件

- 已阅读 `global/` 全局设计。
- 已执行 `global/schema.sql` 基础建表或对应迁移。
- 前置阶段已通过验收。
- 系统仍保持“不接实盘”的强制边界。

## 本阶段主要任务

- 实现 RSI slope
- MACD histogram
- ATR expansion
- Volume impulse
- 实体强度与回调力度
- 输出 momentum_score

## 本阶段完成后应达到

- 输出方向与质量
- 识别过热和衰竭
- 动能结果进入 MarketStateSnapshot
- 动能背离能作为反向证据
