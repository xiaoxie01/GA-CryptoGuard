# 04 — 机会分类现状

- **Query**: 现有 "高等级机会" 怎么算？分几档？阈值？是否检查 risk_check / trade_plan / min_confidence？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- `hourly_report.py:108-201` `render_ga_hourly_summary`：
  - line 109-112 统计 grade_counts（S/A/B/C/D）
  - line 113 `high_grade = [r for r in rows if str(r.get("signal_grade")) in {"S", "A", "B"}]` —— **仅按 grade 字符串过滤，不检查 risk_check、不检查 trade_plan、不检查 confidence**
  - line 114 `no_edge = {C, D}`
  - line 173-201 渲染高等级机会：`f"{symbol}：{grade}，{confidence*100:.0f}%；{_decision_text(decision)}...；{final_summary}"`
  - line 175-180 信号方向：先取 `trade_plan.side`、缺则按 `market_bias` 映射 LONG/SHORT
  - line 183-197 持仓冲突提示：当 sign_side 与 open orders side 不一致才显式提示冲突，否则"已持仓"
- `_decision_text` (`hourly_report.py:998-1008`) 把 decision 枚举映射为中文，但没将 risk_check 失败的决策映射到"未通过"。
- grade → 阈值：`plugins/crypto_guard/strategy/grade_config.py:10-16` `GRADE_THRESHOLDS = {S:0.80, A:0.72, B:0.65, C:0.50, D:0}`，`PAPER_ORDER_GRADES = {"S","A"}`，`MIN_CONFIDENCE_FOR_PAPER_ORDER = 0.72`。

## Why broken

- 用户反例 5：LINK 69%、DOGE 66%、AVAX 66% —— 这些都低于 `min_confidence=0.72`，但 grade=B ≥ 0.65，进入 high_grade 列表，被标记为"高等级机会"。
- 用户反例 2：BTC risk_check.ok=false、trade_plan 缺、decision=opportunity_watch，但 grade=S 仍进入 high_grade，且 `final_summary` 文本称"具备模拟盘做多条件"。
- 用户反例 4：ADA grade=A、机会文本说"风控全部满足"，实际 trade_plan=NULL、risk_check=false → 文字段与结构化字段矛盾：渲染层从未引用 risk_check 字段。
- 现状在 `render_ga_hourly_summary` 中没有读 `row["risk_check"]`、没有读 trade_plan 完整性、没有按 grade threshold 取置信一致。

## Where to fix
- `plugins/crypto_guard/notify/hourly_report.py:113` — 改 high_grade 过滤为：
  - grade ∈ {S, A} 且
  - `risk_check.ok == True` 且
  - `trade_plan` 存在（字段完整）且
  - `confidence >= MIN_CONFIDENCE_FOR_PAPER_ORDER`
  - B 级单独列 "机会监控"（不混入可执行机会）
- `hourly_report.py:108` `_decision_row` 已经把 risk_check / trade_plan 拆出来，但渲染层没用。
- `grade_config.is_paper_order_eligible(grade, confidence)` 已经存在（line 56），应在渲染处复用。

## Tests to add
- grade=B / confidence=0.69 进入"机会监控"而非"可执行机会"
- risk_check.ok=false 时不出现在 high_grade
- decision=opportunity_watch 但 grade=S 应该映射为"可监控不可执行"