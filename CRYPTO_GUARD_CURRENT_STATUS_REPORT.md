# GA CryptoGuard 当前系统现状报告

生成时间：2026-05-26 21:05（UTC+8）

## 1. 启动与进程状态

当前检测到 1 个 `frontends/fsapp.py` 进程正在运行：

- PID：27060
- 命令：`D:\Android\PythonSDK\python.exe frontends/fsapp.py`

`fsapp.py` 主函数会在飞书长连接启动前调用：

```python
start_all_services(send_message=send_message)
```

因此正常启动 `python frontends\fsapp.py` 后，会跟随启动：

- 飞书长连接入口
- 用户高优先级 worker
- 后台 worker
- UTC scheduler
- 模拟盘 3 分钟更新 worker
- alert outbox retry
- K 线定时拉取
- 5m / 15m 定时分析
- 机会监控检查
- 每小时飞书摘要
- 每日 UTC 00:05 复盘

注意：在命令行单独执行 `/status` 查询时，`service_started=false` 是正常现象，因为该状态是进程内标志；外部 Python 进程无法读取正在运行的 `fsapp.py` 内存状态。飞书内发送 `/status` 时才会反映 fsapp 当前进程内状态。

## 2. 当前基础设施状态

最近一次外部状态检查结果：

```text
SQLite: ok
Redis: ok
Parquet: ok
DuckDB: ok
```

详细状态：

- SQLite：`E:\GenericAgent_crypto\data\crypto_guard\crypto_guard.sqlite3`
- Redis：`redis://localhost:6379/0`
- Redis 队列：
  - `queue:user:feishu = 0`
  - `queue:ga:background = 0`
- Parquet 最近写入：
  - `E:\GenericAgent_crypto\data\parquet\klines\binance_um\BNBUSDT\1h\2026-05.parquet`
  - 写入时间：`2026-05-26 13:04:41 UTC`
- DuckDB：
  - 数据库：`E:\GenericAgent_crypto\data\duckdb\crypto_guard_analytics.duckdb`
  - 引擎：`D:\Program Files\duckdb\duckdb.exe`
  - 当前通过 CLI fallback 工作，Python 环境未安装 `duckdb` 模块

## 3. 队列与任务状态

当前任务概况：

```text
pending_user: 0
pending_background: 3
running: 1
failed_24h: 0
```

数据库累计任务状态：

```text
success: 2943
pending: 11
running: 1
failed: 2
```

说明：

- 当前没有用户高优先级积压。
- 后台仍有少量定时分析/模拟盘更新任务在排队或运行。
- 之前遗留的 11 条 stale running 任务已恢复为 pending。
- 已新增启动时自动恢复 stale running job 的逻辑，超过 30 分钟的 running job 会在服务启动时退回 pending。

## 4. 产品池与交易状态

产品池：

```text
enabled symbols: 10
disabled symbols: 0
```

模拟盘：

```text
paper_orders pending: 0
paper_orders open: 0
paper_orders closed: 0
```

当前没有 pending/open 模拟盘订单。

## 5. GA Master 与分析数据

当前核心数据量：

```text
ga_decisions: 19
analysis_states: 188
skill_execution_logs: 4152
skill_feedback_memory: 0
signals: 1837
ad_hoc_analyses: 11
alert_outbox: 18
parquet_archive_runs: 29
```

说明：

- GA Master Controller 已接管用户临时分析和定时分析。
- 每次最终分析会写入 `ga_decisions`。
- 每次分析状态会写入 `analysis_states`。
- 五大 Skill 执行日志已持续写入 `skill_execution_logs`。
- `skill_feedback_memory=0` 是因为当前还没有有效每日复盘样本或自进化触发样本。

最新临时分析：

```text
#11 DRIFTUSDT signal_id=1822 created_at=2026-05-26 12:52:38 UTC
#10 AGTUSDT   signal_id=1678 created_at=2026-05-26 03:27:25 UTC
```

最新临时分析对应 alert：

```text
#17 ad_hoc_analysis DRIFTUSDT sent 2026-05-26 12:52:38 UTC
#14 ad_hoc_analysis AGTUSDT   sent 2026-05-26 03:27:25 UTC
```

从数据库看，最近一次 `DRIFTUSDT` 临时分析只生成了 1 条 `ad_hoc_analyses` 和 1 条 `alert_outbox`。这说明重复推送不是“分析任务创建了两份”，而更像是飞书事件重投递、发送链路幂等不足或运行中旧代码未加载修复造成的结果重复发送。

## 6. 飞书推送现状

已具备：

- 飞书消息入口去重：`feishu_events`
- Redis 飞书事件去重：`dedupe:feishu_event:{message_id}`
- 普通告警静默期：`quiet:{symbol}:{alert_type}`
- alert outbox 失败重试
- 飞书按钮回调
- 按 `GADecision.feishu_actions_json` 生成按钮

