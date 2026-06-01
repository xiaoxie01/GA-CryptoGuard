# 10. 验收标准

## 1. 不接实盘验收

必须满足：

- 配置中 `live_trading_enabled=false`。
- 无真实下单代码路径。
- 无 `/order` 实盘交易调用。
- 不读取交易权限 API Key。
- 代码中所有交易相关类命名为 paper 或 simulation。

## 2. 用户消息不被定时任务覆盖

测试场景：

1. 手动触发 daily_review 长任务。
2. 同时在飞书发送“分析 BTC”。
3. 系统必须优先响应用户消息。
4. 用户 session 不得出现 daily_review 的上下文污染。

验收：

- 用户消息 job priority=1。
- daily_review priority>=7。
- session_id 不同。
- 后台 worker 检测到 user queue pending 时暂停或延迟。

## 3. UTC 定时任务

测试：

- 在 UTC 10:01 执行 1H 任务，只获取 09:00-09:59:59 的已收盘 K 线。
- 在 UTC 10:16 执行 15m 分析，只使用 10:00-10:14:59 或更早的已收盘 15m K 线。

验收：

- 不使用未收盘 K 线。
- candles `open_time` 唯一。
- scheduler_runs 幂等。

## 4. 产品池

测试命令：

```text
把 WIFUSDT 加入监控
暂停 WIFUSDT
恢复 WIFUSDT
移除 WIFUSDT
列出当前监控品种
```

验收：

- symbols 表正确更新。
- 定时任务扫描 active symbols。

## 5. 临时分析

测试命令：

```text
只临时分析一下 SUIUSDT，不加入监控
```

验收：

- 不写入长期 user_watchlist。
- 写入 ad_hoc_analyses。
- 返回决策卡片。
- 根据结果显示正确按钮。

## 6. 机会监控

验收：

- 创建 opportunity_watches。
- 支持 expires_at。
- 支持 invalid_condition。
- 触发后生成 signal。
- 失效后飞书通知。

## 7. 模拟盘

验收：

- 只有完整 trade_plan 才能创建 paper_order。
- 同一个 signal 不能重复创建订单。
- 按价格推进 pending/open/closed。
- 记录 MFE、MAE、PnL、R 值。
- 平仓后创建 trade_review job。

## 8. SOP 输出

每个模块必须输出标准 JSON：

- price_action。
- smc，可第二阶段为空但 schema 存在。
- order_flow，可第二阶段为空但 schema 存在。
- momentum。
- trend_stage。
- chanlun，可第二阶段为空但 schema 存在。

GA decision 必须通过 `ga_decision.schema.json` 校验。

## 9. 自进化

验收：

- trade_review 输出亏损/盈利归因。
- strategy_patch 状态默认为 candidate。
- candidate 策略不得直接 active。
- shadow testing 不推送飞书、不创建模拟盘。
- 满足样本条件后才允许人工或规则升级 active。

## 10. 防未来函数

测试：

- 用历史时间点 analysis_time 回放。
- 数据查询不得读取 close_time > analysis_time 的 K 线。

验收：

- 所有 repository 查询暴露 `analysis_time_utc` 参数。
- 所有策略评估记录 analysis_time。
