# 05 — LLM summary vs 结构化字段

- **Query**: "具备模拟盘做多条件""风控全部满足" 来自哪里？LLM 还是模板？有无 deterministic validator？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- 文本来源：`ga_decisions.final_summary`，由 `llm_agent_judge._normalize_llm_decision` (`llm_agent_judge.py:295-350`) 写入。LLM 调用走 `run_agent_sop_decision` (line 21-48)：解析 LLM `candidate`，`decision.update(candidate)`，所以 `final_summary / summary` 是 LLM 自由文本。
- deterministic SOP 路径 `reasoning/ga_judge.py:_summary` (line 249-256) 用固定模板："当前为 S 级模拟盘候选…" —— 不含"风控全部满足"措辞。
- LLM 路径在 `_normalize_llm_decision` 中已经做：
  - grade 比确定性评分低超 1 级会被 stabilization (line 328-337)
  - A/S 但无 trade_plan 时自动补建 (line 315-326)
  - `has_trade_plan` 与 `trade_plan` 一致性 (line 339-344)
  - `opportunity_watch.direction` 规范化
  - **没有**校验 `final_summary` 与 `risk_check.ok` / `has_trade_plan` / `decision` 是否一致
- `apply_risk_to_decision` (`risk_engine.py:11-27`) 在 risk_check 失败时把 `decision=monitor_only`、`has_trade_plan=False`，但**不修改 final_summary**（LLM 已写"风控全部满足"仍保留）。
- controller `controller.py:388-395` 重复一次风控失败时把 `decision=monitor_only`，仍不重写 summary。
- 渲染端 `hourly_report.py:200` 直接输出 `final_summary` 当作结论。

## Why broken

- 用户反例 2 / 4：BTC 与 ADA 报表中文字"具备模拟盘做多条件""风控全部满足"由 LLM 自由生成，未与 risk_check.ok / trade_plan 字段对齐。
- 当 risk_check 把 decision 降级为 monitor_only 后，final_summary 仍是 LLM 旧措辞，产生矛盾。
- 没有 deterministic validator 在持久化/渲染前重写或屏蔽矛盾文案。

## Where to fix
- `plugins/crypto_guard/reasoning/llm_agent_judge.py:_normalize_llm_decision` 末尾 / `reasoning/ga_judge.py` 新增 `_consistency_rewrite_summary(decision, risk_check)`：当 has_trade_plan=false 或 risk_check.ok=false 时，覆盖 final_summary 为 deterministic 模板。
- `plugins/crypto_guard/ga_master/controller.py:470` 持久化前做断言：若 `risk_check.ok=false` 则 `final_summary` 必须不含"风控通过/全部满足/可模拟盘"字符串。
- `plugins/crypto_guard/notify/hourly_report.py:200` 渲染时若 risk_check.ok=false，标注"风控未通过"覆盖 LLM 文本。

## Tests to add
- LLM 输出 "具备模拟盘做多条件" 但 risk_check.ok=false → final_summary 被覆盖
- LLM 输出 grade=S 但 trade_plan 缺失 → 自动补 plan 或降级为 monitor_only 并重写 summary
- 集成测试：snapshot 走 LLM 失败回退路径，summary 必为 deterministic SOP 模板