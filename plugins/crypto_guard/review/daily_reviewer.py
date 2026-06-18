from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers
from plugins.crypto_guard.review.trade_reviewer import review_trade
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def run_daily_review(repo: CryptoGuardRepository, *, day_utc: str | None = None, force: bool = False) -> dict[str, Any]:
    start, end = _review_window(day_utc)
    report_date = start[:10]

    # Idempotency: if report already exists and not forced, return existing
    existing = repo.conn.execute(
        "SELECT id, summary_json, ga_report, pushed_to_feishu FROM daily_review_reports WHERE review_date=?",
        (report_date,),
    ).fetchone()
    if existing and not force:
        import json
        summary = json.loads(existing["summary_json"] or "{}")
        return {
            "ok": True,
            "idempotent": True,
            "existing": True,
            "day_start_utc": start,
            "day_end_utc": end,
            "daily_review_report_id": int(existing["id"]),
            "text": existing["ga_report"],
            "summary": summary,
            "pushed_to_feishu": bool(existing["pushed_to_feishu"]),
        }

    # If force=True and existing report, archive old skill_feedback_memory
    if force and existing:
        repo.conn.execute(
            "UPDATE skill_feedback_memory SET status='archived' WHERE source_type='daily_review' AND finding LIKE ?",
            (f"每日复盘：%{report_date}%",),
        )

    trades = repo.list_closed_trades_for_review(start_utc=start, end_utc=end, only_unreviewed=True)
    reviewed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for trade in trades:
        try:
            reviewed.append(review_trade(repo, int(trade["id"])))
        except Exception as exc:
            errors.append({"trade_id": trade.get("id"), "error": str(exc)})

    all_window_trades = repo.list_closed_trades_for_review(start_utc=start, end_utc=end, only_unreviewed=False)
    memory = repo.strategy_memory_top(limit=8)
    evolution = evaluate_evolution_triggers(repo)
    fallback_summary = _summary(start, end, all_window_trades, reviewed, errors, memory)
    agent = run_agent_json_task(
        task_name="daily_paper_review_summary",
        payload={
            "window": {"start_utc": start, "end_utc": end},
            "trades": all_window_trades[:50],
            "new_reviews": reviewed[:50],
            "errors": errors,
            "strategy_memory": memory,
            "evolution": evolution,
        },
        fallback={
            "summary_text": fallback_summary,
            "key_findings": [],
            "strategy_actions": [],
            "risk_focus": [],
        },
        instructions=[
            "总结昨日 UTC 模拟盘表现、亏损原因、策略表现和下一步 candidate/shadow 事项。",
            "输出 summary_text 字段，适合直接推送飞书。",
            "不要建议实盘交易。",
        ],
    )
    summary = str(agent.get("summary_text") or fallback_summary)
    skill_updates = _write_skill_memory_updates(repo, all_window_trades, reviewed, evolution)
    report_date = start[:10]
    paper_summary = _paper_summary(all_window_trades)
    report_id = repo.save_daily_review_report(
        review_date=report_date,
        summary={
            "date_utc": report_date,
            "paper_summary": paper_summary,
            "win_analysis": [r for r in reviewed if (r.get("review") or {}).get("result") == "win"],
            "loss_analysis": [r for r in reviewed if (r.get("review") or {}).get("result") == "loss"],
            "analysis_failures": agent.get("analysis_failures", []),
            "next_focus_points": agent.get("risk_focus", []),
            "skill_memory_updates": skill_updates,
            "evolution": evolution,
        },
        ga_report=summary,
        skill_updates=skill_updates,
        evolution_actions=evolution,
    )
    return {
        "ok": not errors,
        "day_start_utc": start,
        "day_end_utc": end,
        "closed_trades": len(all_window_trades),
        "new_reviews": len(reviewed),
        "errors": errors,
        "strategy_memory": memory,
        "daily_review_report_id": report_id,
        "skill_memory_updates": skill_updates,
        "evolution": evolution,
        "agent_summary": agent,
        "text": summary,
    }


