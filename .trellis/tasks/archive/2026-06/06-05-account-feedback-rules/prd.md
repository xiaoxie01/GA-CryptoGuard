# Account-Level Feedback Rules: Dry-Run Bridge Layer

## Goal

让规则引擎读懂 1,688 条账户级亏损记忆（`consecutive_stop_losses` / `daily_loss_threshold`），生成解释型报告，不改交易行为。

## Requirements

### 1. 新增 account-level feedback rules YAML

**文件**: `plugins/crypto_guard/skills/account_risk/feedback_rules.yaml`

```yaml
feedback_rules:
  - when: consecutive_stop_losses
    action: raise_account_risk_sensitivity
    description: "连续止损后提升账户风控敏感度"
    params:
      severity_multiplier: 1.5
      cooldown_hours: 4

  - when: consecutive_stop_losses
    action: require_stronger_confirmation
    description: "连续止损后要求更强的信号确认"
    params:
      min_confidence: 0.80
      min_entry_quality: 0.7

  - when: daily_loss_threshold
    action: cooldown_recent_loss_symbols
    description: "日内亏损阈值触发后冷却近期亏损标的"
    params:
      cooldown_hours: 8
      affected_scope: "trigger_related_symbols"

  - when: daily_loss_threshold
    action: downgrade_trade_to_watch_when_recent_pattern_active
    description: "日内亏损阈值活跃时降级 trade_plan 为 watch_only"
    params:
      lookback_hours: 24
```

### 2. 新增 `evaluate_account_feedback_rules_dry_run()`

**文件**: `plugins/crypto_guard/diagnostics/account_feedback_rules_dry_run.py`

函数签名:
```python
def evaluate_account_feedback_rules_dry_run(
    repo: CryptoGuardRepository,
    *,
    lookback_days: int = 90,
) -> dict[str, Any]
```

逻辑:
1. 加载 account_risk/feedback_rules.yaml
2. 查询 `skill_feedback_memory` 中 `source_type='evolution_trigger'` 且有 `pattern_type` 的条目
3. 按 `pattern_type` 分组统计
4. 对每个规则，计算：
   - 匹配的历史事件数
   - 涉及的 candidate_patch_ids
   - 关联的 related_trade_ids（从 strategy_patches → evolution_triggers 取）
   - 推断 affected symbols/sides（从 paper_trades 关联）
5. 生成 would_apply 动作列表，每条含解释

返回:
```python
{
    "ok": True,
    "matches": [
        {
            "rule_when": "consecutive_stop_losses",
            "rule_action": "raise_account_risk_sensitivity",
            "description": "连续止损后提升账户风控敏感度",
            "event_count": 668,
            "patch_count": 668,
            "sample_patch_ids": [1727, 1725, ...],
            "inferred_symbols": ["BTCUSDT", "ETHUSDT"],
            "inferred_sides": ["LONG", "SHORT"],
            "would_apply": True,
            "params": {"severity_multiplier": 1.5, "cooldown_hours": 4},
        },
        ...
    ],
    "summary": {
        "total_matches": N,
        "by_pattern": {"consecutive_stop_losses": X, "daily_loss_threshold": Y},
        "by_action": {"raise_account_risk_sensitivity": A, ...},
    },
    "rules_loaded": N,
    "events_checked": 1688,
}
```

### 3. 生成解释型报告

输出格式（用于日志或 hourly_report 集成）：
```
Account Feedback Rules Dry-Run Report
=====================================
Events checked: 1,688
Rules loaded: 4
Total matches: 1,336

Rule: consecutive_stop_losses → raise_account_risk_sensitivity
  Matched: 668 events
  Symbols: BTCUSDT, ETHUSDT, SOLUSDT
  Sides: LONG (60%), SHORT (40%)
  Would apply: Yes (would increase risk sensitivity by 1.5x, 4h cooldown)

Rule: consecutive_stop_losses → require_stronger_confirmation
  Matched: 668 events
  Would apply: Yes (would require confidence >= 0.80)

Rule: daily_loss_threshold → cooldown_recent_loss_symbols
  Matched: 1,020 events
  Symbols: BTCUSDT, ETHUSDT
  Would apply: Yes (would cooldown trigger-related symbols for 8h)

Rule: daily_loss_threshold → downgrade_trade_to_watch
  Matched: 1,020 events
  Would apply: Yes (would downgrade trade_plan to watch when pattern active in 24h)
```

### 4. 入口集成

- `diagnostics/__init__.py` 导出新函数
- `hourly_report.py` 可选集成（后期）

## Acceptance Criteria

- [ ] dry-run `matches > 0`
- [ ] 不创建订单、不改策略状态、不改评分
- [ ] `diagnose_state_consistency = 0`
- [ ] 138 测试 OK
- [ ] 每条 match 有解释（event_count, symbols, sides, params）

## Out of Scope

- 不执行任何策略变更
- 不修改 account_risk_guard.py
- 不创建新的订单拦截逻辑
- 不回测这些规则的效果

## Key Files

| File | Action |
|------|--------|
| `plugins/crypto_guard/skills/account_risk/feedback_rules.yaml` | 新建 |
| `plugins/crypto_guard/diagnostics/account_feedback_rules_dry_run.py` | 新建 |
| `plugins/crypto_guard/diagnostics/__init__.py` | 修改导出 |
| `plugins/crypto_guard/tests/test_smoke.py` | 新增测试 |
