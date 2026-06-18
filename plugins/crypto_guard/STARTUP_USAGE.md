# CryptoGuard 启动与使用文档

本文档适用于 `E:\GenericAgent_crypto` 当前项目。

## 1. 当前完成状态

已完成 CryptoGuard MVP 闭环：

- Phase 0：插件目录、配置加载、SQLite/WAL 初始化、schema 执行、默认 symbols/策略 seed。
- Phase 1：symbols add/remove/pause/resume/list，飞书中文 intent parser，高优先级用户 job。
- Phase 2：`agent_jobs` SQLite queue、priority/session_id、scheduler_runs 幂等、task_locks、Binance public K 线 fetch/upsert。
- Phase 3：price_action、momentum、trend_stage、counter_evidence 初版；SMC/订单流/缠论有结构化占位和初步 SMC。
- Phase 4：MarketStateSnapshot、策略评分、GA decision schema 校验，失败降级 `no_edge`。
- Phase 5：飞书临时分析卡片 JSON、按钮 payload、加入模拟盘/机会监控/长期池闭环。
- Phase 6：paper order pending/open/closed、止盈止损检查、equity snapshot、平仓后 review job。
- Phase 7：trade review schema、loss classifier、candidate strategy patch。
- Phase 8：SMC 简版、订单流/缠论占位结构，后续可增强。
- Phase 9：Parquet/DuckDB 归档路径接口预留。

注意：Phase 8/9 当前是 MVP/接口级实现，不是完整缠论、完整订单流和历史回放系统。

## 2. 安全边界

CryptoGuard 只做研究、预警、模拟盘和复盘：

- 不接实盘。
- 不读取交易权限 API Key。
- 不调用 Binance 下单接口。
- 所有交易对象都在 `paper_*` 模块和表内。
- `plugins/crypto_guard/config/trading_mode.yaml` 必须保持：

```yaml
trading_mode:
  live_trading_enabled: false
  paper_trading_enabled: true
  real_order_api_enabled: false
  allow_withdraw_api: false
  allow_trade_api: false
  require_public_market_data_only: true
```

## 3. 环境要求

在项目根目录执行命令：

```powershell
cd E:\GenericAgent_crypto
```

不需要 Python 虚拟环境。当前实现依赖：

- `requests`
- `PyYAML`
- `jsonschema`
- GenericAgent 原有飞书依赖 `lark-oapi`，仅启动飞书时需要

如果缺包，在当前 Python 环境安装即可，不要创建 venv：

```powershell
python -m pip install requests PyYAML jsonschema
```

启动飞书前如果缺少飞书 SDK：

```powershell
python -m pip install lark-oapi
```

## 4. 数据库

默认 SQLite 数据库路径：

```text
E:\GenericAgent_crypto\data\crypto_guard\crypto_guard.sqlite3
```

可以用环境变量覆盖：

```powershell
$env:CRYPTO_GUARD_DB="E:\GenericAgent_crypto\data\crypto_guard\dev.sqlite3"
```

初始化数据库：

```powershell
python -c "from plugins.crypto_guard.storage.migrations import initialize_database; print(initialize_database())"
```

初始化会执行：

- `plugins/crypto_guard/storage/schema.sql`
- 写入默认 symbols
- 写入策略版本
- 开启 SQLite WAL

## 5. 验收命令

每次改动后建议跑：

```powershell
python -m compileall plugins\crypto_guard plugins\hooks.py frontends\fsapp.py
python -m unittest plugins.crypto_guard.tests.test_smoke -v
```

安全扫描：

```powershell
rg -n "live_trading_enabled: true|allow_trade_api: true|allow_withdraw_api: true|real_order_api_enabled: true|fapi/v1/order|/order|SIGNED|apiKey|secretKey" plugins\crypto_guard frontends\fsapp.py plugins\hooks.py
```

预期：

- compileall 无语法错误。
- unittest 显示 `OK`。
- 安全扫描不应出现真实下单/交易权限路径。

## 6. 常用工具命令

列出产品池：

```powershell
python -c "from plugins.crypto_guard.tools.ga_crypto_tools import crypto_symbol_list; import json; print(json.dumps(crypto_symbol_list(), ensure_ascii=False, indent=2))"
```

添加 symbol：

```powershell
python -c "from plugins.crypto_guard.tools.ga_crypto_tools import crypto_symbol_add; print(crypto_symbol_add('WIFUSDT'))"
```

暂停/恢复/移除：

```powershell
python -c "from plugins.crypto_guard.tools.ga_crypto_tools import crypto_symbol_pause; print(crypto_symbol_pause('WIFUSDT'))"
python -c "from plugins.crypto_guard.tools.ga_crypto_tools import crypto_symbol_resume; print(crypto_symbol_resume('WIFUSDT'))"
python -c "from plugins.crypto_guard.tools.ga_crypto_tools import crypto_symbol_remove; print(crypto_symbol_remove('WIFUSDT'))"
```

临时分析：

