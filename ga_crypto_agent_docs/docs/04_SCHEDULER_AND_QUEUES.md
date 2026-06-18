# 04. 定时任务与队列设计

## 1. 设计目标

定时任务必须：

- 使用 UTC-0。
- 不阻塞飞书用户消息。
- 不污染用户对话上下文。
- 可幂等重试。
- 可跳过重复执行。
- 用户消息优先级最高。

## 2. 推荐任务表

| 任务 | UTC 时间 | 职责 | 是否需要 GA |
|---|---:|---|---|
| fetch_1d_klines | 每日 00:01 | 获取上一根完整日线，更新日线画像 | 可选，总结时需要 |
| fetch_4h_klines | 00:01/04:01/08:01/12:01/16:01/20:01 | 更新 4H 画像 | 结构变化时需要 |
| fetch_1h_klines | 每小时 00:01 | 更新 1H 画像 | 通常不需要 |
| analyze_market_15m | 每 15m + 1m | 多周期综合分析 | 高评分时需要 |
| update_paper_positions | 每 3m 或 5m | 更新模拟盘收益 | 平仓/异常时需要 |
| daily_review | 每日 00:08 | 昨日模拟盘复盘 | 需要 |
| strategy_shadow_report | 每日/每周 | 候选策略影子测试统计 | 汇总时需要 |

## 3. 不要直接用 GA cron 执行长任务

允许：

```text
GA 定时任务 → 创建轻量 job
```

禁止：

```text
GA 定时任务 → 直接分析 20 个币 → 多轮工具调用 → 推送大量飞书消息
```

正确链路：

```text
cron_scheduler
  → scheduler_runs 幂等检查
  → 确定性数据任务
  → 必要时 enqueue agent_jobs
  → ga_background_worker 消费
```

## 4. 队列优先级

| priority | 类型 |
|---:|---|
| 1 | 飞书用户消息 |
| 2 | 飞书按钮回调 |
| 3 | 重要预警解释 |
| 4 | 模拟盘平仓复盘 |
| 5 | 15m 定时分析 |
| 7 | 1D/4H 总结 |
| 9 | 历史回放 / 影子测试统计 |

后台 worker 每次处理任务前必须检查是否存在 pending user jobs。若有，则延迟后台任务。

## 5. agent_jobs 表

见 `sql/schema.sql`。核心字段：

- job_type。
- priority。
- source。
- session_id。
- payload_json。
- status。
- scheduled_at。
- started_at。
- finished_at。

## 6. task_locks

每类任务必须有锁，避免重叠：

```text
lock:fetch_klines:BTCUSDT:15m
lock:analyze:BTCUSDT:15m
lock:daily_review
lock:paper_update
```

锁必须带 `locked_until`，防止进程挂掉后永远锁死。

## 7. 幂等规则

### 7.1 scheduler_runs 幂等

同一个 `job_name + scheduled_time` 成功后不得重复执行。

### 7.2 candles 幂等

`UNIQUE(symbol, interval, open_time)`，使用 upsert。

### 7.3 Feishu event 幂等

飞书 event_id 需要缓存或写库，避免重复处理。

### 7.4 Paper order 幂等

同一个 `signal_id` 用户重复点击“加入模拟盘”时，不得创建重复订单。需要唯一约束或业务检查。

## 8. 调度伪代码

```python
def run_scheduled_job(job_name: str, scheduled_time: int, task_fn, **kwargs):
    if repo.scheduler_run_success_exists(job_name, scheduled_time):
        return {"ok": True, "skipped": True}

    if not lock.acquire(f"scheduler:{job_name}", ttl_seconds=600):
        return {"ok": True, "skipped": True, "reason": "locked"}

    run_id = repo.create_scheduler_run(job_name, scheduled_time, status="running")
    try:
        result = task_fn(**kwargs)
        repo.finish_scheduler_run(run_id, status="success", result=result)
        return result
    except Exception as exc:
        repo.finish_scheduler_run(run_id, status="failed", error=str(exc))
        raise
    finally:
        lock.release(f"scheduler:{job_name}")
```

## 9. 15m 分析伪代码

```python
def analyze_market_15m():
    symbols = repo.get_active_analysis_symbols()
    for symbol in symbols:
        snapshot = build_market_state_snapshot(
            symbol=symbol,
            analysis_time_utc=get_latest_closed_time("15m"),
            mode="scheduled",
        )
        repo.save_market_snapshot(snapshot)

        pre_score = deterministic_pre_score(snapshot)
        if pre_score >= 0.65:
            queue.enqueue(
                job_type="scheduled_market_analysis",
                priority=5,
                source="scheduler",
                session_id=f"system:scheduled:15m:{symbol}",
                payload_json=snapshot,
            )
```

## 10. 模拟盘更新伪代码

```python
def update_paper_positions():
    positions = repo.get_open_paper_positions()
    for pos in positions:
        price = price_cache.get_mark_or_last_price(pos.symbol)
        result = paper_broker.update_position(pos, price)
        repo.save_equity_snapshot()
        if result.closed:
            queue.enqueue(
                job_type="trade_review",
                priority=4,
                source="paper_worker",
                session_id=f"system:review:{result.trade_id}",
                payload_json={"trade_id": result.trade_id},
            )
```
