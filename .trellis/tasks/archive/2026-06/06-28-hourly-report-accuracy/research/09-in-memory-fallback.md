# 09 — in_memory_fallback 措辞

- **Query**: "in_memory_fallback" 出现在哪里？真正语义是什么？报表怎么引用？
- **Scope**: internal
- **Date**: 2026-06-28

## What

- 出现点：
  - `hourly_report.py:375` `_duckdb_hourly_stats` 失败时返回 `{"ok": False, "source": "in_memory_fallback", "error": str(exc), "signal_distribution": {}}`
  - `hourly_report.py:212-213` 渲染：
    ```python
    distribution = (duckdb_stats or {}).get("signal_distribution") or grade_counts
    source = (duckdb_stats or {}).get("source") or "in_memory_fallback"
    lines.append("- 等级分布：" + ... + f"（{source}）")
    ```
- 真正语义：DuckDB 等级分布查询失败时回退到 SQLite 内存中刚算好的 `grade_counts`（line 109-112），即"用本次 hourly_report 现场计算 ga_decisions 的 grade 分布"。数据真实来自 ga_decisions，只是分布来自当下结果集而非 DuckDB 时序库；source 写"in_memory_fallback"是技术性 fallback，不是假数据。
- 渲染时即使 DuckDB 成功，也直接套用 source 字段；失败时显示"（in_memory_fallback）"容易被读者误解为"假数据/不可信"。

## Why broken

- 用户反例 7：措辞误导。读者把 "in_memory_fallback" 当成"AI 编造 / 前端兜底"，但实际是 SQLite 实时统计的回退方案。
- distribution 来源注释与文案不对齐：`grade_counts` 来自 SQLite 当前 120 条决策，称为 "in_memory" 是因为字段在 Python dict 里聚合，但底层是真实决策。

## Where to fix
- `plugins/crypto_guard/notify/hourly_report.py:212-213` — 把 source label 改为可读："duckdb"→"DuckDB 时序", "in_memory_fallback"→"SQLite 实时统计（DuckDB 未启用）"。
- 或者追加说明：`"fallback 时统计的是当批 ga_decisions，不是模拟数据"`。

## Tests to add
- DuckDB 异常时 `source == "SQLite 实时统计"` 文案
- DuckDB ok 时使用 `source == "duckdb"`
- distribution 等级数量与本次 ga_decisions 一致