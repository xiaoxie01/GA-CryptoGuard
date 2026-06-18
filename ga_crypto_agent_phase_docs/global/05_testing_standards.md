# 05 测试与验收通用标准

每个阶段至少包含：

## 单元测试

- 数据模型读写。
- 工具函数输入输出。
- 错误分支。
- 边界条件。

## 集成测试

- 定时任务 → 数据写入。
- 飞书消息 → agent_jobs → GA worker → 响应。
- signal → paper_order → paper_trade → trade_review。

## 幂等性测试

重复执行同一个 scheduled_time 的任务，不得重复写入脏数据。

## UTC / 防未来函数测试

查询 K 线必须满足：

```sql
WHERE close_time <= :analysis_time_utc
```

## 用户消息隔离测试

后台任务运行时，用户消息必须能优先响应，且 session 不被污染。

## 验收输出

每阶段完成后输出：

- 变更文件列表。
- 数据库迁移列表。
- 测试结果。
- 手动验收截图或日志。
- 未完成风险。
