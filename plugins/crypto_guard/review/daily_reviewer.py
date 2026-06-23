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


def _parse_json_field(value, default=None):
    """Parse a value that may be a JSON string or already a dict."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return default


def _item_pnl_r(item: dict) -> float | None:
    """Extract pnl_r from a review item, preferring review over trade."""
    review = item.get("review", {})
    # Try review.ga_review_json.metrics.pnl_r first
    ga_review = _parse_json_field(review.get("ga_review_json"), {})
    if isinstance(ga_review, dict):
        metrics = ga_review.get("metrics", {})
        if isinstance(metrics, dict) and "pnl_r" in metrics:
            return float(metrics["pnl_r"])
    # Try review.pnl_r
    if "pnl_r" in review and review["pnl_r"] is not None:
        return float(review["pnl_r"])
    # Fall back to trade.pnl_r
    trade = item.get("trade", {})
    if "pnl_r" in trade and trade["pnl_r"] is not None:
        return float(trade["pnl_r"])
    return None


def _trade_pnl_r(trade: dict, review: dict | None = None) -> float | None:
    """Extract pnl_r from trade+review, with proper 0 handling."""
    if review:
        ga_review = _parse_json_field(review.get("ga_review_json"), {})
        if isinstance(ga_review, dict):
            metrics = ga_review.get("metrics", {})
            if isinstance(metrics, dict) and "pnl_r" in metrics:
                return float(metrics["pnl_r"])
        if "pnl_r" in review and review["pnl_r"] is not None:
            return float(review["pnl_r"])
    if "pnl_r" in trade and trade["pnl_r"] is not None:
        return float(trade["pnl_r"])
    return None


def run_daily_review(repo: CryptoGuardRepository, *, day_utc: str | None = None, force: bool = False) -> dict[str, Any]:
    start, end = _review_window(day_utc)
    report_date = start[:10]

    # Fix 8: Force mode — lightweight, skip heavy operations
    if force:
        LOGGER.info("Force rebuild: lightweight mode, skipping backtest-heavy operations")

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
    # Fix 1: paper_summary from all_closed (not all_review_items)
    paper_summary = _paper_summary(all_closed)
    # Build trade details from all_closed with review data merged in
    closed_trades_detail = _build_closed_trades_detail(all_closed, all_review_items)
    fallback_summary = _summary(start, end, all_closed, all_review_items, failed_review_trade_ids, memory)
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
    # Fix 4: Enforce deterministic overview stats in report text
    summary = _enforce_deterministic_overview(raw_summary_text, all_review_items, all_closed)
    # Fix 2 & 7: Write skill memory updates with proper tracking
    skill_updates = _write_skill_memory_updates(repo, all_closed, all_review_items, failed_review_trade_ids, evolution, review_date=report_date)
    # Fix 6: Build deterministic evolution status filtered by window trades
    evo_status = _evolution_status_for_report(repo, all_closed)
    # Fix 5: Strategy performance by real names
    strategy_perf = _strategy_performance_summary(repo, all_review_items)
    # Fix 2: Build win_analysis from items with positive pnl_r
    win_analysis = _build_win_analysis(all_review_items)
    # Fix 2: Build loss_analysis from items with negative pnl_r
    loss_analysis = _build_loss_analysis(all_review_items)
    # Fix 5: UTC+8 window display
    window_display = _window_display_text(start, end)
    # Fix 5: Append deterministic sections to report
    summary = _append_deterministic_sections(summary, window_display, evo_status, strategy_perf, loss_analysis)
    report_date = start[:10]
    report_id = repo.save_daily_review_report(
        review_date=report_date,
        summary={
            "date_utc": report_date,
            "paper_summary": paper_summary,
            "win_analysis": win_analysis,
            "loss_analysis": loss_analysis,
            "analysis_failures": [{"trade_id": tid, "error": "review failed"} for tid in failed_review_trade_ids],
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
        "closed_trades": len(all_closed),
        "new_reviews": new_reviews,
        "errors": [{"trade_id": tid, "error": "review failed"} for tid in failed_review_trade_ids],
        "analysis_failures": [{"trade_id": tid, "error": "review failed"} for tid in failed_review_trade_ids],
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
    breakevens = len(trades) - wins - losses
    return {
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "breakevens": breakevens,
        "trades": len(trades),
        "daily_pnl": sum(pnl),
        "net_pnl": sum(pnl),
        "avg_r": sum(pnl_r) / len(pnl_r) if pnl_r else 0.0,
        "max_drawdown": min([float(t.get("max_adverse_excursion") or 0) for t in trades], default=0.0),
    }


def _enforce_deterministic_overview(report_text: str, all_review_items: list[dict[str, Any]], all_closed: list[dict[str, Any]]) -> str:
    """Rebuild the 交易概览 block with deterministic values, removing any LLM-generated versions.

    Computes trade counts and net PnL from all_closed (ALL closed trades in window).
    Computes win/loss/breakeven and avg R from all_review_items using pnl_r helper.
    """
    import re

    # Compute from all_closed for trade counts and net PnL
    total = len(all_closed)
    net_pnl = sum(float(t.get("pnl") or 0) for t in all_closed)

    # Compute win/loss/breakeven from all_review_items using pnl_r helper
    wins = sum(1 for item in all_review_items if (_item_pnl_r(item) or 0) > 0.05)
    losses = sum(1 for item in all_review_items if (_item_pnl_r(item) or 0) < -0.05)
    breakevens = total - wins - losses

    # Compute avg R from all_review_items
    pnl_rs = [r for item in all_review_items if (r := _item_pnl_r(item)) is not None]
    avg_r = sum(pnl_rs) / len(pnl_rs) if pnl_rs else 0.0

    overview_block = (
        f"平仓交易: {total} 笔 (胜 {wins} / 负 {losses} / 平 {breakevens})\n"
        f"净 PnL: {net_pnl:+.2f} USDT\n"
        f"平均 R: {avg_r:+.2f}R"
    )

    # Remove any existing 交易概览 section lines
    report_text = re.sub(r'平仓交易[：:][^\n]*\n?', '', report_text)
    report_text = re.sub(r'[胜勝][率]?[：:][^\n]*\n?', '', report_text)
    report_text = re.sub(r'[负負][率]?[：:][^\n]*\n?', '', report_text)
    report_text = re.sub(r'[平][率]?[：:][^\n]*\n?', '', report_text)
    report_text = re.sub(r'净\s*PnL[：:][^\n]*\n?', '', report_text)
    report_text = re.sub(r'平均\s*R[：:][^\n]*\n?', '', report_text)
    report_text = re.sub(r'胜\s*/\s*负\s*/\s*平[：:][^\n]*\n?', '', report_text)

    # Insert the deterministic block after the title or at the beginning
    if '交易概览' in report_text:
        report_text = report_text.replace('交易概览', f'交易概览\n{overview_block}', 1)
    elif '## 交易' in report_text:
        # Insert after the first ## heading
        report_text = re.sub(r'(##[^\n]*交易[^\n]*\n)', f'\\1\n{overview_block}\n', report_text, count=1)
    else:
        report_text = f"## 交易概览\n{overview_block}\n\n{report_text}"

    return report_text


def _write_skill_memory_updates(
    repo: CryptoGuardRepository,
    trades: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
    failed_ids: list[int],
    evolution: dict[str, Any],
    review_date: str = "",
) -> list[dict[str, Any]]:
    from plugins.crypto_guard.review.loss_classifier import classify_trade

    updates: list[dict[str, Any]] = []

    # Fix 7: Clean up "review 错误" or "未生成归因" entries for this specific review_date
    _cleanup_false_review_error_memories(repo, review_date, review_items)

    # Classify losses by failure pattern using review_items primary_reason
    pattern_groups: dict[str, list[dict[str, Any]]] = {}
    for item in review_items:
        review = item["review"]
        trade = item["trade"]
        pnl_r = _item_pnl_r(item)
        if pnl_r is None or pnl_r >= -0.05:
            continue  # Not a loss
        pattern = review.get("primary_reason") or classify_trade(trade)
        # Enrich trade with review data for downstream use
        enriched = dict(trade)
        enriched["_review"] = review
        pattern_groups.setdefault(pattern, []).append(enriched)

    # Build regime context lookup by trade_id from reviews (parse JSON strings)
    regime_by_trade_id: dict[int, dict[str, Any]] = {}
    for item in review_items:
        rev = item["review"]
        tid = rev.get("trade_id")
        ctx = _parse_json_field(rev.get("market_regime_at_loss"))
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
            ctx = regime_by_trade_id.get(int(tid)) if tid else _parse_json_field(t.get("market_regime_at_loss"))
            if ctx and isinstance(ctx, dict):
                regime_info = {
                    "market_phase": ctx.get("market_phase"),
                    "regime_alignment": ctx.get("regime_alignment"),
                    "btc_bias": ctx.get("btc_bias"),
                    "eth_bias": ctx.get("eth_bias"),
                    "relative_strength": ctx.get("symbol_relative_strength"),
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
            "relative_strength": regime_info.get("relative_strength"),
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


def _cleanup_false_review_error_memories(repo: CryptoGuardRepository, review_date: str, all_review_items: list[dict[str, Any]]) -> int:
    """Archive false 'review error' memories for a specific review_date.

    Only cleans up if the date's loss trades all have valid trade_reviews.
    """
    loss_items = [item for item in all_review_items if (_item_pnl_r(item) or 0) < -0.05]

    # Only cleanup if all loss trades have reviews
    if not loss_items:
        return 0

    all_have_reviews = all(
        item.get("review") and item["review"].get("primary_reason")
        for item in loss_items
    )
    if not all_have_reviews:
        return 0

    # Find polluted entries for this review_date
    polluted = repo.conn.execute(
        """SELECT id FROM skill_feedback_memory
           WHERE source_type='daily_review'
             AND (finding LIKE '%review 错误%' OR finding LIKE '%未生成归因%')
             AND status != 'archived'""",
    ).fetchall()

    count = 0
    for row in polluted:
        repo.conn.execute(
            "UPDATE skill_feedback_memory SET status='archived' WHERE id=?",
            (row["id"],),
        )
        count += 1

    return count


def _build_loss_analysis(all_review_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build structured loss_analysis from all_review_items for summary_json.

    Each entry includes trade details and review attribution.
    """
    loss_analysis = []
    for item in all_review_items:
        review = item["review"]
        trade = item["trade"]
        pnl_r = _item_pnl_r(item)
        if pnl_r is None or pnl_r >= -0.05:
            continue
        loss_analysis.append({
            "trade_id": trade["id"],
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "pnl_r": pnl_r,
            "close_reason": trade.get("close_reason"),
            "primary_reason": review.get("primary_reason"),
            "market_regime_at_loss": _parse_json_field(review.get("market_regime_at_loss")),
            "improvement_suggestion": _parse_json_field(review.get("improvement_suggestion")),
        })
    return loss_analysis


