# GA CryptoGuard Codex 完整实施与验收方案

本包用于指导 Codex 对现有 GA CryptoGuard 项目进行架构纠偏、功能完善与验收。目标是把当前实现纠正为：

> **由 GA 绝对主控、通过飞书交互、面向 Binance 合约市场的自主分析与模拟交易研究系统。**

## 关键原则

1. **GA Master Controller 是唯一最终决策出口。**
2. **任何交易判断、机会监控建议、模拟盘动作、飞书按钮都必须来自 `GADecision`。**
3. **Skill 是 Prompt + Tool + Feedback Memory + Evolution Rule 的动态闭环，不是单纯 Python 函数。**
4. **Tool 只计算客观事实，不直接产生最终交易决策。**
5. **飞书是交互层和预警出口，不是内部分析状态的全文展示窗口。**
6. **SQLite 保存业务状态；Redis 承担队列/缓存/锁/静默期；Parquet 自主管理长期 K 线归档；DuckDB 查询 Parquet 和生成统计。**
7. **不接实盘，不调用真实下单接口，不保存交易权限或提现权限 API Key。**

## 用户指定 Windows 路径

- DuckDB 数据库目录：`D:\Program Files\duckdb`
- Redis 安装目录：`D:\Program Files\Redis`
- Parquet：由项目自行处理，默认放在项目目录下 `data/parquet/klines/binance_um/`

## 建议阅读顺序

1. `CODEX_MASTER_PROMPT.md`：直接复制给 Codex 的总提示词。
2. `IMPLEMENTATION_ORDER.md`：严格实施顺序。
3. `01_architecture/GA_MASTER_CONTROL.md`：GA 主控架构。
4. `06_skills/SKILL_CONTRACT.md`：动态 Skill 规范。
5. `03_storage/STORAGE_REDIS_DUCKDB_PARQUET.md`：Redis / Parquet / DuckDB 接入规范。
6. `13_acceptance/ACCEPTANCE_MATRIX.md`：最终验收矩阵。

## 输出要求

Codex 每完成一个实施阶段，必须输出：

- 修改文件列表
- 新增/修改配置
- 数据库迁移说明
- Redis 接入点
- Parquet 写入示例
- DuckDB 查询示例
- 飞书播报新旧对比
- 临时分析按钮规则测试结果
- `/status` 输出示例
- 验收标准逐条对照
