# Hourly Report Market Accuracy Fix

## Context

小时报在整点后 1 分钟生成，但当前一轮 scheduled analysis 尚未完成，导致使用过期决策。同时存在评级虚高、风险状态与文字矛盾、行情术语错误等问题。修复后必须确保推送内容与 SQLite 持久化决策、Binance USDⓈ-M Futures 公共数据、实际执行门禁完全一致。

禁止真实下单。数据库操作前必须备份。

## Confirmed Issues

### 时序问题
- 整点后 1 分钟生成报告，但当前 scheduled analysis 批次未完成
- 2026-06-28 07:01Z 报告使用 06:44:59Z 决策；06:59:59Z 决策到 07:03–07:08Z 才落库

### 评级 / 文字矛盾
- BTC: 报告写 S/82% 且"具备模拟盘做多条件"，但 risk_check.ok=false、trade_plan 缺失、decision=opportunity_watch
- LTC: S/88% 15 分钟后降为 D/49%，高周期仍为 range，评级波动异常
- ADA: 被描述为 A 级可限价做空且"风控全部满足"，实际无 trade_plan、risk_check=false，后续方向频繁翻转
- LINK 69%、DOGE 66%、AVAX 66% 低于 min_confidence=0.72，却被列入"高等级机会"

### 行情事实
- drawdown=-0.50% 符号错误，应显示 0.50%
- in_memory_fallback 措辞误导（只代表统计来源，却容易被理解为整份分析来源）
- liquidity sweep 术语可能反向：向下扫低点后回收应为 sell-side liquidity sweep；向上扫高点后回落才是 buy-side liquidity sweep
- 报表没有展示每个决策的 analysis_time、数据年龄、risk_check、trade_plan、可执行状态

## Requirements

### P0：报告时序一致性
- 小时报不得在当前 scheduled analysis 批次未完成时生成
- 建立明确的 analysis batch/run 身份和完成状态；只有全部启用品种完成，或达到配置化超时后，才能生成报告
- 禁止仅靠 sleep 猜测完成时间
- 超时时允许生成，但必须标明 incomplete，并列出缺失、失败和仍运行的 symbol
- 每个 symbol 必须选择报告 cutoff 前"已完整持久化"的最新决策，禁止混用不同批次而不标注
- 报告显示 analysis_time、created_at、age_minutes、batch_id
- 超过一个分析周期的数据标记 stale，不得进入可执行机会

### P0：机会分类必须服从执行门禁
将原"高等级机会"拆成三类：
1. **可执行机会**：grade 属 S/A/B；confidence ≥ 配置 min_confidence；trade_plan 完整；risk_check.ok=true；decision=create_paper_order 或明确允许执行的等价状态；行情数据和决策均未过期
2. **观察候选**：评级较高，但缺 trade_plan、风险检查失败、信心不足、等待触发或数据过期
3. **无优势品种**：C/D、no_edge、monitor_only 等

任何 risk_check=false 或缺少 trade_plan 的项目，禁止出现"具备模拟盘条件""风控指标全部满足""可创建订单"等措辞。

### P0：确定性文字覆盖
- 报表中 grade、confidence、decision、risk_check、trade_plan、方向和关键价格必须来自结构化字段
- LLM summary 只能作为补充分析，不能覆盖或伪造结构化执行状态
- 若 summary 与结构化字段冲突，替换冲突措辞并追加"仅观察/未通过执行门禁"
- 增加确定性 consistency validator；发现矛盾时记录诊断并降级展示

### P1：行情与评级稳定性
- 对 S/A 评级增加高周期确认、最低样本数、量能/动能确认和矛盾证据上限
- 4H=range/transition、1H/15M 冲突、volume 未确认时，默认不得给 S；除非 independent_trend 门禁有充分结构化证据
- 增加评级迟滞/稳定性：
  - 短时间内禁止 S→D→S 无依据跳变
  - 展示 previous_grade、grade_delta、方向变化原因
  - 方向翻转必须有已收盘 K 线突破及明确 evidence
- 不允许通过平滑掩盖真实风险变化；紧急降级仍可立即生效，但必须记录原因

### P1：Binance 行情事实校验
- 只使用 Binance USDⓈ-M Futures 公共数据，不使用 Yahoo、CoinGecko 等
- 报告关键价位必须附 timeframe、K 线 close_time、price_source
- 所有分析只使用已收盘 K 线
- 修复 liquidity sweep 命名和方向语义，并增加单元测试
- 若订单流、CVD、主动买卖比数据不可用或 degraded，必须降低证据权重，不能输出"订单流确认"

### P1：报表准确性
- drawdown 对外统一显示非负回撤幅度，并保留内部计算方向语义
- 分别显示：decision_source、distribution_source、market_data_source、是否 fallback
- "市场情绪 aligned"只能描述真正执行过 regime gate 的决策，不能推广到未检查品种
- 修复章节编号和术语

### P2：诊断
新增 `state_consistency/report_diagnostics.py`，检测：
- `hourly_report_incomplete_batch`
- `hourly_report_stale_decision`
- `executable_opportunity_without_trade_plan`
- `executable_opportunity_risk_rejected`
- `opportunity_below_confidence_threshold`
- `summary_execution_state_conflict`
- `excessive_grade_flip`
- `direction_flip_without_closed_candle_confirmation`
- `invalid_liquidity_sweep_semantics`
- `negative_drawdown_display`

## Test Plan

1. 报告等待当前分析批次完成
2. 超时报告正确列出 incomplete symbols
3. 旧批次决策不会冒充当前批次
4. risk_check=false 的 S 级只能进入观察候选
5. 缺 trade_plan 不得显示"可执行"
6. B 级低于 min_confidence 不进入可执行机会
7. 决策文字与结构化字段冲突时确定性覆盖
8. BTC transition/range + volume 未确认不能升级为可执行 S
9. LTC S→D 快速跳变被记录并按规则处理
10. ADA 无收盘突破时不得反复翻转方向
11. liquidity sweep 多空语义测试
12. drawdown=-0.5 内部值显示为 0.50%
13. 每条机会包含 analysis_time、age、batch_id、门禁状态
14. 所有测试禁止真实 Binance 下单；外部行情调用必须 mock
15. 全量测试 0 failed、0 skipped

## Production Verification

- 先备份数据库
- 只读核对 2026-06-28 07:01Z 报告对应的决策链
- 用 Binance 公共 K 线验证 BTC 59772.9、LTC 42.13–42.51、ADA 0.1437–0.1448
- 重新生成 dry-run 小时报，确认 BTC/LTC/ADA 被正确分类
- `check_schema_health()` 必须 OK
- `diagnose_state_consistency` 必须 0 issues
- 不推送测试消息，不创建模拟订单，不修改历史交易结果

## Execution Approach

- 先审查现有 scheduler、analysis batch、hourly_report、repository 和 schema，找出根因
- 不做临时 sleep 或字符串补丁
- 使用结构化字段和数据库状态实现
- 修复所有发现的问题，包括 P2 和建议项
- 完成后给出：根因、修改文件、迁移、测试、生产 dry-run 对比、剩余风险
- 未经确认不要提交 commit

## Out of Scope

- 真实下单
- 修改历史交易结果
- 推送测试消息到生产频道
- 修改 Yahoo/CoinGecko 集成（直接弃用即可）