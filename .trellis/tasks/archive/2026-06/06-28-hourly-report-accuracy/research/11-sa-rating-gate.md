# 11 — S/A 评级门禁

- **Query**: S/A 评级现在怎么给？是否有高周期确认 / 最低样本数 / volume / 动能 / 矛盾证据上限？independent_trend 是什么？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- grade 单纯按 score 分档（`grade_config.py:33`），score 在 `strategy_scorer.score_snapshot:40+` 按模块加权汇总：
  - 模块：price_action、momentum、trend_stage、smc、order_flow、chanlun、market_regime
  - 关键加分：smc liquidity sweep +0.10 (`strategy_scorer.py:119-120`)、fvg/order_block、trend alignment (`:135+`)、volume / momentum quality
  - 扣分：counter_evidence、range mode penalty (`:152+`)
- **没有**显式的"高周期确认"硬门禁；高周期方向分歧写入 `counter_evidence`（`counter_evidence_engine.py`）但只是减分。
- **没有**最低样本数检查（注：那是 shadow_testing 性能门禁逻辑）。
- **没有**矛盾证据上限：counter_evidence 只参与减分，无"超过 N 项不得给 S"的规则。
- `performance_gate` (`ga_master/performance_gate.py`) 在 S/A 时根据历史表现降级（cooldown/degrad），仅在 controller 持久化前调用 (`controller.py:424-449`)；这是事后降级，不是评级门禁。
- **independent_trend**：`market_regime_engine._regime_alignment:468-526` 的一个 `regime_alignment` 取值。当 trade direction 与 market regime 相反 (counter_regime) 但满足独立趋势条件 (min_relative_strength_pct 等，`config/trading_mode.yaml:138`) 时改判 `independent_trend`，不降信心。`market_regime_gate_json` 落库到 `ga_decisions`。

## Why broken

- 反例 2：BTC grade=S 82%，但 risk_check.ok=false、trade_plan 缺、decision=opportunity_watch —— 评级时没有联动"可执行性"门禁。
- 反例 4：ADA grade=A 但 trade_plan=NULL —— 只要 score ≥ 0.72 就给 A，"no trade plan" 不是降级条件。
- 反例 5：LINK 69% / DOGE 66% / AVAX 66% 给 B 级 —— B 不该被列为"高等级机会"（见 04），且 B 不该送 paper order，但渲染层混入。

## Where to fix
- `plugins/crypto_guard/strategy/grade_config.py` 新增 `clamp_grade(grade, *, has_trade_plan, risk_ok, min_confidence)`：S/A 评级要求 has_trade_plan=True、risk_ok=True、confidence≥0.72；否则封顶 B。
- `plugins/crypto_guard/strategy/strategy_scorer.py:175` 调用 clamp_grade；`ga_judge.run_ga_sop_decision:223` `signal_grade = ...`。
- `plugins/crypto_guard/reasoning/llm_agent_judge.py:_normalize_llm_decision` 末尾同样调用 clamp。
- 增加高周期确认硬门禁：1D/4H 方向需要与 entry side 一致或为 neutral，否则封顶 B。

## Tests to add
- has_trade_plan=False score=0.85 → grade 封顶 B
- risk_check.ok=False → grade 封顶 B
- 4H 方向与 side 冲突且未触发 independent_trend → 封顶 B
- counter_evidence ≥ 3 条 → 封顶 B