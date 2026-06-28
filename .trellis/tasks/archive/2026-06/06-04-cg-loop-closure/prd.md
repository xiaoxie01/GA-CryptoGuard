# CryptoGuard 闭环修复：账户风控 + pending 复核 + shadow 真实表现 + 自进化反馈

## Goal

修复 CryptoGuard 系统中的多个闭环缺陷：账户风控缺失、pending 订单复核不完整、shadow 测试依赖伪 r 值、自进化反馈过于泛化。目标是从 -2.76% 亏损中止血并建立可持续的正期望循环。

## What I already know

### 当前系统状态
- 账户已亏损约 -2.76%
- pending 订单生命周期只靠 TTL + 方向冲突，needs_recheck 只标记不处理
- shadow 测试用 pseudo_r_from_score，candidate 可能无真实 pnl_r 却显示高胜率
- 每日复盘写入 skill_feedback_memory 过于泛化
- LONG 方向明显拖累账户表现
- strategy_versions/patches/triggers 可能存在状态不一致

### 架构基础
- SQLite 热数据、Redis 缓存/队列、Parquet 冷存储、DuckDB 分析
- GA 决策流: Feishu/Scheduler → GAMasterController → ContextBuilder → SOP → RiskGate → DecisionPersistence
- 自进化: trigger → candidate → shadow_testing → review_required → active
- 三表联动: evolution_triggers, strategy_patches, strategy_versions
- 飞书卡片通知: alert_outbox + interactive card

### 关键文件
- `paper/pending_order_manager.py` — TTL + 冲突取消（刚修复）
- `strategy/shadow_testing.py` — 影子测试
- `review/evolution_triggers.py` — 自进化触发器
- `ga_master/risk_gate.py` — 风控门禁
- `ga_master/controller.py` — GA 主控
- `ga_master/decision_schema.py` — 决策 schema
- `storage/repository.py` — 数据层
- `storage/schema.sql` — DDL
- `notify/hourly_report.py` — 小时报告
- `run_ga_workers.py` — worker 调度

## Requirements (evolving)

### P0: 账户风险止血
- [ ] account_risk_guard 模块
- [ ] drawdown <= -2.5% 进入 risk_off
- [ ] risk_off 下降低单笔风险、禁止历史差 symbol+side、冷却亏损组合
- [ ] 恢复条件: 最近 10 笔 avg_r > 0 且 loss_count <= 4
- [ ] risk_off 状态注入 GA decision + 飞书可见

### P0: pending_order_revalidator
- [ ] 独立模块，每小时复核 pending/needs_recheck
- [ ] 多维复核: GA bias/grade/stage、价格偏离、SL 触及、ATR 扩大、BTC context
- [ ] 输出动作: keep/cancel/convert_to_watch/adjust/needs_manual_review
- [ ] 保守版: 冲突取消、late stage 转 watch、偏离过大转 watch、needs_recheck 超时转 watch

### P0: shadow 测试真实表现修复
- [ ] pnl_r 全空时不允许判高胜率通过
- [ ] verdict 返回 data_quality='pseudo_only'
- [ ] 复用 historical_replay 补充 simulated pnl_r
- [ ] promotion 必须 data_source in ['real_pnl', 'historical_replay']
- [ ] 样本 >=20 但 pnl_r 缺失触发 shadow_quality_alert

### P1: shadow failure reflection
- [ ] 触发条件: avg_r < 0, win_rate < 45%, drawdown 恶化 > 20%, pseudo_only + 样本 >=20
- [ ] 自动生成失败复盘报告
- [ ] 写入 skill_feedback_memory（结构化）
- [ ] 标记旧 candidate rejected/needs_revision
- [ ] 基于失败原因生成新 candidate patch（限频）

### P1: LONG 门禁重构
- [ ] long_entry_quality_gate
- [ ] HTF bias bullish/neutral_bullish, trend_stage != late, momentum != exhausted
- [ ] range/chop 禁止趋势型 LONG
- [ ] BTC context 非 risk_off
- [ ] symbol+side 历史 avg_r >= 0
- [ ] BTCUSDT/LTCUSDT/ETHUSDT LONG 默认 watch_only 直到冷却解除

