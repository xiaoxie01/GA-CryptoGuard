# 08 — drawdown 渲染

- **Query**: 报表 drawdown=-0.50% 在哪个函数渲染？内部存储与对外 sign 约定？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- 渲染点：
  - `hourly_report.py:147` `f"drawdown={float(snap.get('drawdown_percent') or 0):.2f}%"` （render_ga_hourly_summary）
  - `hourly_report.py:825` `f"回撤：{float(snap.get('drawdown_percent') or 0):.2f}%"` （render_hourly_report_text）
  - `hourly_report.py:136,774` `f"（回撤 {risk_state.get('drawdown_pct', 0):.1f}%）"`
  - `hourly_report.py:388` `_fetch_risk_state` 把 `account_risk_guard.check(...).drawdown_pct` 透传
- 内部存储 sign：
  - `paper/execution_quality.py:152` `drawdown_percent = min(0.0, (equity - starting_equity) / starting_equity * 100)` → **永远 <=0**（亏损负值）
  - `risk/account_risk_guard.py:340` `_drawdown_percent = (equity - initial) / initial * 100` → **可正可负**：盈利时为正，亏损时为负
  - `repository.py:1742` `drawdown = min(float(account.get("max_drawdown") or 0), (equity - initial) / initial ...)` —— 内存里也是可正可负
- 阈值约定：`account_risk_guard` 用 `drawdown_pct <= self.drawdown_threshold` 比较，`drawdown_threshold` 默认为负值（见 `account_risk_guard.py:90-93`），即 `<=-2.5%` 触发 risk_off。但 `_ok_result` 用 `drawdown_pct=0.0` 在没启动账户时返回 0。
- `config/trading_mode.yaml`risk thresholds 为负值（`drawdown_risk_off_threshold: -2.5`）。

## Why broken

- 用户反例 6：drawdown=-0.50% 显示符号问题。当 `execution_quality.drawdown_percent` 是 -0.5（正确显示"-0.50%"），但 `_fetch_risk_state.drawdown_pct` 如果账户初始被设置成 0 或盈利，会显示正值"回撤 0.0%"，与左侧 `drawdown=-0.50%` 不一致。
- 两个来源 sign 约定不同：`execution_quality` 强制负，`account_risk_guard._drawdown_percent` 可正。同一报告两处显示可能矛盾。
- 报表汉字用"回撤 -0.50%"——常规人理解为"亏损 0.5%"，没问题；但若 equity > initial 显示"回撤 1.2%"会被误读。

## Where to fix
- 统一 sign 约定：drawdown 在所有存储点都 <=0，正 equity(盈利) 时显示 0；或约定并文档化。
- `plugins/crypto_guard/risk/account_risk_guard.py:_drawdown_percent` 改为 `min(0.0, (equity-initial)/initial*100)`。
- `hourly_report.py:147,825` 显示明确："回撤 -0.50%（账号权益低于初始）"，或在 drawdown>=0 时显示"未回撤"。

## Tests to add
- equity=9950, initial=10000 → `drawdown_pct == -0.5`、显示"-0.50%"
- equity=10500, initial=10000 → `drawdown_pct == 0`（不是 +5.0）
- `account_risk_guard` 与 `execution_quality` 在相同账户下返回同一 sign