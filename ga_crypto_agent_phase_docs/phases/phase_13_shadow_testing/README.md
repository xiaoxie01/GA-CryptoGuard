# Phase 13：影子测试

## 阶段目标

影子测试 是 GA CryptoGuard 在 MVP 后推进路线中的第 13 阶段。

本阶段只实现当前范围内的功能，不提前实现后续阶段高级能力。

## 前置条件

- 已阅读 `global/` 全局设计。
- 已执行 `global/schema.sql` 基础建表或对应迁移。
- 前置阶段已通过验收。
- 系统仍保持“不接实盘”的强制边界。

## 本阶段主要任务

- candidate 策略只记录不推送
- 比较 active 与 candidate
- 样本数阈值
- 升级/拒绝流程

## 本阶段完成后应达到

- candidate 不影响用户推送
- 样本不足不能升级
- 对比指标包含 avg_r/win_rate/drawdown
- 升级必须人工确认或配置允许