def _review_window(day_utc: str | None) -> tuple[str, str]:
    if day_utc:
        day = datetime.strptime(day_utc, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=1)
    start = day.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    end = (day + timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return start, end


def _summary(
    start: str,
    end: str,
    trades: list[dict[str, Any]],
    reviewed: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    memory: list[dict[str, Any]],
) -> str:
    pnl_rs = [float(t.get("pnl_r") or 0) for t in trades]
    wins = len([x for x in pnl_rs if x > 0.05])
    losses = len([x for x in pnl_rs if x < -0.05])
    breakeven = len(pnl_rs) - wins - losses
    avg_r = sum(pnl_rs) / len(pnl_rs) if pnl_rs else 0.0
    lines = [
        "**CryptoGuard 每日模拟盘复盘**",
        f"窗口：{start} ~ {end}",
        "",
        "**交易概览：**",
        f"- 平仓交易：{len(trades)}",
        f"- 新增复盘：{len(reviewed)}",
        f"- 胜 / 负 / 平：{wins} / {losses} / {breakeven}",
        f"- 平均 R：{avg_r:.2f}",
    ]

    if trades:
        lines.append("")
        lines.append("**平仓明细：**")
        for trade in trades[:20]:
            lines.append(
                f"- #{trade['id']} {trade['symbol']} {trade['side']} "
                f"R={float(trade.get('pnl_r') or 0):.2f} reason={trade.get('close_reason') or '-'}"
            )

    if reviewed:
        lines.append("")
        lines.append("**新增归因：**")
        for item in reviewed[:20]:
            review = item.get("review", {})
            lines.append(f"- trade #{review.get('trade_id')}：{review.get('primary_reason')}，{review.get('summary')}")

    if memory:
        lines.append("")
        lines.append("**策略记忆 Top：**")
        for row in memory[:8]:
            lines.append(
                f"- {row.get('strategy_name')} / {row.get('condition_hash')}："
                f"样本 {row.get('sample_count')}，胜 {row.get('win_count')}，负 {row.get('loss_count')}，avgR={float(row.get('avg_rr') or 0):.2f}"
            )

    if errors:
        lines.append("")
        lines.append("**异常：**")
        for err in errors:
            lines.append(f"- trade #{err.get('trade_id')}：{err.get('error')}")

    lines.append("")
    lines.append("所有策略补丁仍只进入 candidate，不会直接 active。不构成实盘建议。")
    return "\n".join(lines)


def _paper_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = [float(t.get("pnl") or 0) for t in trades]
    pnl_r = [float(t.get("pnl_r") or 0) for t in trades]
    wins = len([x for x in pnl_r if x > 0.05])
    losses = len([x for x in pnl_r if x < -0.05])
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "daily_pnl": sum(pnl),
        "avg_r": sum(pnl_r) / len(pnl_r) if pnl_r else 0.0,
        "max_drawdown": min([float(t.get("max_adverse_excursion") or 0) for t in trades], default=0.0),
    }


