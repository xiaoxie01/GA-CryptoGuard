# 修复自进化死循环：补丁重复创建 + shadow 样本缺失

## Goal

修复 GA CryptoGuard 自进化链路的死循环问题，让"亏损复盘 → 候选改进 → 影子验证 → 人工 review → 记忆沉淀"闭环真正运转。

## What I already know

**当前状态**（用户诊断）：

| 项目 | 状态 |
|------|------|
| 自进化触发器 | 2 个，均为 `shadow_testing`，未 resolved |
| Shadow 评估数据 | **0 条** |
| `strategy_patches` | 1523 条，全部 `candidate` |
| 重复补丁 | v2-trigger-2: 822 个，v2-trigger-1: 666 个 |
| 回测结果 | 只有 8 条 patch 有 `backtest_result_json` |

**根因分析**：

1. **`self_evolution.py` 样本不足后继续创建新补丁**
   - 已有候选 → shadow 样本不足 → 没有 return → 创建新候选 → 下轮继续重复

2. **`evolution_triggers.py` 重复创建路径**
   - 即使已有未完成 trigger，仍然继续创建/保存 candidate patch

3. **影子测试没有样本生产者**
   - `run_shadow_test()` 只读取已有 `strategy_evaluations`，不会生成 shadow 样本
   - 当前没有稳定调用 `record_shadow_evaluation()` 的路径

4. **trigger/version/patch 状态不同步**
   - evolution_triggers: shadow_testing
   - strategy_versions: shadow_testing
   - strategy_patches: candidate（应该是 shadow_testing）

5. **stale cleanup 硬编码**
   - 仍按 "7 天 + 少于 3 个样本" 判断，与当前配置不一致

## Assumptions (temporary)

- MVP 范围：先止血（P0），再闭环（P1）
- 数据修复：软拒绝重复补丁，不物理删除
- 状态机：三张表保持同步

## Open Questions

- ~~MVP 范围确认：是否只做 P0（止住重复创建 + 补 shadow 样本），P1/P2 后续再做？~~
- **已确认**：选择 P0 + P1 + P2 全部做，但 P2 测试聚焦关键闭环，不做泛化扩展

## Decision (ADR-lite)

**Context**：自进化状态机失控，补丁重复创建死循环，shadow 样本为 0，闭环断裂
**Decision**：选择全部 P0 + P1 + P2，但 P2 测试聚焦关键闭环
**Consequences**：本次 MVP 不是只止血，而是恢复自进化闭环：阻止重复创建 → 恢复 shadow 样本生产 → verdict 推进状态 → 软清理历史补丁 → 关键测试防复发

## Requirements (evolving)

### P0 - 止血

1. **self_evolution.py 止住重复创建**
   - 已有候选 + shadow 样本不足 → 返回 `existing_candidate_pending_shadow`
   - 不创建新 patch/version/backtest

2. **evolution_triggers.py 止住重复创建**
   - 已有同类型未完成 trigger → 复用，不创建新 patch

3. **补 shadow 样本写入链路**
   - controller.analyze_symbol() 中：用同一 snapshot 跑 candidate 决策
   - record_shadow_evaluation(is_shadow=1)
   - 不发飞书、不建模拟单

### P1 - 闭环

4. **shadow verdict runner**
   - 定时扫描 shadow_testing 状态
   - run_shadow_test() → 推进状态

5. **统一状态机**
   - 三张表状态同步
   - 明确状态流转：triggered → candidate → backtest → shadow → review → active

6. **清理历史重复补丁**
   - 每个 trigger_id + candidate_version 只保留 1 条 canonical patch
   - 其余标记为 `rejected` 或 `duplicate`

### P2 - 完善

7. **修正 stale cleanup**
   - 读取配置而非硬编码
   - 综合判断：created_at, sample_count, last_shadow_eval_at

8. **补测试**
   - 5 个关键测试用例

## Acceptance Criteria (evolving)

**P0 止血**：
- [ ] self_evolution 已有候选时不再创建新 patch
- [ ] evolution_triggers 已有未完成 trigger 时不再创建新 patch
- [ ] controller 能为 shadow_testing 候选生成 shadow 评估数据
- [ ] shadow 评估数据持续增长（不再是 0）

**P1 闭环**：
- [ ] shadow verdict 能推进状态（passed → review, rejected → rejected）
- [ ] 三张表状态同步
- [ ] 历史重复补丁被软拒绝

**P2 完善**：
- [ ] stale cleanup 使用配置阈值
- [ ] 5 个测试用例通过

## Definition of Done

- Tests added/updated (unit/integration where appropriate)
- Lint / typecheck / CI green
- 现有测试全部通过
- 不破坏现有交易流程

## Out of Scope (explicit)

- 物理删除重复补丁（先软拒绝，保留审计）
- 自动 active（保持人工确认）
- 大规模架构重构

## Technical Notes

**关键文件**：
- `plugins/crypto_guard/strategy/self_evolution.py`
- `plugins/crypto_guard/review/evolution_triggers.py`
- `plugins/crypto_guard/strategy/shadow_testing.py`
- `plugins/crypto_guard/ga_master/controller.py`
- `plugins/crypto_guard/storage/repository.py`

**数据库表**：
- `evolution_triggers` - 触发器
- `strategy_patches` - 补丁
- `strategy_versions` - 版本
- `strategy_evaluations` - 评估（shadow 样本）
- `paper_trades` - 交易

**配置**：
```yaml
evolution:
  online_shadow:
    min_samples_after_backtest: 5
    min_samples_without_backtest: 30
```
