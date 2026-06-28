# 07 — liquidity sweep 方向语义

- **Query**: "liquidity_sweep"、"buy_side"、"sell_side" 现有方向语义是否正确？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- 实现 `analysis/smc_engine.py:9-22`：
  ```python
  sweep_low  = cur["low"] < prior_low  and cur["close"] > prior_low   # 下穿低点回收
  sweep_high = cur["high"] > prior_high and cur["close"] < prior_high # 上穿高点回收
  if sweep_low:
      last_event = "sell_side_liquidity_sweep"; direction = "bullish"
  elif sweep_high:
      last_event = "buy_side_liquidity_sweep";  direction = "bearish"
  ```
- 用途：
  - `strategy/strategy_scorer.py:119` 给 `smc_liquidity in {"sell_side_liquidity_sweep", "buy_side_liquidity_sweep"}` 加分
  - `analysis/counter_evidence_engine.py:35-37` 把 sell_side 当作 bullish 证据、buy_side 当作 bearish
  - `config/strategies.yaml:18` 触发 pullback strategy 要求 `smc.liquidity.last_event in [sell_side_liquidity_sweep, discount_retest]`

## Why broken

- **方向语义实际是正确的，不是反的**：标准 SMC 定义中，sell-side liquidity 是位于下方 swing low 的止损买单（散户 short 止损 = buy stop），被下扫掉后回收 = 看涨反转。buy-side liquidity 在上方 swing high，被上扫后回流 = 看跌反转。所以 `sweep_low → sell_side → bullish` 是正确映射。
- 真正风险：用户怀疑"可能反了"，反映变量名 `sell_side_liquidity_sweep` 在不熟悉 SMC 的人看来易误解为"看空"，导致维护时反向修改。语义正确但命名易误读。

## Where to fix
- `plugins/crypto_guard/analysis/smc_engine.py:14-19` — 注释明确"sell_side = taking sell-side liquidity (= stops below lows) → bullish reclaim"。
- 决策输出可新增 `liquidity.sweep_direction_label` 字段，"sell_side_to_bullish_reclaim" 之类自解释。
- 不需要改方向，只防未来误改。

## Tests to add
- candles[-1].low < prior_low 且 close > prior_low → `last_event == "sell_side_liquidity_sweep"`、direction="bullish"
- candles[-1].high > prior_high 且 close < prior_high → `last_event == "buy_side_liquidity_sweep"`、direction="bearish"
- counter_evidence_engine 把 sell_side 当作 bullish 的预期不变