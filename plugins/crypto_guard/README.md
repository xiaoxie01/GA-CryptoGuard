# CryptoGuard Plugin

GenericAgent 的 Binance USDⓈ-M Futures 研究插件。只做行情分析、预警、模拟盘与复盘，不包含任何真实下单路径。

## 核心边界

- `config/trading_mode.yaml` 中 `live_trading_enabled: false`，启动时会强制校验。
- 只调用 Binance public market data REST。
- 飞书文本入口将币圈命令写入 `agent_jobs`，用户消息 priority=1，按钮回调 priority=2。
- 后台 worker 消费任务前会检查 pending user jobs，避免后台任务压住用户消息。
- K 线查询统一使用 `analysis_time_utc`，repository 限制 `close_time <= analysis_time_utc`。
- `paper_orders.signal_id` 唯一，重复点击“加入模拟盘”不会重复创建订单。
- 复盘生成的策略补丁只写入 `candidate`。
- 启动 `frontends/fsapp.py` 会自动拉起 CryptoGuard user/background worker、UTC scheduler、paper worker 和日志。
- 默认日志：`logs/crypto_guard/crypto_guard.log`。

## 常用命令

完整启动与使用说明见 [STARTUP_USAGE.md](STARTUP_USAGE.md)。

```powershell
python -c "from plugins.crypto_guard.storage.migrations import initialize_database; print(initialize_database())"
python -m plugins.crypto_guard.run_scheduler fetch_1h_klines
python -m plugins.crypto_guard.run_scheduler analyze_market_15m
python -m plugins.crypto_guard.run_ga_workers --once --user-only
python -m plugins.crypto_guard.run_paper_worker
python -m unittest plugins.crypto_guard.tests.test_smoke -v
```

## GA Tools

- `crypto_symbol_add/remove/pause/resume/list`
- `crypto_analyze_symbol_once`
- `crypto_create_opportunity_watch`
- `crypto_create_paper_order_from_signal`
- `crypto_get_market_state`
- `crypto_get_open_paper_positions`
- `crypto_review_trade`
- `crypto_daily_review`
- `crypto_system_status`
