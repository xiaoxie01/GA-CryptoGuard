from __future__ import annotations

import argparse
import json
import time
import traceback
from typing import Any, Callable

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.notify.alert_delivery import process_alert_outbox, send_markdown_alert
from plugins.crypto_guard.notify.hourly_report import build_hourly_report, resolve_report_target
from plugins.crypto_guard.notify.feishu_cards import build_analysis_card_json, render_text
from plugins.crypto_guard.notify.signal_policy import should_push_signal
from plugins.crypto_guard.ga_master import GAAnalysisRequest, GAMasterController
from plugins.crypto_guard.ga_master.decision_schema import controller_decision_from_legacy
from plugins.crypto_guard.ga_master.feishu_action_builder import build_feishu_actions
from plugins.crypto_guard.paper.paper_position_updater import update_paper_positions
from plugins.crypto_guard.review.daily_reviewer import run_daily_review
from plugins.crypto_guard.review.trade_reviewer import review_trade
from plugins.crypto_guard.scheduler.opportunity_watcher import render_watch_alert_text, update_opportunity_watches
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.redis_adapter import RedisAdapter, should_use_redis_for_path
from plugins.crypto_guard.storage.sqlite_db import connect_db
from plugins.crypto_guard.tools.ga_crypto_tools import crypto_handle_text_command
from plugins.crypto_guard.utils import utc_ms

LOGGER = get_logger("crypto_guard.worker")


