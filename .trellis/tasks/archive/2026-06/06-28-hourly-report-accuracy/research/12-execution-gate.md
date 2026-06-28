# 12 — 执行门禁 / min_confidence

- **Query**: min_confidence 在哪里配置？trade_plan 完整性检查是否在报告生成处？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- 配置点：`plugins/crypto_guard/config/trading_mode.yaml:24-25` `min_confidence_for_paper_order: 0.72` / `min_confidence: 0.72`；`grade_config.MIN_CONFIDENCE_FOR_PAPER_ORDER = 0.72` (`grade_config.py:29`)；可被 runtime 覆盖 (`tools/ga_crypto_tools.py:486`)。
- 实际门禁作用于决策生成阶段：
  - `risk/risk_engine.validate_trade_plan:30-64`：confidence < min_conf → reasons.append(`置信度低于 X`)，risk_check.ok=false，`apply_risk_to_decision` 把 decision 改为 monitor_only、has_trade_plan=False
  - `paper/paper_broker.py:683` / `paper/position_conflict_revalidator.py:44-46` (`S=0.85, A=0.78, B=0.72`) / `paper_position_updater.py:439` (`0.85`)
  - `run_ga_workers.py:71` 自动开 paper order 要求 `grade in {S,A} and has_plan and risk_ok`
- trade_plan 完整性检查（`risk_engine.validate_trade_plan:43-49`）：要求 `side/entry_type/stop_loss/take_profits` 全部非空 + `entry_price/trigger_price` 至少一个，`risk_engine.py:43-49`。
- **报表生成处（`hourly_report.render_ga_hourly_summary:108-201`）完全不复用上述门禁**：direct take grade from row["signal_grade"]，不做 risk_check / trade_plan 完整性 / min_confidence 二次校验。

## Why broken

- 反例 5：LINK 69% / DOGE 66% / AVAX 66% < 0.72 在决策时 risk_engine 已让 risk_check.ok=false、has_trade_plan=False、decision=monitor_only。但 grade=B 仍进入 hourly_report 的 "high_grade" 列表，且 confidence 显示"69%"让用户觉得是"高等级机会"。
- 决策生成阶段门禁已生效，但渲染阶段缺一致性门禁；trade_plan 完整性检查没有以任何形式出现在报表上，导致用户看不到为什么某些机会其实没有 trade_plan。

## Where to fix
- `plugins/crypto_guard/notify/hourly_report.py:113` — 复用 `grade_config.is_paper_order_eligible(grade, confidence)` + `risk_check.ok` + `bool(trade_plan)` 决定是否进 high_grade "可执行"分类。
- 增加 trade_plan 完整性 short label："计划完整 / 仅 side / 无 plan"，写入 `_decision_row`。
- 单独的 `_opportunity_classifier(row)` 函数集中产出 `{tier: "high_action"|"watch"|", "blockers": [...]}`，渲染层引用；专门测试每个 blocker。

## Tests to add
- confidence < 0.72 → 不在 high_action 分类，标注"未达 min_confidence"
- grade=A、risk_check.ok=False、trade_plan 缺 → 不渲染为"可执行"
- 渲染输出包含 blockers 列表与 risk_check.ok 状态