本次新修复：

- 新增 `feishu_result_sent:{message_id}` 结果级幂等锁。
- 同一个飞书 `message_id` 的临时分析结果，24 小时内只允许发送一次。
- 即使飞书事件重投递、Redis/SQLite 边界并发、worker 重复消费，也会跳过第二次结果发送。

重要：当前正在运行的 PID 27060 仍是修复前启动的进程。需要重启 `fsapp.py` 后，新的结果级幂等锁才会生效。

## 7. 定时任务现状

当前 scheduler 运行逻辑为 UTC：

- 每日 00:01：1d K 线
- 每 4 小时 00:01 / 04:01 / 08:01 / 12:01 / 16:01 / 20:01：4h K 线
- 每小时 01 分：1h K 线
- 每 15 分钟 01 / 16 / 31 / 46 分：15m K 线
- 每 5 分钟 01 / 06 / 11 / ...：5m K 线
- 每 5 分钟 02 / 07 / 12 / ...：5m 行情分析
- 每 15 分钟：15m 行情分析
- 每 3 分钟：模拟盘收益/持仓更新
- 每小时：飞书摘要
- 每日 UTC 00:05：每日复盘

## 8. 已验证测试

最新测试结果：

```text
python -m compileall -q plugins\crypto_guard
通过

python -m unittest plugins.crypto_guard.tests.test_ga_master_acceptance plugins.crypto_guard.tests.test_smoke
Ran 37 tests
OK
```

新增覆盖：

- 飞书按钮规则
- `ga_decisions` 持久化
- 旧 signal 兼容路径必须补建 `GADecision`
- Parquet 合并去重
- DuckDB 读取 Parquet
- 临时库 Redis fallback
- 同一飞书 `message_id` 的临时分析结果只发送一次

## 9. 当前已知问题与风险

### 9.1 当前运行进程未加载最新修复

当前 `fsapp.py` 进程 PID 27060 是本次修复前启动的。需要重启后才会加载：

- `feishu_result_sent:{message_id}` 结果级幂等锁
- stale running job 启动恢复逻辑

建议重启方式：

```powershell
Stop-Process -Id 27060
cd E:\GenericAgent_crypto
python frontends\fsapp.py
```

### 9.2 Python 未安装 duckdb 模块

当前 DuckDB 正常工作，但走的是 CLI fallback：

```text
engine: duckdb_cli
python_module: missing
```

这不影响当前功能；后续如果要做更重的 DuckDB 分析，建议安装 Python 模块：

```powershell
python -m pip install duckdb
```

### 9.3 Skill 记忆仍为空

`skill_feedback_memory=0`，原因是还没有足够复盘/亏损/自进化样本触发记忆写入。

后续优化重点：

- 让每日复盘即使无交易也能写入轻量观察记忆
- 将“无机会原因”转成 Skill feedback memory
- 将 LLM 对分析失效原因的评价沉淀到 Skill memory

### 9.4 每小时摘要与临时分析仍需文案优化

当前结构已改，但仍建议继续优化：

- 中文表达统一
- 避免模块术语堆叠
- 明确“趋势状态、无机会原因、等待什么、何时再看”
- 对 D/C 级只输出简洁结论
- 详细模块只在“详细分析 XXX”时展开

## 10. 后续优化优先级建议

### P0：飞书重复推送复测

重启 fsapp 后，连续发送同一条“分析 xxx”或等待飞书重投递，确认：

- 只收到一次分析卡片
- 日志出现时应为：

```text
skip duplicate feishu result send message_id=...
```

### P1：临时分析卡片可读性

目标格式：

- 当前结论
- 市场结构状态
- 趋势清晰度
- 无机会原因
- 关键关注点位
- 等待触发条件
- 下次分析时间
- 是否允许模拟盘
- 按钮

### P2：每小时播报管理层摘要

目标：

- 不逐币长篇展开
- 高等级机会优先
- C/D 只聚合
- 明确系统健康度
- 明确模拟盘权益/回撤

### P3：复盘与 Skill memory

目标：

- 无交易日也沉淀观察
- 亏损后自动归因
- 自进化进入 shadow_testing
- candidate 不自动覆盖 active

### P4：历史回放 / 回测产品化

目标：

- 飞书命令模板化
- 支持 symbol / interval / 时间区间
- 输出胜率、RR、回撤、信号失效原因
- 回测结果进入策略版本与 Skill memory

## 11. 当前结论

系统主链路已经可运行：

- 飞书入口可启动
- GA Master 已接管决策
- Redis / SQLite / Parquet / DuckDB 正常
- 定时任务会随 `fsapp.py` 启动
- 模拟盘不会接实盘
- 风控不会被人工按钮绕过

当前最需要马上处理的是：重启 `fsapp.py` 使重复推送修复生效，然后观察下一次临时分析是否仍出现重复卡片。
