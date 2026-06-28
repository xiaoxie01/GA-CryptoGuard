# fix: evolution_review 必须用 interactive card

## Goal

修复补发通知走旁路生成纯文本 payload 的问题，确保 evolution_review 始终使用 interactive card 格式。

## Problem

1. 手工补发 #419/#420 生成了 `msg_type=text` 的 payload，没有按钮
2. `repo.enqueue_alert()` 没有校验 evolution_review 必须是 interactive
3. 没有测试断言 card 内容包含按钮

## Requirements

1. **P0: 废弃 #419/#420，重新 enqueue interactive card**
2. **P0: 加校验防线** - evolution_review 强制 msg_type=interactive + 合法 card JSON
3. **P1: 补测试** - 断言 card 包含 approve/reject 按钮

## Acceptance Criteria

- [ ] #419/#420 标记为 superseded
- [ ] 新 enqueue 的 alert_outbox 有 interactive card + buttons
- [ ] 校验防线阻止 text 类型的 evolution_review
- [ ] 测试通过