def process_job(repo: CryptoGuardRepository, job: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    payload = json.loads(job["payload_json"])
    job_type = job["job_type"]
    LOGGER.info("process_job start id=%s type=%s priority=%s session=%s", job.get("id"), job_type, job.get("priority"), job.get("session_id"))
    if job_type == "feishu_user_message":
        result = crypto_handle_text_command(payload.get("text", ""), payload.get("open_id"))
        _maybe_send_feishu_result(repo, payload, result, send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s", job.get("id"), job_type, result.get("ok"))
        return result
    if job_type == "feishu_button_callback":
        result = handle_button_callback(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s", job.get("id"), job_type, result.get("ok"))
        return result
    if job_type == "scheduled_market_analysis":
        snapshot = payload["snapshot"]
        decision = GAMasterController(repo).analyze_symbol(
            GAAnalysisRequest(
                symbol=snapshot["symbol"],
                decision_type="scheduled_analysis",
                analysis_time_utc=int(snapshot.get("analysis_time_utc") or 0),
                mode=snapshot.get("mode") or "scheduled",
                snapshot=snapshot,
                snapshot_id=payload.get("snapshot_id"),
                allow_realtime_signal_alert=bool(payload.get("allow_realtime_signal_alert")),
            )
        )
        signal_id = int(decision["signal_id"])
        sent = False
        target = None

        # Auto-create paper order for S/A grade signals with valid trade plan
        auto_order = None
        grade = str(decision.get("signal_grade") or "D").upper()
        has_plan = bool(decision.get("has_trade_plan") and decision.get("trade_plan"))
        risk_ok = bool((decision.get("risk_check") or {}).get("ok"))
        ga_decision_id = decision.get("ga_decision_id")
        # Don't auto-create if there's already an open order for this symbol
        existing_orders = repo.list_open_paper_orders_for_symbol(decision.get("symbol", ""))
        if grade in {"S", "A"} and has_plan and risk_ok and ga_decision_id and not existing_orders:
            try:
                from plugins.crypto_guard.paper.paper_broker import create_paper_order_from_ga_decision
                auto_order = create_paper_order_from_ga_decision(repo, int(ga_decision_id))
                LOGGER.info("auto paper order created ga_decision_id=%s result=%s", ga_decision_id, auto_order)
                # Send notification when order is newly created (not idempotent)
                if auto_order.get("ok") and auto_order.get("created"):
                    _target = resolve_report_target(repo, payload)
                    if _target and send_message:
                        plan = decision.get("trade_plan") or {}
                        tps = ", ".join(str(tp.get("price")) for tp in plan.get("take_profits", []))
                        side_cn = {"LONG": "做多", "SHORT": "做空"}.get(str(plan.get("side") or "").upper(), plan.get("side") or "-")
                        entry_type = str(plan.get("entry_type") or "limit")
                        status_cn = "待成交挂单" if entry_type == "limit" else "已成交（市价）"
                        entry_price = plan.get("entry_price") or plan.get("trigger_price") or "-"
                        from datetime import datetime, timezone, timedelta
                        now_utc8 = datetime.now(timezone(timedelta(hours=8)))
                        order_text = "\n".join([
                            "**CryptoGuard 已自动创建模拟盘订单**",
                            "",
                            f"- 时间：{now_utc8.strftime('%Y-%m-%d %H:%M')} (UTC+8)",
                            f"- 产品：{decision.get('symbol')}",
                            f"- 方向：{side_cn}",
                            f"- 状态：{status_cn}",
                            f"- 入场价：{entry_price}",
                            f"- 止损价：{plan.get('stop_loss')}",
                            f"- 止盈价：{tps}",
                            f"- 信号等级：{grade}，置信度：{round(float(decision.get('confidence', 0)) * 100)}%",
                            "",
                            "不构成实盘建议，仅用于模拟盘与策略研究。",
                        ])
                        send_markdown_alert(
                            repo, send_message,
                            receive_id=_target["receive_id"],
                            receive_id_type=_target.get("receive_id_type", "chat_id"),
                            text=order_text,
                            alert_type="paper_order_filled",
                            symbol=decision.get("symbol"),
                            priority=3,
                        )
            except Exception as exc:
                LOGGER.warning("auto paper order failed ga_decision_id=%s error=%s", ga_decision_id, exc)
                auto_order = {"ok": False, "error": str(exc)}

        # Position-aware analysis: check if new analysis conflicts with open position
        open_trades = repo.list_open_paper_trades()
        symbol_trades = [t for t in open_trades if t.get("symbol") == decision.get("symbol")]
        if symbol_trades and send_message:
            _pos_target = resolve_report_target(repo, payload)
            if _pos_target:
                for trade in symbol_trades:
                    pos_side = str(trade.get("side") or "").upper()
                    new_bias = str(decision.get("market_bias") or "neutral").lower()
                    pos_cn = {"LONG": "做多", "SHORT": "做空"}.get(pos_side, pos_side)
                    bias_cn = {
                        "bullish": "偏多（bullish）",
                        "bearish": "偏空（bearish）",
                        "neutral": "中性（neutral）",
                        "mixed": "多空混合（mixed）",
                    }.get(new_bias, new_bias)
                    # Direction conflict: open LONG but analysis says bearish, or vice versa
                    if (pos_side == "LONG" and new_bias == "bearish") or (pos_side == "SHORT" and new_bias == "bullish"):
                        summary = decision.get("summary") or ""
                        from datetime import datetime, timezone, timedelta
                        now_utc8 = datetime.now(timezone(timedelta(hours=8)))
                        conflict_text = "\n".join([
                            f"**CryptoGuard 持仓方向冲突提醒**",
                            "",
                            f"- 时间：{now_utc8.strftime('%Y-%m-%d %H:%M')} (UTC+8)",
                            f"- 产品：{decision.get('symbol')}",
                            f"- 当前持仓：{pos_cn}（入场价 {trade.get('entry_price')}）",
                            f"- 最新研判：{bias_cn}",
                            f"- 信号等级：{grade}，置信度：{round(float(decision.get('confidence', 0)) * 100)}%",
                            f"- 研判摘要：{summary}" if summary else "",
                            f"- 建议：关注是否需要提前平仓或调整止损",
                            "",
                            "不构成实盘建议，仅用于模拟盘与策略研究。",
                        ])
                        send_markdown_alert(
                            repo, send_message,
                            receive_id=_pos_target["receive_id"],
                            receive_id_type=_pos_target.get("receive_id_type", "chat_id"),
                            text=conflict_text,
                            alert_type="risk_alert",
                            symbol=decision.get("symbol"),
                            priority=3,
                        )

        # v2: scheduled analysis is recorded into analysis_states/signals and summarized hourly.
        # Real-time Feishu alerts are reserved for paper/risk/opportunity events.
        if payload.get("allow_realtime_signal_alert") and should_push_signal(decision):
            target = resolve_report_target(repo, payload)
            if target and send_message:
                sent = bool(
                    _send_interactive_alert(
                        repo,
                        send_message,
                        target["receive_id"],
                        target.get("receive_id_type", "chat_id"),
                        build_analysis_card_json(decision, signal_id=signal_id),
                        alert_type="signal_alert",
                        symbol=decision.get("symbol"),
                        priority=5,
                    ).get("sent")
                )
        result = {"ok": True, "signal_id": signal_id, "decision": decision, "pushed": sent, "target": target, "auto_order": auto_order}
        LOGGER.info(
            "process_job done id=%s type=%s signal_id=%s grade=%s pushed=%s decision=%s",
            job.get("id"),
            job_type,
            signal_id,
            decision.get("signal_grade"),
            sent,
            decision.get("decision"),
        )
        return result
    if job_type == "update_opportunity_watches":
        result = update_opportunity_watches(repo, analysis_time_utc=payload.get("analysis_time_utc"))
        LOGGER.info("process_job done id=%s type=%s ok=%s triggered=%s", job.get("id"), job_type, result.get("ok"), result.get("triggered"))
        return result
    if job_type == "opportunity_watch_alert":
        result = handle_opportunity_watch_alert(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"))
        return result
    if job_type == "trade_review":
        result = review_trade(repo, int(payload["trade_id"]))
        LOGGER.info("process_job done id=%s type=%s ok=%s", job.get("id"), job_type, result.get("ok"))
        return result
    if job_type == "daily_review":
        result = run_daily_review(repo, day_utc=payload.get("day_utc"))
        target = resolve_report_target(repo, payload)
        loss_count = payload.get("loss_count")
        loss_header = f"（今日 {loss_count} 笔止损触发复盘）\n" if loss_count else ""
        # Build detailed evolution status
        evolution_text = _build_evolution_status_text(repo)
        full_text = loss_header + result["text"] + evolution_text

        # Three-layer push defense:
        # L1: run_daily_review(force=False) already returns idempotent if report exists
        # L2: check pushed_to_feishu before sending
        # L3: alert dedupe_key includes review_date
        review_date = result.get("day_start_utc", "")[:10]
        already_pushed = result.get("pushed_to_feishu")
        if target and send_message and not already_pushed:
            sent_result = send_markdown_alert(
                repo, send_message,
                receive_id=target["receive_id"],
                receive_id_type=target.get("receive_id_type", "chat_id"),
                text=full_text,
                alert_type="daily_review",
                priority=5,
                dedupe_key=f"daily_review:{review_date}",
            )
            result["sent"] = bool(sent_result.get("sent"))
            result["target"] = target
            # Mark pushed_to_feishu on successful send
            if sent_result.get("sent") and review_date:
                repo.conn.execute(
                    "UPDATE daily_review_reports SET pushed_to_feishu=1 WHERE review_date=?",
                    (review_date,),
                )
        else:
            result["sent"] = False
            result["target"] = target
        LOGGER.info("process_job done id=%s type=%s ok=%s reviews=%s sent=%s idempotent=%s", job.get("id"), job_type, result.get("ok"), result.get("new_reviews"), result.get("sent"), result.get("idempotent"))
        return result
    if job_type == "intraday_loss_review":
        result = _handle_intraday_loss_review(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"))
        return result
    if job_type == "hourly_feishu_report":
        report = build_hourly_report(repo)
        target = resolve_report_target(repo, payload)
        if target and send_message:
            sent_result = send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=report["text"], alert_type="hourly_summary", priority=3)
            report["sent"] = bool(sent_result.get("sent"))
            report["target"] = target
        else:
            report["sent"] = False
            report["target"] = target
        LOGGER.info("process_job done id=%s type=%s sent=%s", job.get("id"), job_type, report.get("sent"))
        return report
    if job_type == "update_paper_positions":
        result = update_paper_positions(repo)
        LOGGER.info("process_job done id=%s type=%s ok=%s", job.get("id"), job_type, result.get("ok"))
        return result
    if job_type == "paper_event_alert":
        result = handle_paper_event_alert(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"))
        return result
    if job_type == "alert_outbox_retry":
        result = process_alert_outbox(repo, send_message, limit=int(payload.get("limit") or 10))
        LOGGER.info("process_job done id=%s type=%s processed=%s sent=%s", job.get("id"), job_type, result.get("processed"), result.get("sent"))
        return result
    if job_type == "paper_drawdown_alert":
        result = handle_paper_drawdown_alert(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"))
        return result
    if job_type == "evolution_trigger_alert":
        result = handle_evolution_trigger_alert(repo, payload, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s sent=%s queued=%s", job.get("id"), job_type, result.get("ok"), result.get("sent"), result.get("queued"))
        return result
    if job_type == "pending_order_management":
        from plugins.crypto_guard.paper.pending_order_manager import run_pending_order_management
        result = run_pending_order_management(repo, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s expired=%s cancelled=%s", job.get("id"), job_type, result.get("ok"), result.get("expire", {}).get("expired_count"), result.get("conflict", {}).get("cancelled_count"))
        return result
    if job_type == "pending_order_revalidation":
        from plugins.crypto_guard.paper.pending_revalidator import revalidate_pending_orders
        result = revalidate_pending_orders(repo, send_message=send_message)
        LOGGER.info("process_job done id=%s type=%s ok=%s reviewed=%s actions=%s", job.get("id"), job_type, result.get("ok"), result.get("reviewed_count"), result.get("actions_count"))
        return result
    return {"ok": False, "error": f"未知 job_type: {job_type}"}


def _handle_intraday_loss_review(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Handle intraday loss threshold alert — risk warning only, NOT daily review.

    Does NOT write daily_review_reports or skill_feedback_memory.
    Only pushes a risk alert and optionally evaluates evolution triggers.
    """
    from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers

    day_utc = payload.get("day_utc", "")
    loss_count = int(payload.get("loss_count") or 0)
    target = resolve_report_target(repo, payload)

    # Evaluate evolution triggers (creates/updates trigger, does NOT create candidate)
    evolution = evaluate_evolution_triggers(repo)

    # Build risk alert text
    lines = [
        "**CryptoGuard 盘中风险提醒 · 止损阈值触发**",
        "",
        f"- 日期：{day_utc}",
        f"- 今日止损：{loss_count} 笔",
        f"- 进化状态：{'已触发' if evolution.get('triggered') else '未触发'}",
        "",
        "系统将继续监控，不影响现有模拟盘持仓。",
        "",
        "不构成实盘建议，仅用于模拟盘与策略研究。",
    ]
    text = "\n".join(lines)

    sent = False
    if target and send_message:
        loss_bucket = "3_loss" if loss_count <= 3 else "5_loss"
        sent_result = send_markdown_alert(
            repo, send_message,
            receive_id=target["receive_id"],
            receive_id_type=target.get("receive_id_type", "chat_id"),
            text=text,
            alert_type="intraday_loss_review",
            priority=4,
            dedupe_key=f"intraday_loss_review:{day_utc}:{loss_bucket}",
        )
        sent = bool(sent_result.get("sent"))

    return {
        "ok": True,
        "sent": sent,
        "target": target,
        "loss_count": loss_count,
        "day_utc": day_utc,
        "evolution": evolution,
    }


def handle_button_callback(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    from plugins.crypto_guard.data.symbol_registry import add_symbol
    from plugins.crypto_guard.paper.paper_broker import create_paper_order_from_ga_decision, create_paper_order_from_signal

    action = payload.get("action")
    symbol = payload.get("symbol")
    signal_id = payload.get("signal_id")
    ga_decision_id = payload.get("ga_decision_id")
    if action == "create_paper_order":
        result = create_paper_order_from_ga_decision(repo, int(ga_decision_id)) if ga_decision_id else create_paper_order_from_signal(repo, int(signal_id))
    elif action == "add_to_watchlist":
        result = add_symbol(repo, symbol, validate=False)
    elif action == "create_opportunity_watch":
        ga_decision = repo.get_ga_decision(int(ga_decision_id)) if ga_decision_id else None
        if ga_decision:
            actions = set(ga_decision.get("feishu_actions") or [])
            grade = str(ga_decision.get("signal_grade") or "D").upper()
            watch = ga_decision.get("opportunity_watch") or {}
            if "create_opportunity_watch" not in actions or grade in {"D", "C"}:
                result = {"ok": False, "error": "该 GA decision 不允许加入机会监控"}
            elif not watch:
                result = {"ok": False, "error": "该 GA decision 没有机会监控条件"}
            else:
                result = {
                    "ok": True,
                    "watch_id": repo.create_opportunity_watch(
                        symbol or ga_decision["symbol"],
                        watch,
                        source_signal_id=int(signal_id) if signal_id else None,
                        ga_decision_id=int(ga_decision_id),
                        created_by_user_action=True,
                        source_button_action=action,
                    ),
                }
        else:
            signal = repo.get_signal(int(signal_id)) if signal_id else None
            watch = json.loads(signal.get("opportunity_watch_json") or "{}") if signal else {}
            if not signal:
                result = {"ok": False, "error": "该 signal 不存在"}
            elif str(signal.get("signal_grade") or "D").upper() in {"D", "C"}:
                result = {"ok": False, "error": "D/C 级信号不允许加入机会监控"}
            elif not watch:
                result = {"ok": False, "error": "该 signal 没有机会监控条件"}
            else:
                compat_ga_decision_id = signal.get("ga_decision_id") or _ensure_ga_decision_for_watch_signal(repo, signal, watch)
                result = {
                    "ok": True,
                    "watch_id": repo.create_opportunity_watch(
                        symbol or signal["symbol"],
                        watch,
                        source_signal_id=int(signal_id),
                        ga_decision_id=int(compat_ga_decision_id),
                        created_by_user_action=True,
                        source_button_action=action,
                    ),
                }
    elif action == "ignore":
        marked = repo.mark_ad_hoc_analysis_status_by_signal(int(signal_id), "ignored") if signal_id else False
        result = {"ok": True, "ignored": True, "ad_hoc_marked": marked}
    elif action == "approve_evolution":
        candidate_version = payload.get("candidate_version")
        if not candidate_version:
            result = {"ok": False, "error": "missing candidate_version"}
        else:
            # Find strategy name from strategy_versions
            row = repo.conn.execute(
                "SELECT strategy_name FROM strategy_versions WHERE version=?",
                (candidate_version,)
            ).fetchone()
            strategy_name = row["strategy_name"] if row else "smc_pullback_long"

            from plugins.crypto_guard.strategy.shadow_testing import promote_shadow_candidate
            result = promote_shadow_candidate(
                repo,
                strategy_name=strategy_name,
                candidate_version=candidate_version,
                confirm=True,
                change_reason="manual approve from Feishu evolution review",
            )

            # Only update trigger and patches if promotion succeeded
            if result.get("ok"):
                # Update trigger resolved_at
                repo.conn.execute(
                    "UPDATE evolution_triggers SET resolved_at=datetime('now'), status='active' WHERE id IN "
                    "(SELECT trigger_id FROM strategy_patches WHERE candidate_version=? AND trigger_id IS NOT NULL)",
                    (candidate_version,)
                )
                repo.conn.execute(
                    "UPDATE strategy_patches SET status='active' WHERE candidate_version=? AND status NOT IN ('rejected', 'duplicate', 'active')",
                    (candidate_version,)
                )
                repo.conn.commit()
    elif action == "reject_evolution":
        candidate_version = payload.get("candidate_version")
        if not candidate_version:
            result = {"ok": False, "error": "missing candidate_version"}
        else:
            # Update all 3 tables to rejected
            repo.conn.execute(
                "UPDATE strategy_versions SET status='rejected', change_reason='manual reject from Feishu' WHERE version=?",
                (candidate_version,)
            )
            repo.conn.execute(
                "UPDATE strategy_patches SET status='rejected' WHERE candidate_version=? AND status NOT IN ('rejected', 'duplicate')",
                (candidate_version,)
            )
            repo.conn.execute(
                "UPDATE evolution_triggers SET status='rejected', resolved_at=datetime('now') WHERE id IN "
                "(SELECT trigger_id FROM strategy_patches WHERE candidate_version=? AND trigger_id IS NOT NULL)",
                (candidate_version,)
            )
            repo.conn.commit()
            result = {"ok": True, "action": "reject_evolution", "candidate_version": candidate_version}
    else:
        result = {"ok": False, "error": f"未知按钮动作: {action}"}
    if send_message and payload.get("receive_id"):
        send_markdown_alert(
            repo,
            send_message,
            receive_id=payload["receive_id"],
            receive_id_type=payload.get("receive_id_type", "open_id"),
            text=_button_result_text(action, result),
            alert_type="button_callback_result",
            symbol=symbol,
            priority=2,
        )
    return result


def handle_opportunity_watch_alert(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    watch = repo.get_opportunity_watch(int(payload["watch_id"]))
    if not watch:
        return {"ok": False, "error": "opportunity_watch 不存在", "sent": False}
    target = resolve_report_target(repo, payload)
    text = render_watch_alert_text(watch, payload.get("result") or {})
    sent = False
    if target and send_message:
        sent = bool(send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=text, alert_type="opportunity_triggered", symbol=watch.get("symbol"), priority=3).get("sent"))
    return {"ok": True, "watch_id": watch["id"], "sent": sent, "target": target, "text": text}


def handle_paper_event_alert(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    target = resolve_report_target(repo, payload)
    event_type = payload.get("event_type", "paper_event")
    event_cn = {
        "paper_order_filled": "已成交",
        "paper_order_expired": "已过期",
        "take_profit_hit": "止盈触发",
        "stop_loss_hit": "止损触发",
        "stop_loss_adjustment": "止损调整",
        "close_position": "手动平仓",
        "risk_alert": "风险提醒",
        "opportunity_triggered": "机会触发",
    }.get(event_type, event_type)
    side_cn = {"LONG": "做多", "SHORT": "做空"}.get(str(payload.get("side") or "").upper(), payload.get("side") or "-")
    fill_method_cn = {
        "limit_range_touch": "限价触及",
        "trigger_touch": "触发价触及",
        "next_candle_open_with_slippage": "市价成交（含滑点）",
    }.get(payload.get("fill_method"), payload.get("fill_method") or "")
    close_reason_cn = {
        "take_profit": "止盈",
        "stop_loss": "止损",
        "timeout": "超时平仓",
        "manual": "手动平仓",
    }.get(payload.get("close_reason"), payload.get("close_reason") or "")

    # Calculate USDT P&L from R-multiple
    pnl_r = payload.get("pnl_r")
    pnl_usdt_text = ""
    if pnl_r is not None:
        order_id = payload.get("order_id")
        if order_id:
            try:
                order_row = repo.conn.execute("SELECT entry_price, stop_loss FROM paper_orders WHERE id=?", (int(order_id),)).fetchone()
                if order_row:
                    entry = float(order_row["entry_price"] or 0)
                    stop = float(order_row["stop_loss"] or 0)
                    risk_per_unit = abs(entry - stop)
                    risk_pct = 0.005  # 0.5% default
                    risk_usdt = 10000.0 * risk_pct  # 10000U starting equity * 0.5%
                    pnl_usdt = float(pnl_r) * risk_usdt
                    pnl_usdt_text = f"（{pnl_usdt:+.2f}U）"
            except Exception:
                pass

    # Build event-specific details
    detail_lines = []
    # Add UTC+8 timestamp
    from datetime import datetime, timezone, timedelta
    now_utc8 = datetime.now(timezone(timedelta(hours=8)))
    detail_lines.append(f"- 时间：{now_utc8.strftime('%Y-%m-%d %H:%M')} (UTC+8)")
    if event_type == "stop_loss_adjustment":
        new_stop = payload.get("new_stop_loss")
        adj_reason = payload.get("reason", "")
        if new_stop:
            detail_lines.append(f"- 新止损：{new_stop}")
        if adj_reason:
            detail_lines.append(f"- 原因：{adj_reason}")
    elif event_type in ("take_profit_hit", "stop_loss_hit", "close_position"):
        reason = close_reason_cn
        detail_lines.append(f"- 原因：{reason}")
        # Entry details
        entry_price = payload.get("entry_price")
        if entry_price:
            detail_lines.append(f"- 入场价：{float(entry_price):.4f}")
        filled_at = payload.get("filled_at")
        if filled_at:
            try:
                # filled_at is UTC ISO string, convert to UTC+8 for display
                from datetime import datetime as _dt
                filled_dt = _dt.fromisoformat(str(filled_at).replace("Z", "+00:00"))
                filled_cn = filled_dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
                detail_lines.append(f"- 入场时间：{filled_cn} (UTC+8)")
            except Exception:
                detail_lines.append(f"- 入场时间：{filled_at}")
        # TP/SL prices
        stop_loss = payload.get("stop_loss")
        if stop_loss:
            detail_lines.append(f"- 止损价：{float(stop_loss):.4f}")
        take_profits = payload.get("take_profits") or []
        if take_profits:
            tp_prices = [f"{float(tp.get('price', tp)):.4f}" if isinstance(tp, dict) else f"{float(tp):.4f}" for tp in take_profits]
            detail_lines.append(f"- 止盈价：{', '.join(tp_prices)}")
        # Exit price
        exit_price = payload.get("exit_price")
        if exit_price:
            detail_lines.append(f"- 退出价：{float(exit_price):.4f}")
        if pnl_r is not None:
            detail_lines.append(f"- 盈亏：{float(pnl_r):+.2f}R{pnl_usdt_text}")
    elif event_type == "paper_order_filled":
        if fill_method_cn:
            detail_lines.append(f"- 成交方式：{fill_method_cn}")
        stop_loss = payload.get("stop_loss")
        if stop_loss:
            detail_lines.append(f"- 止损价：{float(stop_loss):.4f}")
        take_profits = payload.get("take_profits") or []
        if take_profits:
            tp_prices = [f"{float(tp.get('price', tp)):.4f}" if isinstance(tp, dict) else f"{float(tp):.4f}" for tp in take_profits]
            detail_lines.append(f"- 止盈价：{', '.join(tp_prices)}")
    else:
        reason = close_reason_cn or fill_method_cn
        if reason:
            detail_lines.append(f"- 原因：{reason}")
        if pnl_r is not None:
            detail_lines.append(f"- 盈亏：{float(pnl_r):+.2f}R{pnl_usdt_text}")

    lines = [
        f"**CryptoGuard 模拟盘 · {event_cn}**",
        "",
        f"- 产品：{payload.get('symbol', '-')}",
        f"- 方向：{side_cn}",
        f"- 订单：#{payload.get('order_id', '-')}",
        f"- 价格：{payload.get('entry_price') or payload.get('exit_price') or '-'}",
    ] + detail_lines + [
        "",
        "不构成实盘建议，仅用于模拟盘与策略研究。",
    ]
    text = "\n".join(lines)
    sent = False
    if target and send_message:
        sent = bool(send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=text, alert_type=str(event_type), symbol=payload.get("symbol"), priority=3).get("sent"))
    return {"ok": True, "sent": sent, "target": target, "text": text}


def handle_paper_drawdown_alert(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    snapshot = payload.get("snapshot") or {}
    target = resolve_report_target(repo, payload)
    text = "\n".join(
        [
            "**CryptoGuard 模拟盘回撤提醒**",
            "",
            f"- 账户权益：{float(snapshot.get('account_equity') or 0):.2f}",
            f"- 已实现盈亏：{float(snapshot.get('realized_pnl') or 0):.2f}",
            f"- 未实现盈亏：{float(snapshot.get('unrealized_pnl') or 0):.2f}",
            f"- 回撤：{float(snapshot.get('drawdown_percent') or 0):.2f}%",
            "",
            "不构成实盘建议，仅用于模拟盘与策略研究。",
        ]
    )
    sent = False
    if target and send_message:
        sent = bool(send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=text, alert_type="risk_alert", priority=3).get("sent"))
    return {"ok": True, "sent": sent, "target": target, "text": text}


def _build_evolution_status_text(repo: CryptoGuardRepository) -> str:
    """Build detailed evolution status text for daily review notification."""
    import json
    lines = []

    # Get recent evolution triggers
    triggers = repo.conn.execute(
        "SELECT * FROM evolution_triggers WHERE status IN ('pending', 'shadow_testing', 'review_required') ORDER BY id DESC LIMIT 5"
    ).fetchall()

    if not triggers:
        return ""

    lines.append("")
    lines.append("---")
    lines.append("**自进化状态**")
    lines.append("")

    for t in triggers:
        t = dict(t)
        trigger_type_cn = {
            "consecutive_stop_losses": "连续止损",
            "daily_loss_threshold": "单日止损",
            "account_drawdown": "账户回撤",
        }.get(t.get("trigger_type"), t.get("trigger_type"))

        trigger_value = t.get("trigger_value", 0)
        threshold = t.get("threshold_value", 0)
        related_ids = []
        try:
            related_ids = json.loads(t.get("related_trade_ids") or "[]")
        except Exception:
            pass

        status_cn = {
            "pending": "待处理",
            "shadow_testing": "影子测试中",
            "active": "已激活",
            "rejected": "已拒绝",
        }.get(t.get("status"), t.get("status"))

        lines.append(f"[{status_cn}] {trigger_type_cn}")
        lines.append(f"  触发值：{trigger_value}（阈值 {threshold}）")
        if related_ids:
            ids_str = "/".join(f"#{tid}" for tid in related_ids[:5])
            lines.append(f"  关联交易：{ids_str}")
        lines.append(f"  创建时间：{t.get('created_at', '-')}")
        lines.append("")

    # Get related patches
    patches = repo.conn.execute(
        "SELECT * FROM strategy_patches WHERE status IN ('candidate', 'shadow_testing') ORDER BY id DESC LIMIT 5"
    ).fetchall()

    if patches:
        lines.append("**候选补丁**")
        lines.append("")
        for p in patches:
            p = dict(p)
            patch_json = {}
            try:
                patch_json = json.loads(p.get("patch_json") or "{}")
            except Exception:
                pass

            patch_id = p.get("id")
            version = p.get("candidate_version", "-")
            reason = p.get("reason", "-")
            created = p.get("created_at", "-")[:16]

            # Get shadow test results if available
            shadow_results = repo.conn.execute(
                "SELECT COUNT(*) as total, SUM(CASE WHEN pnl_r > 0.05 THEN 1 ELSE 0 END) as wins FROM paper_trades WHERE close_reason IS NOT NULL AND created_at >= ?",
                (p.get("created_at", "2000-01-01"),)
            ).fetchone()
            total = int(shadow_results["total"] or 0) if shadow_results else 0
            wins = int(shadow_results["wins"] or 0) if shadow_results else 0

            if total > 0:
                wr = wins / total * 100
                lines.append(f"Patch #{patch_id}（{version}）：{reason}")
                lines.append(f"  影子测试：{total}笔交易，胜率 {wr:.0f}%（{wins}W/{total - wins}L）")
            else:
                lines.append(f"Patch #{patch_id}（{version}）：{reason}")
                lines.append(f"  影子测试：暂无数据")

            lines.append(f"  创建时间：{created}")
            lines.append("")

    # Next steps - use actual config values
    from plugins.crypto_guard.config.loader import load_config as _load_cfg
    _cfg = _load_cfg().trading_mode
    _online_cfg = _cfg.get("evolution", {}).get("online_shadow", {})
    _min_after_bt = _online_cfg.get("min_samples_after_backtest", 5)
    _min_without_bt = _online_cfg.get("min_samples_without_backtest", 30)
    _backtest_enabled = _cfg.get("evolution", {}).get("backtest_gate", {}).get("enabled", True)

    lines.append("**下一步**")
    if _backtest_enabled:
        lines.append(f"- 影子测试需 {_min_after_bt} 个样本（通过回测门禁后）或 {_min_without_bt} 个样本（未通过回测）")
    else:
        lines.append(f"- 影子测试需至少 {_min_without_bt} 个样本确认效果")
    lines.append("- 胜率和盈亏比达标后可进入 review 阶段")
    lines.append("- review 通过后可手动确认进入 active")
    lines.append("")

    return "\n".join(lines)


def _get_backtest_status(repo: CryptoGuardRepository, candidate_version: str) -> dict[str, Any]:
    """Get backtest result for a candidate version."""
    import json
    row = repo.conn.execute(
        "SELECT backtest_result_json FROM strategy_patches WHERE candidate_version=? AND backtest_result_json IS NOT NULL ORDER BY id DESC LIMIT 1",
        (candidate_version,)
    ).fetchone()
    if row and row["backtest_result_json"]:
        try:
            return json.loads(row["backtest_result_json"])
        except Exception:
            pass
    return {"status": "unknown", "skipped": True, "reason": "no_backtest_data"}


def handle_evolution_trigger_alert(repo: CryptoGuardRepository, payload: dict[str, Any], *, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Send immediate notification when evolution is triggered or verdict promotes."""
    import json

    # Cleanup old text-type evolution_review alerts (should be interactive)
    repo.conn.execute(
        "UPDATE alert_outbox SET status='superseded' WHERE alert_type='evolution_review' AND payload_json LIKE '%\"msg_type\": \"text\"%' AND status IN ('pending', 'sent')"
    )
    repo.conn.commit()

    target = resolve_report_target(repo, payload)
    trigger_type = payload.get("trigger_type", "unknown")
    loss_count = payload.get("loss_count", 0)
    day = payload.get("day_utc", "-")
    trigger_id = payload.get("trigger_id")
    patch_id = payload.get("patch_id")
    reason = payload.get("reason", "")
    related_ids = payload.get("related_trade_ids") or []
    trigger_value = payload.get("trigger_value")
    threshold = payload.get("threshold_value")
    candidate_version = payload.get("candidate_version")
    sample_count = payload.get("sample_count", 0)

    trigger_type_cn = {
        "consecutive_stop_losses": "连续止损",
        "daily_loss_threshold": "单日止损",
        "account_drawdown": "账户回撤",
        "verdict_promotion": "影子测试通过",
    }.get(trigger_type, trigger_type)

    # Build trigger detail
    detail_lines = [f"**CryptoGuard 自进化触发**", ""]

    if trigger_type == "verdict_promotion":
        # Special handling for verdict promotion
        detail_lines.append(f"- 触发类型：{trigger_type_cn}")
        detail_lines.append(f"- 候选版本：{candidate_version}")
        detail_lines.append(f"- 影子样本数：{sample_count}")
        detail_lines.append(f"- 原因：{reason}")
        detail_lines.append("")
        detail_lines.append("候选策略已通过影子测试，等待人工确认升级。")
        detail_lines.append("")
        detail_lines.append("**请审核以下内容后决定是否批准：**")
        detail_lines.append("1. 候选策略的改进逻辑是否合理")
        detail_lines.append("2. 影子测试的样本量是否足够")
        detail_lines.append("3. 是否存在过拟合风险")
    else:
        # Original trigger handling
        detail_lines.append(f"- 触发类型：{trigger_type_cn}")
        if trigger_value and threshold:
            detail_lines.append(f"- 触发值：{trigger_value}（阈值 {threshold}）")
        if loss_count:
            detail_lines.append(f"- 今日止损：{loss_count} 笔")
        if reason:
            detail_lines.append(f"- 原因：{reason}")
        if related_ids:
            ids_str = "/".join(f"#{tid}" for tid in related_ids[:5])
            detail_lines.append(f"- 关联交易：{ids_str}")
        if trigger_id:
            detail_lines.append(f"- 触发器 ID：#{trigger_id}")
        if patch_id:
            detail_lines.append(f"- 候选补丁 ID：#{patch_id}")
        detail_lines.append("")
        detail_lines.append("系统已自动创建候选补丁并进入影子测试。")

    # Use actual config values for sample requirement
    from plugins.crypto_guard.config.loader import load_config as _load_cfg2
    _cfg2 = _load_cfg2().trading_mode
    _online_cfg2 = _cfg2.get("evolution", {}).get("online_shadow", {})
    _min_after_bt2 = _online_cfg2.get("min_samples_after_backtest", 5)
    _min_without_bt2 = _online_cfg2.get("min_samples_without_backtest", 30)
    _backtest_enabled2 = _cfg2.get("evolution", {}).get("backtest_gate", {}).get("enabled", True)

    if trigger_type != "verdict_promotion":
        if _backtest_enabled2:
            detail_lines.append(f"影子测试需 {_min_after_bt2} 个样本（通过回测门禁）或 {_min_without_bt2} 个样本（未通过回测）后方可进入 review。")
        else:
            detail_lines.append(f"影子测试需至少 {_min_without_bt2} 个样本确认效果后方可进入 review。")

    # Get full evolution status
    evolution_text = _build_evolution_status_text(repo)

    text = "\n".join(detail_lines) + "\n" + evolution_text + "\n不构成实盘建议，所有策略变更仅进入 candidate/shadow 流程。"

    sent = False
    queued = False

    # For verdict_promotion: always enqueue to outbox if target exists (independent of send_message)
    if target and trigger_type == "verdict_promotion" and candidate_version:
        # Send card via outbox for retry capability
        from plugins.crypto_guard.notify.feishu_cards import build_evolution_review_card
        backtest_status = _get_backtest_status(repo, candidate_version)
        card = build_evolution_review_card(candidate_version, sample_count, reason, backtest_status=backtest_status)

        # Use alert_outbox for reliable delivery
        alert_id = repo.enqueue_alert(
            alert_type="evolution_review",
            symbol=None,
            priority=4,
            payload={
                "receive_id": target["receive_id"],
                "receive_id_type": target.get("receive_id_type", "chat_id"),
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
                "fallback_text": f"CryptoGuard 自进化人工审核: {candidate_version}, {sample_count} 样本",
            },
            dedupe_key=f"evolution_review:{candidate_version}",
        )
        queued = bool(alert_id)
        sent = queued  # For backward compatibility

    # For other types: use send_markdown_alert (requires send_message)
    elif target and send_message:
        sent = bool(send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=text, alert_type="evolution_trigger", priority=4).get("sent"))

    return {"ok": True, "sent": sent, "queued": queued, "target": target, "text": text}


def run_once(*, user_only: bool = False, background: bool = False, send_message: Callable[..., Any] | None = None) -> dict[str, Any]:
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        repo = CryptoGuardRepository(conn)
        redis = RedisAdapter() if should_use_redis_for_path(cfg.database_path) else None
        redis_payload = (redis.pop_user_job() if user_only else (redis.pop_background_job() if background else None)) if redis else None
        if redis_payload and redis_payload.get("database_path"):
            db_row = conn.execute("PRAGMA database_list").fetchone()
            current_db = db_row["file"] if db_row and "file" in db_row.keys() else None
            if current_db and str(redis_payload.get("database_path")) != str(current_db):
                redis_payload = None
        if redis_payload:
            payload = redis_payload.get("payload") or {}
            sqlite_job_id = redis_payload.get("sqlite_job_id")
            if sqlite_job_id:
                claimed = repo.conn.execute(
                    "UPDATE agent_jobs SET status='running', started_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                    (int(sqlite_job_id),),
                )
                if claimed.rowcount != 1:
                    redis_payload = None
            if not redis_payload:
                job = repo.claim_next_job(max_priority=2) if user_only else repo.claim_next_job(background=background)
                if not job:
                    return {"ok": True, "processed": False, "reason": "redis_payload_stale"}
                result = process_job(repo, job, send_message=send_message)
                repo.finish_job(job["id"], result=result)
                return {"ok": True, "processed": True, "job_id": job["id"], "result": result, "queue": "sqlite_after_stale_redis"}
            job = {
                "id": sqlite_job_id or redis_payload.get("redis_job_id") or "redis",
                "job_type": redis_payload.get("job_type"),
                "priority": redis_payload.get("priority", 1),
                "source": redis_payload.get("source", "redis"),
                "session_id": redis_payload.get("session_id", "redis"),
                "payload_json": json.dumps(payload, ensure_ascii=False),
            }
            try:
                result = process_job(repo, job, send_message=send_message)
                if sqlite_job_id:
                    repo.finish_job(int(sqlite_job_id), result=result)
                return {"ok": True, "processed": True, "job_id": job["id"], "result": result, "queue": "redis"}
            except Exception as exc:
                if sqlite_job_id:
                    repo.finish_job(int(sqlite_job_id), error_message=str(exc))
                raise
        job = repo.claim_next_job(max_priority=2) if user_only else repo.claim_next_job(background=background)
        if not job:
            if background:
                outbox = process_alert_outbox(repo, send_message, limit=10)
                if outbox.get("processed"):
                    return {"ok": True, "processed": True, "job_id": None, "result": outbox}
                # Run shadow verdict runner periodically when idle in background mode
                try:
                    from plugins.crypto_guard.strategy.shadow_testing import run_shadow_verdict_runner
                    verdict_result = run_shadow_verdict_runner(repo)
                    if verdict_result.get("processed"):
                        LOGGER.info("shadow_verdict_runner processed=%s", verdict_result.get("processed"))
                except Exception:
                    LOGGER.exception("shadow_verdict_runner failed")
            return {"ok": True, "processed": False}
        try:
            result = process_job(repo, job, send_message=send_message)
            repo.finish_job(job["id"], result=result)
            return {"ok": True, "processed": True, "job_id": job["id"], "result": result}
        except Exception as exc:
            LOGGER.exception("process_job failed id=%s type=%s", job.get("id"), job.get("job_type"))
            _send_job_error_to_user(repo, job, exc, send_message)
            repo.finish_job(job["id"], error_message=str(exc))
            raise
    finally:
        conn.close()


def run_loop(*, user_only: bool = False, background: bool = False, sleep_seconds: float = 1.0) -> None:
    while True:
        try:
            run_once(user_only=user_only, background=background)
        except KeyboardInterrupt:
            raise
        except Exception:
            LOGGER.exception("run_loop iteration failed")
            traceback.print_exc()
        time.sleep(sleep_seconds)


def _maybe_send_feishu_result(
    repo: CryptoGuardRepository,
    payload: dict[str, Any],
    result: dict[str, Any],
    send_message: Callable[..., Any] | None = None,
) -> None:
    if not send_message or not payload.get("receive_id"):
        return
    message_id = str(payload.get("message_id") or "").strip()
    if message_id:
        lock_name = f"feishu_result_sent:{message_id}"
        if not repo.acquire_lock(lock_name, "feishu_result_sender", 24 * 60 * 60):
            LOGGER.info("skip duplicate feishu result send message_id=%s", message_id)
            return
    receive_id = payload["receive_id"]
    receive_id_type = payload.get("receive_id_type", "open_id")
    if result.get("card_json"):
        sent_result = _send_interactive_alert(
            repo,
            send_message,
            receive_id,
            receive_id_type,
            result["card_json"],
            alert_type="ad_hoc_analysis",
            symbol=result.get("symbol"),
            priority=1,
        )
        if sent_result.get("silenced"):
            LOGGER.info("ad hoc analysis card silenced receive_id=%s signal_id=%s", receive_id, result.get("signal_id"))
        elif not sent_result.get("sent"):
            LOGGER.warning(
                "send interactive card failed or queued for retry receive_id=%s signal_id=%s alert_id=%s error=%s",
                receive_id,
                result.get("signal_id"),
                sent_result.get("alert_id"),
                sent_result.get("error"),
            )
    elif result.get("decision"):
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=receive_id_type, text=render_text(result["decision"], signal_id=result.get("signal_id")), alert_type="ad_hoc_analysis_text", symbol=(result.get("decision") or {}).get("symbol"), priority=1)
    elif result.get("text"):
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=receive_id_type, text=result["text"], alert_type="user_command_result", priority=1)
    elif isinstance(result.get("symbols"), list):
        rows = result.get("symbols", [])
        text = _render_symbol_list(rows)
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=receive_id_type, text=text, alert_type="symbol_list", priority=1)
    else:
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=receive_id_type, text=f"**CryptoGuard 返回结果**\n\n```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```", alert_type="user_command_result", priority=1)


def _send_job_error_to_user(repo: CryptoGuardRepository, job: dict[str, Any], exc: Exception, send_message: Callable[..., Any] | None) -> None:
    if not send_message:
        return
    try:
        payload = json.loads(job.get("payload_json") or "{}")
        receive_id = payload.get("receive_id")
        if not receive_id:
            return
        text = (
            "CryptoGuard 处理这条消息时遇到异常，已写入日志和 agent_jobs.error_message。\n\n"
            f"任务：{job.get('job_type')} #{job.get('id')}\n"
            f"错误：{exc}\n\n"
            "如果是行情接口网络错误，可以稍后重试，或检查代理/网络后再发送分析请求。"
        )
        send_markdown_alert(repo, send_message, receive_id=receive_id, receive_id_type=payload.get("receive_id_type", "open_id"), text=text, alert_type="job_error", priority=1)
    except Exception:
        LOGGER.exception("failed to send job error to user id=%s", job.get("id"))


def _send_interactive_alert(
    repo: CryptoGuardRepository,
    send_message: Callable[..., Any] | None,
    receive_id: str,
    receive_id_type: str,
    content: str,
    *,
    alert_type: str,
    symbol: str | None = None,
    priority: int = 5,
) -> dict[str, Any]:
    quiet_cfg = ((load_config().trading_mode.get("feishu") or {}).get("quiet_period") or {})
    quiet_minutes = int(quiet_cfg.get("normal_duplicate_alert_minutes", 5))
    never_silence = set(quiet_cfg.get("never_silence") or [])
    redis = RedisAdapter() if should_use_redis_for_path(load_config().database_path) else None
    redis_quiet_symbol = symbol or "-"
    if alert_type not in never_silence and redis and redis.is_quiet(redis_quiet_symbol, alert_type):
        return {"ok": True, "sent": False, "silenced": True, "source": "redis_quiet"}
    if repo.should_silence_alert(alert_type=alert_type, symbol=symbol, quiet_minutes=quiet_minutes, never_silence=never_silence):
        return {"ok": True, "sent": False, "silenced": True}
    if alert_type not in never_silence:
        lock_name = f"alert_dedupe:{symbol or '-'}:{alert_type}"
        redis_locked = bool(redis and redis.acquire_lock(lock_name, max(quiet_minutes * 60, 1), owner="interactive_alert"))
        if not redis_locked and not repo.acquire_lock(lock_name, "interactive_alert", max(quiet_minutes * 60, 1)):
            return {"ok": True, "sent": False, "silenced": True}
        if redis:
            redis.set_quiet(redis_quiet_symbol, alert_type, max(quiet_minutes * 60, 1))
    alert_id = repo.enqueue_alert(
        alert_type=alert_type,
        symbol=symbol,
        priority=priority,
        payload={"receive_id": receive_id, "receive_id_type": receive_id_type, "msg_type": "interactive", "content": content},
        dedupe_key=f"{symbol or '-'}:{alert_type}",
    )
    if not send_message:
        return {"ok": True, "sent": False, "queued": True, "alert_id": alert_id}
    try:
        sent = send_message(receive_id, content, msg_type="interactive", receive_id_type=receive_id_type)
        if sent:
            repo.mark_alert_sent(alert_id)
            return {"ok": True, "sent": True, "alert_id": alert_id}
        raise RuntimeError("send_message returned falsy")
    except Exception as exc:
        max_attempts = int((load_config().trading_mode.get("alerts") or {}).get("retry_max_attempts", 3))
        repo.mark_alert_failed(alert_id, str(exc), max_attempts=max_attempts)
        return {"ok": True, "sent": False, "alert_id": alert_id, "error": str(exc)}


def _render_symbol_list(rows: list[dict[str, Any]]) -> str:
    lines = ["**当前监控品种**", ""]
    if not rows:
        lines.append("- 暂无监控品种")
        return "\n".join(lines)
    for r in rows:
        enabled = "启用" if r.get("enabled") else "暂停"
        category = r.get("category") or "-"
        source = r.get("source") or "-"
        timeframes = r.get("default_timeframes") or "[]"
        lines.append(f"- **{r['symbol']}**：{enabled}，{category}，source={source}，周期={timeframes}")
    return "\n".join(lines)


def _button_result_text(action: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"操作失败：{result.get('error', '未知错误')}"
    if action == "create_paper_order":
        return "已加入模拟盘。" if result.get("created") else "这条信号已经加入过模拟盘，不会重复创建订单。"
    if action == "create_opportunity_watch":
        return "已加入机会监控。"
    if action == "add_to_watchlist":
        return "已加入长期产品池。"
    if action == "approve_evolution":
        return "已批准候选策略升级。"
    if action == "reject_evolution":
        return "已拒绝候选策略。"
    return "已忽略。"


def _ensure_ga_decision_for_watch_signal(repo: CryptoGuardRepository, signal: dict[str, Any], watch: dict[str, Any]) -> int:
    legacy = {
        "symbol": signal["symbol"],
        "decision": signal.get("decision") or "wait_for_pullback",
        "signal_grade": signal.get("signal_grade") or "B",
        "confidence": float(signal.get("confidence") or 0),
        "summary": signal.get("ga_reason") or "兼容旧 signal 创建的 GA decision。",
        "market_bias": signal.get("direction") or "neutral",
        "trend_stage": signal.get("trend_stage") or "unknown",
        "has_trade_plan": False,
        "trade_plan": None,
        "opportunity_watch": watch,
        "risk_check": {"ok": False, "reasons": ["未提供完整 trade_plan，仅允许机会监控"]},
        "evidence": [],
        "counter_evidence": [],
        "risk_notes": _safe_json_list(signal.get("risk_notes")),
    }
    actions = build_feishu_actions(legacy, legacy["risk_check"])
    ga_decision = controller_decision_from_legacy(
        legacy=legacy,
        decision_type="legacy_signal_compat",
        analysis_time=utc_ms(),
        skill_result_refs={},
        feishu_actions=actions,
        snapshot_id=signal.get("market_snapshot_id"),
        analysis_state_id=None,
    )
    ga_decision_id = repo.create_ga_decision(ga_decision)
    legacy["ga_decision_id"] = ga_decision_id
    repo.conn.execute(
        "UPDATE signals SET ga_decision_id=?, ga_decision_json=? WHERE id=?",
        (ga_decision_id, json.dumps(legacy, ensure_ascii=False), int(signal["id"])),
    )
    repo.conn.commit()
    return int(ga_decision_id)


def _safe_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except Exception:
        return [raw]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-only", action="store_true")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(run_once(user_only=args.user_only, background=args.background), ensure_ascii=False, indent=2))
    else:
        run_loop(user_only=args.user_only, background=args.background)


if __name__ == "__main__":
    main()
