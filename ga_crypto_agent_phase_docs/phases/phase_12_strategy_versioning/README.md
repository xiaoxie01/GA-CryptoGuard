# Phase 12：策略版本管理

## 阶段目标

策略版本管理 是 GA CryptoGuard 在 MVP 后推进路线中的第 12 阶段。

本阶段只实现当前范围内的功能，不提前实现后续阶段高级能力。

## 前置条件

- 已阅读 `global/` 全局设计。
- 已执行 `global/schema.sql` 基础建表或对应迁移。
- 前置阶段已通过验收。
- 系统仍保持“不接实盘”的强制边界。

## 本阶段主要任务

- 实现 strategy_versions
- 实现 strategy_patches
- active/candidate/shadow_testing/deprecated 状态
- 策略补丁不得直接覆盖 active

## 本阶段完成后应达到

- GA 只能创建 candidate
- active 策略可回滚
- 策略变更有 change_reason
- 飞书可查看策略版本