```powershell
python -c "from plugins.crypto_guard.tools.ga_crypto_tools import crypto_analyze_symbol_once; import json; print(json.dumps(crypto_analyze_symbol_once('BTCUSDT'), ensure_ascii=False, indent=2)[:4000])"
```

查看模拟盘订单：

```powershell
python -c "from plugins.crypto_guard.tools.ga_crypto_tools import crypto_get_open_paper_positions; import json; print(json.dumps(crypto_get_open_paper_positions(), ensure_ascii=False, indent=2))"
```

## 7. 一键启动

正常使用只需要启动飞书入口：

```powershell
cd E:\GenericAgent_crypto
python frontends\fsapp.py
```

`fsapp.py` 启动后会自动执行：

- 初始化 CryptoGuard SQLite 数据库。
- 启动 high priority user worker。
- 启动 background worker。
- 启动 UTC scheduler loop。
- 启动 paper worker loop。
- 启动日志系统。

可以用环境变量关闭自动启动，便于单独排查飞书：

```powershell
$env:CRYPTO_GUARD_AUTOSTART="0"
python frontends\fsapp.py
```

自动调度表：

| 任务 | UTC 执行时间 | 当前行为 |
|---|---:|---|
| 日线获取 | 每日 00:01 | 获取上一根完整 1d K 线，写入 candles；后续可扩展 GA 日线总结 |
| 4H 获取 | 00:01 / 04:01 / 08:01 / 12:01 / 16:01 / 20:01 | 获取上一根完整 4h K 线 |
| 1H 获取 | 每小时 00:01 | 获取上一根完整 1h K 线 |
| 每小时飞书简报 | 每小时 00:01~00:10 UTC 补偿窗口，同小时只入队一次且优先于重型 K 线抓取 | 推送各产品最近分析、模拟盘 pending/open、队列和失败任务 |
| 飞书失败重试 | 每分钟 | 扫描 alert_outbox，失败消息指数退避重试，最多 3 次 |
| 15m 行情分析 | 每 15 分钟 + 约 60 秒 | 生成多周期 snapshot，入队后台分析 |
| 模拟盘更新 | 每 3 分钟 | 更新 pending/open，触发 SL/TP 后创建复盘 job |
| 复盘任务 | 每日 00:08 | 扫描昨日 UTC 已平仓模拟单，批量生成复盘，更新 strategy_memory，推送复盘日报 |

策略记忆压缩的 weekly job 当前仍是后续增强项。

## 8. 日志

默认日志路径：

```text
E:\GenericAgent_crypto\logs\crypto_guard\crypto_guard.log
```

日志会轮转，单文件 10MB，保留 5 个备份。

查看实时日志：

```powershell
Get-Content logs\crypto_guard\crypto_guard.log -Wait -Tail 100
```

提高日志级别：

```powershell
$env:CRYPTO_GUARD_LOG_LEVEL="DEBUG"
python frontends\fsapp.py
```

自定义日志目录：

```powershell
$env:CRYPTO_GUARD_LOG_DIR="E:\GenericAgent_crypto\logs\crypto_guard_dev"
python frontends\fsapp.py
```

关闭控制台日志，只保留文件日志：

```powershell
$env:CRYPTO_GUARD_LOG_CONSOLE="0"
python frontends\fsapp.py
```

重点排查关键词：

```powershell
Select-String -Path logs\crypto_guard\crypto_guard.log -Pattern "ERROR","failed","scheduler","process_job","paper_worker","enqueue_feishu"
```

如果飞书卡片格式被平台拒绝，worker 会记录：

```text
send interactive card failed, fallback to text
```

并自动改发纯文本分析结果，避免用户只收到“已收到”。

如果 Binance public 行情接口网络失败，日志中通常能看到：

```text
Binance public request failed
ConnectionResetError / ConnectionError
```

用户侧会收到中文失败说明，不会再静默失败。常见原因是代理或网络重置，可以检查：

```powershell
Get-ChildItem Env:HTTP_PROXY,Env:HTTPS_PROXY,Env:ALL_PROXY
```

确认代理可用后重启 `frontends/fsapp.py`。

## 9. 手动 Scheduler

通常不需要手动执行；`fsapp.py` 自动启动后会按 UTC 调度。下面命令只用于排查。

手动执行单个定时任务：

```powershell
python -m plugins.crypto_guard.run_scheduler fetch_1d_klines
python -m plugins.crypto_guard.run_scheduler fetch_4h_klines
python -m plugins.crypto_guard.run_scheduler fetch_1h_klines
python -m plugins.crypto_guard.run_scheduler hourly_feishu_report
python -m plugins.crypto_guard.run_scheduler analyze_market_15m
python -m plugins.crypto_guard.run_scheduler daily_review
```

说明：

- 所有 scheduler job 使用 UTC。
- `fetch_*_klines` 只取最近已收盘 K 线。
- `scheduler_runs(job_name, scheduled_time)` 幂等，重复成功任务会跳过。
- `analyze_market_15m` 会为 active symbols 创建 background job。