### P1: 每日复盘升级
- [ ] 结构化 skill updates: skill_name, failure_pattern, affected_symbols/sides/stages
- [ ] suggested_rule_change, confidence_delta, cooldown_rule, requires_shadow_testing
- [ ] LLM prompt 注入结构化历史记忆

### P1: 状态一致性
- [ ] strategy_versions/patches/triggers 一致性检查
- [ ] 软标记 stale/rejected/duplicate/needs_revision
- [ ] 诊断工具输出 active/shadow/stuck/patch 状态

### P2: 报告与通知
- [ ] 小时报告加入 risk state + drawdown + pending/shadow 状态
- [ ] 自进化通知加入 data_source 信息
- [ ] pending 通知加入完整上下文

## Acceptance Criteria (evolving)

- [ ] 全部新增测试通过
- [ ] -2.76% drawdown 下系统进入 risk_off
- [ ] risk_off 下新订单不能按原风险开仓
- [ ] shadow candidate 不能仅凭 pseudo_r 进入可升级状态
- [ ] needs_recheck 不允许长期悬挂
- [ ] 复盘结果进入 skill memory 且可被 GA 上下文引用
- [ ] 不允许自动实盘交易

## Definition of Done

- 测试全部通过（新增 + 现有除 Windows 文件锁）
- 配置化风险参数（trading_mode.yaml）
- schema.sql 同步
- conventions.md 更新
- 飞书通知可见

## Out of Scope

- 自动实盘交易
- 真实交易所 API 调用
- 完整 LLM prompt 工程（P1 只做结构化注入）

## Research Findings (3 parallel codebase explorations)

### 1. Shadow Testing — pseudo_r 问题根源

**`_stats()` in shadow_testing.py (line 618-670)**:
- 真实路径: 扫描 `pnl_r` 列，计算 avg_r/win_rate/drawdown → `data_source: "real_pnl"`
- 伪路径: `(score - 0.5) * 2` 映射 R，win_rate 用 >0.1 阈值 → `data_source: "pseudo_r_from_score"`
- **关键缺陷**: `run_shadow_test()` 的 verdict 逻辑（line 93-101）不检查 `data_source`。candidate 可以仅凭伪 R 数据被判定为 "candidate_can_be_promoted_with_manual_confirmation"
- `data_source` 字段被返回但从未被 verdict runner 使用

**两个不一致的 pseudo_r 公式**:
| 位置 | 公式 | 用途 |
|------|------|------|
| `shadow_testing.py::_stats()` | `(score - 0.5) * 2` | verdict 决策 |
| `historical_replay.py::_pseudo_r()` | `(confidence - 0.5) + grade_bonus + decision_bonus` | 回放统计 |

**`_performance_stats()` in historical_replay.py**: 混合 real + pseudo R 到 `all_rs`，污染 avg_r 和 drawdown。Sharpe 只用 real_rs（正确）。

**`record_shadow_evaluation()`**: `pnl_r` 参数可选，但 shadow candidate 从未被填充。只有 active 版本的实盘成交才有真实 pnl_r。

### 2. Paper Account — 风控完全缺失

**RiskGate (risk_gate.py)**: 14 行薄包装，只调 `validate_trade_plan()`。无 repo 依赖，无法访问 paper_accounts。

**风险引擎 (risk_engine.py)**: 纯交易级检查 (RR, confidence, HTF support, 结构动量对齐, 极端市场, SL/TP 距离, ATR buffer)。无账户级检查。

**Controller (controller.py)**:
- `risk_check` → 存入 `ga_decisions.risk_check_json`（一等字段）
- `performance_gate` → 只存在 `raw_decision_json` blob 中（无独立列）
- PerformanceGate 是 symbol+side 级别，无跨组合检查

**Paper broker**: `create_paper_order_from_signal()` 和 `create_paper_order_from_ga_decision()` 只跑 `validate_trade_plan()`，不查 paper_accounts 状态。

