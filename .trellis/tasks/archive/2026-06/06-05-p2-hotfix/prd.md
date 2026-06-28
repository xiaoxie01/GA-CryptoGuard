# P2 Hotfix: Schema Migration, Time Comparison, Test Fixes

## Goal

修复 P2 bug fixes 遗留的 5 个阻断问题，确保真实库和测试环境都稳定通过。

## Problems Identified

### 1. 真实库 schema 缺列
- **问题**: `E:\GenericAgent_crypto\data\crypto_guard\crypto_guard.sqlite3` 缺少 `pattern_type`, `affected_symbols`, `affected_sides`
- **影响**: `evaluate_feedback_rules_dry_run()` 直接报 `no such column: pattern_type`
- **根因**: 迁移未执行

### 2. 测试失败
- **问题**: `test_no_daily_loss_pause_with_one_stop_loss` 失败
- **根因**: 时间比较问题导致查询返回错误结果

### 3. account_risk_guard.py 时间比较
- **位置**: Line 202: `closed_at >= ?`
- **问题**: 裸字符串比较 ISO `T/Z` 和 `YYYY-MM-DD HH:MM:SS`
- **影响**: 当天交易、daily pause、cooldown、recovery 漏算

### 4. feedback_ttl.py 时间比较
- **位置**: Line 52: `created_at < ?`
- **问题**: 同样的时间格式比较问题
- **额外**: `str(protected_ids)` 应改为 `json.dumps(protected_ids)`

### 5. schema health check 没有闭环
- **问题**: `check_schema_health()` 只检查，没有被 dry-run / hourly report / TTL 保护使用
- **影响**: 真实库缺列时，dry-run 不返回 degraded，直接崩

## Acceptance Criteria

- [ ] 运行迁移后，真实库 schema health check 通过
- [ ] 全量测试 138 passed，0 failures
- [ ] account_risk_guard.py 所有时间比较使用 `datetime()` 包装器
- [ ] feedback_ttl.py 所有时间比较使用 `datetime()` 包装器，protected_ids 使用 `json.dumps()`
- [ ] dry-run / hourly report / TTL 入口检查 schema health，不健康时返回清晰错误
- [ ] 真实库重跑：schema health, state consistency, feedback rules dry-run 全部通过

## Implementation Plan

### Step 1: 运行迁移
```bash
python -c "from plugins.crypto_guard.storage.migrations import run_migrations; run_migrations()"
```

### Step 2: 修复 account_risk_guard.py 时间比较
- 所有 `closed_at >= ?` 改为 `datetime(closed_at) >= datetime(?)`
- 确保所有时间查询一致

### Step 3: 修复 feedback_ttl.py 时间比较
- Line 52: `created_at < ?` → `datetime(created_at) < datetime(?)`
- Line 55: `str(protected_ids)` → `json.dumps(protected_ids)`
- Line 63: 同样修复
- Line 66: `str(protected_ids)` → `json.dumps(protected_ids)`
- Line 76: `str(protected_ids)` → `json.dumps(protected_ids)`

### Step 4: 添加 schema health 保护
在 dry-run / hourly report / TTL 入口添加 schema health 检查：
```python
from plugins.crypto_guard.storage.migrations import check_schema_health

def evaluate_feedback_rules_dry_run(repo, ...):
    schema = check_schema_health()
    if not schema["ok"]:
        return {"ok": False, "error": "schema_unhealthy", "missing": schema["missing_columns"]}
    # ... 原逻辑
```

### Step 5: 修复测试
- 确保 `_insert_closed_trade` 使用一致的时间格式
- 或者修复 account_risk_guard.py 使测试通过

### Step 6: 验证
- 运行全量测试
- 真实库重跑诊断

## Definition of Done

- 所有 138 测试通过
- 真实库 schema health OK
- 真实库 state consistency 0 issues
- 真实库 feedback rules dry-run 正常运行
- 无时间比较相关的 bug
