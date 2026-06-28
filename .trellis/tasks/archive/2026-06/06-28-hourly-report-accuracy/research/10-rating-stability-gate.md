# 10 — 评级稳定性门禁

- **Query**: 是否有 grade hysteresis / previous_grade？短时间 grade 跳变有无防护？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- grade 计算 `plugins/crypto_guard/strategy/grade_config.py:33-43` `grade_from_score`：**纯单点函数**，根据 score 阈值 S=0.80 / A=0.72 / B=0.65 / C=0.50 / D=0；无 hysteresis。
- score 来源 `strategy/strategy_scorer.py:175` `"signal_grade": grade_from_score(score)` 每次决策都重新计算。
- LLM 路径有微弱稳定：`llm_agent_judge._normalize_llm_decision:328-337` 限制 LLM grade 不能比 deterministic grade 低超过 1 级；但允许任意升高；不防"短时间降级跳变"。
- `ga_decisions` schema (`schema.sql:145-173`) **没有 previous_grade 字段**；`analysis_states` 也不存。
- `controller.py` `/ `context_builder.py` 在 build context 时不读取上一条 grade。
- grep `previous_grade|grade_hysteresis|previous_signal_grade|grade_stability|stability` 在 crypto_guard 全仓无命中。

## Why broken

- 用户反例 3：LTC S/88% 15 分钟后降为 D/49%，高周期仍 range —— 评分由 0.88 跌到 0.49 直接降 4 级。当前没有任何机制要求 grade 跨档跳变时确认或迟滞。
- 反例 4 / 5：高 grade 自由跳变，使报告忽高忽低，可信度低。

## Where to fix
- `plugins/crypto_guard/strategy/grade_config.py` — 新增 `grade_with_hysteresis(current_score, previous_grade, *, up_threshold, down_threshold)`：
  - 升级需要 score 越过新档 + 0.02 缓冲
  - 降级在短时间窗需要连续 2 次确认
- `plugins/crypto_guard/storage/schema.sql:145` + `migrations.py` — 给 `ga_decisions` 加 `previous_grade TEXT` 列。
- `plugins/crypto_guard/ga_master/controller.py` `analyze_symbol` / `ga_master/context_builder.py` — 读取上一条同 symbol 决策的 signal_grade，传入新决策的 `previous_grade`，由 `run_ga_sop_decision` / `_normalize_llm_decision` 使用。

## Tests to add
- 上一轮 S（0.85），本次 0.79 → 仍保 S 或仅降到 A，不能直接 D
- 上一轮 D（0.45），本次 0.81 → 升级到 A（需 hysteresis 上沿），不跳到 S
- previous_grade 字段在 latest_ga_decisions_by_symbol 中可读