from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.storage.duckdb_analytics import DuckDBAnalytics
from plugins.crypto_guard.storage.migrations import check_schema_health
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.diagnostics.state_consistency import diagnose_state_consistency


def resolve_report_target(repo: CryptoGuardRepository, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    payload = payload or {}
    if payload.get("receive_id"):
        return {"receive_id": payload["receive_id"], "receive_id_type": payload.get("receive_id_type", "chat_id")}
    env_receive_id = os.environ.get("CRYPTO_GUARD_FEISHU_RECEIVE_ID")
    if env_receive_id:
        return {
            "receive_id": env_receive_id,
            "receive_id_type": os.environ.get("CRYPTO_GUARD_FEISHU_RECEIVE_ID_TYPE", "chat_id"),
        }
    return repo.latest_feishu_target()


def build_hourly_report(repo: CryptoGuardRepository) -> dict[str, Any]:
    # Check schema health first
    schema = check_schema_health()
    if not schema["ok"]:
        return {
            "ok": False,
            "error": "schema_unhealthy",
            "missing_columns": schema["missing_columns"],
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    active_symbols = repo.active_analysis_symbols()
    ga_decisions = repo.latest_ga_decisions_by_symbol(limit=120)
    signals = repo.latest_signals_by_symbol(limit=80)
    states = repo.latest_analysis_states(limit=120)
    open_orders = repo.list_open_paper_orders()
    active_watches = repo.list_active_opportunity_watches()
    equity = repo.latest_equity_snapshot()
    failed_jobs = repo.recent_failed_jobs(limit=5)
    queue_counts = {
        "pending_user": _count(repo, "SELECT COUNT(*) FROM agent_jobs WHERE status='pending' AND priority <= 2"),
        "pending_background": _count(repo, "SELECT COUNT(*) FROM agent_jobs WHERE status='pending' AND priority > 2"),
        "running": _count(repo, "SELECT COUNT(*) FROM agent_jobs WHERE status='running'"),
    }
    duckdb_stats = _duckdb_hourly_stats(now)
    risk_state = _fetch_risk_state(repo)
    shadow_data_quality = _fetch_shadow_data_quality(repo)
    feedback_patterns = _fetch_feedback_patterns(repo)
    long_short_performance = _fetch_long_short_performance(repo)
    account_feedback_gate = _fetch_account_feedback_gate_stats(repo)
    state_consistency = _fetch_state_consistency(repo)
    agent_brief = _agent_hourly_brief(active_symbols, signals, open_orders, failed_jobs, queue_counts)
    return {
        "ok": True,
        "generated_at_utc": now,
        "active_symbols": active_symbols,
        "latest_signals": signals,
        "analysis_states": states,
        "ga_decisions": ga_decisions,
        "active_watches": active_watches,
        "open_orders": open_orders,
        "equity_snapshot": equity,
        "failed_jobs": failed_jobs,
        "queue_counts": queue_counts,
        "duckdb_stats": duckdb_stats,
        "risk_state": risk_state,
        "shadow_data_quality": shadow_data_quality,
        "feedback_patterns": feedback_patterns,
        "long_short_performance": long_short_performance,
        "account_feedback_gate": account_feedback_gate,
        "state_consistency": state_consistency,
        "agent_brief": agent_brief,
        "text": (
            render_ga_hourly_summary(now, active_symbols, ga_decisions, open_orders, active_watches, failed_jobs, queue_counts, equity_snapshot=equity, duckdb_stats=duckdb_stats, risk_state=risk_state, shadow_data_quality=shadow_data_quality, feedback_patterns=feedback_patterns, long_short_performance=long_short_performance, account_feedback_gate=account_feedback_gate, state_consistency=state_consistency)
            if ga_decisions
            else render_hourly_report_text(now, active_symbols, signals, open_orders, failed_jobs, queue_counts, agent_brief=agent_brief, analysis_states=states, equity_snapshot=equity, risk_state=risk_state, shadow_data_quality=shadow_data_quality, feedback_patterns=feedback_patterns, long_short_performance=long_short_performance, account_feedback_gate=account_feedback_gate, state_consistency=state_consistency)
        ),
    }


def render_ga_hourly_summary(
    generated_at_utc: str,
    active_symbols: list[str],
    ga_decisions: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    active_watches: list[dict[str, Any]],
    failed_jobs: list[dict[str, Any]],
    queue_counts: dict[str, int],
    equity_snapshot: dict[str, Any] | None = None,
    duckdb_stats: dict[str, Any] | None = None,
    risk_state: dict[str, Any] | None = None,
    shadow_data_quality: dict[str, Any] | None = None,
    feedback_patterns: dict[str, Any] | None = None,
    long_short_performance: dict[str, Any] | None = None,
    account_feedback_gate: dict[str, Any] | None = None,
    state_consistency: dict[str, Any] | None = None,
) -> str:
    rows = [_decision_row(row) for row in ga_decisions]
    grade_counts: dict[str, int] = {grade: 0 for grade in ("S", "A", "B", "C", "D")}
    for row in rows:
        grade = str(row.get("signal_grade") or "D")
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
    high_grade = [r for r in rows if str(r.get("signal_grade")) in {"S", "A", "B"}]
    no_edge = [r for r in rows if str(r.get("signal_grade")) in {"C", "D"}]
    lines = [
        "**GA CryptoGuard 每小时摘要**",
        f"北京时间（UTC+8）：{_format_time_utc8(generated_at_utc)}",
        f"UTC 时间：{generated_at_utc}",
        "",
        "**一、系统状态**",
        f"- scheduler：运行中；队列 user={queue_counts.get('pending_user', 0)} background={queue_counts.get('pending_background', 0)} running={queue_counts.get('running', 0)}",
        "- market data：SQLite 热数据；Redis/Parquet/DuckDB 状态见 /status",
        f"- Feishu queue：最近失败任务 {len(failed_jobs)}",
    ]

    # P2-B: Add risk_off state
    if risk_state:
        risk_status = []
        if risk_state.get("risk_off"):
            risk_status.append("risk_off")
        if risk_state.get("hard_risk_off"):
            risk_status.append("hard_risk_off")
        if risk_state.get("daily_loss_pause"):
            risk_status.append("daily_loss_pause")
        if risk_status:
            lines.append(f"- 风险状态：**{', '.join(risk_status)}**（回撤 {risk_state.get('drawdown_pct', 0):.1f}%）")
        else:
            lines.append("- 风险状态：正常")

    lines.extend(["", "**二、模拟盘摘要**"])
    if equity_snapshot:
        snap = _safe_json(equity_snapshot.get("snapshot_json"), {}) or equity_snapshot
        lines.append(
            f"- equity={float(equity_snapshot.get('account_equity') or 0):.2f}；"
            f"unrealized={float(equity_snapshot.get('unrealized_pnl') or 0):.2f}；"
            f"realized={float(equity_snapshot.get('realized_pnl') or 0):.2f}；"
            f"drawdown={float(snap.get('drawdown_percent') or 0):.2f}%"
        )
    else:
        lines.append("- 暂无净值快照")
    lines.append(f"- open/pending orders：{len(open_orders)}")

    # P2-B: Add LONG vs SHORT performance
    if long_short_performance and not long_short_performance.get("error"):
        long = long_short_performance.get("long", {})
        short = long_short_performance.get("short", {})
        if long.get("count", 0) > 0 or short.get("count", 0) > 0:
            lines.extend(["", "**模拟盘方向表现（近 30 天）**"])
            if long.get("count", 0) > 0:
                win_rate = long["wins"] / long["count"] * 100 if long["count"] > 0 else 0
                lines.append(f"- LONG：{long['count']} 笔，胜率 {win_rate:.0f}%，avg R={long['avg_r']:.2f}")
            if short.get("count", 0) > 0:
                win_rate = short["wins"] / short["count"] * 100 if short["count"] > 0 else 0
                lines.append(f"- SHORT：{short['count']} 笔，胜率 {win_rate:.0f}%，avg R={short['avg_r']:.2f}")

    lines.extend(["", "**三、高等级机会（S/A/B）**"])
    if not high_grade:
        lines.append("- 暂无 S/A/B 级机会")
    for row in high_grade[:10]:
        lines.append(
            f"- {row['symbol']}：{row.get('signal_grade')}，{float(row.get('confidence') or 0) * 100:.0f}%；"
            f"{_decision_text(row.get('decision'))}；{row.get('final_summary') or '-'}"
        )

    lines.extend(["", "**四、当前机会监控**"])
    if not active_watches:
        lines.append("- 暂无 active 机会监控")
    for watch in active_watches[:10]:
        condition = _compact_items(_safe_json(watch.get("watch_condition_json"), []), max_items=2)
        lines.append(f"- #{watch['id']} {watch['symbol']} {watch.get('direction') or '-'}：{condition or watch.get('watch_reason') or '-'}")

    lines.extend(["", "**五、C/D 无优势品种汇总**"])
    distribution = (duckdb_stats or {}).get("signal_distribution") or grade_counts
    source = (duckdb_stats or {}).get("source") or "in_memory_fallback"
    lines.append("- 等级分布：" + "，".join(f"{k}={v}" for k, v in distribution.items()) + f"（{source}）")
    if no_edge:
        symbols = ", ".join(row["symbol"] for row in no_edge[:30])
        lines.append(f"- C/D：{symbols}")
        reasons = _compact_items([row.get("final_summary") for row in no_edge], max_items=3)
        lines.append(f"- 主要原因：{reasons or '趋势不清晰或风控不足'}")
    else:
        lines.append("- 暂无 C/D 无优势品种")

    # P2-B: Add shadow data quality
    if shadow_data_quality and not shadow_data_quality.get("error"):
        lines.extend(["", "**六、影子测试数据质量**"])
        total = shadow_data_quality.get("total_shadow_samples", 0)
        if total > 0:
            real_ratio = shadow_data_quality.get("real_ratio", 0) * 100
            lines.append(
                f"- 样本总数：{total}；"
                f"真实 PnL：{shadow_data_quality.get('real_pnl_count', 0)}（{real_ratio:.0f}%）；"
                f"伪 R：{shadow_data_quality.get('pseudo_r_count', 0)}"
            )
        else:
            lines.append("- 暂无影子测试样本")

    # P2: Add state consistency diagnostics
    if state_consistency and not state_consistency.get("error"):
        summary = state_consistency.get("summary", {})
        total = state_consistency.get("total_issues", 0)
        if total > 0:
            lines.extend(["", "**状态一致性诊断**"])
            alert_parts = []
            if summary.get("duplicate_open_trades", 0) > 0:
                alert_parts.append(f"重复开仓={summary['duplicate_open_trades']}")
            if summary.get("orphan_patches", 0) > 0:
                alert_parts.append(f"孤儿补丁={summary['orphan_patches']}")
            if summary.get("status_mismatches", 0) > 0:
                alert_parts.append(f"状态不一致={summary['status_mismatches']}")
            if summary.get("duplicate_patches", 0) > 0:
                alert_parts.append(f"重复补丁={summary['duplicate_patches']}")
            if summary.get("stale_shadows", 0) > 0:
                alert_parts.append(f"过期影子={summary['stale_shadows']}")
            if summary.get("draft_limbo", 0) > 0:
                alert_parts.append(f"草稿滞留={summary['draft_limbo']}")
            if alert_parts:
                lines.append(f"- 发现问题 {total} 个：{'，'.join(alert_parts)}")
            else:
                lines.append(f"- 发现问题 {total} 个（非关键）")
        else:
            lines.extend(["", "**状态一致性诊断**", "- 全部正常，未发现状态不一致"])

    # P2-B: Add top failure patterns
    if feedback_patterns and not feedback_patterns.get("error"):
        top_patterns = feedback_patterns.get("top_patterns", [])
        most_active = feedback_patterns.get("most_active_skill")
        if top_patterns or most_active:
            lines.extend(["", "**八、本周失败模式（反馈记忆）**"])
            if top_patterns:
                for p in top_patterns:
                    lines.append(f"- {p['pattern']}：{p['count']} 次")
            else:
                lines.append("- 暂无失败模式记录")
            if most_active:
                lines.append(f"- 最活跃反馈 Skill：{most_active}（{feedback_patterns.get('most_active_count', 0)} 条）")

    # Account feedback gate stats
    if account_feedback_gate and not account_feedback_gate.get("error"):
        gate = account_feedback_gate
        if gate.get("total_checks", 0) > 0:
            lines.extend(["", "**账户反馈门禁（近 24 小时）**"])
            lines.append(
                f"- 总检查：{gate['total_checks']}；"
                f"门禁激活：{gate['active_checks']}；"
                f"未通过：{gate['not_passed']}"
            )
            if gate.get("invalid_json_count", 0) > 0:
                lines.append(f"- JSON 解析失败：{gate['invalid_json_count']} 条（有效：{gate.get('valid_checks', 0)}）")
            if gate.get("decision_counts"):
                decision_text = "，".join(f"{k}={v}" for k, v in gate["decision_counts"].items())
                lines.append(f"- 决策分布：{decision_text}")
            # Shadow projection (what WOULD have happened)
            shadow_proj = gate.get("shadow_projection", {})
            if any(shadow_proj.get(k, 0) > 0 for k in ("annotate_only", "downgrade_to_watch", "block_order")):
                sp = shadow_proj
                lines.append(
                    f"- 影子预判（会被执行的动作）：仅注释={sp.get('annotate_only', 0)}；"
                    f"降级观察={sp.get('downgrade_to_watch', 0)}；阻止={sp.get('block_order', 0)}；"
                    f"合计会被阻止={sp.get('total_blocked', 0)}"
                )
            # Controlled actual (what DID happen)
            controlled_act = gate.get("controlled_actual", {})
            if any(controlled_act.get(k, 0) > 0 for k in ("passed", "annotate_only", "downgrade_to_watch", "block_order")):
                ca = controlled_act
                lines.append(
                    f"- 受控实际（已执行的动作）：通过={ca.get('passed', 0)}；"
                    f"仅注释={ca.get('annotate_only', 0)}；降级观察={ca.get('downgrade_to_watch', 0)}；"
                    f"阻止={ca.get('block_order', 0)}"
                )
            if gate.get("controlled_gating_factors"):
                factor_text = "，".join(
                    f"{k}={v}" for k, v in gate["controlled_gating_factors"].items()
                )
                lines.append(f"  - 受阻因素：{factor_text}")

    lines.extend(["", "**九、风险事件**"])
    if failed_jobs:
        for job in failed_jobs[:5]:
            lines.append(f"- #{job['id']} {job['job_type']}：{(job.get('error_message') or '-')[:100]}")
    else:
        lines.append("- 暂无新的失败任务或风险事件")
    lines.append("")
    lines.append("不构成实盘建议，仅用于模拟盘与策略研究。")
    return "\n".join(lines)


def _duckdb_hourly_stats(generated_at_utc: str) -> dict[str, Any]:
    try:
        end = datetime.fromisoformat(generated_at_utc.replace("Z", "+00:00"))
        start = end - timedelta(hours=1)
        distribution = DuckDBAnalytics().hourly_signal_distribution(
            start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            end.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )
        return {"ok": True, "source": "duckdb", "signal_distribution": distribution}
    except Exception as exc:
        return {"ok": False, "source": "in_memory_fallback", "error": str(exc), "signal_distribution": {}}


def _fetch_risk_state(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Fetch current risk_off / daily_loss_pause state."""
    try:
        from plugins.crypto_guard.risk.account_risk_guard import AccountRiskGuard
        guard = AccountRiskGuard(repo)
        result = guard.check(symbol="BTCUSDT", side="LONG")
        return {
            "risk_off": result.get("risk_off", False),
            "hard_risk_off": result.get("hard_risk_off", False),
            "daily_loss_pause": result.get("daily_loss_pause", False),
            "drawdown_pct": result.get("drawdown_pct", 0),
            "effective_risk_percent": result.get("effective_risk_percent", 1.0),
        }
    except Exception as exc:
        return {"risk_off": False, "error": str(exc)}


def _fetch_shadow_data_quality(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Fetch shadow data quality (real_pnl vs pseudo_r counts)."""
    try:
        # Count real pnl vs pseudo_r in shadow evaluations
        # pnl_r = 0 is real data (breakeven), only NULL is pseudo
        real_count = _count(repo, """
            SELECT COUNT(*) FROM strategy_evaluations
            WHERE is_shadow = 1 AND pnl_r IS NOT NULL
        """)
        pseudo_count = _count(repo, """
            SELECT COUNT(*) FROM strategy_evaluations
            WHERE is_shadow = 1 AND pnl_r IS NULL
        """)
        total = real_count + pseudo_count
        return {
            "real_pnl_count": real_count,
            "pseudo_r_count": pseudo_count,
            "total_shadow_samples": total,
            "real_ratio": real_count / total if total > 0 else 0,
        }
    except Exception as exc:
        return {"error": str(exc), "real_pnl_count": 0, "pseudo_r_count": 0}


def _fetch_state_consistency(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Run state consistency diagnostics for the hourly report."""
    try:
        result = diagnose_state_consistency(repo)
        return {
            "ok": result["ok"],
            "summary": result["summary"],
            "total_issues": result["total_issues"],
            "issues": result["issues"],
        }
    except Exception as exc:
        return {"error": str(exc), "ok": True, "summary": {}, "total_issues": 0, "issues": []}


def _fetch_feedback_patterns(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Fetch top 3 failure patterns this week from skill_feedback_memory."""
    try:
        # Get feedback from last 7 days - use datetime() wrapper for consistent comparison
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        rows = repo.conn.execute(
            """
            SELECT pattern_type, COUNT(*) as count
            FROM skill_feedback_memory
            WHERE datetime(created_at) >= datetime(?)
              AND pattern_type IS NOT NULL AND pattern_type != ''
              AND status='candidate'
            GROUP BY pattern_type
            ORDER BY count DESC
            LIMIT 3
            """,
            (week_ago,),
        ).fetchall()

        top_patterns = [{"pattern": row["pattern_type"], "count": row["count"]} for row in rows]

        # Most active feedback skill (only candidate status)
        most_active = repo.conn.execute(
            """
            SELECT skill_name, COUNT(*) as count
            FROM skill_feedback_memory
            WHERE datetime(created_at) >= datetime(?)
              AND status='candidate'
            GROUP BY skill_name
            ORDER BY count DESC
            LIMIT 1
            """,
            (week_ago,),
        ).fetchone()

        return {
            "top_patterns": top_patterns,
            "most_active_skill": most_active["skill_name"] if most_active else None,
            "most_active_count": most_active["count"] if most_active else 0,
        }
    except Exception as exc:
        return {"error": str(exc), "top_patterns": [], "most_active_skill": None}


def _fetch_long_short_performance(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Fetch LONG vs SHORT performance breakdown."""
    try:
        # Get last 30 days performance - use datetime() wrapper for consistent comparison
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")

        long_stats = repo.conn.execute(
            """
            SELECT COUNT(*) as count,
                   AVG(pnl_r) as avg_r,
                   SUM(CASE WHEN pnl_r > 0.05 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl_r < -0.05 THEN 1 ELSE 0 END) as losses
            FROM paper_trades
            WHERE side = 'LONG' AND datetime(closed_at) >= datetime(?) AND pnl_r IS NOT NULL
            """,
            (thirty_days_ago,),
        ).fetchone()

        short_stats = repo.conn.execute(
            """
            SELECT COUNT(*) as count,
                   AVG(pnl_r) as avg_r,
                   SUM(CASE WHEN pnl_r > 0.05 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl_r < -0.05 THEN 1 ELSE 0 END) as losses
            FROM paper_trades
            WHERE side = 'SHORT' AND datetime(closed_at) >= datetime(?) AND pnl_r IS NOT NULL
            """,
            (thirty_days_ago,),
        ).fetchone()

        return {
            "long": {
                "count": long_stats["count"] if long_stats else 0,
                "avg_r": float(long_stats["avg_r"] or 0) if long_stats else 0,
                "wins": long_stats["wins"] if long_stats else 0,
                "losses": long_stats["losses"] if long_stats else 0,
            },
            "short": {
                "count": short_stats["count"] if short_stats else 0,
                "avg_r": float(short_stats["avg_r"] or 0) if short_stats else 0,
                "wins": short_stats["wins"] if short_stats else 0,
                "losses": short_stats["losses"] if short_stats else 0,
            },
        }
    except Exception as exc:
        return {"error": str(exc), "long": {}, "short": {}}


def _fetch_account_feedback_gate_stats(repo: CryptoGuardRepository) -> dict[str, Any]:
    """Fetch account feedback gate statistics from recent GA decisions.

    Separates shadow projections (what WOULD have happened) from controlled actuals
    (what DID happen). Only counts controlled_projection for shadow-mode records.
    """
    try:
        day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        rows = repo.conn.execute(
            """
            SELECT account_feedback_gate_json
            FROM ga_decisions
            WHERE datetime(created_at) >= datetime(?) AND account_feedback_gate_json IS NOT NULL
            """,
            (day_ago,),
        ).fetchall()

        if not rows:
            return {"ok": True, "total_checks": 0, "active_checks": 0, "not_passed": 0, "decision_counts": {}}

        total = len(rows)
        active = 0
        not_passed = 0
        decision_counts: dict[str, int] = {}
        controlled_gating_factors: dict[str, int] = {}
        # Shadow projection (what WOULD have happened)
        shadow_projection_annotate_only = 0
        shadow_projection_downgrade_to_watch = 0
        shadow_projection_block_order = 0
        shadow_projection_controlled_blocked = 0
        # Controlled actual (what DID happen)
        controlled_actual_passed = 0
        controlled_actual_annotate_only = 0
        controlled_actual_downgrade_to_watch = 0
        controlled_actual_block_order = 0
        valid_checks = 0
        invalid_json_count = 0

        for row in rows:
            try:
                gate = json.loads(row["account_feedback_gate_json"])
            except (json.JSONDecodeError, TypeError):
                invalid_json_count += 1
                continue

            valid_checks += 1

            if gate.get("active"):
                active += 1
            if gate.get("passed") is False:
                not_passed += 1
            decision = gate.get("decision", "unknown")
            decision_counts[decision] = decision_counts.get(decision, 0) + 1

            mode = gate.get("mode", "shadow")

            if mode == "shadow":
                # Shadow mode: extract controlled_projection (what WOULD have happened)
                controlled_proj = gate.get("controlled_projection", {})
                if controlled_proj:
                    would_decide = controlled_proj.get("would_decide", "")
                    if would_decide == "annotate_only":
                        shadow_projection_annotate_only += 1
                    elif would_decide == "downgrade_to_watch":
                        shadow_projection_downgrade_to_watch += 1
                    elif would_decide == "block_order":
                        shadow_projection_block_order += 1

                    if not controlled_proj.get("would_pass"):
                        shadow_projection_controlled_blocked += 1
                        gating_factor = controlled_proj.get("gating_factor", "unknown")
                        controlled_gating_factors[gating_factor] = controlled_gating_factors.get(gating_factor, 0) + 1
            else:
                # Controlled mode: count actual decisions (what DID happen)
                actual_decision = gate.get("decision", "")
                if actual_decision == "passed":
                    controlled_actual_passed += 1
                elif actual_decision == "annotate_only":
                    controlled_actual_annotate_only += 1
                elif actual_decision == "downgrade_to_watch":
                    controlled_actual_downgrade_to_watch += 1
                elif actual_decision == "block_order":
                    controlled_actual_block_order += 1

        return {
            "ok": True,
            "total_checks": total,
            "valid_checks": valid_checks,
            "invalid_json_count": invalid_json_count,
            "active_checks": active,
            "not_passed": not_passed,
            "decision_counts": decision_counts,
            # Legacy fields for backward compatibility (shadow projection)
            "controlled_blocked": shadow_projection_downgrade_to_watch + shadow_projection_block_order,
            "projected_annotate_only": shadow_projection_annotate_only,
            "projected_downgrade_to_watch": shadow_projection_downgrade_to_watch,
            "projected_block_order": shadow_projection_block_order,
            "controlled_gating_factors": controlled_gating_factors,
            # New: shadow projection breakdown
            "shadow_projection": {
                "annotate_only": shadow_projection_annotate_only,
                "downgrade_to_watch": shadow_projection_downgrade_to_watch,
                "block_order": shadow_projection_block_order,
                "total_blocked": shadow_projection_controlled_blocked,
            },
            # New: controlled actual breakdown
            "controlled_actual": {
                "passed": controlled_actual_passed,
                "annotate_only": controlled_actual_annotate_only,
                "downgrade_to_watch": controlled_actual_downgrade_to_watch,
                "block_order": controlled_actual_block_order,
            },
        }
    except Exception as exc:
        return {"error": str(exc), "total_checks": 0}


def render_hourly_report_text(
    generated_at_utc: str,
    active_symbols: list[str],
    signals: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    failed_jobs: list[dict[str, Any]],
    queue_counts: dict[str, int],
    agent_brief: dict[str, Any] | None = None,
    analysis_states: list[dict[str, Any]] | None = None,
    equity_snapshot: dict[str, Any] | None = None,
    risk_state: dict[str, Any] | None = None,
    shadow_data_quality: dict[str, Any] | None = None,
    feedback_patterns: dict[str, Any] | None = None,
    long_short_performance: dict[str, Any] | None = None,
    account_feedback_gate: dict[str, Any] | None = None,
    state_consistency: dict[str, Any] | None = None,
) -> str:
    signal_by_symbol = {s["symbol"]: s for s in signals}
    state_by_symbol: dict[str, dict[str, Any]] = {}
    for item in analysis_states or []:
        symbol = item.get("symbol")
        if symbol and symbol not in state_by_symbol:
            state_by_symbol[symbol] = item.get("state") or _safe_json(item.get("state_json"), {})
    orders_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for order in open_orders:
        orders_by_symbol.setdefault(order["symbol"], []).append(order)
    lines = [
        "**CryptoGuard 每小时简报**",
        f"北京时间（UTC+8）：{_format_time_utc8(generated_at_utc)}",
        f"UTC 时间：{generated_at_utc}",
        "",
        "**产品分析概览：**",
    ]

    # P2-B: Add risk_off state
    if risk_state:
        risk_status = []
        if risk_state.get("risk_off"):
            risk_status.append("risk_off")
        if risk_state.get("hard_risk_off"):
            risk_status.append("hard_risk_off")
        if risk_state.get("daily_loss_pause"):
            risk_status.append("daily_loss_pause")
        if risk_status:
            lines.append(f"- **风险状态：{', '.join(risk_status)}**（回撤 {risk_state.get('drawdown_pct', 0):.1f}%）")
        else:
            lines.append("- 风险状态：正常")

    if agent_brief and agent_brief.get("summary"):
        lines.extend(["**GA/LLM 巡航摘要：**", str(agent_brief["summary"]), ""])
    if not active_symbols:
        lines.append("- 暂无启用产品")
    for symbol in active_symbols[:30]:
        signal = signal_by_symbol.get(symbol)
        if not signal:
            lines.append(f"- {symbol}：暂无分析记录")
            continue
        lines.extend(_signal_report_lines(symbol, signal, orders_by_symbol.get(symbol, []), state_by_symbol.get(symbol)))
    if len(active_symbols) > 30:
        lines.append(f"- 其余 {len(active_symbols) - 30} 个产品略。")

    lines.extend(["", "**模拟盘持仓/订单：**"])
    if not open_orders:
        lines.append("- 当前无 pending/open 模拟盘订单")
    else:
        for order in open_orders[:20]:
            tps = _safe_json(order.get("take_profit_json"), [])
            tp_text = ", ".join(str(tp.get("price")) for tp in tps if isinstance(tp, dict)) or "-"
            lines.append(
                f"- #{order['id']} {order['symbol']} {order['side']} {order['status']} "
                f"entry={order.get('entry_price') or order.get('trigger_price') or '-'} "
                f"SL={order.get('stop_loss') or '-'} TP={tp_text}"
            )

    # P2-B: Add LONG vs SHORT performance
    if long_short_performance and not long_short_performance.get("error"):
        long = long_short_performance.get("long", {})
        short = long_short_performance.get("short", {})
        if long.get("count", 0) > 0 or short.get("count", 0) > 0:
            lines.extend(["", "**模拟盘方向表现（近 30 天）**"])
            if long.get("count", 0) > 0:
                win_rate = long["wins"] / long["count"] * 100 if long["count"] > 0 else 0
                lines.append(f"- LONG：{long['count']} 笔，胜率 {win_rate:.0f}%，avg R={long['avg_r']:.2f}")
            if short.get("count", 0) > 0:
                win_rate = short["wins"] / short["count"] * 100 if short["count"] > 0 else 0
                lines.append(f"- SHORT：{short['count']} 笔，胜率 {win_rate:.0f}%，avg R={short['avg_r']:.2f}")

    lines.extend(["", "**净值曲线摘要：**"])
    if equity_snapshot:
        try:
            snap = _safe_json(equity_snapshot.get("snapshot_json"), {}) or equity_snapshot
            lines.append(
                f"- 当前权益：{float(equity_snapshot.get('account_equity') or 0):.2f}；"
                f"未实现盈亏：{float(equity_snapshot.get('unrealized_pnl') or 0):.2f}；"
                f"已实现盈亏：{float(equity_snapshot.get('realized_pnl') or 0):.2f}；"
                f"回撤：{float(snap.get('drawdown_percent') or 0):.2f}%"
            )
        except Exception:
            lines.append("- 暂无可解析净值快照")
    else:
        lines.append("- 暂无净值快照")

    # P2-B: Add shadow data quality
    if shadow_data_quality and not shadow_data_quality.get("error"):
        total = shadow_data_quality.get("total_shadow_samples", 0)
        if total > 0:
            real_ratio = shadow_data_quality.get("real_ratio", 0) * 100
            lines.extend(["", "**影子测试数据质量：**"])
            lines.append(
                f"- 样本总数：{total}；"
                f"真实 PnL：{shadow_data_quality.get('real_pnl_count', 0)}（{real_ratio:.0f}%）；"
                f"伪 R：{shadow_data_quality.get('pseudo_r_count', 0)}"
            )

    # P2: Add state consistency diagnostics
    if state_consistency and not state_consistency.get("error"):
        summary = state_consistency.get("summary", {})
        total = state_consistency.get("total_issues", 0)
        if total > 0:
            lines.extend(["", "**状态一致性诊断：**"])
            alert_parts = []
            if summary.get("duplicate_open_trades", 0) > 0:
                alert_parts.append(f"重复开仓={summary['duplicate_open_trades']}")
            if summary.get("orphan_patches", 0) > 0:
                alert_parts.append(f"孤儿补丁={summary['orphan_patches']}")
            if summary.get("status_mismatches", 0) > 0:
                alert_parts.append(f"状态不一致={summary['status_mismatches']}")
            if summary.get("duplicate_patches", 0) > 0:
                alert_parts.append(f"重复补丁={summary['duplicate_patches']}")
            if summary.get("stale_shadows", 0) > 0:
                alert_parts.append(f"过期影子={summary['stale_shadows']}")
            if summary.get("draft_limbo", 0) > 0:
                alert_parts.append(f"草稿滞留={summary['draft_limbo']}")
            if alert_parts:
                lines.append(f"- 发现问题 {total} 个：{'，'.join(alert_parts)}")
            else:
                lines.append(f"- 发现问题 {total} 个（非关键）")
        else:
            lines.extend(["", "**状态一致性诊断：**", "- 全部正常，未发现状态不一致"])

    # P2-B: Add top failure patterns
    if feedback_patterns and not feedback_patterns.get("error"):
        top_patterns = feedback_patterns.get("top_patterns", [])
        most_active = feedback_patterns.get("most_active_skill")
        if top_patterns or most_active:
            lines.extend(["", "**本周失败模式（反馈记忆）**"])
            if top_patterns:
                for p in top_patterns:
                    lines.append(f"- {p['pattern']}：{p['count']} 次")
            else:
                lines.append("- 暂无失败模式记录")
            if most_active:
                lines.append(f"- 最活跃反馈 Skill：{most_active}（{feedback_patterns.get('most_active_count', 0)} 条）")

    # Account feedback gate stats
    if account_feedback_gate and not account_feedback_gate.get("error"):
        gate = account_feedback_gate
        if gate.get("total_checks", 0) > 0:
            lines.extend(["", "**账户反馈门禁（近 24 小时）**"])
            lines.append(
                f"- 总检查：{gate['total_checks']}；"
                f"门禁激活：{gate['active_checks']}；"
                f"未通过：{gate['not_passed']}"
            )
            if gate.get("invalid_json_count", 0) > 0:
                lines.append(f"- JSON 解析失败：{gate['invalid_json_count']} 条（有效：{gate.get('valid_checks', 0)}）")
            if gate.get("decision_counts"):
                decision_text = "，".join(f"{k}={v}" for k, v in gate["decision_counts"].items())
                lines.append(f"- 决策分布：{decision_text}")
            # Shadow projection (what WOULD have happened)
            shadow_proj = gate.get("shadow_projection", {})
            if any(shadow_proj.get(k, 0) > 0 for k in ("annotate_only", "downgrade_to_watch", "block_order")):
                sp = shadow_proj
                lines.append(
                    f"- 影子预判（会被执行的动作）：仅注释={sp.get('annotate_only', 0)}；"
                    f"降级观察={sp.get('downgrade_to_watch', 0)}；阻止={sp.get('block_order', 0)}；"
                    f"合计会被阻止={sp.get('total_blocked', 0)}"
                )
            # Controlled actual (what DID happen)
            controlled_act = gate.get("controlled_actual", {})
            if any(controlled_act.get(k, 0) > 0 for k in ("passed", "annotate_only", "downgrade_to_watch", "block_order")):
                ca = controlled_act
                lines.append(
                    f"- 受控实际（已执行的动作）：通过={ca.get('passed', 0)}；"
                    f"仅注释={ca.get('annotate_only', 0)}；降级观察={ca.get('downgrade_to_watch', 0)}；"
                    f"阻止={ca.get('block_order', 0)}"
                )
            if gate.get("controlled_gating_factors"):
                factor_text = "，".join(
                    f"{k}={v}" for k, v in gate["controlled_gating_factors"].items()
                )
                lines.append(f"  - 受阻因素：{factor_text}")

    lines.extend(["", "**队列：**", f"- 用户待处理：{queue_counts['pending_user']}", f"- 后台待处理：{queue_counts['pending_background']}", f"- 运行中：{queue_counts['running']}"])
    health = "正常" if not failed_jobs and queue_counts.get("running", 0) < 5 else "需关注"
    lines.extend(["", "**系统健康度：**", f"- 状态：{health}", f"- 飞书 outbox/队列：用户 {queue_counts['pending_user']}，后台 {queue_counts['pending_background']}，运行中 {queue_counts['running']}"])
    if failed_jobs:
        lines.extend(["", "**最近失败任务：**"])
        for job in failed_jobs:
            err = (job.get("error_message") or "")[:120]
            lines.append(f"- #{job['id']} {job['job_type']}：{err}")
    lines.append("")
    lines.append("不构成实盘建议，仅用于模拟盘与策略研究。")
    return "\n".join(lines)


def _count(repo: CryptoGuardRepository, sql: str) -> int:
    return int(repo.conn.execute(sql).fetchone()[0])


def _decision_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = _safe_json(row.get("raw_decision_json"), {})
    return {
        "ga_decision_id": row.get("id"),
        "symbol": row.get("symbol"),
        "decision": row.get("decision"),
        "legacy_decision": raw.get("legacy_decision"),
        "signal_grade": row.get("signal_grade"),
        "confidence": row.get("confidence"),
        "market_bias": row.get("market_bias"),
        "trend_stage": row.get("trend_stage"),
        "final_summary": row.get("final_summary"),
        "risk_check": _safe_json(row.get("risk_check_json"), {}),
        "feishu_actions": _safe_json(row.get("feishu_actions_json"), []),
    }


def _decision_text(value: Any) -> str:
    mapping = {
        "create_paper_order": "模拟盘候选",
        "opportunity_watch": "等待触发",
        "monitor_only": "仅观察",
        "no_edge": "无明显优势",
        "close_position": "平仓候选",
        "adjust_stop_loss": "调整止损",
        "hold_position": "继续持有",
    }
    return mapping.get(str(value or ""), str(value or "-"))


def _agent_hourly_brief(
    active_symbols: list[str],
    signals: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    failed_jobs: list[dict[str, Any]],
    queue_counts: dict[str, int],
) -> dict[str, Any]:
    fallback = {
        "summary": "本小时巡航已完成，详见各产品趋势状态、机会判断与风险说明。",
        "focus_symbols": [],
        "why_no_opportunity": [],
        "next_checks": [],
    }
    try:
        from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task

        compact_signals = []
        for signal in signals[:30]:
            decision = _safe_json(signal.get("ga_decision_json"), {}) or signal
            compact_signals.append(
                {
                    "symbol": signal.get("symbol"),
                    "decision": decision.get("decision"),
                    "signal_grade": decision.get("signal_grade"),
                    "confidence": decision.get("confidence"),
                    "trend_stage": decision.get("trend_stage"),
                    "market_bias": decision.get("market_bias"),
                    "summary": decision.get("summary"),
                    "counter_evidence": decision.get("counter_evidence"),
                    "analysis_source": decision.get("analysis_source"),
                    "llm_status": decision.get("llm_status"),
                }
            )
        return run_agent_json_task(
            task_name="hourly_alert_quality_brief",
            payload={
                "active_symbols": active_symbols,
                "latest_signals": compact_signals,
                "open_orders": open_orders[:20],
                "failed_jobs": failed_jobs,
                "queue_counts": queue_counts,
            },
            fallback=fallback,
            instructions=[
                "总结本小时各产品趋势状态、为什么有/没有机会、下一小时应重点观察什么。",
                "不要输出实盘建议。",
                "summary 字段应适合放在飞书简报顶部。",
            ],
        )
    except Exception:
        return fallback


def _signal_report_lines(symbol: str, signal: dict[str, Any], open_orders: list[dict[str, Any]] | None = None, analysis_state: dict[str, Any] | None = None) -> list[str]:
    decision_json = _safe_json(signal.get("ga_decision_json"), {})
    decision = decision_json if isinstance(decision_json, dict) and decision_json else signal
    grade = decision.get("signal_grade") or signal.get("signal_grade") or "-"
    confidence = decision.get("confidence", signal.get("confidence"))
    confidence_text = f"{float(confidence) * 100:.0f}%" if confidence is not None else "-"
    decision_name = decision.get("decision") or signal.get("decision") or "unknown"
    trend = decision.get("trend_stage") or signal.get("trend_stage") or "-"
    bias = decision.get("market_bias") or signal.get("direction") or "-"
    conclusion = _analysis_conclusion(symbol, decision)
    lines = [
        f"- **{symbol}**：{decision_name}，等级 {grade}，置信度 {confidence_text}",
        f"  - 研判来源：{_analysis_source_text(decision)}",
        f"  - 趋势状态：{trend}；市场倾向：{bias}",
        f"  - GA 分析结论：{conclusion}",
    ]
    profiles = _profile_summary(decision)
    if profiles:
        lines.append(f"  - 多周期：{profiles}")
    opportunity = _opportunity_summary(decision)
    lines.append(f"  - 机会判断：{opportunity}")
    plan = _trade_plan_summary(decision)
    lines.append(f"  - 交易计划：{plan}")
    position = _position_summary(open_orders or [])
    lines.append(f"  - 持仓/订单：{position}")
    no_opportunity = _no_opportunity_reason(decision)
    if no_opportunity:
        lines.append(f"  - 暂无机会原因：{no_opportunity}")
    if analysis_state:
        lines.extend(_analysis_state_report_lines(analysis_state))
    counter = _compact_items(decision.get("counter_evidence") or _safe_json(signal.get("risk_notes"), []), max_items=2)
    if counter:
        lines.append(f"  - 反向证据/风险：{counter}")
    return lines


def _trade_plan_summary(decision: dict[str, Any]) -> str:
    plan = decision.get("trade_plan")
    risk = decision.get("risk_check") or {}
    if decision.get("has_trade_plan") and isinstance(plan, dict):
        tps = _compact_items([tp.get("price") for tp in plan.get("take_profits", []) if isinstance(tp, dict)], max_items=3)
        return f"{plan.get('side')} {plan.get('entry_type')}，入场 {plan.get('entry_price') or plan.get('trigger_price')}，止损 {plan.get('stop_loss')}，止盈 {tps or '-'}，风控={'通过' if risk.get('ok') else '未通过'}"
    if risk.get("reasons"):
        return "无可执行模拟盘计划；风控原因：" + "；".join(str(x) for x in risk.get("reasons", [])[:2])
    return "暂无完整交易计划。"


def _position_summary(open_orders: list[dict[str, Any]]) -> str:
    if not open_orders:
        return "无 pending/open 模拟盘订单。"
    parts = []
    for order in open_orders[:3]:
        tps = _safe_json(order.get("take_profit_json"), [])
        tp_text = _compact_items([tp.get("price") for tp in tps if isinstance(tp, dict)], max_items=2)
        parts.append(
            f"#{order['id']} {order['side']} {order['status']} 入场={order.get('entry_price') or order.get('trigger_price') or '-'} "
            f"SL={order.get('stop_loss') or '-'} TP={tp_text or '-'}"
        )
    return "；".join(parts)


def _analysis_conclusion(symbol: str, decision: dict[str, Any]) -> str:
    summary = decision.get("summary")
    grade = str(decision.get("signal_grade") or "D").upper()
    decision_name = decision.get("decision")
    has_tp = decision.get("has_trade_plan")

    if summary and decision_name == "trade_plan_available":
        return str(summary)
    if decision_name == "trade_plan_available" and has_tp:
        return f"{symbol} 有完整模拟盘计划（{grade}级），按失效位执行。"
    if decision_name and str(decision_name).startswith("wait_for"):
        return f"{symbol} 有方向倾向（{grade}级），等待触发条件确认。"
    if decision_name == "opportunity_watch" and grade in {"S", "A"}:
        return f"{symbol} {grade}级机会，可加入机会监控或模拟盘。"
    if decision_name == "monitor_only":
        return f"{symbol} 仅适合观察，优势不足以生成模拟盘计划。"
    if summary:
        return str(summary)
    return f"{symbol} 当前无明显优势，系统仅记录本次分析。"


def _analysis_source_text(decision: dict[str, Any]) -> str:
    source = decision.get("analysis_source")
    status = decision.get("llm_status")
    if source == "llm_agent" and status == "ok":
        return "LLM/GA Agent"
    if source == "deterministic_fallback":
        return "LLM/GA 失败后规则降级"
    if source == "deterministic_sop":
        return "规则 SOP"
    return "GA SOP"


def _profile_summary(decision: dict[str, Any]) -> str:
    profiles = decision.get("profiles") or {}
    if not isinstance(profiles, dict):
        return ""
    parts = []
    for tf in ("1d", "4h", "1h", "15m", "5m"):
        profile = profiles.get(tf)
        if not isinstance(profile, dict):
            continue
        stage = profile.get("trend_stage") or "-"
        structure = profile.get("market_structure") or "-"
        momentum = profile.get("momentum") or "-"
        parts.append(f"{tf}={structure}/{stage}/{momentum}")
    return "；".join(parts[:5])


def _opportunity_summary(decision: dict[str, Any]) -> str:
    grade = str(decision.get("signal_grade") or "D").upper()
    if decision.get("has_trade_plan") and decision.get("trade_plan"):
        plan = decision["trade_plan"]
        return f"{grade}级模拟盘计划，方向 {plan.get('side')}，entry={plan.get('entry_price') or plan.get('trigger_price')}，SL={plan.get('stop_loss')}"
    watch = decision.get("opportunity_watch")
    if isinstance(watch, dict) and watch.get("needed"):
        conditions = _compact_items(watch.get("conditions") or [], max_items=2)
        return f"{grade}级机会监控，方向 {watch.get('direction') or '-'}；条件：{conditions or watch.get('reason') or '-'}"
    actions = decision.get("suggested_actions") or []
    if "create_opportunity_watch" in actions:
        return f"{grade}级可观察，但尚未形成完整模拟盘计划。"
    return f"{grade}级暂无可执行机会。"


def _no_opportunity_reason(decision: dict[str, Any]) -> str:
    if decision.get("has_trade_plan"):
        return ""
    reasons: list[str] = []
    grade = str(decision.get("signal_grade") or "")
    confidence = decision.get("confidence")
    trend_stage = decision.get("trend_stage")
    if grade in {"C", "D"}:
        reasons.append(f"等级 {grade} 低于推送/执行阈值")
    if confidence is not None and float(confidence) < 0.65:
        reasons.append(f"置信度 {float(confidence) * 100:.0f}% 未达到 B 级观察阈值")
    if trend_stage == "range":
        reasons.append("震荡区间内不强行判断趋势")
    elif trend_stage == "late":
        reasons.append("趋势末端，追价信号降级")
    policy = ((decision.get("modules") or {}).get("trend_stage") or {}).get("strategy_policy")
    if policy == "filter_trend_strategy":
        reasons.append("多周期偏震荡，趋势策略被过滤")
    elif policy == "downgrade_chasing_signal":
        reasons.append("趋势阶段策略要求降级追单")
    counter = decision.get("counter_evidence") or []
    if counter:
        reasons.append(str(counter[0]))
    return "；".join(_dedupe(reasons)[:4])


def _compact_items(items: Any, max_items: int = 3) -> str:
    if isinstance(items, str):
        return items
    if not isinstance(items, list):
        return ""
    values = [str(x) for x in items if x not in (None, "")]
    return "；".join(_dedupe(values)[:max_items])


def _analysis_state_report_lines(state: dict[str, Any]) -> list[str]:
    market_structure = state.get("market_structure") or {}
    clarity = state.get("trend_clarity") or {}
    no_trade = state.get("no_trade_reason") or {}
    key_levels = state.get("key_levels") or {}
    next_analysis = state.get("next_analysis") or {}
    boundary = key_levels.get("breakout_boundary") or {}
    permission = state.get("trade_permission") or {}
    breakout_watch = state.get("breakout_watch") or {}
    triggers = state.get("next_triggers") or []
    support = _compact_items([_format_level(x) for x in key_levels.get("support") or []], max_items=3)
    resistance = _compact_items([_format_level(x) for x in key_levels.get("resistance") or []], max_items=3)
    trigger_text = _compact_items([t.get("condition") if isinstance(t, dict) else t for t in triggers], max_items=3)
    reason_text = _compact_items(clarity.get("reason") or [], max_items=3)
    permission_text = "允许" if permission.get("paper_trade_allowed") else "不允许"
    watch_text = "建议" if state.get("opportunity_watch_recommended") else "不建议"
    next_time = next_analysis.get("suggested_time_utc")
    next_reason = str(next_analysis.get("reason") or "-").replace("15m/15m", "15m")
    no_trade_text = "已有候选交易计划，等待风控和触发确认。"
    if no_trade.get("has_no_trade"):
        no_trade_text = f"{no_trade.get('reason_code') or '-'}：{no_trade.get('detail') or '-'}"
    return [
        (
            "  - 市场结构状态："
            f"状态={market_structure.get('structure_status') or '-'}；"
            f"日线={market_structure.get('direction_1d') or '-'}；"
            f"4H={market_structure.get('direction_4h') or '-'}；"
            f"1H趋势={market_structure.get('trend_1h') or '-'}；"
            f"15M结构={market_structure.get('structure_15m') or '-'}；"
            f"5M触发={market_structure.get('trigger_5m') or '-'}"
        ),
        f"  - 趋势清晰度：{float(clarity.get('score') or 0) * 100:.0f}%（{_clarity_text(clarity.get('level'))}）；原因：{reason_text or '-'}",
        f"  - 无交易机会归因：{no_trade_text}",
        f"  - 关键关注点位：支撑={support or '-'}；阻力={resistance or '-'}；失效位={_format_level(key_levels.get('invalid_level'))}",
        f"  - 下次触发条件：{trigger_text or '-'}",
        f"  - 下次分析时间：{_format_time_utc8(next_time)}（UTC {next_time or '-'}）；{next_reason}",
        (
            "  - 等待突破边界："
            f"上沿={_format_level(boundary.get('upper'))}；"
            f"下沿={_format_level(boundary.get('lower'))}；"
            f"确认要求={breakout_watch.get('confirmation_required') or '-'}"
        ),
        f"  - 模拟盘权限：{permission_text}；原因={permission.get('reason') or '-'}",
        f"  - 机会监控建议：{watch_text}",
    ]


def _clarity_text(level: Any) -> str:
    mapping = {"clear": "清晰", "mixed": "分歧", "unclear": "不清晰"}
    return mapping.get(str(level or ""), str(level or "-"))


def _format_level(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):g}"
    except Exception:
        return str(value)


def _format_time_utc8(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value) / 1000, timezone.utc)
        else:
            text = str(value)
            if text.isdigit():
                dt = datetime.fromtimestamp(int(text) / 1000, timezone.utc)
            else:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (dt.astimezone(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    except Exception:
        return str(value)


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _safe_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default
