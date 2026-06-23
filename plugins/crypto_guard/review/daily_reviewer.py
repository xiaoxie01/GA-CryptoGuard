from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers
from plugins.crypto_guard.review.trade_reviewer import review_trade
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.daily_reviewer")


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

    # Fix 1: Get ALL closed trades in window (not just unreviewed)
    all_closed = repo.list_closed_trades_for_review(start_utc=start, end_utc=end, only_unreviewed=False)
    all_review_items: list[dict[str, Any]] = []
    new_reviews = 0
    failed_review_trade_ids: list[int] = []
    for trade in all_closed:
        existing_review = repo.get_trade_review_by_trade(trade["id"])
        if existing_review:
            all_review_items.append({"trade": trade, "review": dict(existing_review), "is_new": False})
        else:
            try:
                review_result = review_trade(repo, int(trade["id"]))
                if review_result.get("ok"):
                    new_review = repo.get_trade_review_by_trade(trade["id"])
                    if new_review:
                        all_review_items.append({"trade": trade, "review": dict(new_review), "is_new": True})
                        new_reviews += 1
                    else:
                        failed_review_trade_ids.append(trade["id"])
                else:
                    failed_review_trade_ids.append(trade["id"])
            except Exception:
                LOGGER.exception("review_trade failed for trade_id=%s", trade["id"])
                failed_review_trade_ids.append(trade["id"])

    all_window_trades = [item["trade"] for item in all_review_items]
    memory = repo.strategy_memory_top(limit=8)
    evolution = evaluate_evolution_triggers(repo)
    paper_summary = _paper_summary(all_window_trades)
    fallback_summary = _summary(start, end, all_window_trades, all_review_items, failed_review_trade_ids, memory)
    agent = run_agent_json_task(
        task_name="daily_paper_review_summary",
        payload={
            "window": {"start_utc": start, "end_utc": end},
            "trades": all_window_trades[:50],
            "new_reviews": [item for item in all_review_items if item["is_new"]][:50],
            "errors": [{"trade_id": tid, "error": "review failed"} for tid in failed_review_trade_ids],
            "strategy_memory": memory,
            "evolution": evolution,
            "paper_summary": paper_summary,
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
            f"交易概览必须使用以下确定性数据：净 PnL={paper_summary['daily_pnl']:+.2f} USDT，"
            f"胜={paper_summary['wins']}，负={paper_summary['losses']}，"
            f"平仓={paper_summary['trades']}，avg_r={paper_summary['avg_r']:.2f}。",
        ],
    )
    raw_summary_text = str(agent.get("summary_text") or fallback_summary)
    # Fix 3: Enforce deterministic overview stats in report text
    summary = _enforce_deterministic_overview(raw_summary_text, all_review_items)
    # Fix 2 & 4: Write skill memory updates with proper tracking
    skill_updates = _write_skill_memory_updates(repo, all_window_trades, all_review_items, failed_review_trade_ids, evolution)
    # Fix 5: Build deterministic evolution status
    evo_status = _evolution_status_for_report(repo, all_window_trades)
    # Fix 6: Strategy performance by real names
    strategy_perf = _strategy_performance_summary(repo, all_review_items)
    # Fix 7: UTC+8 window display
    window_display = _window_display_text(start, end)
    report_date = start[:10]
    report_id = repo.save_daily_review_report(
        review_date=report_date,
        summary={
            "date_utc": report_date,
            "paper_summary": paper_summary,
            "win_analysis": [item for item in all_review_items if (item["review"].get("pnl_r") or 0) > 0],
            "loss_analysis": _build_loss_analysis(all_review_items),
            "analysis_failures": agent.get("analysis_failures", []),
            "next_focus_points": agent.get("risk_focus", []),
            "skill_memory_updates": skill_updates,
            "evolution": evolution,
            "evo_status": evo_status,
            "strategy_performance": strategy_perf,
            "window_display": window_display,
        },
        ga_report=summary,
        skill_updates=skill_updates,
        evolution_actions=evolution,
    )
    return {
        "ok": not failed_review_trade_ids,
        "day_start_utc": start,
        "day_end_utc": end,
        "closed_trades": len(all_window_trades),
        "new_reviews": new_reviews,
        "errors": [{"trade_id": tid, "error": "review failed"} for tid in failed_review_trade_ids],
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
    review_items: list[dict[str, Any]],
    failed_ids: list[int],
    memory: list[dict[str, Any]],
) -> str:
    pnl_rs = [float(t.get("pnl_r") or 0) for t in trades]
    pnls = [float(t.get("pnl") or 0) for t in trades]
    daily_pnl = sum(pnls)
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
        f"- 新增复盘：{sum(1 for item in review_items if item['is_new'])}",
        f"- 胜 / 负 / 平：{wins} / {losses} / {breakeven}",
        f"- 净 PnL：{daily_pnl:+.2f} USDT",
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

    if review_items:
        lines.append("")
        lines.append("**归因：**")
        for item in review_items[:20]:
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

    if failed_ids:
        lines.append("")
        lines.append("**异常：**")
        for tid in failed_ids:
            lines.append(f"- trade #{tid}：review failed")

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