## 10. 手动 Worker

通常不需要手动执行；`fsapp.py` 自动启动后会常驻运行。下面命令只用于排查。

处理一次用户队列：

```powershell
python -m plugins.crypto_guard.run_ga_workers --once --user-only
```

处理一次后台队列：

```powershell
python -m plugins.crypto_guard.run_ga_workers --once --background
```

常驻用户 worker：

```powershell
python -m plugins.crypto_guard.run_ga_workers --user-only
```

常驻后台 worker：

```powershell
python -m plugins.crypto_guard.run_ga_workers --background
```

后台 worker 在消费前会检查是否存在 pending user jobs。若存在 priority 1/2 的用户任务，后台任务会让路。

## 11. 手动 Paper Worker

通常不需要手动执行；`fsapp.py` 自动启动后每 3 分钟执行一次。下面命令只用于排查。

执行一次模拟盘更新：

```powershell
python -m plugins.crypto_guard.run_paper_worker
```

行为：

- pending order 满足条件后转 open。
- open order 触发 SL/TP 后转 closed。
- 写入 equity snapshot。
- 平仓后自动 enqueue `trade_review`，priority=4。

## 12. 飞书配置检查

先按 GenericAgent 原有方式配置飞书：

```powershell
python frontends\fsapp.py --check
```

确认 `fs_app_id` 和 `fs_app_secret` 可用后启动，启动时会自动拉起 CryptoGuard 后台服务：

```powershell
python frontends\fsapp.py
```

CryptoGuard 飞书文本命令会走独立高优先级队列。入口只做快速识别和入队，不在飞书 event handler 里跑长分析。

除“已收到”、按钮确认、文件失败这类短提示外，CryptoGuard 的长输出都会优先使用飞书 Markdown 交互卡片：

- 临时分析卡片：Markdown + 按钮。
- 系统状态：Markdown 卡片。
- 每小时简报：Markdown 卡片。
- 每日复盘：Markdown 卡片。
- 产品池列表：Markdown 卡片。
- 异常摘要：Markdown 卡片。

如果飞书拒绝卡片内容，会自动降级为纯文本并写入日志。

支持的示例消息：

```text
把 WIFUSDT 加入监控
暂停 DOGE 的分析
恢复 DOGE
移除 LTCUSDT
列出当前监控币种
系统状态
定时任务状态
队列状态
昨日复盘
每日复盘
执行复盘
只临时分析一下 SUIUSDT，不加入监控
帮我看 BTC 现在有没有机会
```

状态命令会返回：

- 自动服务是否启动。
- SQLite 数据库路径。
- 日志路径。
- 用户/后台队列积压。
- 最近 scheduler_runs。
- 当前 task_locks。
- 产品池和模拟盘数量。

复盘命令会返回：

- UTC 复盘窗口。
- 平仓交易数、新增复盘数。
- 胜/负/平与平均 R。
- 平仓明细。
- 新增亏损/盈利归因。
- strategy_memory Top。
- candidate patch 提醒。

按钮动作：

- 加入模拟盘
- 加入机会监控
- 加入长期产品池
- 忽略

同一个 `signal_id` 重复点击“加入模拟盘”不会重复创建订单。

## 13. GA 工具

插件加载后会给 GenericAgent 注入这些工具：

- `crypto_symbol_add`
- `crypto_symbol_remove`
- `crypto_symbol_pause`
- `crypto_symbol_resume`
- `crypto_symbol_list`
- `crypto_analyze_symbol_once`
- `crypto_create_opportunity_watch`
- `crypto_create_paper_order_from_signal`
- `crypto_get_market_state`
- `crypto_get_open_paper_positions`
- `crypto_review_trade`
- `crypto_daily_review`

## 14. 验收清单

已覆盖的关键验收：

- 用户消息 priority=1。
- daily/background job priority 更低，后台 worker 会让路。
- session_id 区分 `feishu:user:*`、`system:scheduled:*`、`system:review:*`。
- 15m 分析使用最近已收盘 close_time。
- repository 查询限制 `close_time <= analysis_time_utc`。
- `paper_orders.signal_id` 唯一。
- 平仓后创建 review job。
- 每日复盘会批量处理未复盘平仓单，并幂等跳过已复盘交易。
- 复盘会更新 `strategy_memory`。
- strategy patch 固定 candidate。
- GA decision 和 trade review 使用 JSON Schema 校验。

## 15. 后续增强建议

下一阶段可以继续补：

- 完整订单流 WebSocket/aggTrade 缓存。
- CVD、主动买卖比例、大单方向。
- 完整 SMC order block/FVG/mitigation。
- 缠论分型、笔、线段、中枢、背驰。
- DuckDB + Parquet 历史回放和 shadow testing 报表。
- 用更完整的持仓路径数据计算 MFE/MAE、entry/exit efficiency。
- 将 weekly strategy memory compression 落到 GA skill / strategy memory 文件。
