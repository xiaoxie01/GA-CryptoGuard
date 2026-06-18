# Phase 02 测试用例

## 单元测试

- 测试：确保 market_snapshots、module_analysis_results、signals、paper_orders、paper_trades、trade_reviews 串联
- 测试：所有 GA 分析保存原始结构化 JSON
- 测试：新增数据质量检查
- 测试：补充 no-lookahead 查询工具

## 集成测试

1. 启动相关 worker。
2. 准备测试数据。
3. 触发本阶段目标功能。
4. 检查数据库记录。
5. 检查飞书输出或模拟输出。
6. 检查日志。
7. 重复执行，验证幂等性。

## 回归测试

- 用户在后台任务运行时发送消息，应优先响应。
- 任务失败应写入 error_message。
- 相同 scheduled_time 的任务重复执行，不应重复创建脏数据。
- GA 输出 JSON 不符合 schema 时应拒绝入库或进入错误队列。
