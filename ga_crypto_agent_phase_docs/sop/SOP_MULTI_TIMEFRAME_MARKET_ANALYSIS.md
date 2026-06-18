# SOP 多周期市场分析

## 输入

- symbol
- analysis_time_utc
- mode: scheduled / ad_hoc / opportunity_watch / paper_review
- timeframes: 1D, 4H, 1H, 15m, 5m

## 步骤

1. 数据完整性检查。
2. 高周期背景分析。
3. 主周期结构分析。
4. 低周期触发分析。
5. 反向证据检查。
6. 趋势阶段判断。
7. 策略匹配。
8. 交易计划或机会监控生成。
9. 输出标准 JSON。

## 输出

必须符合 `schemas/ga_decision.schema.json`。
