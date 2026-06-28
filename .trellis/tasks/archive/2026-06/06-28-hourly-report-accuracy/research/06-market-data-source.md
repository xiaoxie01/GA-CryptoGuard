# 06 — 行情数据来源

- **Query**: BTC 59772.9、LTC 42.13–42.51、ADA 0.1437–0.1448 价位从哪个函数取？Binance USDⓈ-M 还是别的？K 线 close_time 是否记录？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- 供应商：Binance USDⓈ-M perpetual futures，base URL `https://fapi.binance.com` (`plugins/crypto_guard/data/binance_rest.py:15`)。
- K 线 GET `/fapi/v1/klines` (`binance_rest.py:104`)，参数 `interval, end_time, limit`；`fetch_closed_klines` (line 131-134) 过滤 `is_closed=True`，即 `close_time <= latest_closed_close_time_ms(interval)`。
- K 线 row 字段（line 108-124）：`{open, high, low, close, volume, close_time (ms), is_closed, ...}` —— **close_time 已落库**。
- 上游：`fetch_and_upsert_closed_klines` (`candle_store.py`)；调度 `cron_scheduler.fetch_closed_klines_for_active_symbols:20` 按 `repo.active_analysis_symbols()` 逐 symbol 拉。
- 全仓 LLM/strategy 看到的 `snapshot.modules.price_action.key_levels / swing_lows / swing_highs` 来自 `analysis/deterministic_preprocessor.py` 读 `repo.get_candles`，价位是 Binance close/high/low。
- 全仓 grep `yahoo|coingecko|yfinance` 在 `plugins/crypto_guard` 下无命中 —— 没有第二数据源。

## Why broken

- 价格本身不是 bug；问题在 hourly_report 用 stale 决策时显示的价位是 analysis_time 对应的 K 线 close，而非整点 close。例如 analysis_time=06:44:59Z 的决策显示 BTC=59772.9，但 07:00 这根 1h K 线在 07:00:00Z 才收盘，价位应该更新。
- 由于 `latest_ga_decisions_by_symbol` 不限 min_analysis_time，会展示 16 分钟前 的价位当作"当前"。

## Where to fix
- `plugins/crypto_guard/notify/hourly_report.py:build_hourly_report` — 渲染时同时显示 `decision.analysis_time_utc`、`age = now - analysis_time`、`close_time` 来源；让用户知道这是哪根 K 线的价位。
- `hourly_report.py:198-201` — 机会行加 `analysis_time_utc`、`age_minutes` 字段。

## Caveats / Not Found
- 没有发现 Yahoo / CoinGecko / CCXT 等备份供应商痕迹。
- 不需要切换供应商；只需标注 stale 状态。