# 数据模型关系

## 1. 核心关系

```text
ga_decisions 1 -> 1 analysis_states
ga_decisions 1 -> N skill_execution_logs via skill_result_refs_json
ga_decisions 1 -> 0/1 paper_order
ga_decisions 1 -> 0/1 opportunity_watch after user confirmation
ga_decisions N -> 1 hourly_report
daily_review_reports N -> skill_feedback_memory
trade_reviews N -> skill_feedback_memory
```

## 2. paper_order 约束

如果已有 `paper_orders` 表，Codex 必须添加：

```text
ga_decision_id INTEGER NOT NULL for new paper orders
source = 'ga_decision'
risk_check_passed = 1
```

新模拟单必须校验：

```text
trade_plan_json 存在
RR >= 2
confidence >= 0.72
高周期方向不冲突
非极端行情
```

## 3. opportunity_watch 约束

如果已有 `opportunity_watches` 表，Codex 必须添加：

```text
ga_decision_id INTEGER
created_by_user_action INTEGER DEFAULT 0
source_button_action TEXT
```

必须满足：

```text
signal_grade in ('B', 'A', 'S')
用户点击按钮
D/C 不能创建 opportunity_watch，除非用户明确发出“强制关注该币”，也必须记录 override_reason。
```

## 4. analysis_state 作为下次分析基石

每次分析前必须读取：

```sql
SELECT * FROM analysis_states WHERE symbol = ? ORDER BY analysis_time DESC LIMIT 1;
```

并传入 GA ContextBuilder。
