# fix: evolution_review 入队不依赖 send_message

## Goal

修复 `handle_evolution_trigger_alert()` 中 `verdict_promotion` 入队逻辑被 `send_message` 条件挡住的 bug，确保 `evolution_review` 通知在有 target 时必须入 outbox，不依赖 `send_message` 是否传入。

## Problem

当前代码：
```python
if target and send_message:
    if trigger_type == "verdict_promotion" and candidate_version:
        alert_id = repo.enqueue_alert(...)
        sent = bool(alert_id)
```

问题：
- 如果 worker 处理 job 时 `send_message=None`，不会入 outbox
- job 被标记 `success`，`sent=false`
- 重启后 verdict runner 不会重新触发（已 review_required）
- 结果：通知永远不会发送

## Requirements

1. **P0: 修复入队逻辑**
   - `evolution_review` 只要有 target 就必须入 outbox
   - `send_message` 只影响是否立即投递，不影响入队

2. **P0: 重发当前两个 review_required 通知**
   - v2-trigger-3 (53 样本)
   - v2-trigger-4 (49 样本)

3. **P1: 补测试**
   - `test_verdict_promotion_enqueues_outbox_without_send_message`
   - 断言 send_message=None 时仍能入队

4. **P2: 返回值改进**
   - 区分 `queued` 和 `sent`，避免日志误判

## Acceptance Criteria

- [ ] `send_message=None` 时 verdict_promotion 仍能入 outbox
- [ ] alert_outbox 有 evolution_review pending 记录
- [ ] 两个 review_required 候选的通知已补发
- [ ] 测试通过
