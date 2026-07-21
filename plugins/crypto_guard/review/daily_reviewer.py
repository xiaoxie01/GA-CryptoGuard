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


def _normalize_trade_review(row: dict[str, Any], trade: dict[str, Any]) -> dict[str, Any]:
    """Normalize a trade_review row so summary is always present.

    Real DB reviews may have summary in market_context (from older schema) or
    only in ga_review_json. This ensures review.get("summary") never returns None.
    """
    review = dict(row)
    if review.get("summary") is None:
        # Try ga_review_json.summary first
        ga_review = _parse_json_field(review.get("ga_review_json"), {})
        if isinstance(ga_review, dict) and ga_review.get("summary"):
            review["summary"] = str(ga_review["summary"])
        else:
            # Generate a minimal summary from the review data
            primary = review.get("primary_reason") or "unknown"
            pnl_r = _trade_pnl_r(trade, review)
            symbol = trade.get("symbol", "?")
            review["summary"] = f"{symbol} {primary}, R={pnl_r:.2f}" if pnl_r is not None else f"{symbol} {primary}"
    return review


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
        LOGGER.info("Force rebuild: report_only mode, evolution triggers will NOT create patches/backtests")

    # Idempotency: if report already exists and not forced, return existing
    existing = repo.conn.execute(
        "SELECT id, summary_json, ga_report, pushed_to_feishu FROM daily_review_reports WHERE review_date=%s",
        (report_date,),
    ).fetchone()
    if existing and not force:
        summary = _parse_json_field(existing["summary_json"], {})
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
        with repo.conn.transaction():
            repo.conn.execute(
                "UPDATE skill_feedback_memory SET status='archived' WHERE source_type='daily_review' AND finding LIKE %s",
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
            all_review_items.append({"trade": trade, "review": _normalize_trade_review(existing_review, trade), "is_new": False})
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
    evolution = evaluate_evolution_triggers(repo, report_only=force)
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
    key_findings = agent.get("key_findings") or []
    strategy_actions = agent.get("strategy_actions") or []
    risk_focus = agent.get("risk_focus") or []

    # Fix 2 & 7: Write skill memory updates with proper tracking
    skill_updates = _write_skill_memory_updates(repo, all_closed, all_review_items, failed_review_trade_ids, evolution, review_date=report_date)
    # Build deterministic sections
    evo_status = _evolution_status_for_report(repo, all_closed)
    strategy_perf = _strategy_performance_summary(repo, all_closed, all_review_items)
    loss_analysis = _build_loss_analysis(all_review_items)
    win_analysis = _build_win_analysis(all_review_items)
    window_display = _window_display_text(start, end)

    # Fix 9: Build deterministic template as primary output
    # LLM supplementary insights are placed in a separate section at the end
    summary = _build_deterministic_report(
        all_closed=all_closed,
        all_review_items=all_review_items,
        paper_summary=paper_summary,
        window_display=window_display,
        evo_status=evo_status,
        strategy_perf=strategy_perf,
        loss_analysis=loss_analysis,
        win_analysis=win_analysis,
        key_findings=key_findings,
        strategy_actions=strategy_actions,
        risk_focus=risk_focus,
    )
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
    pnl_r = [float(t.get("pnl_r") or 0) for t in trades if t.get("pnl_r") is not None]
    # wins/losses computed from pnl_r values (R-multiple based, not absolute PnL)
    wins = len([x for x in pnl_r if x > 0.05])
    losses = len([x for x in pnl_r if x < -0.05])
    breakevens = len(pnl_r) - wins - losses
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
    # 07-16 PG cutover: an empty ``review_date`` (the parameter default;
    # production always passes ``report_date``) must short-circuit. The PG cast
    # ``created_at::date=%s::date`` raises ``InvalidDatetimeFormat`` on the empty
    # string -- SQLite's ``date('')`` returned NULL and matched no rows, so the
    # historical behavior with no date scope is "archive nothing". Guard here to
    # preserve that behavior and avoid the cast error.
    if not review_date:
        return 0

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

    # Find polluted entries for this specific review_date
    # Filter by created_at falling within the review date window
    polluted = repo.conn.execute(
        """SELECT id FROM skill_feedback_memory
           WHERE source_type='daily_review'
             AND (finding LIKE '%%review 错误%%' OR finding LIKE '%%未生成归因%%')
             AND status != 'archived'
             AND created_at::date=%s::date""",
        (review_date,),
    ).fetchall()

    count = 0
    if polluted:
        ids = [row["id"] for row in polluted]
        placeholders = ",".join("%s" for _ in ids)
        with repo.conn.transaction():
            repo.conn.execute(
                f"UPDATE skill_feedback_memory SET status='archived' WHERE id IN ({placeholders})",
                ids,
            )
        count = len(ids)

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
        placeholders = ",".join("%s" for _ in trigger_ids)
        patch_rows = repo.conn.execute(
            f"SELECT * FROM strategy_patches WHERE trigger_id IN ({placeholders})",
            trigger_ids,
        ).fetchall()
        related_patches = [dict(r) for r in patch_rows]

    # Also find trade-level candidates: version like "candidate-trade-{trade_id}"
    for trade_id in window_trade_ids:
        candidate_version = f"candidate-trade-{trade_id}"
        rows = repo.conn.execute(
            "SELECT * FROM strategy_patches WHERE candidate_version LIKE %s",
            (f"%{candidate_version}%",),
        ).fetchall()
        for r in rows:
            r = dict(r)
            if r["id"] not in {p["id"] for p in related_patches}:
                related_patches.append(r)

    # Parse backtest results — only store summary, never raw JSON
    for p in related_patches:
        bt = _parse_json_field(p.get("backtest_result_json"), {})
        p["backtest_parsed"] = {
            "passed": bt.get("passed"),
            "skipped": bt.get("skipped"),
            "gate_disabled": bt.get("gate_disabled"),
            "reason": bt.get("reason"),
            "delta_avg_r": bt.get("delta_avg_r"),
            "delta_win_rate": bt.get("delta_win_rate"),
        }

    # Compute shadow stats from strategy_evaluations for each patch
    for p in related_patches:
        stats = repo.conn.execute(
            """SELECT COUNT(*) as sample_count,
                      COUNT(CASE WHEN pnl_r IS NOT NULL AND outcome_source='real_pnl' THEN 1 END) as real_pnl_count,
                      COUNT(CASE WHEN pnl_r IS NULL OR outcome_source IS NULL OR outcome_source!='real_pnl' THEN 1 END) as pseudo_r_count,
                      AVG(CASE WHEN pnl_r IS NOT NULL AND outcome_source='real_pnl' THEN pnl_r END) as avg_r
               FROM strategy_evaluations
               WHERE strategy_name=%s AND strategy_version=%s AND is_shadow=TRUE""",
            (p.get("strategy_name"), p.get("candidate_version")),
        ).fetchone()
        if stats:
            p["sample_count"] = stats["sample_count"]
            p["real_pnl_count"] = stats["real_pnl_count"] or 0
            p["pseudo_r_count"] = stats["pseudo_r_count"] or 0
            # avg_r is None when no real PnL data — not 0.0 (which reads as breakeven)
            raw_avg = stats["avg_r"]
            p["avg_r"] = round(float(raw_avg), 4) if raw_avg is not None else None
            real_count = p["real_pnl_count"]
            if real_count >= 5:
                p["data_quality"] = "good"
            elif real_count >= 1:
                p["data_quality"] = "limited"
            else:
                p["data_quality"] = "no_real_pnl"

            # Compute win_rate from real PnL evaluations only (not pseudo-R)
            p["win_rate"] = None
            if real_count >= 5:
                win_row = repo.conn.execute(
                    """SELECT COUNT(*) as wins FROM strategy_evaluations
                       WHERE strategy_name=%s AND strategy_version=%s AND is_shadow=TRUE AND pnl_r IS NOT NULL AND outcome_source='real_pnl' AND pnl_r > 0.005""",
                    (p.get("strategy_name"), p.get("candidate_version")),
                ).fetchone()
                if win_row:
                    wins = int(win_row["wins"]) or 0
                    p["win_rate"] = round(wins / real_count, 4)

    # Build standard patch_summary list — single source of truth for all return lists.
    # Never leak raw strategy_patches rows (which contain backtest_result_json, etc.).
    patch_summaries: list[dict[str, Any]] = []
    for p in related_patches:
        patch_summaries.append({
            "id": p["id"],
            "candidate_version": p.get("candidate_version"),
            "status": p.get("status"),
            "backtest_result": p.get("backtest_parsed"),
            "shadow_sample_count": p.get("sample_count", 0),
            "real_pnl_count": p.get("real_pnl_count", 0),
            "pseudo_r_count": p.get("pseudo_r_count", 0),
            "avg_r": p.get("avg_r"),
            "win_rate": p.get("win_rate"),
            "data_quality": p.get("data_quality", "unknown"),
        })

    review_required = [ps for ps in patch_summaries if ps.get("status") == "review_required"]
    shadow_testing = [ps for ps in patch_summaries if ps.get("status") == "shadow_testing"]
    rejected = [ps for ps in patch_summaries if ps.get("status") == "rejected"]

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

    # Compute global totals from the full query (not just window-filtered)
    open_trigger_count = repo.conn.execute(
        "SELECT COUNT(*) as cnt FROM evolution_triggers WHERE status IN ('pending','shadow_testing','review_required')"
    ).fetchone()
    total_trigger_count = repo.conn.execute(
        "SELECT COUNT(*) as cnt FROM evolution_triggers"
    ).fetchone()
    total_patch_count = repo.conn.execute(
        "SELECT COUNT(*) as cnt FROM strategy_patches"
    ).fetchone()

    return {
        "triggers": trigger_items,
        "patches": patch_summaries,
        "review_required": review_required,
        "shadow_testing": shadow_testing,
        "rejected": rejected,
        "total_triggers": int(total_trigger_count["cnt"]) if total_trigger_count else 0,
        "total_open_triggers": int(open_trigger_count["cnt"]) if open_trigger_count else 0,
        "total_patches": int(total_patch_count["cnt"]) if total_patch_count else 0,
    }


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
    repo: CryptoGuardRepository, all_closed: list[dict[str, Any]], all_review_items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute per-strategy performance stats from all_closed (all trades in window).

    Uses review data when available, falls back to trade.pnl_r for review-less trades.
    """
    from collections import defaultdict

    review_by_trade_id = {item["trade"]["id"]: item.get("review") for item in all_review_items}

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in all_closed:
        name = _get_strategy_name_for_trade(repo, trade)
        review = review_by_trade_id.get(trade["id"])
        groups[name].append({"trade": trade, "review": review})

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
    utc_start_dt = datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
    utc_end_dt = datetime.fromisoformat(str(end_utc).replace("Z", "+00:00"))
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


def _build_deterministic_report(
    *,
    all_closed: list[dict[str, Any]],
    all_review_items: list[dict[str, Any]],
    paper_summary: dict[str, Any],
    window_display: str,
    evo_status: dict[str, Any],
    strategy_perf: dict[str, Any],
    loss_analysis: list[dict[str, Any]],
    win_analysis: list[dict[str, Any]],
    key_findings: list[str] | None = None,
    strategy_actions: list[str] | None = None,
    risk_focus: list[str] | None = None,
) -> str:
    """Build a fully deterministic daily review report.

    The report is constructed from computed data only — no LLM-generated text is used
    as the base. LLM output (key_findings, strategy_actions, risk_focus) is appended
    as a separate supplementary section if available.
    """
    sections: list[str] = []

    # ── Header ──
    sections.append("**CryptoGuard 每日模拟盘复盘**")

    # ── 交易概览 (deterministic, from all_closed) ──
    review_by_trade_id = {item["trade"]["id"]: item.get("review") for item in all_review_items}
    total = len(all_closed)
    net_pnl = sum(float(t.get("pnl") or 0) for t in all_closed)

    pnl_rs_all: list[float] = []
    wins = 0
    losses = 0
    for t in all_closed:
        review = review_by_trade_id.get(t["id"])
        r = _trade_pnl_r(t, review if review else None)
        if r is not None:
            pnl_rs_all.append(r)
            if r > 0.05:
                wins += 1
            elif r < -0.05:
                losses += 1
    breakevens = total - wins - losses
    avg_r = sum(pnl_rs_all) / len(pnl_rs_all) if pnl_rs_all else 0.0

    sections.append("## 交易概览")
    sections.append(f"平仓交易: {total} 笔 (胜 {wins} / 负 {losses} / 平 {breakevens})")
    sections.append(f"净 PnL: {net_pnl:+.2f} USDT")
    sections.append(f"平均 R: {avg_r:+.2f}R")

    # ── 平仓明细 ──
    if all_closed:
        sections.append("## 平仓明细")
        for trade in all_closed[:20]:
            review = review_by_trade_id.get(trade["id"])
            pnl_r_val = _trade_pnl_r(trade, review if review else None)
            r_str = f"R={pnl_r_val:.2f}" if pnl_r_val is not None else "R=N/A"
            reason = trade.get("close_reason") or "-"
            sections.append(
                f"- #{trade['id']} {trade['symbol']} {trade['side']} "
                f"{r_str} reason={reason}"
            )

    # ── 分析窗口 ──
    if window_display:
        sections.append("## 分析窗口")
        sections.append(window_display)

    # ── 策略表现 ──
    if strategy_perf:
        perf_lines = ["## 策略表现"]
        for name, stats in sorted(strategy_perf.items()):
            perf_lines.append(
                f"- {name}: {stats['trades']}笔 "
                f"(胜{stats['wins']}/负{stats['losses']}/平{stats.get('breakevens', stats['trades'] - stats['wins'] - stats['losses'])}) "
                f"净PnL {stats['net_pnl']:+.2f} 平均R {stats['avg_r']:+.2f}"
            )
        sections.append("\n".join(perf_lines))

    # ── 亏损归因 ──
    if loss_analysis:
        loss_lines = ["## 亏损归因"]
        for loss in loss_analysis:
            loss_lines.append(
                f"- #{loss['trade_id']} {loss['symbol']} {loss['side']} "
                f"R={loss['pnl_r']:.2f} 原因: {loss.get('primary_reason', 'unknown')}"
            )
        sections.append("\n".join(loss_lines))

    # ── 胜场分析 ──
    if win_analysis:
        win_lines = ["## 胜场分析"]
        for win in win_analysis:
            win_lines.append(
                f"- #{win['trade_id']} {win['symbol']} {win['side']} "
                f"R={win['pnl_r']:.2f} 原因: {win.get('primary_reason', 'unknown')}"
            )
        sections.append("\n".join(win_lines))

    # ── 策略进化状态 ──
    if evo_status:
        evo_lines = ["## 策略进化状态"]
        for patch in evo_status.get("review_required", []):
            evo_lines.append(f"- [进入 review] patch#{patch['id']} {patch.get('candidate_version', '?')}")
        for patch in evo_status.get("shadow_testing", []):
            backtest_info = ""
            bt = patch.get("backtest_result", {})
            if bt:
                if bt.get("passed"):
                    backtest_info = " 回测通过"
                elif bt.get("gate_disabled"):
                    backtest_info = " 回测门禁未启用"
                elif bt.get("skipped"):
                    backtest_info = " 回测跳过"
                else:
                    backtest_info = f" 回测未通过"
            avg_r_str = ""
            if patch.get("avg_r") is not None:
                avg_r_str = f" avgR={patch['avg_r']:.2f}"
            samples_str = f" 样本={patch.get('shadow_sample_count', 0)}"
            dq = patch.get("data_quality", "")
            dq_str = f" [{dq}]" if dq else ""
            evo_lines.append(
                f"- [影子测试中] patch#{patch['id']} {patch.get('candidate_version', '?')}"
                f"{samples_str}{avg_r_str}{backtest_info}{dq_str}"
            )
        for patch in evo_status.get("rejected", []):
            bt = patch.get("backtest_result", {})
            reason_str = f" 原因: {bt.get('reason')}" if bt and bt.get("reason") else ""
            evo_lines.append(f"- [已拒绝] patch#{patch['id']} {patch.get('candidate_version', '?')}{reason_str}")
        sections.append("\n".join(evo_lines))

    # ── LLM Supplementary Insights (appended last, not interleaved) ──
    insights: list[str] = []
    if key_findings:
        insights.append("## LLM 补充分析")
        for f in key_findings:
            insights.append(f"- {f}")
    if strategy_actions:
        if not insights:
            insights.append("## LLM 补充分析")
        insights.append("### 策略建议")
        for a in strategy_actions:
            insights.append(f"- {a}")
    if risk_focus:
        if not insights:
            insights.append("## LLM 补充分析")
        insights.append("### 风险关注")
        for r in risk_focus:
            insights.append(f"- {r}")
    if insights:
        sections.append("\n".join(insights))

    # ── 免责声明 ──
    sections.append("")
    sections.append("所有策略补丁仍只进入 candidate，不会直接 active。不构成实盘建议。")

    return "\n\n".join(sections)


# Keep _enforce_deterministic_overview for backward compatibility with tests
def _enforce_deterministic_overview(report_text: str, all_review_items: list[dict[str, Any]], all_closed: list[dict[str, Any]]) -> str:
    """Legacy wrapper — delegates to _build_deterministic_report for a complete rebuild."""
    import re

    # Build review lookup
    review_by_trade_id = {item["trade"]["id"]: item.get("review") for item in all_review_items}
    total = len(all_closed)
    net_pnl = sum(float(t.get("pnl") or 0) for t in all_closed)

    pnl_rs_all: list[float] = []
    wins = 0
    losses = 0
    for t in all_closed:
        review = review_by_trade_id.get(t["id"])
        r = _trade_pnl_r(t, review if review else None)
        if r is not None:
            pnl_rs_all.append(r)
            if r > 0.05:
                wins += 1
            elif r < -0.05:
                losses += 1
    breakevens = total - wins - losses
    avg_r = sum(pnl_rs_all) / len(pnl_rs_all) if pnl_rs_all else 0.0

    overview_block = (
        f"## 交易概览\n"
        f"平仓交易: {total} 笔 (胜 {wins} / 负 {losses} / 平 {breakevens})\n"
        f"净 PnL: {net_pnl:+.2f} USDT\n"
        f"平均 R: {avg_r:+.2f}R"
    )

    # Strip any existing 交易概览 section: find the header line and remove
    # all following non-empty lines (stat lines) until a blank line or next heading
    lines = report_text.split('\n')
    filtered: list[str] = []
    skip_until_blank = False
    for line in lines:
        if '交易概览' in line:
            skip_until_blank = True
            continue
        if skip_until_blank:
            if line.strip() == '':
                skip_until_blank = False
                continue
            # Also stop if we hit a heading (## or **)
            if re.match(r'^#{1,4}\s', line.strip()) or re.match(r'^\*\*[^*]+\*\*', line.strip()):
                skip_until_blank = False
                filtered.append(line)
                continue
            # Skip stat lines (non-empty, non-heading lines after 交易概览 header)
            continue
        filtered.append(line)
    report_text = '\n'.join(filtered)
    report_text = re.sub(r'\n{3,}', '\n\n', report_text)
    report_text = report_text.strip()
    report_text = re.sub(r'\n{3,}', '\n\n', report_text)
    report_text = report_text.strip()

    if report_text:
        report_text = overview_block + "\n\n" + report_text
    else:
        report_text = overview_block

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
