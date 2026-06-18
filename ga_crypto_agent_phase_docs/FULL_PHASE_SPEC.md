# GA CryptoGuard 全阶段规格总览

本文件是阶段包总览。详细执行内容请进入每个阶段文件夹查看。

## 使用方式

1. 先读 `CODEX_MASTER_PROMPT.md`。
2. 读 `global/` 下全部全局文档。
3. 按 `phase_manifest.yaml` 顺序进入 `phases/phase_xx_*`。
4. 每阶段严格按照：
   - README.md
   - IMPLEMENTATION.md
   - DATA_MODEL.md
   - API_AND_TOOLS.md
   - ACCEPTANCE.md
   - TEST_CASES.md
   - REVIEW_CHECKLIST.md
5. 阶段通过验收后再进入下一阶段。

## 系统铁律

- 不接实盘。
- 不下真实订单。
- 用户消息优先。
- 后台任务隔离。
- UTC 时间。
- 已收盘 K 线。
- 所有判断可追溯。
- 所有自进化可审计。
- active 策略不可被 GA 直接覆盖。
