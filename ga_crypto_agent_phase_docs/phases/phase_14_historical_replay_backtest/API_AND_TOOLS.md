# Phase 14 API 与工具函数

## 工具函数约定

所有工具函数返回 dict：

```python
{
    "ok": True,
    "data": {},
    "error": None
}
```

失败时：

```python
{
    "ok": False,
    "data": None,
    "error": "human readable error"
}
```

## 推荐工具函数

根据本阶段实现范围，优先从以下工具中选择或扩展：

```python
crypto_system_status()
crypto_list_recent_errors()
crypto_fetch_closed_klines(symbols, interval, lookback)
crypto_build_market_profile(symbol, interval)
crypto_analyze_symbol_once(symbol, timeframes)
crypto_analyze_market(symbols, timeframes)
crypto_create_opportunity_watch(symbol, watch_condition)
crypto_update_opportunity_watches()
crypto_create_paper_order_from_signal(signal_id)
crypto_update_paper_positions()
crypto_review_trade(trade_id)
crypto_list_strategy_versions()
crypto_create_strategy_patch(review_id)
crypto_run_shadow_test(strategy_name, candidate_version)
crypto_run_historical_replay(symbol, interval, start_time, end_time)
```

## 飞书命令建议

```text
/status
/errors
/watchlist
/analyze BTCUSDT
/opportunities
/paper
/review latest
/strategies
/shadow-tests
```
