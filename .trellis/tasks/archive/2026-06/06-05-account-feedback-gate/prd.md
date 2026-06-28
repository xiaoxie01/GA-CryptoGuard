# Account Feedback Gate: Shadow/Annotate Controlled Execution

## Goal

将 `require_stronger_confirmation` 规则从 dry-run 推进到 shadow/annotate 模式，在 trade_plan 进入 paper order 前检查确认质量，但不直接阻断开仓。

## Requirements

### P0: 执行边界

**启用规则**: `consecutive_stop_losses → require_stronger_confirmation`

**执行效果**:
- 当近期账户级亏损模式活跃时，提高交易确认门槛
- 不改策略版本
- 不自动创建/取消订单
- 不直接 watch-only
- 不触发 cooldown

**初始参数**:
- `lookback_hours: 24`
- `min_confidence: 0.80`
- `min_entry_quality: 0.70`
- `affected_scope: trigger_related_symbols`
- `mode: shadow` / `controlled`

### P1: 新建模块

**文件**: `plugins/crypto_guard/risk/account_feedback_gate.py`

```python
def check_account_feedback_gate(
    repo: CryptoGuardRepository,
    symbol: str,
    side: str,
    confidence: float,
    entry_quality: float | None = None,
) -> dict[str, Any]:
    """
    Returns:
        {
            "ok": True,
            "active": True/False,
            "action": "require_stronger_confirmation",
            "required": {"min_confidence": 0.80, "min_entry_quality": 0.70},
            "actual": {"confidence": 0.75, "entry_quality": 0.65},
            "passed": False,
            "decision": "annotate_only",
            "reason": "recent consecutive_stop_losses pattern active",
            "lookback_hours": 24,
            "events_matched": 3,
        }
    """
```

### P2: 配置

**文件**: `plugins/crypto_guard/config/trading_mode.yaml`

```yaml
account_feedback_rules:
  enabled: true
  mode: shadow
  lookback_hours: 24
  actions:
    require_stronger_confirmation:
      enabled: true
      min_confidence: 0.80
      min_entry_quality: 0.70
      on_fail: annotate_only
```

阶段推进:
- `shadow`: 只记录，不影响决策
- `annotate_only`: 写入 GA decision / report
- `downgrade_to_watch`: 失败时转 watch（后期）
- `block_order`: 暂不启用

### P3: 数据库迁移

**文件**: `plugins/crypto_guard/storage/migrations.py`

添加 `account_feedback_gate_json TEXT` 到 `ga_decisions` 表。

### P4: 集成

1. 在 `paper_broker.py` 的 `create_paper_order_from_signal()` 中，account_risk 检查之后、创建订单之前，调用 `check_account_feedback_gate()`
2. 将 gate 结果写入 `ga_decisions.account_feedback_gate_json`
3. 在 `hourly_report.py` 中添加 gate 统计

### P5: 测试

- 测试 shadow 模式不改变 decision
- 测试 lookback_hours 过滤
- 测试 confidence 阈值检查
- 测试 gate 结果写入 GA decision

## Acceptance Criteria

- 142+ 测试 OK
- `diagnose_state_consistency = 0`
- shadow 模式下不改变任何 decision
- 能在 GA decision 中看到 gate 结果
- 能在 report 中看到 gate 统计

## Key Files

| File | Action |
|------|--------|
| `plugins/crypto_guard/risk/account_feedback_gate.py` | 新建 |
| `plugins/crypto_guard/paper/paper_broker.py` | 集成 gate |
| `plugins/crypto_guard/storage/migrations.py` | 添加列 |
| `plugins/crypto_guard/config/trading_mode.yaml` | 添加配置 |
| `plugins/crypto_guard/notify/hourly_report.py` | 添加统计 |
| `plugins/crypto_guard/tests/test_smoke.py` | 新增测试 |
