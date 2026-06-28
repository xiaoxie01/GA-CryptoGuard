# GA CryptoGuard 置信度校准与门禁优化

## Goal

修复 GA CryptoGuard 系统中置信度校准失真问题，降低 wrong_direction 和 entry_too_late 亏损，提高多空交易质量。

## What I already know

**数据现状**：
- 总计 51 笔，47 笔已平仓
- 净结果：-326.04U / -5.91R
- LONG 24 笔：+1.52R（胜 10，负 12）
- SHORT 27 笔：-7.43R（胜 6，负 15）

**主要亏损原因**：
- `wrong_direction`：LONG -10R，SHORT -12R
- `entry_too_late`：LONG -2R，SHORT -3R

**反常现象**：
- S 级信号合计 -6.94R，A 级反而 +1.03R
- early + S 级 SHORT：15 笔，-5.999R
- early + S 级 LONG：11 笔，-0.945R

**根因分析**（用户识别）：
1. 方向判断没有上下文胜率校准
2. S 级/高 confidence 失真
3. early trend 阶段太激进
4. counter evidence 没有硬否决权
5. 入场质量不足

## Assumptions (temporary)

- 用户提供数据来自 paper trading（模拟盘）
- 优化目标是降低亏损，不是追求高收益
- 优化应渐进式实施，不破坏现有流程

## Open Questions

- 优化优先级排序？✓ 已确定：先做 context_performance_gate
- 是否需要回测验证每项优化？✓ 用户自行验证
- 单个优化 vs 批量优化？✓ 两个门禁一起做

## Requirements (evolving)

**用户提出的 7 项优化**：
1. `context_performance_gate` - 历史表现查询门禁
2. `symbol_side_cooldown` - 组合冷却机制
3. 重写 S 级定义 - 要求历史正收益
4. early 阶段确认条件 - 必须满足多项确认
5. neutral/mixed 禁止直接开单
6. entry_too_late 预测门禁
7. drawdown recovery mode - 账户回撤降级

**Skill 优化**：
- `trend_stage`: early 拆分为 unconfirmed/confirmed
- `price_action`: range compression 禁止直接开单
- `momentum`: RSI 极端降级
- `order_flow`: degraded 不能作支持证据
- `chanlun`: 反向候选硬扣分
- `SMC`: liquidity sweep 需确认

**context_performance_gate 设计**：
- 查询维度：symbol + side + trend_stage
- 查询路径：ga_decisions → signals → paper_orders → paper_trades
- 降级逻辑：样本 >= 3 且 avg_r < 0 → S→A/B，A→B/C
- 开单降级：create_paper_order → opportunity_watch
- 集成位置：新建独立模块 `context_performance_gate.py`
- 触发时机：事件驱动更新（每笔交易平仓后更新缓存）

**symbol_side_cooldown 设计**：
- 冷却对象：symbol + side 组合
- 冷却规则：
  - 最近 3 笔亏 2 笔 → 24 小时只 watch
  - 最近 5 笔 avg_r < -0.2 → 降低该组合 confidence 0.10
- 集成位置：合并到 context_performance_gate 模块
- 配置项：冷却时长、亏损阈值、confidence 降级幅度

**集成位置**：controller.py 决策流程

**实现顺序**：
1. 先实现 symbol_side_cooldown
2. 再实现 context_performance_gate
3. 集成到 controller.py

**配置位置**：trading_mode.yaml 添加 performance_gate 配置节

**测试策略**：
- 单元测试：验证降级逻辑、冷却规则
- 集成测试：验证与 controller.py 的集成

**验收标准**：
- 功能正确性：降级逻辑、冷却规则按预期工作
- 模拟数据测试：使用模拟数据验证门禁效果
- 现有测试通过：不破坏现有功能

**实施计划**：
- PR1: symbol_side_cooldown 模块实现
- PR2: context_performance_gate 模块实现 + controller.py 集成
- 暂不使用 git，本地实现