def _write_skill_memory_updates(
    repo: CryptoGuardRepository,
    trades: list[dict[str, Any]],
    reviewed: list[dict[str, Any]],
    evolution: dict[str, Any],
) -> list[dict[str, Any]]:
    from plugins.crypto_guard.review.loss_classifier import classify_trade

    updates: list[dict[str, Any]] = []
    losses = [r for r in reviewed if (r.get("review") or {}).get("result") == "loss"]

    # Build trade lookup by id
    trade_by_id = {t.get("id"): t for t in trades}

    # Classify losses by failure pattern
    pattern_groups: dict[str, list[dict[str, Any]]] = {}
    for loss in losses:
        # trade_id is inside the review dict, not at top level
        review = loss.get("review") or {}
        trade_id = loss.get("trade_id") or review.get("trade_id")
        trade = trade_by_id.get(trade_id) or loss
        pattern = classify_trade(trade)
        pattern_groups.setdefault(pattern, []).append(trade)

    # Write one entry per pattern (not per skill)
    for pattern, pattern_trades in pattern_groups.items():
        affected_symbols = list({t.get("symbol") for t in pattern_trades if t.get("symbol")})
        affected_sides = list({t.get("side") for t in pattern_trades if t.get("side")})

        # Map pattern to feedback_rules.yaml conditions
        pattern_type = _map_pattern_to_rule(pattern, pattern_trades)

        finding = f"每日复盘：{len(pattern_trades)} 笔亏损符合 {pattern} 模式"
        if evolution.get("triggered"):
            finding += "，自进化触发器已启动"

        suggested_adjustment = {
            "loss_count": len(pattern_trades),
            "pattern": pattern,
            "symbols": affected_symbols,
            "sides": affected_sides,
            "evolution_triggered": bool(evolution.get("triggered")),
        }

        # Write to primary skill based on pattern
        primary_skill = _primary_skill_for_pattern(pattern)
        memory_id = repo.save_skill_feedback_memory(
            skill_name=primary_skill,
            feedback_type="daily_review",
            source_type="daily_review",
            finding=finding,
            pattern_type=pattern_type,
            affected_symbols=affected_symbols,
            affected_sides=affected_sides,
            suggested_adjustment=suggested_adjustment,
        )
        updates.append({
            "skill": primary_skill,
            "memory_id": memory_id,
            "finding": finding,
            "pattern_type": pattern_type,
            "affected_symbols": affected_symbols,
            "affected_sides": affected_sides,
        })

    # If no losses, write a general observation
    if not losses and trades:
        finding = "每日复盘：今日无显著亏损，保持当前 Skill 权重并继续观察。"
        for skill in ("price_action", "momentum", "trend_stage", "smc_orderflow", "chanlun"):
            memory_id = repo.save_skill_feedback_memory(
                skill_name=skill,
                feedback_type="daily_review",
                source_type="daily_review",
                finding=finding,
                suggested_adjustment={"loss_count": 0, "evolution_triggered": False},
            )
            updates.append({"skill": skill, "memory_id": memory_id, "finding": finding})
    elif not trades:
        finding = "每日复盘：今日无平仓样本，仅记录观察。"
        for skill in ("price_action", "momentum", "trend_stage", "smc_orderflow", "chanlun"):
            memory_id = repo.save_skill_feedback_memory(
                skill_name=skill,
                feedback_type="daily_review",
                source_type="daily_review",
                finding=finding,
                suggested_adjustment={"loss_count": 0, "evolution_triggered": False},
            )
            updates.append({"skill": skill, "memory_id": memory_id, "finding": finding})

    return updates


def _map_pattern_to_rule(pattern: str, trades: list[dict[str, Any]]) -> str:
    """Map loss_classifier pattern to feedback_rules.yaml condition."""
    # Check market regime context if available
    regimes = [t.get("market_regime_at_loss") for t in trades if t.get("market_regime_at_loss")]

    if pattern == "late_trend_chasing":
        return "overextended_chase_loss"
    if pattern == "entry_chasing":
        if any(r and "late" in str(r).lower() for r in regimes):
            return "late_stage_misclassified"
        return "false_breakout_loss"
    if pattern == "entry_too_late":
        return "momentum_failed_after_entry"
    if pattern == "wrong_direction":
        return "buy_point_failed" if any(t.get("side") == "LONG" for t in trades) else "sweep_without_reclaim_failed"
    if pattern == "stop_loss_too_tight":
        return "range_misclassified_as_trend"
    if pattern == "entry_too_early":
        return "zhongshu_breakout_failed"
    if pattern == "take_profit_too_far":
        return "range_breakout_success"
    return "unknown_pattern"


def _primary_skill_for_pattern(pattern: str) -> str:
    """Determine primary skill responsible for a failure pattern."""
    if pattern in ("late_trend_chasing", "entry_chasing"):
        return "trend_stage"
    if pattern in ("entry_too_late", "entry_too_early"):
        return "momentum"
    if pattern == "wrong_direction":
        return "smc_orderflow"
    if pattern == "stop_loss_too_tight":
        return "price_action"
    if pattern == "take_profit_too_far":
        return "chanlun"
    return "price_action"