def _enforce_deterministic_overview(report_text: str, all_review_items: list[dict[str, Any]]) -> str:
    """Replace LLM-generated overview stats with deterministic computed values from all_review_items.

    Forces these 4 lines in the report:
      - 平仓交易: X 笔 (胜 Y / 负 Z / 平 W)
      - 净 PnL: +XXX.XX USDT
      - 平均 R: +X.XXR

    Computes everything from all_review_items (not from LLM text), so even when review_trade
    was skipped (existing reviews loaded), the numbers are correct.
    """
    import re

    wins = sum(1 for item in all_review_items if (item["review"].get("pnl_r") or item["trade"].get("pnl_r") or 0) > 0)
    losses = sum(1 for item in all_review_items if (item["review"].get("pnl_r") or item["trade"].get("pnl_r") or 0) < 0)
    breakevens = sum(1 for item in all_review_items if (item["review"].get("pnl_r") or item["trade"].get("pnl_r") or 0) == 0)
    total = len(all_review_items)
    net_pnl = sum(item["trade"].get("pnl") or 0 for item in all_review_items)
    avg_r = sum(item["review"].get("pnl_r") or item["trade"].get("pnl_r") or 0 for item in all_review_items) / max(total, 1)

    # Replace lines matching the patterns
    report_text = re.sub(
        r'平仓交易[：:]\s*\d+\s*笔\s*\([^)]*\)',
        f'平仓交易: {total} 笔 (胜 {wins} / 负 {losses} / 平 {breakevens})',
        report_text,
        count=1,
    )
    report_text = re.sub(
        r'平仓交易[：:]\s*\d+',
        f'平仓交易: {total} 笔 (胜 {wins} / 负 {losses} / 平 {breakevens})',
        report_text,
        count=1 if '平仓交易:' not in report_text else 0,
    )
    # Re-replace with full format
    if f"平仓交易: {total} 笔" not in report_text:
        # Append at the beginning of the overview section
        report_text = report_text.replace(
            "**交易概览：**",
            f"**交易概览：**\n- 平仓交易: {total} 笔 (胜 {wins} / 负 {losses} / 平 {breakevens})",
            1,
        )

    report_text = re.sub(r'净\s*PnL[：:]\s*[^\n]*', f'净 PnL: {net_pnl:+.2f} USDT', report_text, count=1)
    report_text = re.sub(r'平均\s*R[：:]\s*[^\n]*', f'平均 R: {avg_r:+.2f}R', report_text, count=1)

    return report_text