def _evolution_status_for_report(repo: CryptoGuardRepository, window_trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Build deterministic evolution/shadow status filtered to window trade IDs.

    Only returns patches whose triggers or trade_ids are related to window trades.
    """
    window_trade_ids = set(t["id"] for t in window_trades)

    # Find triggers related to window trades
    all_triggers = repo.conn.execute(
        "SELECT * FROM evolution_triggers ORDER BY latest_triggered_at DESC LIMIT 50"
    ).fetchall()

    related_triggers = []
    for t in all_triggers:
        t = dict(t)
        related_ids = set()
        for field in ["related_trade_ids", "original_related_trade_ids", "latest_related_trade_ids"]:
            val = t.get(field)
            if val:
                try:
                    ids = json.loads(val) if isinstance(val, str) else val
                    if isinstance(ids, list):
                        related_ids.update(int(i) for i in ids)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        if related_ids & window_trade_ids:
            related_triggers.append(t)

    # Find patches linked to related triggers
    trigger_ids = [t["id"] for t in related_triggers]
    related_patches = []
    if trigger_ids:
        placeholders = ",".join("?" * len(trigger_ids))
        patch_rows = repo.conn.execute(
            f"SELECT * FROM strategy_patches WHERE trigger_id IN ({placeholders})",
            trigger_ids,
        ).fetchall()
        related_patches = [dict(r) for r in patch_rows]

    # Also find trade-level candidates: version like "candidate-trade-{trade_id}"
    for trade_id in window_trade_ids:
        candidate_version = f"candidate-trade-{trade_id}"
        rows = repo.conn.execute(
            "SELECT * FROM strategy_patches WHERE candidate_version LIKE ?",
            (f"%{candidate_version}%",),
        ).fetchall()
        for r in rows:
            r = dict(r)
            if r["id"] not in {p["id"] for p in related_patches}:
                related_patches.append(r)

    # Parse backtest results
    for p in related_patches:
        bt = _parse_json_field(p.get("backtest_result_json"), {})
        p["backtest_parsed"] = {
            "passed": bt.get("passed"),
            "skipped": bt.get("skipped"),
            "gate_disabled": bt.get("gate_disabled"),
            "reason": bt.get("reason"),
        }

    # Compute shadow stats from strategy_evaluations for each patch
    for p in related_patches:
        stats = repo.conn.execute(
            """SELECT COUNT(*) as sample_count,
                      COUNT(CASE WHEN pnl_r IS NOT NULL THEN 1 END) as real_pnl_count,
                      AVG(CASE WHEN pnl_r IS NOT NULL THEN pnl_r END) as avg_r
               FROM strategy_evaluations
               WHERE strategy_name=? AND strategy_version=? AND is_shadow=1""",
            (p.get("strategy_name"), p.get("candidate_version")),
        ).fetchone()
        if stats:
            p["sample_count"] = stats["sample_count"]
            p["real_pnl_count"] = stats["real_pnl_count"]
            p["avg_r"] = round(float(stats["avg_r"] or 0), 4)
            p["data_quality"] = "good" if (stats["real_pnl_count"] or 0) >= 3 else "limited"

    review_required = [p for p in related_patches if p.get("status") == "review_required"]
    shadow_testing = [p for p in related_patches if p.get("status") == "shadow_testing"]
    rejected = [p for p in related_patches if p.get("status") == "rejected"]

    # Build patch items for report
    patch_items = []
    for p in related_patches:
        patch_items.append({
            "id": p["id"],
            "candidate_version": p.get("candidate_version"),
            "status": p.get("status"),
            "backtest_result": p.get("backtest_result_json"),
            "shadow_sample_count": p.get("sample_count", 0),
            "real_pnl_count": p.get("real_pnl_count", 0),
            "avg_r": p.get("avg_r"),
        })

    trigger_items = []
    for t in related_triggers:
        trigger_items.append({
            "id": t["id"],
            "type": t.get("trigger_type"),
            "status": t.get("status"),
            "original_trade_ids": t.get("original_related_trade_ids"),
            "latest_trade_ids": t.get("latest_related_trade_ids"),
            "triggered_at": t.get("latest_triggered_at") or t.get("created_at"),
        })

    return {
        "triggers": trigger_items,
        "patches": patch_items,
        "review_required": review_required,
        "shadow_testing": shadow_testing,
        "rejected": rejected,
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
        pnl_rs = [r for it in items if (r := _trade_pnl_r(it["trade"], it.get("review"))) is not None]
        pnls = [float(it["trade"].get("pnl") or 0) for it in items]
        wins = len([x for x in pnl_rs if x > 0.05])
        losses = len([x for x in pnl_rs if x < -0.05])
        breakevens = len(items) - wins - losses
        result[name] = {
            "trades": len(items),
            "wins": wins,
            "losses": losses,
            "breakevens": breakevens,
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


def _build_closed_trades_detail(all_closed: list[dict[str, Any]], all_review_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build trade details from all_closed, merging review data from all_review_items."""
    review_by_trade_id = {item["trade"]["id"]: item.get("review", {}) for item in all_review_items}
    details = []
    for trade in all_closed:
        review = review_by_trade_id.get(trade["id"], {})
        details.append({
            "trade_id": trade["id"],
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "pnl": float(trade.get("pnl") or 0),
            "pnl_r": _trade_pnl_r(trade, review if review else None),
            "close_reason": trade.get("close_reason"),
            "primary_reason": review.get("primary_reason") if review else None,
            "has_review": bool(review),
        })
    return details


def _build_win_analysis(all_review_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build win_analysis from review items with positive pnl_r."""
    wins = []
    for item in all_review_items:
        pnl_r = _item_pnl_r(item)
        if pnl_r is not None and pnl_r > 0.05:
            trade = item["trade"]
            review = item.get("review", {})
            wins.append({
                "trade_id": trade["id"],
                "symbol": trade["symbol"],
                "side": trade["side"],
                "pnl_r": pnl_r,
                "close_reason": trade.get("close_reason"),
                "primary_reason": review.get("primary_reason"),
            })
    return wins


def _append_deterministic_sections(
    report_text: str,
    window_display: str,
    evo_status: dict[str, Any],
    strategy_performance: dict[str, Any],
    loss_analysis: list[dict[str, Any]],
) -> str:
    """Append deterministic sections to the report that LLM cannot fabricate."""
    sections = []

    # Window display
    if window_display:
        sections.append(f"## 分析窗口\n{window_display}")

    # Strategy performance
    if strategy_performance:
        perf_lines = ["## 策略表现"]
        for name, stats in sorted(strategy_performance.items()):
            perf_lines.append(
                f"- {name}: {stats['trades']}笔 "
                f"(胜{stats['wins']}/负{stats['losses']}/平{stats.get('breakevens', stats['trades'] - stats['wins'] - stats['losses'])}) "
                f"净PnL {stats['net_pnl']:+.2f} 平均R {stats['avg_r']:+.2f}"
            )
        sections.append("\n".join(perf_lines))

    # Loss analysis summary
    if loss_analysis:
        loss_lines = ["## 亏损归因"]
        for loss in loss_analysis:
            loss_lines.append(
                f"- #{loss['trade_id']} {loss['symbol']} {loss['side']} "
                f"R={loss['pnl_r']:.2f} 原因: {loss.get('primary_reason', 'unknown')}"
            )
        sections.append("\n".join(loss_lines))

    # Evolution/Shadow status
    if evo_status:
        evo_lines = ["## 策略进化状态"]
        for p in evo_status.get("review_required", []):
            evo_lines.append(f"- [进入 review] patch#{p['id']} {p.get('candidate_version', '?')}")
        for p in evo_status.get("shadow_testing", []):
            evo_lines.append(f"- [影子测试中] patch#{p['id']} {p.get('candidate_version', '?')} 样本={p.get('sample_count', 0)}")
        for p in evo_status.get("rejected", []):
            evo_lines.append(f"- [已拒绝] patch#{p['id']} {p.get('candidate_version', '?')}")
        sections.append("\n".join(evo_lines))

    if sections:
        return report_text + "\n\n" + "\n\n".join(sections)
    return report_text


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