**MVP 范围**：
- 实现 context_performance_gate 模块
- 实现 symbol_side_cooldown 模块
- 新增 repository 方法查询历史表现
- 集成到 risk_gate.py 或 controller.py
- 配置项：样本阈值、avg_r 阈值、降级规则、冷却规则
- 单元测试验证降级逻辑
- 更新 code-spec 文档

## Acceptance Criteria (evolving)

**本次 MVP 范围**：
- [x] symbol_side_cooldown 模块实现并通过测试
- [x] context_performance_gate 模块实现并通过测试
- [x] 新增 repository 方法查询历史表现（复用现有 repo.conn 查询）
- [x] 集成到 controller.py 决策流程
- [x] 配置项添加到 trading_mode.yaml
- [x] 单元测试 + 集成测试通过（4个性能门测试全部通过）
- [x] 现有测试全部通过（35个测试通过，1个因临时文件权限问题失败，与本次改动无关）

**后续优化（不在本次 MVP）**：
- [ ] S 级定义重写
- [ ] early 阶段确认条件
- [ ] neutral/mixed 禁止直接开单
- [ ] entry_too_late 预测门禁
- [ ] drawdown recovery mode
- [ ] 各 Skill 优化

## Definition of Done

- Tests added/updated (unit/integration where appropriate)
- Lint / typecheck / CI green
- Docs/notes updated if behavior changes
- 现有测试全部通过
- 不破坏现有自进化流程

## Out of Scope (explicit)

- 回测验证每项优化（用户自行验证）
- 实盘接口修改（仅 paper trading）
- 大规模架构重构

## Technical Notes

**当前系统状态**：

**评分逻辑** (`strategy_scorer.py`)：
- 基础分：0.55
- S >= 0.80，A >= 0.72，B >= 0.65，C >= 0.50，D < 0.50
- Paper order 资格：grade in {S, A} 且 confidence >= 0.72

**趋势阶段** (`trend_stage_engine.py`)：
- early：momentum healthy/building 或刚触发 BOS
- middle：结构 bullish/bearish，动能不极端
- late：extended/overheated/exhausted
- transition：默认回退
- range：市场结构为 range
- early 阶段获得 +0.10 分加成（与 middle 相同）

**反向证据** (`counter_evidence_engine.py`)：
- 收集 bullish_evidence、bearish_evidence、neutral_or_risk_evidence
- contradiction_level：high（2+ 项双向冲突）/ medium（1vs1）/ low
- high contradiction 扣 -0.15 分

**市场偏向** (`strategy_scorer.py`)：
- bullish：看涨结构 + 非看跌动能
- bearish：看跌结构 + 非看涨动能
- mixed/neutral：其他

**风险门禁** (`risk_gate.py` → `risk_engine.py`)：
- 交易计划完整性、RR >= 2.0、confidence >= 0.72
- HTF 结构支撑、结构-动能对齐
- 极端行情阻断、最小止损/止盈距离

**相关文件**：
- `plugins/crypto_guard/strategy/strategy_scorer.py`
- `plugins/crypto_guard/analysis/trend_stage_engine.py`
- `plugins/crypto_guard/analysis/counter_evidence_engine.py`
- `plugins/crypto_guard/ga_master/controller.py`
- `plugins/crypto_guard/ga_master/risk_gate.py`
- `plugins/crypto_guard/risk/risk_engine.py`
- `plugins/crypto_guard/config/trading_mode.yaml`

**数据结构**：
- `ga_decisions` 表：`signal_grade`, `trend_stage`, `market_bias`, `symbol`, `analysis_time`
- `signals` 表：`signal_grade`, `trend_stage`, `direction`, `ga_decision_id`
- `paper_orders` 表：`signal_id`
- `paper_trades` 表：`order_id`, `symbol`, `side`, `pnl_r`, `closed_at`
- JOIN 链：ga_decisions → signals → paper_orders → paper_trades

**现有聚合方法**：
- `strategy_memory` 表：按 `condition_hash` 聚合 win/loss/avg_rr
- 无按 signal_grade/trend_stage 聚合的预建方法