def _write_skill_memory_updates(
    repo: CryptoGuardRepository,
    trades: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
    failed_ids: list[int],
    evolution: dict[str, Any],
) -> list[dict[str, Any]]:
    from plugins.crypto_guard.review.loss_classifier import classify_trade

    updates: list[dict[str, Any]] = []

    # Fix 2: Clean up existing "review 错误" or "未生成归因" entries for this date range
    _cleanup_false_review_error_memories(repo)

    # Classify losses by failure pattern using review_items primary_reason
    pattern_groups: dict[str, list[dict[str, Any]]] = {}
    for item in review_items:
        review = item["review"]
        trade = item["trade"]
        pnl_r = float(review.get("pnl_r") or trade.get("pnl_r") or 0)
        if pnl_r >= -0.05:
            continue  # Not a loss
        pattern = review.get("primary_reason") or classify_trade(trade)
        # Enrich trade with review data for downstream use
        enriched = dict(trade)
        enriched["_review"] = review
        pattern_groups.setdefault(pattern, []).append(enriched)

    # Build regime context lookup by trade_id from reviews
    regime_by_trade_id: dict[int, dict[str, Any]] = {}
    for item in review_items:
        rev = item["review"]
        tid = rev.get("trade_id")
        ctx = rev.get("market_regime_at_loss")
        if tid and ctx and isinstance(ctx, dict):
            regime_by_trade_id[int(tid)] = ctx

    # Write one entry per pattern (not per skill)
    for pattern, pattern_trades in pattern_groups.items():
        affected_symbols = list({t.get("symbol") for t in pattern_trades if t.get("symbol")})
        affected_sides = list({t.get("side") for t in pattern_trades if t.get("side")})

        # Map pattern to feedback_rules.yaml conditions
        pattern_type = _map_pattern_to_rule(pattern, pattern_trades, regime_by_trade_id)

        finding = f"每日复盘：{len(pattern_trades)} 笔亏损符合 {pattern} 模式"
        if evolution.get("triggered"):
            finding += "，自进化触发器已启动"

        # Build structured suggested_adjustment with regime context
        regime_info: dict[str, Any] = {}
        for t in pattern_trades:
            tid = t.get("id")
            ctx = regime_by_trade_id.get(int(tid)) if tid else t.get("market_regime_at_loss")
            if ctx and isinstance(ctx, dict):
                regime_info = {
                    "market_phase": ctx.get("market_phase"),
                    "regime_alignment": ctx.get("regime_alignment"),
                    "btc_bias": ctx.get("btc_bias"),
                    "eth_bias": ctx.get("eth_bias"),
                    "symbol_relative_strength": ctx.get("symbol_relative_strength"),
                }
                break

        suggested_adjustment = {
            "loss_count": len(pattern_trades),
            "pattern": pattern,
            "symbols": affected_symbols,
            "sides": affected_sides,
            "evolution_triggered": bool(evolution.get("triggered")),
            "market_phase": regime_info.get("market_phase"),
            "regime_alignment": regime_info.get("regime_alignment"),
            "btc_bias": regime_info.get("btc_bias"),
            "eth_bias": regime_info.get("eth_bias"),
            "symbol_relative_strength": regime_info.get("symbol_relative_strength"),
            # Fix 4: Include improvement suggestions and avg R
            "avg_r": sum(float(t["_review"].get("pnl_r") or 0) for t in pattern_trades) / len(pattern_trades),
            "suggestions": [
                t["_review"].get("improvement_suggestion")
                for t in pattern_trades
                if t["_review"].get("improvement_suggestion")
            ],
        }

        # Add action-oriented suggested_adjustment_json for regime-mismatch patterns
        if "regime_mismatch" in pattern or "counter_regime" in pattern or "macro_" in pattern:
            suggested_adjustment["suggested_adjustment_json"] = {
                "action": "raise_confirmation_threshold",
                "when": {
                    "pattern_type": pattern,
                    "market_phase": regime_info.get("market_phase"),
                    "side": affected_sides[0] if len(affected_sides) == 1 else affected_sides,
                },
                "adjustments": {
                    "min_confidence": 0.82,
                    "min_rr": 2.0,
                    "risk_multiplier": 0.5,
                    "allow_order_types": ["trigger", "retest"],
                    "downgrade_to_watch_if_no_independent_trend": True,
                },
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

    # If no losses at all, write a general observation
    trade_losses = [t for t in trades if float(t.get("pnl_r") or 0) < -0.05]
    if not pattern_groups and not trade_losses:
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
    elif not pattern_groups and failed_ids:
        # Fix 2: Only write "review error" when actual review failures occurred, and only for the specific failed trades
        finding = f"每日复盘：{len(failed_ids)} 笔亏损交易因 review 异常未生成归因，仅记录观察。"
        for skill in ("price_action", "momentum", "trend_stage", "smc_orderflow", "chanlun"):
            memory_id = repo.save_skill_feedback_memory(
                skill_name=skill,
                feedback_type="daily_review",
                source_type="daily_review",
                finding=finding,
                suggested_adjustment={"loss_count": len(failed_ids), "failed_trade_ids": failed_ids, "evolution_triggered": False},
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


def _cleanup_false_review_error_memories(repo: CryptoGuardRepository) -> None:
    """Archive existing skill_feedback_memory entries that falsely claim 'review 错误' or '未生成归因'.

    These polluted entries may have been written by previous run_daily_review versions
    when the `reviewed` list was empty but trades actually had valid reviews.
    """
    repo.conn.execute(
        """
        UPDATE skill_feedback_memory
        SET status='archived'
        WHERE source_type='daily_review'
          AND (finding LIKE '%review 错误%' OR finding LIKE '%未生成归因%')
          AND status='candidate'
        """
    )
    repo.conn.commit()


def _build_loss_analysis(all_review_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build structured loss_analysis from all_review_items for summary_json.

    Each entry includes trade details and review attribution.
    """
    loss_analysis = []
    for item in all_review_items:
        review = item["review"]
        trade = item["trade"]
        pnl_r = float(review.get("pnl_r") or trade.get("pnl_r") or 0)
        if pnl_r >= -0.05:
            continue
        loss_analysis.append({
            "trade_id": trade["id"],
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "pnl_r": pnl_r,
            "close_reason": trade.get("close_reason"),
            "primary_reason": review.get("primary_reason"),
            "market_regime_at_loss": review.get("market_regime_at_loss"),
            "improvement_suggestion": review.get("improvement_suggestion"),
        })
    return loss_analysis


def _evolution_status_for_report(repo: CryptoGuardRepository, window_trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Build deterministic evolution/shadow status for daily report.

    Queries evolution_triggers and strategy_patches to build structured status
    that the report can render instead of relying on LLM interpretation.
    """
    triggers = repo.conn.execute(
        "SELECT * FROM evolution_triggers ORDER BY id DESC LIMIT 20"
    ).fetchall()

    patches = repo.conn.execute(
        "SELECT * FROM strategy_patches ORDER BY id DESC LIMIT 50"
    ).fetchall()

    # Compute shadow stats from strategy_evaluations (not from strategy_patches which lacks these columns)
    eval_stats: dict[tuple[str, str], dict[str, Any]] = {}
    evals = repo.conn.execute(
        """
        SELECT strategy_name, strategy_version, COUNT(*) AS sample_count,
               SUM(CASE WHEN pnl_r IS NOT NULL THEN 1 ELSE 0 END) AS real_pnl_count,
               AVG(pnl_r) AS avg_r
        FROM strategy_evaluations
        WHERE is_shadow = 1
        GROUP BY strategy_name, strategy_version
        """
    ).fetchall()
    for e in evals:
        key = (e["strategy_name"], e["strategy_version"])
        eval_stats[key] = {
            "shadow_sample_count": e["sample_count"],
            "real_pnl_count": e["real_pnl_count"],
            "avg_r": e["avg_r"],
        }

    trigger_items = []
    for t in triggers:
        trigger_items.append({
            "id": t["id"],
            "type": t["trigger_type"],
            "status": t["status"],
            "original_trade_ids": t["original_related_trade_ids"],
            "latest_trade_ids": t["latest_related_trade_ids"],
            "triggered_at": _safe_col(t, "latest_triggered_at") or _safe_col(t, "created_at"),
        })

    patch_items = []
    for p in patches:
        key = (p["strategy_name"], p["candidate_version"])
        stats = eval_stats.get(key, {})
        patch_items.append({
            "id": p["id"],
            "candidate_version": p["candidate_version"],
            "status": p["status"],
            "backtest_result": _safe_col(p, "backtest_result_json"),
            "shadow_sample_count": stats.get("shadow_sample_count", 0),
            "real_pnl_count": stats.get("real_pnl_count", 0),
            "avg_r": stats.get("avg_r"),
        })

    # Only status=review_required means "进入 review"
    review_required = [p for p in patch_items if p["status"] == "review_required"]
    shadow_testing = [p for p in patch_items if p["status"] == "shadow_testing"]

    return {
        "triggers": trigger_items,
        "patches": patch_items,
        "review_required": review_required,
        "shadow_testing": shadow_testing,
    }


def _safe_col(row: Any, col: str) -> Any:
    """Safely get a column from a sqlite3.Row, returning None if missing."""
    try:
        return row[col]
    except (IndexError, KeyError):
        return None


def _get_strategy_name_for_trade(repo: CryptoGuardRepository, trade: dict[str, Any]) -> str:
    """Get the real strategy name for a trade via paper_orders -> ga_decisions chain.

    Falls back to trade-level strategy_name or 'unknown'.
    """
    from plugins.crypto_guard.review.trade_reviewer import _derive_strategy_name_from_trade

    name = _derive_strategy_name_from_trade(repo, trade)
    if name:
        return name
    return trade.get("strategy_name") or "unknown"


def _strategy_performance_summary(
    repo: CryptoGuardRepository, all_review_items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute per-strategy performance stats from all_review_items."""
    from collections import defaultdict

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in all_review_items:
        name = _get_strategy_name_for_trade(repo, item["trade"])
        groups[name].append(item)

    result = {}
    for name, items in groups.items():
        pnl_rs = [float(it["review"].get("pnl_r") or it["trade"].get("pnl_r") or 0) for it in items]
        pnls = [float(it["trade"].get("pnl") or 0) for it in items]
        wins = len([x for x in pnl_rs if x > 0.05])
        losses = len([x for x in pnl_rs if x < -0.05])
        result[name] = {
            "trades": len(items),
            "wins": wins,
            "losses": losses,
            "net_pnl": sum(pnls),
            "avg_r": sum(pnl_rs) / len(pnl_rs) if pnl_rs else 0.0,
        }
    return result


def _window_display_text(start_utc: str, end_utc: str) -> str:
    """Build UTC+8 window display text for the daily report."""
    utc_start_dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    utc_end_dt = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    beijing_tz = timezone(timedelta(hours=8))
    bj_start = utc_start_dt.astimezone(beijing_tz)
    bj_end = utc_end_dt.astimezone(beijing_tz)

    return (
        f"UTC窗口: {utc_start_dt.strftime('%Y-%m-%d %H:%M')} ~ {utc_end_dt.strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"北京时间窗口: {bj_start.strftime('%Y-%m-%d %H:%M')} ~ {bj_end.strftime('%Y-%m-%d %H:%M')} UTC+8"
    )


def _map_pattern_to_rule(
    pattern: str,
    trades: list[dict[str, Any]],
    regime_by_trade_id: dict[int, dict[str, Any]] | None = None,
) -> str:
    """Map loss_classifier pattern to feedback_rules.yaml condition."""
    # Check market regime context if available (from reviews, not raw trades)
    regimes: list[Any] = []
    for t in trades:
        tid = t.get("id")
        if tid and regime_by_trade_id and regime_by_trade_id.get(int(tid)):
            regimes.append(regime_by_trade_id[int(tid)])
        elif t.get("market_regime_at_loss"):
            regimes.append(t["market_regime_at_loss"])

    # New regime-mismatch patterns
    if pattern == "macro_rebound_short_squeeze_loss":
        return "macro_rebound_short_squeeze_loss"
    if pattern == "macro_selloff_long_trap_loss":
        return "macro_selloff_long_trap_loss"
    if pattern == "counter_regime_entry_loss":
        return "counter_regime_entry_loss"
    if pattern == "market_regime_mismatch_short_loss":
        return "market_regime_mismatch_short_loss"
    if pattern == "market_regime_mismatch_long_loss":
        return "market_regime_mismatch_long_loss"

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
    # Macro regime patterns: market-level context failure
    if pattern in (
        "macro_rebound_short_squeeze_loss",
        "macro_selloff_long_trap_loss",
        "counter_regime_entry_loss",
        "market_regime_mismatch_short_loss",
        "market_regime_mismatch_long_loss",
    ):
        return "trend_stage"
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
