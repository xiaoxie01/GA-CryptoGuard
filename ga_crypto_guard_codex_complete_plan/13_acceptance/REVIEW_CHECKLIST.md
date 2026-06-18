# 代码审查清单

## GA 主控

- [ ] 是否还有绕过 GA 的最终交易决策？
- [ ] 是否所有最终决策都写入 `ga_decisions`？
- [ ] 是否所有飞书按钮都来自 `feishu_actions_json`？

## Skill

- [ ] Skill 是否是 Prompt + Tool + Memory + Evolution？
- [ ] Tool 是否只输出事实？
- [ ] Skill 是否写执行日志？

## 飞书

- [ ] 每小时播报是否过长？
- [ ] D/C 是否被正确压缩？
- [ ] D 级是否错误建议机会监控？

## 存储

- [ ] Redis 是否真用于队列/锁/缓存/静默期？
- [ ] Parquet 是否真生成文件？
- [ ] DuckDB 是否真查询 Parquet？

## 风控

- [ ] 用户手动开仓是否被风控拦截？
- [ ] 无 trade_plan 是否被禁止模拟盘？
- [ ] RR/confidence 是否执行？

## 自进化

- [ ] patch 是否只进入 candidate？
- [ ] shadow_testing 是否实现？
- [ ] 是否禁止自动覆盖 active？