**Paper accounts 表**: `status` 列存在但从未被读写。`max_drawdown` 追踪运行最小值但只触发 alert job，不阻止开仓。

**8 个具体缺口**:
1. RiskGate 无 repo 依赖
2. 无 drawdown→halt 阈值
3. paper_accounts.status 未使用
4. 10% drawdown 只触发改进化
5. 无账户级上下文在 RiskGate
6. PerformanceGate 只看 symbol+side
7. 无 risk_off 配置项
8. Paper broker 完全绕过账户状态

### 3. Daily Review — 反馈过于泛化

**三个写入 skill_feedback_memory 的入口**:
1. **Daily Review** (`_write_skill_memory_updates`): 5 个 skill 写完全相同的 finding 和 adjustment
2. **Skill 执行** (`_maybe_write_skill_feedback`): 按 skill 差异化，但只在异常时触发
3. **Evolution Triggers**: 5 个 skill 写相同的 trigger reason

**feedback_rules.yaml**: 声明式 `when → action` 映射，存在于 5 个 skill 目录中，但**从未被程序化执行**。只是元数据。

**Context Builder**: `skill_feedback_memory` 被注入 GA 上下文（最多 50 条），LLM 被要求调整 confidence ±0.05~0.15。但无结构化 skill 参数更新路径。

**缺少**:
- 反馈规则引擎
- 按 skill 差异化反馈
- 结构化 skill 参数更新
- 按市场制度聚合反馈
- 反馈条目状态生命周期管理

## Decision (ADR-lite)

### 问题
当前系统存在多个闭环断裂：账户可以无视亏损继续开仓、shadow 候选可以凭伪数据通过审核、pending 订单复核维度不足、复盘反馈无法转化为具体修正。

### 决策
分 3 个增量 PR 实现，每个 PR 可独立上线：

**PR1 (P0 止血)**: account_risk_guard + shadow pseudo_r 堵漏 + pending revalidator 保守版
**PR2 (P1 加固)**: shadow failure reflection + LONG gate + 每日复盘结构化
**PR3 (P2 完善)**: 报告增强 + 状态一致性诊断

### 取舍
- account_risk_guard 作为独立模块注入 RiskGate，不重写 RiskGate 架构
- pseudo_r 修复用 data_quality 标记 + verdict 拦截，不删除伪 R 逻辑（仍可用于观察）
- pending revalidator 保守版只做 4 条规则，不做完整 SOP 回放
- 每日复盘结构化用 deterministic 分类 + LLM 辅助，不做规则引擎

## Technical Notes

### 现有代码关键位置
- `paper_accounts.status` 列存在但未使用 → 直接利用
- `execution_quality.py:equity_snapshot()` 已有 `drawdown_alert` 标记 → 扩展
- `_stats()` 返回 `data_source` 但 verdict 未检查 → 加断言
- `_build_memory_section()` 已注入 GA 上下文 → 扩展结构化
- `feedback_rules.yaml` 已存在但未执行 → 不做规则引擎，改为结构化写入
- `performance_gate` 只存在 raw blob → 提升为一等字段

### 配置扩展 (trading_mode.yaml)
```yaml
account_risk:
  drawdown_risk_off_threshold: -2.5   # %
  risk_off_risk_percent: 0.25         # 降低后的单笔风险
  recovery_min_avg_r: 0.0
  recovery_max_loss_count: 4
  recovery_lookback: 10               # 最近 N 笔
  cooldown_symbols:
    BTCUSDT_LONG: 48                   # 小时
    LTCUSDT_LONG: 48
    ETHUSDT_LONG: 48
    BNBUSDT_SHORT: 48
```

### 测试策略
- account_risk_guard: mock paper_accounts 状态，验证 risk_off 进出
- shadow pseudo_r: 构造全空 pnl_r 的 strategy_evaluations，验证 verdict 被拦截
- pending revalidator: 插入不同状态的 pending 订单，验证各规则输出
- LONG gate: 构造 bearish context + LONG order，验证被转 watch_only
