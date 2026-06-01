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
        result = {"ok": True, "signal_id": signal_id, "decision": decision, "pushed": sent, "target": target}
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
        if target and send_message:
            sent_result = send_markdown_alert(repo, send_message, receive_id=target["receive_id"], receive_id_type=target.get("receive_id_type", "chat_id"), text=result["text"], alert_type="daily_review", priority=5)
            result["sent"] = bool(sent_result.get("sent"))
            result["target"] = target
        else:
            result["sent"] = False
            result["target"] = target
        LOGGER.info("process_job done id=%s type=%s ok=%s reviews=%s sent=%s", job.get("id"), job_type, result.get("ok"), result.get("new_reviews"), result.get("sent"))
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
    return {"ok": False, "error": f"未知 job_type: {job_type}"}


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
    text = "\n".join(
        [
            "**CryptoGuard 模拟盘事件**",
            "",
            f"- 类型：{event_type}",
            f"- 产品：{payload.get('symbol', '-')}",
            f"- 订单：#{payload.get('order_id', '-')}",
            f"- 成交/退出价：{payload.get('entry_price') or payload.get('exit_price') or '-'}",
            f"- 原因：{payload.get('close_reason') or payload.get('fill_method') or '-'}",
            "",
            "不构成实盘建议，仅用于模拟盘与策略研究。",
        ]
    )
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
