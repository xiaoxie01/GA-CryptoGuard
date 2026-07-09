from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from plugins.crypto_guard.storage.duckdb_analytics import DuckDBAnalytics
from plugins.crypto_guard.storage.migrations import check_schema_health
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.diagnostics.state_consistency import diagnose_state_consistency
from plugins.crypto_guard.diagnostics.report_diagnostics import run_for_report
from plugins.crypto_guard.notify.report_consistency import (
    FORBIDDEN_EXECUTABLE_PHRASES, contains_forbidden_phrase, rewrite_inconsistent_summary,
)
from plugins.crypto_guard.strategy.grade_config import (
    MIN_CONFIDENCE_FOR_PAPER_ORDER,
)
from plugins.crypto_guard.utils import INTERVAL_MS, latest_closed_close_time_ms, utc_ms

from plugins.crypto_guard.logging_utils import get_logger

LOGGER = get_logger("crypto_guard.hourly_report")


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


def build_hourly_report(repo: CryptoGuardRepository, *, retry_count: int = 0, expected_batch_id: str | None = None, report_hour_utc: str | None = None, expected_analysis_time: int | None = None, receive_id: str | None = None, receive_id_type: str | None = None) -> dict[str, Any]:
    # Check schema health first — pass repo.conn so a repo DB missing columns
    # is detected even when the default DB is healthy.
    schema = check_schema_health(conn=repo.conn)
    if not schema["ok"]:
        return {
            "ok": False,
            "error": "schema_unhealthy",
            "missing_columns": schema["missing_columns"],
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    active_symbols = repo.active_analysis_symbols()

    # P2 (R4): config-driven retry budget
    gate_cfg = _hourly_report_gate_config()
    retry_budget = _compute_retry_budget(gate_cfg)
    max_retries = retry_budget["max_retries"]
    # FR-3 (P1 fix): use the normalized poll_interval from _compute_retry_budget
    # so invalid/missing config values don't crash with ValueError here.
    poll_interval = retry_budget["poll_interval_seconds"]

    batch_state = _await_batch_completion(
        repo, primary_interval="15m",
        expected_batch_id=expected_batch_id,
        expected_analysis_time=expected_analysis_time,
    )

    # FR-2: carry the immutable expected_batch_id from the scheduler/parent job
    if expected_batch_id is None:
        expected_batch_id = batch_state.get("batch_id")
    if expected_analysis_time is None:
        expected_analysis_time = batch_state.get("analysis_time")
    if report_hour_utc is None:
        report_hour_utc = now

    # Determine if we need a degraded report (FR-1)
    use_degraded = _should_use_degraded_report(batch_state)

    if batch_state["incomplete"] and retry_count < max_retries:
        # Re-enqueue self with delay; worker is freed immediately.
        from datetime import timedelta as _td
        scheduled_at = (datetime.now(timezone.utc) + _td(seconds=poll_interval)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # FR-2: identity chain — pin to the original expected batch and hour
        session_id = f"hourly_report_retry:{report_hour_utc}:{expected_batch_id}:{retry_count + 1}"
        retry_payload: dict[str, Any] = {
            "retry_count": retry_count + 1,
            "expected_batch_id": expected_batch_id,
            "report_hour_utc": report_hour_utc,
            "expected_analysis_time": expected_analysis_time,
        }
        if receive_id:
            retry_payload["receive_id"] = receive_id
            retry_payload["receive_id_type"] = receive_id_type or "chat_id"
        repo.enqueue_job_once(
            "hourly_feishu_report",
            priority=3,
            source="hourly_report_requeue",
            session_id=session_id,
            payload=retry_payload,
            scheduled_at=scheduled_at,
        )
        return {
            "ok": False,
            "error": "batch_incomplete_requeued",
            "retry_count": retry_count + 1,
            "batch_id": expected_batch_id,
            "generated_at_utc": now,
        }

    # FR-1: degraded report path — no historical signals/analysis_states/ga_decisions
    if use_degraded:
        return _render_degraded_report(repo, now, batch_state, report_hour_utc, expected_batch_id)

    # Normal report: batch completed with at least one symbol
    min_analysis_time = batch_state["min_analysis_time"]
    batches_reported = batch_state.get("batch_id")
    # FR-1: only render ga_decisions whose batch_id matches the expected batch
    ga_decisions = repo.latest_ga_decisions_by_symbol(limit=120, min_analysis_time=min_analysis_time, batch_id=batches_reported)

    # FR-1 (P0 fix): if the batch shows completed symbols but the exact
    # batch_id filter returns zero decisions, the decisions are stale or
    # missing. Rendering the legacy text path would surface unfiltered
    # signals/analysis_states from previous cycles — fall back to degraded.
    if not ga_decisions:
        # Phase E (07-07) per design §9.4: before degrading, try the latest
        # *complete* batch (status=success AND enabled_count>0 AND
        # completed_count==enabled_count AND matching GA decision count).
        # This is the safety net when the expected batch's decisions are
        # missing/stale but a previous complete batch exists. If found, use
        # its batch_id for the GA decision fetch so the operator still gets a
        # useful report. If none, degrade - the Phase A diagnostic
        # ``_check_hourly_report_used_partial_running_batch`` will catch the
        # condition on the next diagnostic run (latest batch running/partial
        # + recent hourly alert).
        now_ms_fallback = utc_ms()
        complete_batch = _select_latest_complete_batch(repo, now_ms=now_ms_fallback)
        if complete_batch is not None:
            fallback_batch_id = complete_batch.get("batch_id")
            fallback_summary = complete_batch.get("summary_json") or {}
            if isinstance(fallback_summary, str):
                try:
                    fallback_summary = json.loads(fallback_summary) or {}
                except Exception:
                    fallback_summary = {}
            fallback_analysis_time = int(complete_batch.get("analysis_time") or 0)
            fallback_min_time = fallback_analysis_time - INTERVAL_MS["15m"] + 1 if fallback_analysis_time > 0 else min_analysis_time
            ga_decisions = repo.latest_ga_decisions_by_symbol(
                limit=120, min_analysis_time=fallback_min_time, batch_id=fallback_batch_id,
            )
            if ga_decisions:
                # Switch to the complete batch for rendering.
                batches_reported = fallback_batch_id
                batch_state = {
                    **batch_state,
                    "batch_id": fallback_batch_id,
                    "status": str(complete_batch.get("status") or "success"),
                    "analysis_time": fallback_analysis_time,
                    "min_analysis_time": fallback_min_time,
                    "summary_json": fallback_summary,
                }
            else:
                LOGGER.warning(
                    "hourly_report: expected batch %s has no ga_decisions and no "
                    "complete fallback batch found - rendering degraded report",
                    expected_batch_id,
                )
                degraded_state = {**batch_state, "status": "absent", "completed_count": 0}
                return _render_degraded_report(repo, now, degraded_state, report_hour_utc, expected_batch_id)
        else:
            LOGGER.warning(
                "hourly_report: expected batch %s has no ga_decisions and no "
                "complete fallback batch found - rendering degraded report",
                expected_batch_id,
            )
            degraded_state = {**batch_state, "status": "absent", "completed_count": 0}
            return _render_degraded_report(repo, now, degraded_state, report_hour_utc, expected_batch_id)

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
    market_regime_gate = _fetch_market_regime_gate_stats(repo)
    state_consistency = _fetch_state_consistency(repo)
    report_accuracy_diagnostics = run_for_report(repo, batch_id=batches_reported)
    market_data_quality = _fetch_market_data_quality(repo, analysis_time_utc=batch_state.get("analysis_time"))
    # P2 (07-09 R4): ``_agent_hourly_brief`` applies the legacy schema-fail
    # split internally so the LLM brief context never receives archived
    # legacy jobs. Pass raw ``failed_jobs`` here - the function filters.
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
        "market_regime_gate": market_regime_gate,
        "state_consistency": state_consistency,
        "report_accuracy_diagnostics": report_accuracy_diagnostics,
        "market_data_quality": market_data_quality,
        "batch": batch_state,
        "agent_brief": agent_brief,
        "text": (
            render_ga_hourly_summary(now, active_symbols, ga_decisions, open_orders, active_watches, failed_jobs, queue_counts, equity_snapshot=equity, duckdb_stats=duckdb_stats, risk_state=risk_state, shadow_data_quality=shadow_data_quality, feedback_patterns=feedback_patterns, long_short_performance=long_short_performance, account_feedback_gate=account_feedback_gate, market_regime_gate=market_regime_gate, state_consistency=state_consistency, batch_state=batch_state, report_accuracy_diagnostics=report_accuracy_diagnostics, market_data_quality=market_data_quality)
            if ga_decisions
            else render_hourly_report_text(now, active_symbols, signals, open_orders, failed_jobs, queue_counts, agent_brief=agent_brief, analysis_states=states, equity_snapshot=equity, risk_state=risk_state, shadow_data_quality=shadow_data_quality, feedback_patterns=feedback_patterns, long_short_performance=long_short_performance, account_feedback_gate=account_feedback_gate, market_regime_gate=market_regime_gate, state_consistency=state_consistency, batch_state=batch_state, report_accuracy_diagnostics=report_accuracy_diagnostics, market_data_quality=market_data_quality)
        ),
    }


def _await_batch_completion(repo: CryptoGuardRepository, *, primary_interval: str = "15m", expected_batch_id: str | None = None, expected_analysis_time: int | None = None) -> dict[str, Any]:
    """Take a single snapshot of the current analysis batch for the hourly report.

    P0-1 (Round 3): No longer polls or sleeps. Returns instantly with the
    current batch state. If the batch is incomplete, the caller should
    re-enqueue the hourly_feishu_report job with a delay instead of blocking
    the worker.

    FR-2: If expected_batch_id is provided (from retry chain), use it instead
    of computing a new one. This ensures a retry crossing a 15-minute boundary
    still inspects the original expected batch.

    FR-2 (P0 fix): If expected_analysis_time is provided, pin cutoff_ms and
    min_analysis_time to it. Without this, a retry crossing a 15-minute
    boundary would recompute cutoff_ms from utc_ms() at retry time, shifting
    min_analysis_time forward and filtering out the original batch's decisions.
    """
    cfg = _hourly_report_gate_config()
    # FR-3 (P1 fix): use normalized values from _compute_retry_budget so
    # invalid/missing config doesn't crash with ValueError here.
    retry_budget = _compute_retry_budget(cfg)
    timeout_seconds = retry_budget["timeout_seconds"]

    # FR-2: pin cutoff_ms to expected_analysis_time when provided so a retry
    # crossing a 15-minute boundary still inspects the original batch window.
    # Without this, min_analysis_time would shift forward and filter out the
    # original batch's decisions.
    if expected_analysis_time is not None:
        cutoff_ms = int(expected_analysis_time)
    else:
        cutoff_ms = latest_closed_close_time_ms(primary_interval, utc_ms())
    span = INTERVAL_MS[primary_interval]
    if expected_batch_id is None:
        expected_batch_id = f"{primary_interval}:{cutoff_ms}"

    def _snapshot() -> dict[str, Any] | None:
        # P0 (R4): NEVER fall back to a previous batch. If the expected
        # batch_id is absent, the analysis hasn't started yet — return None
        # so the caller re-enqueues with delay instead of rendering stale data.
        return repo.get_analysis_batch(expected_batch_id)

    snapshot = _snapshot()
    missing: list[str] = []
    failed: list[str] = []
    pending_symbols: list[str] = []
    enabled_symbols: list[str] = []
    status = "absent"
    completed_count = 0
    total_count = 0

    if snapshot is not None:
        enabled_symbols = list(snapshot.get("enabled_symbols") or [])
        total_count = len(enabled_symbols)
        completed_syms = list(snapshot.get("completed_symbols") or [])
        failed = list(snapshot.get("failed_symbols") or [])
        pending_symbols = list(snapshot.get("pending_symbols") or [])
        status = str(snapshot.get("status") or "running")
        completed_count = len(completed_syms)
        missing = sorted(set(enabled_symbols) - set(completed_syms) - set(failed))

    incomplete = bool(missing or pending_symbols) or snapshot is None
    # P0 (R4): no fallback adoption — use the expected batch's timestamps.
    effective_batch_id = expected_batch_id
    effective_min_time = cutoff_ms - span + 1

    # Phase E (07-07) per design §9.1: carry the batch's summary_json so the
    # renderer can emit the LLM health line. ``get_analysis_batch`` parses
    # the JSON blob into a dict; missing/None becomes {} so downstream
    # ``_render_llm_health_line`` sees an empty llm_health block and renders
    # no line (pre-Phase-B batches render without the line).
    batch_summary_json: dict[str, Any] = {}
    if snapshot is not None:
        raw_summary = snapshot.get("summary_json")
        if isinstance(raw_summary, dict):
            batch_summary_json = raw_summary
        elif isinstance(raw_summary, str) and raw_summary:
            try:
                parsed = json.loads(raw_summary)
                if isinstance(parsed, dict):
                    batch_summary_json = parsed
            except Exception:
                batch_summary_json = {}

    return {
        "batch_id": effective_batch_id,
        "primary_interval": primary_interval,
        "analysis_time": cutoff_ms,
        "min_analysis_time": effective_min_time,
        "status": status,
        "incomplete": incomplete,
        "enabled_symbols": enabled_symbols,
        "missing_symbols": missing,
        "failed_symbols": failed,
        "still_running": sorted(missing),
        "pending_symbols": pending_symbols,
        "completed_count": completed_count,
        "total_count": total_count,
        "timeout_seconds": timeout_seconds,
        # Phase E (07-07): full summary_json (with llm_health block) so the
        # renderer can call ``_render_llm_health_line`` without re-fetching.
        "summary_json": batch_summary_json,
    }


def _hourly_report_gate_config() -> dict[str, Any]:
    """Load batch-gate config from scheduler.yaml (no exception noise)."""
    try:
        from plugins.crypto_guard.config.loader import load_config
        scheduler = load_config().scheduler or {}
        jobs = scheduler.get("jobs") or {}
        report_cfg = jobs.get("hourly_feishu_report") or {}
        return report_cfg.get("batch_gate") or {}
    except Exception:
        return {}


def _compute_retry_budget(gate_cfg: dict[str, Any]) -> dict[str, Any]:
    """FR-3: Compute max_retries from timeout_seconds / poll_interval_seconds.

    max_retries = ceil(timeout_seconds / poll_interval_seconds).
    Removes max_retries from configuration — derived entirely at runtime.
    """
    from math import ceil
    timeout_raw = gate_cfg.get("timeout_seconds", 300)
    interval_raw = gate_cfg.get("poll_interval_seconds", 30)

    # Validate and coerce
    try:
        timeout_seconds = int(timeout_raw)
    except (ValueError, TypeError):
        LOGGER.warning("FR-3: invalid timeout_seconds=%r, falling back to 300", timeout_raw)
        timeout_seconds = 300
    try:
        poll_interval_seconds = int(interval_raw)
    except (ValueError, TypeError):
        LOGGER.warning("FR-3: invalid poll_interval_seconds=%r, falling back to 30", interval_raw)
        poll_interval_seconds = 30

    if timeout_seconds < 0:
        LOGGER.warning("FR-3: negative timeout_seconds=%d, falling back to 300", timeout_seconds)
        timeout_seconds = 300
    if poll_interval_seconds <= 0:
        LOGGER.warning("FR-3: non-positive poll_interval_seconds=%d, falling back to 30", poll_interval_seconds)
        poll_interval_seconds = 30

    # timeout_seconds == 0 → immediate degraded report, no retries
    if timeout_seconds == 0:
        return {"max_retries": 0, "timeout_seconds": 0, "poll_interval_seconds": poll_interval_seconds}

    max_retries = ceil(timeout_seconds / poll_interval_seconds)
    return {"max_retries": max_retries, "timeout_seconds": timeout_seconds, "poll_interval_seconds": poll_interval_seconds}


def _should_use_degraded_report(batch_state: dict[str, Any]) -> bool:
    """FR-1: Determine if the hourly report must use the degraded path.

    Degraded when: batch absent, batch failed, or zero completed symbols.
    NOT degraded for partial-failed batches (those still render completed symbols).
    """
    status = batch_state.get("status", "absent")
    completed_count = batch_state.get("completed_count", 0)
    # absent → never started, failed → all symbols failed, zero completed → nothing to show
    if status == "absent":
        return True
    if status == "failed":
        return True
    if completed_count == 0 and status in ("success", "partial_failed", "running"):
        return True
    return False


def _render_degraded_report(repo: CryptoGuardRepository, now: str, batch_state: dict[str, Any], report_hour_utc: str, expected_batch_id: str | None) -> dict[str, Any]:
    """FR-1: Render a deterministic degraded report when the expected batch is
    absent, failed, or has zero completed symbols.

    Contains system, queue, account, position, and risk state but MUST NOT
    contain historical opportunities, C/D classifications, legacy signals,
    analysis_states, or LLM market commentary.
    """
    active_symbols = repo.active_analysis_symbols()
    open_orders = repo.list_open_paper_orders()
    equity = repo.latest_equity_snapshot()
    failed_jobs = repo.recent_failed_jobs(limit=5)
    queue_counts = {
        "pending_user": _count(repo, "SELECT COUNT(*) FROM agent_jobs WHERE status='pending' AND priority <= 2"),
        "pending_background": _count(repo, "SELECT COUNT(*) FROM agent_jobs WHERE status='pending' AND priority > 2"),
        "running": _count(repo, "SELECT COUNT(*) FROM agent_jobs WHERE status='running'"),
    }
    risk_state = _fetch_risk_state(repo)
    state_consistency = _fetch_state_consistency(repo)
    report_accuracy_diagnostics = run_for_report(repo, batch_id=expected_batch_id)

    # P2 (07-09 R3): split failed_jobs once at the top so the system-status
    # line and the 三、风险事件 section share the same current_jobs list.
    _current_failed_jobs, _legacy_schema_fail_count = _split_current_and_legacy_failed_jobs(
        failed_jobs,
    )

    # Build degraded text
    lines: list[str] = [
        "**CryptoGuard 每小时简报（降级模式）**",
        f"北京时间：{_format_time_utc8(now)}",
        f"UTC 时间：{now}",
        "",
        "⚠ 当前行情分析不可用，本报告未采用历史信号代替",
        "",
        "**零、分析批次状态**",
        f"- {_batch_status_label(batch_state.get('status'))} · "
        f"完成 {batch_state.get('completed_count', 0)}/{batch_state.get('total_count', 0)} 个品种",
    ]
    failed_syms = batch_state.get("failed_symbols") or []
    if failed_syms:
        lines.append(f"- 分析失败：{', '.join(failed_syms)}")

    lines.extend(["", "**一、系统状态**"])
    waiting = queue_counts.get("pending_user", 0) + queue_counts.get("pending_background", 0)
    lines.append(
        f"- 调度正常 · 等待任务 {waiting} 个 · "
        f"正在执行 {queue_counts.get('running', 0)} 个 · 最近失败 {len(_current_failed_jobs)} 个"
    )

    if risk_state:
        risk_status = []
        if risk_state.get("risk_off"):
            risk_status.append("risk_off")
        if risk_state.get("hard_risk_off"):
            risk_status.append("hard_risk_off")
        if risk_state.get("daily_loss_pause"):
            risk_status.append("daily_loss_pause")
        if risk_status:
            dd_pct = abs(float(risk_state.get('drawdown_pct', 0)))
            lines.append(f"- 风险状态：**{', '.join(risk_status)}**（回撤 {dd_pct:.1f}%）")
        else:
            lines.append("- 风险状态：正常")

    lines.extend(["", "**二、模拟盘摘要**"])
    if equity:
        snap = _safe_json(equity.get("snapshot_json"), {}) or equity
        dd_value = float(snap.get("drawdown_percent") or 0)
        dd_display = abs(dd_value)
        lines.append(
            f"- 权益 {float(equity.get('account_equity') or 0):.2f} USDT · "
            f"浮动盈亏 {float(equity.get('unrealized_pnl') or 0):+.2f} · "
            f"已实现 {float(equity.get('realized_pnl') or 0):+.2f} · "
            f"回撤 {dd_display:.2f}%"
        )
    else:
        lines.append("- 暂无净值快照")
    lines.append(f"- 当前持仓/挂单：{len(open_orders)}")

    # Risk events
    # P1/P2 fix (07-09): filter known legacy schema-fail signatures out of
    # the current risk-events list. ``_current_failed_jobs`` /
    # ``_legacy_schema_fail_count`` are computed once at the top of the
    # renderer so the count shown at the top matches the items listed below.
    lines.extend(["", "**三、风险事件**"])
    if _current_failed_jobs or _legacy_schema_fail_count > 0:
        for job in _current_failed_jobs[:5]:
            lines.append(f"- #{job['id']} {job['job_type']}：{(job.get('error_message') or '-')[:100]}")
        if _legacy_schema_fail_count > 0:
            lines.append(
                f"- 另有 {_legacy_schema_fail_count} 个历史 schema 校验失败已归档到审计"
                "（07-09 alias-repair SOP 已处理，不再列入当前风险事件）"
            )
        if not _current_failed_jobs and _legacy_schema_fail_count == 0:
            lines.append("- 暂无新的失败任务或风险事件")
    else:
        lines.append("- 暂无新的失败任务或风险事件")

    # State consistency diagnostics
    if state_consistency and not state_consistency.get("error"):
        summary = state_consistency.get("summary", {})
        total = state_consistency.get("total_issues", 0)
        lines.extend(["", "**状态一致性诊断**"])
        if total > 0:
            lines.append(f"- 发现问题 {total} 个")
        else:
            lines.append("- 全部正常，未发现状态不一致")

    # Report accuracy diagnostics
    if report_accuracy_diagnostics and not report_accuracy_diagnostics.get("error"):
        diag_summary = report_accuracy_diagnostics.get("summary") or {}
        diag_total = report_accuracy_diagnostics.get("total_issues", 0)
        lines.extend(["", "**报告准确性诊断**"])
        if diag_total == 0:
            lines.append("- 报告准确性诊断全部通过")
        else:
            for code, count in diag_summary.items():
                # `layer_counts` is a nested dict, not a count — skip it
                if isinstance(count, dict):
                    continue
                if int(count or 0) > 0:
                    lines.append(f"- {code}={count}")

    lines.append("")
    lines.append("不构成实盘建议，仅用于模拟盘与策略研究。")

    return {
        "ok": True,
        "degraded": True,
        "generated_at_utc": now,
        "active_symbols": active_symbols,
        "latest_signals": [],
        "analysis_states": [],
        "ga_decisions": [],
        "active_watches": [],
        "open_orders": open_orders,
        "equity_snapshot": equity,
        "failed_jobs": failed_jobs,
        "queue_counts": queue_counts,
        "duckdb_stats": {},
        "risk_state": risk_state,
        "shadow_data_quality": {},
        "feedback_patterns": {},
        "long_short_performance": {},
        "account_feedback_gate": {},
        "market_regime_gate": {},
        "state_consistency": state_consistency,
        "report_accuracy_diagnostics": report_accuracy_diagnostics,
        "batch": batch_state,
        "agent_brief": {},
        "text": "\n".join(lines),
    }


def _json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def _market_data_quality_problem_rows(
    market_data_quality: dict[str, Any],
) -> list[str]:
    """Render only unhealthy symbol/timeframe rows for user notifications."""
    rows: list[str] = []
    symbols_md = market_data_quality.get("symbols") or {}
    for sym, tf_health in symbols_md.items():
        for tf, health in (tf_health or {}).items():
            if not isinstance(health, dict) or health.get("ready") is True:
                continue
            contiguous = health.get(
                "contiguous_tail_count",
                health.get("contiguous_count", 0),
            )
            required = health.get("required_count", 0)
            gap_count = health.get("gap_count", 0)
            largest_gap = health.get("largest_gap_bars", 0)
            last_close = health.get("last_close_time")
            reason = health.get("reason", "")
            last_close_str = _format_time_utc8(str(last_close)) if last_close else "-"
            rows.append(
                f"- {sym} {tf}：连续 {contiguous}/{required}，"
                f"缺口 {gap_count}（最大 {largest_gap}），"
                f"最新收盘 {last_close_str}，降级({reason})"
            )
    return rows


def _append_market_data_quality_section(
    lines: list[str],
    market_data_quality: dict[str, Any] | None,
    *,
    heading: str,
    include_status: bool,
) -> None:
    """Append a compact market-data section only when a problem exists."""
    if not market_data_quality:
        return
    problem_rows = _market_data_quality_problem_rows(market_data_quality)
    deferred = market_data_quality.get("deferred_analyses") or []
    fail_closed = bool(market_data_quality.get("fail_closed"))
    degraded = bool(market_data_quality.get("degraded"))

    # Legacy reports already render the status banner near the top. Avoid an
    # empty duplicate section when there are no per-TF/deferred details.
    if not include_status and not problem_rows and not deferred:
        return
    if not (fail_closed or degraded or problem_rows or deferred):
        return

    lines.extend(["", heading])
    if include_status:
        if fail_closed:
            error_msg = market_data_quality.get("error", "unknown")
            lines.append(f"- ⚠️ 行情质量状态不可用：{error_msg}")
        elif degraded or problem_rows:
            lines.append("- ⚠️ 行情分析不可用/降级 — 数据不完整")
    lines.extend(problem_rows)
    if deferred:
        lines.append(f"- 延迟分析：{len(deferred)} 项")


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
    market_regime_gate: dict[str, Any] | None = None,
    state_consistency: dict[str, Any] | None = None,
    batch_state: dict[str, Any] | None = None,
    report_accuracy_diagnostics: dict[str, Any] | None = None,
    market_data_quality: dict[str, Any] | None = None,
) -> str:
    rows = [_decision_row(row) for row in ga_decisions]
    grade_counts: dict[str, int] = {grade: 0 for grade in ("S", "A", "B", "C", "D")}
    for row in rows:
        grade = str(row.get("signal_grade") or "D")
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
    # P0: classify opportunities by execution gate instead of grade-only.
    executable: list[dict[str, Any]] = []
    observation: list[dict[str, Any]] = []
    no_edge: list[dict[str, Any]] = []
    now_ms = utc_ms()
    # P2 (07-09 R3): split failed_jobs once at the top so the system-status
    # line and the 九、风险事件 section share the same current_jobs list.
    # Without this, "最近失败 N 个" would count archived legacy schema-fail
    # jobs while the list below shows only current failures.
    _current_failed_jobs, _legacy_schema_fail_count = _split_current_and_legacy_failed_jobs(
        failed_jobs,
    )
    # The renderer's stale cutoff mirrors the batch completion gate (one
    # analysis cycle aged beyond the current 15m slot = stale).
    stale_cutoff_ms = latest_closed_close_time_ms("15m", now_ms) - INTERVAL_MS["15m"]
    for row in rows:
        tier = _opportunity_classifier(row)
        row["_tier"] = tier["tier"]
        row["_blockers"] = tier["blockers"]
        row["_stale"] = _is_stale_decision(row, stale_cutoff_ms)
        if row["_stale"] and tier["tier"] == "executable":
            # stale + executable → demote to observation with explicit blocker
            row["_tier"] = "observation"
            row["_blockers"] = row["_blockers"] + ["stale_decision"]
        if str(row.get("signal_grade")) in {"C", "D"}:
            no_edge.append(row)
            continue
        if tier["tier"] == "executable":
            executable.append(row)
        else:
            observation.append(row)
    high_grade = executable + observation  # legacy alias for position conflict logic
    lines: list[str] = [
        "**GA CryptoGuard 每小时摘要**",
        f"北京时间：{_format_time_utc8(generated_at_utc)}",
        f"UTC 时间：{generated_at_utc}",
        "",
    ]
    # P0: report batch completion / incompleteness header.
    if batch_state:
        status = batch_state.get("status") or "-"
        incomplete = bool(batch_state.get("incomplete"))
        enabled_syms = batch_state.get("enabled_symbols") or []
        completed_syms_raw = batch_state.get("completed_symbols") or []
        if isinstance(completed_syms_raw, str):
            completed_syms = _json_list(completed_syms_raw)
        else:
            completed_syms = list(completed_syms_raw)
        failed_syms = batch_state.get("failed_symbols") or []
        if isinstance(failed_syms, str):
            failed_syms = _json_list(failed_syms)
        completed_count = int(batch_state.get("completed_count") or len(completed_syms))
        total_count = int(batch_state.get("total_count") or len(enabled_syms))
        analysis_time = batch_state.get("analysis_time")
        lines.append("**分析批次**")
        lines.append(
            f"- {_batch_status_label(status, incomplete)}"
            f" · 完成 {completed_count}/{total_count} 个品种"
            f" · 数据截至 {_format_time_utc8(analysis_time)}"
        )
        if failed_syms:
            lines.append("- 分析失败：" + "、".join(failed_syms))
        if incomplete:
            missing = batch_state.get("missing_symbols") or []
            still_running = batch_state.get("still_running") or []
            pending = _dedupe([str(x) for x in list(missing) + list(still_running)])
            lines.append("- 尚未完成：" + ("、".join(pending) if pending else "部分品种仍在分析"))
        lines.append("")

    lines.extend([
        "**一、系统状态**",
        f"- 调度正常 · 等待任务 {queue_counts.get('pending_user', 0) + queue_counts.get('pending_background', 0)} 个"
        f" · 正在执行 {queue_counts.get('running', 0)} 个"
        f" · 最近失败 {len(_current_failed_jobs)} 个",
        "- 行情数据：Binance U本位合约公共行情 · SQLite",
    ])

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
            # P2-14: drawdown display as non-negative amplitude
            dd_pct = abs(float(risk_state.get('drawdown_pct', 0)))
            labels = {
                "risk_off": "风险收缩",
                "hard_risk_off": "暂停开仓",
                "daily_loss_pause": "当日亏损暂停",
            }
            lines.append(f"- 风险状态：**{'、'.join(labels.get(x, x) for x in risk_status)}**（回撤 {dd_pct:.1f}%）")
        else:
            lines.append("- 风险状态：正常")

    # P0-5: Market data quality section — surface degraded state in GA path.
    # P2-4 R3: Distinguish "health check crashed" (fail_closed=True) from
    # "data is genuinely gappy" (degraded=True, fail_closed != True). The
    # generic "数据不完整" banner only appears when the health check ran
    # successfully and found real gaps. When the health check itself crashed,
    # a distinct "行情质量状态不可用" message is shown with the error.
    _append_market_data_quality_section(
        lines,
        market_data_quality,
        heading="**行情数据质量**",
        include_status=True,
    )

    # Phase E (07-07) per design §9.1: LLM health summary line. Placed after
    # the system status section so the operator sees LLM call health (success
    # / failure / retry counts, dominant error, breaker state) in one glance.
    # Renders ``""`` (no line) when the batch has no ``llm_health`` block
    # (pre-Phase-B batches or batches where LLM was disabled).
    if batch_state:
        llm_health_line = _render_llm_health_line(batch_state)
        if llm_health_line:
            lines.append(f"- {llm_health_line}")

    lines.extend(["", "**二、模拟盘摘要**"])
    if equity_snapshot:
        snap = _safe_json(equity_snapshot.get("snapshot_json"), {}) or equity_snapshot
        dd_value = float(snap.get("drawdown_percent") or 0)
        dd_display = abs(dd_value)  # P1: external amplitude is non-negative
        lines.append(
            f"- 权益 {float(equity_snapshot.get('account_equity') or 0):.2f} USDT"
            f" · 浮动盈亏 {float(equity_snapshot.get('unrealized_pnl') or 0):+.2f}"
            f" · 已实现 {float(equity_snapshot.get('realized_pnl') or 0):+.2f}"
            f" · 回撤 {dd_display:.2f}%"
            + ("（账号权益低于初始）" if dd_value < 0 else "（未回撤）" if dd_value >= 0 else "")
        )
        analytics_source = "DuckDB 时序统计" if (duckdb_stats or {}).get("ok") else "SQLite 实时统计"
        lines.append(f"- 决策来源：GA 决策 · 统计来源：{analytics_source}")
    else:
        lines.append("- 暂无净值快照")
    lines.append(f"- 当前持仓/挂单：{len(open_orders)} 个")

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

    # P1-10 (07-05 final review): title previously said "S/A/B" but
    # ``_opportunity_classifier`` already demotes B to observation via
    # ``PAPER_ORDER_GRADES = {"S", "A"}`` (see strategy/grade_config.py).
    # The title must match the executable gate policy so the report does
    # not promise a B-grade executable section that never appears.
    lines.extend(["", "**三、可执行机会（S/A 且通过执行门禁）**"])
    if not executable:
        lines.append("- 暂无可执行机会")
    # Index open orders by symbol for position-aware display
    open_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for o in open_orders:
        open_by_symbol.setdefault(o["symbol"], []).append(o)
    # P1-5: pass market_data_degraded so direction/stage text is suppressed
    # when the data is degraded.
    md_degraded = bool(market_data_quality and market_data_quality.get("degraded"))
    for row in executable[:10]:
        lines.append(_format_opportunity_row(row, open_by_symbol, tier_label="可执行", market_data_degraded=md_degraded))

    lines.extend(["", "**四、观察候选（评级较高但未通过执行门禁）**"])
    if not observation:
        lines.append("- 暂无观察候选")
    for row in observation[:20]:
        lines.append(_format_opportunity_row(row, open_by_symbol, tier_label="观察候选", market_data_degraded=md_degraded))

    lines.extend(["", "**五、当前机会监控**"])
    if not active_watches:
        lines.append("- 暂无 active 机会监控")
    for watch in active_watches[:10]:
        condition = _compact_items(_safe_json(watch.get("watch_condition_json"), []), max_items=2)
        lines.append(f"- #{watch['id']} {watch['symbol']} {watch.get('direction') or '-'}：{condition or watch.get('watch_reason') or '-'}")

    lines.extend(["", "**六、无优势品种汇总（C/D）**"])
    distribution = (duckdb_stats or {}).get("signal_distribution") or grade_counts
    source_raw = (duckdb_stats or {}).get("source") or "in_memory_fallback"
    # P2 语法澄清 (research 09): describe the fallback honestly
    source_label = _distribution_source_label(source_raw, duckdb_stats)
    lines.append("- 等级分布：" + " · ".join(f"{k} {v}" for k, v in distribution.items()) + f"（{source_label}）")
    if no_edge:
        symbols = ", ".join(row["symbol"] for row in no_edge[:30])
        lines.append(f"- C/D：{symbols}")
        # Phase C (07-03): prefer rendered_summary (canonical) over
        # final_summary for the C/D reason display. rendered_summary is the
        # deterministic canonical text generated by
        # build_canonical_market_summary; final_summary is kept as a
        # fallback for rows written before the Phase C integration.
        # Phase D (07-03): route through _format_cd_reasons so the "前 N 项"
        # truncation label appears when reasons exceed max_items. This also
        # keeps both report paths on the shared _compact_items helper.
        reason_items = [
            row.get("rendered_summary") or row.get("final_summary")
            for row in no_edge
        ]
        reasons = _format_cd_reasons(reason_items, max_items=3)
        lines.append(f"- 主要原因：{reasons or '趋势不清晰或风控不足'}")
    else:
        lines.append("- 暂无 C/D 无优势品种")

    # P2-B: Add shadow data quality
    if shadow_data_quality and not shadow_data_quality.get("error"):
        lines.extend(["", "**七、影子测试数据质量**"])
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
            critical_parts = []
            info_parts = []
            # Critical: active PnL loop integrity
            if summary.get("active_eval_missing_ga_decision_id", 0) > 0:
                critical_parts.append(f"Active缺GA决策ID={summary['active_eval_missing_ga_decision_id']}")
            if summary.get("paper_order_missing_active_eval", 0) > 0:
                critical_parts.append(f"订单缺Active评估={summary['paper_order_missing_active_eval']}")
            if summary.get("closed_trade_missing_active_real_pnl", 0) > 0:
                critical_parts.append(f"平仓缺Active实PnL={summary['closed_trade_missing_active_real_pnl']}")
            # Standard issues
            if summary.get("duplicate_open_trades", 0) > 0:
                critical_parts.append(f"重复开仓={summary['duplicate_open_trades']}")
            if summary.get("orphan_patches", 0) > 0:
                critical_parts.append(f"孤儿补丁={summary['orphan_patches']}")
            if summary.get("status_mismatches", 0) > 0:
                critical_parts.append(f"状态不一致={summary['status_mismatches']}")
            if summary.get("duplicate_patches", 0) > 0:
                critical_parts.append(f"重复补丁={summary['duplicate_patches']}")
            if summary.get("stale_shadows", 0) > 0:
                critical_parts.append(f"过期影子={summary['stale_shadows']}")
            if summary.get("draft_limbo", 0) > 0:
                critical_parts.append(f"草稿滞留={summary['draft_limbo']}")
            # Warning: shadow candidate quality
            if summary.get("shadow_candidate_legacy_only", 0) > 0:
                info_parts.append(f"候选仅旧样本={summary['shadow_candidate_legacy_only']}")
            if critical_parts:
                lines.append(f"- **关键问题 {len(critical_parts)} 项**：{'，'.join(critical_parts)}")
            if info_parts:
                lines.append(f"- 提示：{'，'.join(info_parts)}")
            if not critical_parts and not info_parts:
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

    # Market regime gate stats (Fix 5 + Fix 8)
    if market_regime_gate and not market_regime_gate.get("error"):
        mg = market_regime_gate
        if mg.get("total_checks", 0) > 0:
            lines.extend(["", "**市场情绪门禁（24h）**"])
            lines.append(
                f"- 检查 {mg['total_checks']} 次，"
                f"counter_regime {mg.get('counter_regime', 0)} 次，"
                f"independent_trend {mg.get('independent_trend', 0)} 次，"
                f"watch_only {mg.get('watch_only', 0)} 次，"
                f"数据不足 {mg.get('unknown', 0)} 次，"
                f"aligned {mg.get('aligned', 0)} 次"
            )
            # Fix 8: time_source fallback_now warning
            fallback_count = mg.get("fallback_now_count", 0)
            if fallback_count > 0:
                lines.append(
                    f"- ⚠ {fallback_count} 次门禁使用当前时间（无原始分析时间），可能存在 lookahead 风险"
                )
            # Top 3 symbols with counter_regime
            top_counter = mg.get("top_counter_regime_symbols", [])
            if top_counter:
                symbol_text = "，".join(f"{s['symbol']}({s['count']})" for s in top_counter[:3])
                lines.append(f"- counter_regime 前三品种：{symbol_text}")

    lines.extend(["", "**九、风险事件**"])
    # P1/P2 fix (07-09): filter known legacy schema-fail signatures out of
    # the current risk-events list. The 07-09 alias-repair SOP is already
    # handling ``analysis_time_utc is a required property`` failures by
    # normalizing ``entry_trigger_confirmation.type`` aliases to
    # ``closed_candle_confirmation``. Historical agent_jobs rows within the
    # 7-day ``recent_failed_jobs`` window would otherwise keep surfacing in
    # every hourly report and drown out actionable current failures.
    # Filtered rows are surfaced as a single legacy-audit count line so the
    # operator knows they were archived (not silently dropped).
    #
    # P2 (07-09 R3): ``_current_failed_jobs`` / ``_legacy_schema_fail_count``
    # are computed once at the top of the renderer (above the system-status
    # line) so the count shown at the top matches the items listed below.
    if _current_failed_jobs or _legacy_schema_fail_count > 0:
        for job in _current_failed_jobs[:5]:
            lines.append(f"- #{job['id']} {job['job_type']}：{(job.get('error_message') or '-')[:100]}")
        if _legacy_schema_fail_count > 0:
            lines.append(
                f"- 另有 {_legacy_schema_fail_count} 个历史 schema 校验失败已归档到审计"
                "（07-09 alias-repair SOP 已处理，不再列入当前风险事件）"
            )
        if not _current_failed_jobs and _legacy_schema_fail_count == 0:
            lines.append("- 暂无新的失败任务或风险事件")
    else:
        lines.append("- 暂无新的失败任务或风险事件")

    # Phase E (07-07) per design §9.3: recent LLM failures (24h window).
    # Replaces any prior unfiltered "最近失败" rendering of ga_decisions.
    # Only decisions with ``llm_status='failed'`` whose ``analysis_time``
    # falls within the 24h window are shown - older failures remain in the
    # audit trail but are hidden from the hourly report (AC17).
    now_ms_for_failures = utc_ms()
    recent_llm_failures = _render_recent_failures(rows, now_ms=now_ms_for_failures)
    if recent_llm_failures:
        lines.extend(["", "**九之二、最近 24 小时 LLM 失败（仅本窗口内）**"])
        for failed_row in recent_llm_failures[:10]:
            sym = failed_row.get("symbol") or "-"
            err = str(failed_row.get("llm_error") or "")[:100]
            cat = failed_row.get("llm_error_category") or ""
            # Phase E (07-09): breaker-skipped rows carry
            # llm_error_category=None (no LLM call was made) but a
            # readable llm_fallback_reason. Translate the fallback reason
            # into a Chinese category so the row does not render as "-".
            if not cat:
                cat = _fallback_reason_to_category_zh(
                    failed_row.get("llm_fallback_reason"),
                )
            lines.append(f"- {sym}：{cat} · {err or '-'}")

    # P2: 报告准确性诊断 (research 00 P2 diagnostics)
    if report_accuracy_diagnostics and not report_accuracy_diagnostics.get("error"):
        lines.extend(["", "**十、报告准确性诊断**"])
        errors = int(report_accuracy_diagnostics.get("error_count") or 0)
        warnings = int(report_accuracy_diagnostics.get("warning_count") or 0)
        legacy = int(report_accuracy_diagnostics.get("legacy_info_count") or 0)
        if errors == 0 and warnings == 0:
            suffix = f"；另有历史审计记录 {legacy} 条（不影响当前运行）" if legacy else ""
            lines.append(f"- 当前检查通过，未发现新的不一致{suffix}")
        else:
            lines.append(f"- 当前异常 {errors} 项 · 提醒 {warnings} 项")
            if legacy:
                lines.append(f"- 历史审计记录 {legacy} 条（不计入当前异常）")
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
            WHERE is_shadow = 1 AND outcome_source='real_pnl' AND pnl_r IS NOT NULL
        """)
        pseudo_count = _count(repo, """
            SELECT COUNT(*) FROM strategy_evaluations
            WHERE is_shadow = 1 AND (outcome_source != 'real_pnl' OR outcome_source IS NULL)
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


def _fetch_market_data_quality(repo: CryptoGuardRepository, *, analysis_time_utc: int | None = None) -> dict[str, Any]:
    """P0-5: Fetch market data quality for the hourly report.

    Aggregates per-symbol per-TF health using assess_health. Returns a dict
    with ``degraded`` (bool), ``symbols`` (dict[str, dict[str, health]]),
    and ``deferred_analyses`` (list). When the config or DB is unavailable,
    returns ``degraded=False`` with empty symbols (fail-open for the report
    text path — the generation layer fail-closed is handled elsewhere).

    Phase B (07-05): ``analysis_time_utc`` anchors the health assessment to
    the same batch cutoff the report is rendering. When ``None``, falls back
    to ``latest_closed_close_time_ms("15m", utc_ms())`` (wall-clock). The
    chosen analysis_time is surfaced in the returned dict under
    ``analysis_time`` so the renderer and diagnostics can reference it.
    """
    try:
        from plugins.crypto_guard.config.loader import load_config
        from plugins.crypto_guard.data.market_data_health import assess_health
        from plugins.crypto_guard.utils import latest_closed_close_time_ms, utc_ms

        cfg = load_config()
        market_data_cfg = cfg.market_data or {}
        required_samples = market_data_cfg.get("required_samples", {})
        tfs = list(market_data_cfg.get("analysis_window", {}).keys()) or ["1d", "4h", "1h", "15m", "5m"]

        # Phase B (07-05): use the batch's analysis_time when provided so the
        # health check is anchored to the same batch cutoff as the rest of the
        # report. Fall back to the wall-clock latest 15m close only when the
        # caller did not supply a batch analysis_time (e.g. ad-hoc invocations).
        if analysis_time_utc is not None:
            analysis_time = int(analysis_time_utc)
        else:
            analysis_time = latest_closed_close_time_ms("15m", utc_ms())

        # Get active symbols from the repo
        symbols = repo.active_analysis_symbols()
        if not symbols:
            return {"degraded": False, "symbols": {}, "deferred_analyses": []}

        symbols_md: dict[str, dict[str, Any]] = {}
        any_degraded = False
        deferred: list[dict[str, Any]] = []

        for sym in symbols:
            tf_health: dict[str, Any] = {}
            for tf in tfs:
                required = int(required_samples.get(tf, 200))
                health = assess_health(
                    repo, sym, tf,
                    analysis_time_utc=analysis_time,
                    required_count=required,
                )
                tf_health[tf] = health
                if not health.get("ready"):
                    any_degraded = True
                    deferred.append({
                        "symbol": sym,
                        "interval": tf,
                        "reason": health.get("reason", ""),
                        "contiguous_count": health.get("contiguous_count", 0),
                        "required_count": required,
                    })
            symbols_md[sym] = tf_health

        return {
            "degraded": any_degraded,
            "symbols": symbols_md,
            "deferred_analyses": deferred,
            "analysis_time": analysis_time,
        }
    except Exception as exc:
        LOGGER.warning("_fetch_market_data_quality failed: %s", exc)
        # P2-9: fail-closed. Previously this returned degraded=False, causing
        # the report to show "data is fine" when the health check itself
        # crashed. Now return degraded=True so the report shows the
        # "行情质量状态不可用" banner.
        return {
            "degraded": True,
            "symbols": {},
            "deferred_analyses": [],
            "analysis_time": int(analysis_time_utc) if analysis_time_utc is not None else None,
            "error": str(exc),
            "fail_closed": True,
        }


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


def _fetch_market_regime_gate_stats(repo: CryptoGuardRepository, hours: int = 24) -> dict[str, Any]:
    """Fetch market regime gate statistics from recent GA decisions.

    Counts alignment categories, watch_only occurrences, unknown data,
    and fallback_now time_source usage (Fix 8).
    """
    try:
        day_ago = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        rows = repo.conn.execute(
            """
            SELECT market_regime_gate_json
            FROM ga_decisions
            WHERE datetime(created_at) >= datetime(?) AND market_regime_gate_json IS NOT NULL
            """,
            (day_ago,),
        ).fetchall()

        if not rows:
            return {"ok": True, "total_checks": 0, "counter_regime": 0, "independent_trend": 0, "watch_only": 0, "unknown": 0, "aligned": 0, "fallback_now_count": 0}

        total = len(rows)
        counter_regime = 0
        watch_only = 0
        unknown = 0
        aligned = 0
        independent_trend = 0
        fallback_now_count = 0
        counter_regime_symbols: dict[str, int] = {}

        for row in rows:
            try:
                gate = json.loads(row["market_regime_gate_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            # Count time_source=fallback_now (Fix 8)
            if gate.get("time_source") == "fallback_now":
                fallback_now_count += 1

            # Extract alignment from adjustments or market_regime sub-dict
            alignment = ""
            adjustments = gate.get("adjustments", {})
            market_regime = gate.get("market_regime", {})
            if adjustments.get("regime_alignment"):
                alignment = adjustments["regime_alignment"]
            elif market_regime.get("regime_alignment"):
                alignment = market_regime["regime_alignment"]

            if alignment == "counter_regime":
                counter_regime += 1
                # Track symbol for top counter_regime
                symbol = gate.get("market_regime", {}).get("symbol") or adjustments.get("symbol", "")
                if symbol:
                    counter_regime_symbols[symbol] = counter_regime_symbols.get(symbol, 0) + 1
            elif alignment == "independent_trend":
                independent_trend += 1
            elif alignment == "aligned":
                aligned += 1
            elif alignment == "unclear":
                unknown += 1

            # Check watch_only in adjustments
            if adjustments.get("watch_only"):
                watch_only += 1

        # Top 3 symbols with counter_regime
        top_counter_regime_symbols = sorted(
            [{"symbol": s, "count": c} for s, c in counter_regime_symbols.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:3]

        return {
            "ok": True,
            "total_checks": total,
            "counter_regime": counter_regime,
            "independent_trend": independent_trend,
            "watch_only": watch_only,
            "unknown": unknown,
            "aligned": aligned,
            "fallback_now_count": fallback_now_count,
            "top_counter_regime_symbols": top_counter_regime_symbols,
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
    market_regime_gate: dict[str, Any] | None = None,
    state_consistency: dict[str, Any] | None = None,
    batch_state: dict[str, Any] | None = None,
    report_accuracy_diagnostics: dict[str, Any] | None = None,
    market_data_quality: dict[str, Any] | None = None,
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
        f"北京时间：{_format_time_utc8(generated_at_utc)}",
        f"UTC 时间：{generated_at_utc}",
        "",
        "**产品分析概览：**",
    ]

    # R6: Market data degraded banner — show at top when any TF not ready.
    # P2-4 R3: Distinguish "health check crashed" (fail_closed=True) from
    # "data is genuinely gappy" (degraded=True, fail_closed != True).
    if market_data_quality and market_data_quality.get("fail_closed"):
        error_msg = market_data_quality.get("error", "unknown")
        lines.insert(4, "")
        lines.insert(4, f"⚠️ 行情质量状态不可用：{error_msg}")
        lines.insert(4, "")
    elif market_data_quality and market_data_quality.get("degraded"):
        lines.insert(4, "")
        lines.insert(4, "⚠️ 行情分析不可用/降级 — 数据不完整")
        lines.insert(4, "")

    # P0: render legacy summary also surfaces batch completion header if provided.
    if batch_state:
        incomplete = bool(batch_state.get("incomplete"))
        lines.append(
            f"- 分析批次：{_batch_status_label(batch_state.get('status'), incomplete)} · "
            f"完成 {batch_state.get('completed_count', 0)}/{batch_state.get('total_count', 0)} 个品种"
        )
        if incomplete:
            missing = batch_state.get("missing_symbols") or []
            lines.append(f"  - 尚未完成：{', '.join(missing) if missing else '等待分析结果'}")
        lines.append("")

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
            # P2-14: drawdown display as non-negative amplitude
            dd_pct = abs(float(risk_state.get('drawdown_pct', 0)))
            lines.append(f"- **风险状态：{', '.join(risk_status)}**（回撤 {dd_pct:.1f}%）")
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

    # Phase D (07-03): C/D reason summary — route through the shared
    # _format_cd_reasons helper (which calls _compact_items) so both report
    # paths share the same C/D rendering logic. Extract C/D signals from
    # ga_decision_json. When there are no signals, _compact_items is still
    # called with an empty list to satisfy the shared-helper contract.
    cd_reason_items: list[str] = []
    for signal in signals:
        decision_json = _safe_json(signal.get("ga_decision_json"), {})
        grade = str((decision_json or {}).get("signal_grade") or signal.get("signal_grade") or "").upper()
        if grade in {"C", "D"}:
            summary = (decision_json or {}).get("rendered_summary") or (decision_json or {}).get("summary") or (decision_json or {}).get("final_summary") or signal.get("summary") or ""
            if summary:
                cd_reason_items.append(str(summary))
    cd_reasons_text = _format_cd_reasons(cd_reason_items, max_items=3)
    if cd_reasons_text:
        lines.extend(["", "**无优势品种汇总（C/D）**", f"- 主要原因：{cd_reasons_text}"])

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
                f"回撤：{abs(float(snap.get('drawdown_percent') or 0)):.2f}%"
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
            critical_parts = []
            info_parts = []
            # Critical: active PnL loop integrity
            if summary.get("active_eval_missing_ga_decision_id", 0) > 0:
                critical_parts.append(f"Active缺GA决策ID={summary['active_eval_missing_ga_decision_id']}")
            if summary.get("paper_order_missing_active_eval", 0) > 0:
                critical_parts.append(f"订单缺Active评估={summary['paper_order_missing_active_eval']}")
            if summary.get("closed_trade_missing_active_real_pnl", 0) > 0:
                critical_parts.append(f"平仓缺Active实PnL={summary['closed_trade_missing_active_real_pnl']}")
            # Standard issues
            if summary.get("duplicate_open_trades", 0) > 0:
                critical_parts.append(f"重复开仓={summary['duplicate_open_trades']}")
            if summary.get("orphan_patches", 0) > 0:
                critical_parts.append(f"孤儿补丁={summary['orphan_patches']}")
            if summary.get("status_mismatches", 0) > 0:
                critical_parts.append(f"状态不一致={summary['status_mismatches']}")
            if summary.get("duplicate_patches", 0) > 0:
                critical_parts.append(f"重复补丁={summary['duplicate_patches']}")
            if summary.get("stale_shadows", 0) > 0:
                critical_parts.append(f"过期影子={summary['stale_shadows']}")
            if summary.get("draft_limbo", 0) > 0:
                critical_parts.append(f"草稿滞留={summary['draft_limbo']}")
            # Warning: shadow candidate quality
            if summary.get("shadow_candidate_legacy_only", 0) > 0:
                info_parts.append(f"候选仅旧样本={summary['shadow_candidate_legacy_only']}")
            if critical_parts:
                lines.append(f"- **关键问题 {len(critical_parts)} 项**：{'，'.join(critical_parts)}")
            if info_parts:
                lines.append(f"- 提示：{'，'.join(info_parts)}")
            if not critical_parts and not info_parts:
                lines.append(f"- 发现问题 {total} 个（非关键）")
            # R6/AC17: Show structured issue details — type, scope, time_window
            issues = state_consistency.get("issues") or []
            for issue in issues[:10]:
                issue_type = issue.get("type") or issue.get("issue_type") or ""
                scope = issue.get("scope") or ""
                time_window = issue.get("time_window") or ""
                severity = issue.get("severity") or ""
                detail = issue.get("detail") or issue.get("details") or ""
                parts = [p for p in (issue_type, scope, time_window, severity, detail) if p]
                if parts:
                    lines.append(f"  - {' | '.join(parts)}")
        else:
            lines.extend(["", "**状态一致性诊断：**", "- 全部正常，未发现状态不一致"])

    # R6: Market data quality section
    # P2-4 R3: Distinguish "health check crashed" (fail_closed=True) from
    # "data is genuinely gappy" (degraded=True, fail_closed != True).
    _append_market_data_quality_section(
        lines,
        market_data_quality,
        heading="**行情数据质量：**",
        include_status=False,
    )

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

    # Market regime gate stats (Fix 5 + Fix 8)
    if market_regime_gate and not market_regime_gate.get("error"):
        mg = market_regime_gate
        if mg.get("total_checks", 0) > 0:
            lines.extend(["", "**市场情绪门禁（24h）**"])
            lines.append(
                f"- 检查 {mg['total_checks']} 次，"
                f"counter_regime {mg.get('counter_regime', 0)} 次，"
                f"independent_trend {mg.get('independent_trend', 0)} 次，"
                f"watch_only {mg.get('watch_only', 0)} 次，"
                f"数据不足 {mg.get('unknown', 0)} 次，"
                f"aligned {mg.get('aligned', 0)} 次"
            )
            # Fix 8: time_source fallback_now warning
            fallback_count = mg.get("fallback_now_count", 0)
            if fallback_count > 0:
                lines.append(
                    f"- ⚠ {fallback_count} 次门禁使用当前时间（无原始分析时间），可能存在 lookahead 风险"
                )
            # Top 3 symbols with counter_regime
            top_counter = mg.get("top_counter_regime_symbols", [])
            if top_counter:
                symbol_text = "，".join(f"{s['symbol']}({s['count']})" for s in top_counter[:3])
                lines.append(f"- counter_regime 前三品种：{symbol_text}")

    lines.extend(["", "**队列：**", f"- 用户待处理：{queue_counts['pending_user']}", f"- 后台待处理：{queue_counts['pending_background']}", f"- 运行中：{queue_counts['running']}"])
    # P1/P2 fix (07-09): split failed_jobs via the shared helper so this
    # brief path stays consistent with the system-status count and the
    # 九、风险事件 section in render_ga_hourly_summary /
    # render_hourly_report_text.
    _brief_current_failed, _brief_legacy_count = _split_current_and_legacy_failed_jobs(
        failed_jobs,
    )
    health = "正常" if not _brief_current_failed and queue_counts.get("running", 0) < 5 else "需关注"
    lines.extend(["", "**系统健康度：**", f"- 状态：{health}", f"- 飞书 outbox/队列：用户 {queue_counts['pending_user']}，后台 {queue_counts['pending_background']}，运行中 {queue_counts['running']}"])
    if _brief_current_failed or _brief_legacy_count > 0:
        lines.extend(["", "**最近失败任务：**"])
        for job in _brief_current_failed:
            err = (job.get("error_message") or "")[:120]
            lines.append(f"- #{job['id']} {job['job_type']}：{err}")
        if _brief_legacy_count > 0:
            lines.append(
                f"- 另有 {_brief_legacy_count} 个历史 schema 校验失败已归档到审计"
                "（07-09 alias-repair SOP 已处理，不再列入当前风险事件）"
            )

    # P2: report accuracy diagnostics (legacy renderer also surfaces them).
    if report_accuracy_diagnostics and not report_accuracy_diagnostics.get("error"):
        summary = report_accuracy_diagnostics.get("summary") or {}
        total = report_accuracy_diagnostics.get("total_issues", 0)
        lines.extend(["", "**报告准确性诊断：**"])
        if total == 0:
            lines.append("- 报告准确性诊断全部通过，未发现不一致")
        else:
            for code, count in summary.items():
                # `layer_counts` is a nested dict, not a count — skip it
                # (Phase E 07-09: report_diagnostics now returns a
                # per-layer breakdown that would otherwise crash int()).
                if isinstance(count, dict):
                    continue
                if int(count or 0) > 0:
                    lines.append(f"- {code}={count}")
    lines.append("")
    lines.append("不构成实盘建议，仅用于模拟盘与策略研究。")
    return "\n".join(lines)


def _count(repo: CryptoGuardRepository, sql: str) -> int:
    return int(repo.conn.execute(sql).fetchone()[0])


def _decision_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = _safe_json(row.get("raw_decision_json"), {})
    trade_plan = _safe_json(row.get("trade_plan_json"), {})
    # P1-9: prefer rendered_summary, fallback to final_summary
    rendered_summary = row.get("rendered_summary")
    final_summary = row.get("final_summary")
    summary_to_use = rendered_summary if rendered_summary else final_summary
    return {
        "ga_decision_id": row.get("id"),
        "symbol": row.get("symbol"),
        "decision": row.get("decision"),
        "legacy_decision": raw.get("legacy_decision"),
        "signal_grade": row.get("signal_grade"),
        "confidence": row.get("confidence"),
        "market_bias": row.get("market_bias"),
        "trend_stage": row.get("trend_stage"),
        "final_summary": summary_to_use,
        "rendered_summary": rendered_summary,
        "risk_check": _safe_json(row.get("risk_check_json"), {}),
        "feishu_actions": _safe_json(row.get("feishu_actions_json"), []),
        "trade_plan": trade_plan,
        # Phase E (07-05): plan lifecycle fields. candidate_trade_plan is
        # the deterministic plan preserved for audit even when execution is
        # blocked (LLM failure / risk rejection / continuity invalidated).
        # plan_status / plan_source / plan_blockers carry the structured
        # reason so the report can surface the actual blocking stage
        # instead of collapsing to "缺交易计划".
        "candidate_trade_plan": raw.get("candidate_trade_plan"),
        "plan_status": raw.get("plan_status"),
        "plan_source": raw.get("plan_source"),
        "plan_blockers": raw.get("plan_blockers") or [],
        "llm_status": raw.get("llm_status"),
        "llm_error": raw.get("llm_error"),
        # Phase B (07-07): LLM error taxonomy fields used by recent-failure
        # rendering to show the category alongside the error text.
        "llm_error_category": raw.get("llm_error_category"),
        "llm_fallback_reason": raw.get("llm_fallback_reason"),
        "plan_origin": raw.get("plan_origin"),
        "plan_execution_state": raw.get("plan_execution_state"),
        # Phase F (07-05): raw vs effective grade/score. raw_signal_grade
        # / raw_score are the deterministic SOP's pre-gate conclusions.
        # effective_signal_grade / effective_execution_confidence are the
        # post-gate canonical values. grade_adjustments records every
        # downgrade with reason code so the report can surface the
        # hysteresis/clamp/LLM failure reason.
        "raw_signal_grade": raw.get("raw_signal_grade"),
        "raw_score": raw.get("raw_score"),
        "effective_signal_grade": raw.get("effective_signal_grade"),
        "effective_execution_confidence": raw.get("effective_execution_confidence"),
        "grade_adjustments": raw.get("grade_adjustments") or [],
        # P0 latency fields exposed for render helpers.
        "analysis_time": int(row.get("analysis_time") or 0),
        "created_at": row.get("created_at"),
        "batch_id": row.get("batch_id"),
        "previous_grade": row.get("previous_grade"),
        # Phase D (07-03): expose structured multi-TF fields from
        # raw_decision_json so the report renderer can build market-context
        # reason text without re-parsing the LLM summary.
        "timeframe_context": raw.get("timeframe_context") or {},
        "alignment": raw.get("alignment"),
        "htf_conflict": raw.get("htf_conflict"),
        "market_reason_codes": raw.get("market_reason_codes") or [],
    }


def _opportunity_classifier(row: dict[str, Any]) -> dict[str, Any]:
    """P0 classify a decision row into executable / observation / no_edge.

    ``executable`` requires grade ∈ {S,A,B}, confidence ≥
    MIN_CONFIDENCE_FOR_PAPER_ORDER, a complete trade_plan, risk_check.ok
    truthy, and a decision that authorises a paper order.  Any missing
    gate demotes to ``observation``; C/D/monitor_only/no_edge fall to
    ``no_edge`` is left to the caller.
    """
    grade = str(row.get("signal_grade") or "D").upper()
    confidence = float(row.get("confidence") or 0)
    risk_check = row.get("risk_check") or {}
    if isinstance(risk_check, str):
        try:
            risk_check = json.loads(risk_check)
        except Exception:
            risk_check = {}
    trade_plan = row.get("trade_plan") or {}
    if isinstance(trade_plan, str):
        try:
            trade_plan = json.loads(trade_plan)
        except Exception:
            trade_plan = {}
    from plugins.crypto_guard.notify.report_consistency import is_valid_trade_plan
    decision = str(row.get("decision") or "")
    blockers: list[str] = []

    if grade not in {"S", "A", "B"}:
        return {"tier": "no_edge", "blockers": [f"grade={grade}"]}

    # Phase F (07-05): B belongs to observation only — never executable.
    # Uses PAPER_ORDER_GRADES from the single grade configuration source
    # so the controller and report share one policy.
    from plugins.crypto_guard.strategy.grade_config import PAPER_ORDER_GRADES
    if grade not in PAPER_ORDER_GRADES:
        return {"tier": "observation", "blockers": [f"grade={grade}_observation_only"]}

    if confidence < MIN_CONFIDENCE_FOR_PAPER_ORDER:
        blockers.append(f"confidence<{MIN_CONFIDENCE_FOR_PAPER_ORDER:.2f}")
    if not is_valid_trade_plan(trade_plan):
        blockers.append("missing_trade_plan")
    if not bool(risk_check.get("ok")):
        blockers.append("risk_check_failed")
    if decision not in {"create_paper_order", "trade_plan_available"}:
        blockers.append(f"decision={decision}")

    if not blockers:
        return {"tier": "executable", "blockers": []}
    return {"tier": "observation", "blockers": blockers}


def _is_stale_decision(row: dict[str, Any], stale_cutoff_ms: int) -> bool:
    """A decision is stale when its analysis_time falls before the start of
    the current 15m analysis window (i.e. it belongs to an older batch).
    """
    analysis_time = int(row.get("analysis_time") or 0)
    if analysis_time <= 0:
        return True
    return analysis_time <= stale_cutoff_ms


def _format_opportunity_row(
    row: dict[str, Any],
    open_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    tier_label: str,
    market_data_degraded: bool = False,
) -> str:
    """Render a concise user-facing opportunity row.

    Full batch IDs, millisecond timestamps, grade deltas and raw gate keys
    remain available in the structured report payload for audit purposes.

    P1-5: when ``market_data_degraded=True``, suppress the deterministic
    direction/stage text and replace with "方向不可靠" / "数据降级" so the
    report does not show "方向偏多 · 趋势初期" while the data is degraded.
    """
    symbol = row.get("symbol") or "-"
    grade = str(row.get("signal_grade") or "D").upper()
    confidence = float(row.get("confidence") or 0)
    analysis_time = int(row.get("analysis_time") or 0)
    created_at = row.get("created_at")
    # Phase F (07-05): render raw vs effective grade. When raw_signal_grade
    # differs from canonical signal_grade (post-gate), show "原始评分 X%
    # · 执行等级 Y" so B/95% never reads as a high-confidence executable.
    raw_signal_grade = str(row.get("raw_signal_grade") or "").upper()
    raw_score = row.get("raw_score")
    try:
        raw_score_text = f"{float(raw_score) * 100:.0f}%" if raw_score is not None else f"{confidence * 100:.0f}%"
    except (TypeError, ValueError):
        raw_score_text = f"{confidence * 100:.0f}%"
    grade_adjustments = row.get("grade_adjustments") or []
    # Summarize grade_adjustments into a short reason code list.
    adj_texts: list[str] = []
    for adj in grade_adjustments:
        if not isinstance(adj, dict):
            continue
        code = str(adj.get("code") or "")
        if code == "hysteresis":
            adj_texts.append("评级迟滞")
        elif code == "clamp_sa_evidence":
            adj_texts.append("S/A 证据不足")
        elif code == "llm_parse_failed":
            adj_texts.append("LLM 解析失败")
        elif code == "llm_disabled":
            adj_texts.append("LLM 已禁用")
        elif code == "performance_gate_degraded":
            adj_texts.append("Performance 降级")
        elif code == "performance_gate_watch_only":
            adj_texts.append("Performance 阻断")
        elif code:
            adj_texts.append(code)
    adj_suffix = f"（{'；'.join(adj_texts)}）" if adj_texts else ""
    # P2-13: age based on analysis_time (market time), not created_at (DB insert time)
    age_min = ""
    if analysis_time > 0:
        age_min = f"{max(0, int((utc_ms() - analysis_time) / 60000))}m"
    elif created_at:
        try:
            parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            age_min = f"{int((utc_ms() - int(parsed.timestamp() * 1000)) / 60000)}m"
        except Exception:
            age_min = "-"
    else:
        age_min = "-"
    open_orders = open_by_symbol.get(symbol, [])
    pos_status = _position_summary(open_orders) if open_orders else "无持仓"
    blockers = row.get("_blockers") or []
    # P1-5: when market data is degraded, do not show deterministic direction/stage.
    if market_data_degraded:
        bias = "方向不可靠"
        stage = "数据降级"
    else:
        bias = _market_bias_label(row.get("market_bias"))
        stage = _trend_stage_label(row.get("trend_stage"))
    age_text = _age_label(age_min)
    status_text = "可执行" if tier_label == "可执行" else "继续观察"
    details = [bias, stage, pos_status, age_text]
    details_text = " · ".join(x for x in details if x)
    reason = _humanize_gate_blockers(blockers, row)
    # Phase F (07-05): build the grade display. When grade is B/C/D and
    # raw_score is high, show "原始评分 X% · 执行等级 Y{suffix}" so it
    # never reads as "B 级 95%" (which implies a high-confidence
    # executable B). For S/A, keep the legacy "等级 X · 置信度 Y%" format
    # but append the suffix if adjustments exist.
    if grade in {"B", "C", "D"} or adj_suffix:
        grade_line = f"原始评分 {raw_score_text} · 执行等级 {grade}{adj_suffix}"
    else:
        grade_line = f"{grade}级 · {confidence * 100:.0f}%{adj_suffix}"
    # Phase D (07-03): for observation rows, append the structured
    # 市场/门禁 reason block so the user sees multi-TF context before
    # gate terminology. This ensures "交易计划尚未形成" is never the
    # sole explanation.
    # R1-12 (07-03 final review): drop the separate "原因：{reason}" line
    # for non-executable rows because it duplicates the "门禁：{reason}"
    # line emitted by _format_observation_market_and_gate_reasons (both
    # call _humanize_gate_blockers on the same blockers). For executable
    # rows the "条件：{reason}" line is preserved because no 市场/门禁
    # block is appended for them.
    if tier_label == "可执行":
        result = (
            f"- **{symbol}** · {grade_line} · **{status_text}**\n"
            f"  {details_text}\n"
            f"  条件：{reason}"
        )
    else:
        result = (
            f"- **{symbol}** · {grade_line} · **{status_text}**\n"
            f"  {details_text}"
        )
        market_gate_lines = _format_observation_market_and_gate_reasons(row)
        if market_gate_lines:
            result += "\n  " + "\n  ".join(market_gate_lines)
    # Phase E (07-05): when candidate_trade_plan exists with structured
    # blockers (LLM failure / risk rejection / continuity invalidated),
    # append the candidate summary so the operator sees the deterministic
    # path produced a plan that was then blocked. This is required by
    # Phase A Fact 4 — the report must mention "候选计划已生成" / "LLM 失败".
    candidate = row.get("candidate_trade_plan")
    plan_blockers = row.get("plan_blockers") or []
    if isinstance(candidate, dict) and plan_blockers:
        candidate_summary = _trade_plan_summary(row)
        if candidate_summary and candidate_summary not in result:
            result += f"\n  {candidate_summary}"
    # Phase C (07-07): always append the plan_execution_state label per
    # design §6.5 so the operator sees the 5-branch candidate state wording
    # (confirmed / unconfirmed / risk_rejected / invalidated / no_candidate)
    # regardless of whether a structured blocker exists. This replaces the
    # legacy single "候选计划已生成" text with the 5-branch wording.
    _state_label = _render_plan_state_label(row)
    if _state_label and _state_label not in result:
        result += f"\n  {_state_label}"
    return result


def _batch_status_label(status: Any, incomplete: bool = False) -> str:
    if incomplete:
        return "分析进行中"
    return {
        "success": "分析完成",
        "partial_failed": "部分完成",
        "failed": "分析失败",
        "absent": "等待分析",
        "running": "分析进行中",
    }.get(str(status or ""), "状态未知")


def _humanize_gate_blockers(blockers: list[Any], row: dict[str, Any]) -> str:
    labels: list[str] = []
    blocker_strings = [str(x) for x in blockers]
    if any(x == "missing_trade_plan" for x in blocker_strings):
        labels.append("交易计划尚未形成")
    if any(x.startswith("confidence<") for x in blocker_strings):
        labels.append(f"置信度未达到 {MIN_CONFIDENCE_FOR_PAPER_ORDER * 100:.0f}%")
    if "stale_decision" in blocker_strings:
        labels.append("分析结果已过期")
    if any(x == "risk_check_failed" for x in blocker_strings):
        risk = row.get("risk_check") or {}
        reasons = risk.get("reasons") if isinstance(risk, dict) else []
        meaningful = [
            str(reason) for reason in (reasons or [])
            if "缺少完整 trade_plan" not in str(reason)
        ]
        if meaningful:
            labels.append("风控未通过：" + "；".join(meaningful[:2]))
        elif "交易计划尚未形成" not in labels:
            labels.append("风控条件未通过")
    if any(x.startswith("decision=") for x in blocker_strings) and not labels:
        labels.append("当前决策仅用于观察")
    if not blocker_strings:
        plan = row.get("trade_plan") or {}
        entry_type = str(plan.get("entry_type") or "")
        return {
            "limit": "等待限价触发并持续满足风控",
            "trigger": "等待突破触发并持续满足风控",
            "market": "执行门禁已通过",
        }.get(entry_type, "执行门禁已通过")
    return "；".join(_dedupe(labels)) or "当前条件不足，继续观察"


# Phase D (07-03): structured market-reason / gate-reason helpers. These
# translate structured fields (timeframe_context, alignment, htf_conflict,
# market_reason_codes) into user-facing Chinese so the report explains the
# real market structure instead of only citing execution-gate terminology.

# Mapping from market_reason_codes to concise Chinese reason phrases.
_MARKET_REASON_CODE_LABELS: dict[str, str] = {
    "htf_conflict": "高周期冲突",
    "countertrend_rebound": "反趋势反弹",
    "bias_stage_contradiction": "方向与阶段矛盾",
    "overextended": "追价风险",
    "data_incomplete": "数据不完整",
}

# Mapping from alignment enum to concise Chinese label.
_ALIGNMENT_LABELS_REPORT: dict[str, str] = {
    "aligned": "已对齐",
    "partial": "部分对齐",
    "countertrend_rebound": "反趋势反弹",
    "neutral": "中性",
    "unknown": "未知",
}

# Mapping from market_bias to concise Chinese label for market-reason text.
_BIAS_LABELS_REPORT: dict[str, str] = {
    "bullish": "偏多",
    "bearish": "偏空",
    "neutral": "中性",
    "mixed": "混合",
    "unknown": "未知",
}


def _format_market_reason_text(row: dict[str, Any]) -> str:
    """Build a concise Chinese market-reason line from structured fields.

    Reads ``timeframe_context``, ``alignment``, ``htf_conflict`` and
    ``market_reason_codes`` from the decision row and produces text like:
    ``日线偏空，4H震荡，1H反弹；尚未形成高周期同向确认``.

    Returns "" when no structured market context is available.
    """
    tf_ctx = row.get("timeframe_context") or {}
    if not isinstance(tf_ctx, dict):
        tf_ctx = {}
    alignment = str(row.get("alignment") or "").lower()
    htf_conflict = bool(row.get("htf_conflict"))
    reason_codes = row.get("market_reason_codes") or []
    if not isinstance(reason_codes, list):
        reason_codes = []

    # Build per-TF summary (1d, 4h, 1h).
    tf_parts: list[str] = []
    tf_labels = {"1d": "日线", "4h": "4H", "1h": "1H"}
    for tf, label in tf_labels.items():
        ctx = tf_ctx.get(tf)
        if not isinstance(ctx, dict):
            continue
        bias = str(ctx.get("bias") or "").lower()
        structure = str(ctx.get("structure") or "").lower()
        bias_label = _BIAS_LABELS_REPORT.get(bias, "")
        # Describe the TF with a concise phrase.
        if structure == "range":
            tf_parts.append(f"{label}震荡")
        elif structure == "transition":
            tf_parts.append(f"{label}转换")
        elif structure in {"bullish", "uptrend"}:
            tf_parts.append(f"{label}偏多")
        elif structure in {"bearish", "downtrend"}:
            tf_parts.append(f"{label}偏空")
        elif structure == "rebound":
            tf_parts.append(f"{label}反弹")
        elif bias_label:
            tf_parts.append(f"{label}{bias_label}")
    tf_text = "，".join(tf_parts)

    # Build the conflict / alignment qualifier.
    qualifier = ""
    if alignment == "countertrend_rebound" or htf_conflict:
        qualifier = "尚未形成高周期同向确认"
    elif alignment == "partial":
        qualifier = "部分周期未同向确认"
    elif alignment == "aligned":
        qualifier = "高周期已同向确认"

    # Reason-code labels (additional context).
    code_labels: list[str] = []
    for code in reason_codes:
        label = _MARKET_REASON_CODE_LABELS.get(str(code))
        if label and label not in code_labels:
            code_labels.append(label)

    # Compose: TF summary ; qualifier ; reason codes.
    segments: list[str] = []
    if tf_text:
        segments.append(tf_text)
    if qualifier:
        segments.append(qualifier)
    # Reason codes are appended only when they add info not already in the
    # TF summary or qualifier (e.g. "追价风险", "数据不完整").
    extra_codes = [
        c for c in code_labels
        if c not in {"高周期冲突", "反趋势反弹"}
        or (c == "高周期冲突" and not qualifier)
    ]
    if extra_codes:
        segments.append("；".join(extra_codes))

    return "；".join(segments) if segments else ""


def _format_observation_market_and_gate_reasons(row: dict[str, Any]) -> list[str]:
    """Build the multi-line 市场/门禁 reason block for an observation row.

    Market reasons are rendered first (multi-TF context, alignment, conflict),
    followed by gate-blocker reasons. This ensures '交易计划尚未形成' is
    never the sole explanation — the market context always precedes it.

    Returns a list of report lines (without the leading "- " bullet). Each
    line is prefixed with "市场：" or "门禁：". Returns an empty list when
    neither market nor gate context is available.
    """
    lines: list[str] = []
    market_text = _format_market_reason_text(row)
    if market_text:
        lines.append(f"市场：{market_text}")

    blockers = row.get("_blockers") or []
    gate_text = _humanize_gate_blockers(blockers, row)
    # Suppress the default "当前条件不足，继续观察" placeholder when there
    # are no real blockers — it carries no information.
    if gate_text and gate_text != "当前条件不足，继续观察":
        lines.append(f"门禁：{gate_text}")

    # If the gate text is the only reason and there's no market context,
    # force a market-context line so "交易计划尚未形成" is not the sole
    # explanation. Fall back to the bias/stage labels.
    if not market_text and gate_text:
        bias = _market_bias_label(row.get("market_bias"))
        stage = _trend_stage_label(row.get("trend_stage"))
        fallback_parts = [p for p in (bias, stage) if p]
        if fallback_parts:
            lines.insert(0, f"市场：{'，'.join(fallback_parts)}")

    return lines


def _market_bias_label(value: Any) -> str:
    return {
        "bullish": "方向偏多",
        "bearish": "方向偏空",
        "neutral": "方向中性",
        "mixed": "方向分歧",
    }.get(str(value or "").lower(), "")


def _trend_stage_label(value: Any) -> str:
    return {
        "early": "趋势初期",
        "middle": "趋势中段",
        "late": "趋势后段",
        "range": "震荡区间",
        "transition": "方向转换中",
        "unknown": "阶段不明",
    }.get(str(value or "").lower(), "")


def _age_label(age: str) -> str:
    if age.endswith("m"):
        return f"{age[:-1]} 分钟前"
    return ""


def _distribution_source_label(source_raw: str, duckdb_stats: dict[str, Any] | None) -> str:
    """P2 phrasing clarification for the distribution source label."""
    if source_raw == "duckdb" and (duckdb_stats or {}).get("ok"):
        return "DuckDB 时序"
    if source_raw in {"in_memory_fallback", "sqlite_fallback"}:
        return "SQLite 实时等级统计（DuckDB 未启用）"
    return str(source_raw or "SQLite 实时等级统计（DuckDB 未启用）")


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
    # P2 (07-09 R4): apply the legacy schema-fail split INSIDE the brief
    # builder so the LLM context never receives archived legacy jobs. The
    # renderers (``_render_degraded_report``, ``render_ga_hourly_summary``,
    # ``render_hourly_report_text``) filter legacy jobs out of the
    # user-visible risk-events section - if the brief still received them,
    # the brief would reference them as current failures, contradicting
    # the rendered "另有 N 个历史 schema 校验失败已归档" line. Making the
    # brief self-contained ensures every caller gets the filter for free.
    brief_failed_jobs, _brief_legacy_count = _split_current_and_legacy_failed_jobs(
        failed_jobs,
    )
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
                "failed_jobs": brief_failed_jobs,
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
    # Phase F (07-05): render raw vs effective grade. When raw_signal_grade
    # differs from the canonical signal_grade (post-gate), show both so the
    # operator can see the original signal strength and the downgrade reason.
    # Format: "等级 B（原始评分 95%）" when raw_score >= 0.80 but effective
    # grade is B due to hysteresis/clamp. Also surface grade_adjustments
    # reason parenthetically when present.
    raw_signal_grade = str(decision.get("raw_signal_grade") or "").upper()
    raw_score = decision.get("raw_score")
    effective_signal_grade = str(decision.get("effective_signal_grade") or grade or "").upper()
    grade_adjustments = decision.get("grade_adjustments") or []
    raw_score_text = ""
    if raw_score is not None:
        try:
            raw_score_text = f"{float(raw_score) * 100:.0f}%"
        except (TypeError, ValueError):
            raw_score_text = ""
    # Build the grade display.
    grade_display = str(grade or "-")
    grade_suffix_parts: list[str] = []
    if raw_signal_grade and raw_signal_grade != str(grade).upper() and raw_score_text:
        grade_suffix_parts.append(f"原始评分 {raw_score_text}")
    # Summarize grade_adjustments into a short reason code list.
    if grade_adjustments:
        adj_texts: list[str] = []
        for adj in grade_adjustments:
            if not isinstance(adj, dict):
                continue
            code = str(adj.get("code") or "")
            if code == "hysteresis":
                adj_texts.append("评级迟滞")
            elif code == "clamp_sa_evidence":
                adj_texts.append("S/A 证据不足")
            elif code == "llm_parse_failed":
                adj_texts.append("LLM 解析失败")
            elif code == "llm_disabled":
                adj_texts.append("LLM 已禁用")
            elif code == "performance_gate_degraded":
                adj_texts.append("Performance 降级")
            elif code == "performance_gate_watch_only":
                adj_texts.append("Performance 阻断")
            elif code:
                adj_texts.append(code)
        if adj_texts:
            grade_suffix_parts.append("；".join(adj_texts))
    grade_suffix = f"（{'；'.join(grade_suffix_parts)}）" if grade_suffix_parts else ""
    # Compose the headline. When grade is B/C/D and raw_score is high, show
    # "原始评分 95% · 执行等级 B" so it never reads as "B 级 95%" (which
    # would imply a high-confidence executable B).
    if raw_score_text and (str(grade).upper() in {"B", "C", "D"} or grade_suffix):
        headline_grade = f"原始评分 {raw_score_text} · 执行等级 {grade_display}{grade_suffix}"
    else:
        headline_grade = f"等级 {grade_display}，置信度 {confidence_text}"
        if grade_suffix:
            headline_grade = f"{headline_grade}{grade_suffix}"
    lines = [
        f"- **{symbol}**：{decision_name}，{headline_grade}",
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


def _render_plan_state_label(decision: dict[str, Any]) -> str:
    """Phase C (07-07): render the plan_execution_state × plan_origin label.

    Per design §6.5, the hourly report distinguishes 5 candidate states so
    the operator can tell a confirmed LLM plan from a deterministic fallback
    candidate, a risk-rejected plan, an invalidated trigger, or a no-candidate
    observation round. The function reads the structured fields set by
    ``run_agent_sop_decision`` and ``controller.analyze_symbol``; it does NOT
    parse rendered text.

    Branches (design §6.5):
      1. confirmed + llm_confirmed  -> "候选计划已生成（LLM 已确认）"
      2. unconfirmed + deterministic_fallback
         -> "规则候选计划已生成，LLM 未确认，禁止执行"
      3. risk_rejected              -> "候选计划已生成，但风控未通过"
      4. invalidated                -> "候选计划已生成，但前次触发已反转"
      5. no_candidate               -> "无候选计划，本轮仅观察"

    A ``confirmed`` state with ``plan_origin=deterministic_sop`` (LLM disabled
    path where the deterministic SOP produced a plan) renders a distinct label
    so operators know the plan is SOP-confirmed, not LLM-confirmed. Any
    unrecognized combination falls back to the "no_candidate" observation
    wording so the report never claims a candidate exists when the state is
    ambiguous.
    """
    state = decision.get("plan_execution_state")
    origin = decision.get("plan_origin")
    if state == "confirmed" and origin == "llm_confirmed":
        return "候选计划已生成（LLM 已确认）"
    if state == "unconfirmed" and origin == "deterministic_fallback":
        return "规则候选计划已生成，LLM 未确认，禁止执行"
    if state == "risk_rejected":
        return "候选计划已生成，但风控未通过"
    if state == "invalidated":
        return "候选计划已生成，但前次触发已反转"
    if state == "no_candidate":
        return "无候选计划，本轮仅观察"
    if state == "confirmed" and origin == "deterministic_sop":
        return "规则候选计划已生成（LLM 已禁用，SOP 确认）"
    return "无候选计划，本轮仅观察"


def _trade_plan_summary(decision: dict[str, Any]) -> str:
    plan = decision.get("trade_plan")
    risk = decision.get("risk_check") or {}
    if decision.get("has_trade_plan") and isinstance(plan, dict):
        tps = _compact_items([tp.get("price") for tp in plan.get("take_profits", []) if isinstance(tp, dict)], max_items=3)
        return f"{plan.get('side')} {plan.get('entry_type')}，入场 {plan.get('entry_price') or plan.get('trigger_price')}，止损 {plan.get('stop_loss')}，止盈 {tps or '-'}，风控={'通过' if risk.get('ok') else '未通过'}"
    # Phase E (07-05): plan lifecycle separation. Surface the candidate
    # plan and the structured blocker (LLM failure / risk rejection /
    # continuity invalidated) rather than collapsing to "缺交易计划". This
    # is required by Phase A Fact 4 — the report must mention
    # "候选计划已生成" / "LLM 失败" so the operator can see the
    # deterministic path produced a candidate that was then blocked.
    # Phase C (07-07): the 5-branch candidate-state wording now lives in
    # ``_render_plan_state_label`` (design §6.5). This function keeps the
    # detailed plan info (side/entry/stop) and the blocker summary; the
    # "候选计划已生成" prefix is replaced with "候选计划详情" so the
    # state label is not duplicated.
    candidate = decision.get("candidate_trade_plan")
    plan_status = str(decision.get("plan_status") or "")
    blockers = decision.get("plan_blockers") or []
    llm_status = str(decision.get("llm_status") or "").lower()
    if isinstance(candidate, dict):
        # Build a human-readable blocker summary.
        blocker_texts: list[str] = []
        for b in blockers:
            if isinstance(b, dict):
                code = str(b.get("code") or "")
                stage = str(b.get("stage") or "")
                detail = str(b.get("detail") or "")
                if code == "llm_parse_failed":
                    blocker_texts.append("LLM 解析失败")
                elif code == "llm_disabled":
                    blocker_texts.append("LLM 已禁用")
                elif code == "risk_rejected":
                    blocker_texts.append(f"风控未通过（{detail}）")
                elif code == "continuity_trigger_invalidated":
                    blocker_texts.append("前次触发已被反转")
                elif code:
                    blocker_texts.append(f"{code}（{stage}）")
        # Assemble the summary text.
        side = candidate.get("side") or ""
        entry = candidate.get("entry_price") or candidate.get("trigger_price") or "-"
        stop = candidate.get("stop_loss") or "-"
        if blocker_texts:
            blockers_text = "；".join(blocker_texts)
            return f"候选计划详情（{side} 入场 {entry} 止损 {stop}），阻断原因：{blockers_text}"
        if llm_status in {"failed", "disabled"}:
            return f"候选计划详情（{side} 入场 {entry} 止损 {stop}），阻断原因：LLM 失败"
        if plan_status == "withheld":
            return f"候选计划详情（{side} 入场 {entry} 止损 {stop}），阻断原因：执行门禁未通过"
    if llm_status in {"failed", "disabled"}:
        # P1-10 (07-05 final review): if a candidate was expected
        # (plan_status=withheld/executable) but is missing, surface that
        # as the root cause. If plan_status=no_plan, the deterministic
        # path did not produce a candidate — the LLM had nothing to
        # fail over, so do NOT claim a candidate exists.
        if plan_status == "no_plan":
            return "LLM 失败但本轮无 deterministic candidate（no-edge / 低分路径）。"
        if plan_status in {"withheld", "executable"}:
            return "候选计划被 LLM 失败阻断执行。"
        return "LLM 失败阻断本轮分析。"
    if risk.get("reasons"):
        return "无可执行模拟盘计划；风控原因：" + "；".join(str(x) for x in risk.get("reasons", [])[:2])
    return "暂无完整交易计划。"


# Phase E (07-07): LLM health summary line, latest-complete-batch selection,
# and 24h-window recent-failure filtering per design §9.1 / §9.4 / §9.3.

_LLM_CATEGORY_SHORT_LABELS = {
    "llm_empty_response": "empty_response",
    "llm_json_parse_failed": "json_parse",
    "llm_transport_error": "timeout",
    "llm_config_error": "config",
    "llm_rate_limited": "rate_limited",
    "llm_schema_validation_failed": "schema",
    "llm_semantic_validation_failed": "semantic",
}


def _cat_short(category: str) -> str:
    """Map a full ``llm_error_category`` to the short label used in the
    LLM health summary line (design §9.1)."""
    return _LLM_CATEGORY_SHORT_LABELS.get(str(category or ""), str(category or "unknown"))


# Phase E (07-09): translate llm_fallback_reason (set when the LLM call
# was never made or was skip-failed) into a readable Chinese category for
# the 九之二 row. Without this, breaker-skipped rows render category as
# "-" because llm_error_category is None (no call was attempted).
_FALLBACK_REASON_TO_CATEGORY_ZH = {
    "circuit_breaker_open": "熔断跳过",
    "wall_clock_budget_exhausted": "重试预算耗尽",
    "llm_disabled": "LLM 未启用",
    "schema_validation_failed": "Schema 校验失败",
    "retry_exhausted": "重试耗尽",
    "llm_skipped": "LLM 跳过",
}


def _fallback_reason_to_category_zh(reason: Any) -> str:
    """Map a fallback reason code to a short Chinese category label.

    Falls back to ``"未知"`` when the reason is missing or unrecognized
    so the row never renders as ``"-"``.
    """
    key = str(reason or "").strip()
    if not key:
        return "未知"
    return _FALLBACK_REASON_TO_CATEGORY_ZH.get(key, key)


def _render_llm_health_line(batch: dict[str, Any]) -> str:
    """Phase E (07-07) per design §9.1: render the LLM health summary line.

    Phase E (07-09) per design §4: the banner is now split into 5 cases
    based on breaker_state + dominant_error_category + fallback_reason so
    the operator can distinguish "Schema/输出格式异常" (alias-repair SOP
    is handling it) from "网关/模型空响应" (real gateway outage) from
    "配置错误" (immediate open) from "重试预算耗尽" (wall-clock budget
    exhausted, breaker still closed).
    """
    summary = batch.get("summary_json") or {}
    if isinstance(summary, str):
        try:
            summary = json.loads(summary) or {}
        except Exception:
            summary = {}
    llm_health = summary.get("llm_health") if isinstance(summary, dict) else None
    if not isinstance(llm_health, dict) or not llm_health:
        return ""
    state = str(llm_health.get("breaker_state") or "closed").lower()
    dominant = str(llm_health.get("dominant_error_category") or "")
    skipped = int(llm_health.get("skipped_by_breaker") or 0)
    fallback_reason = str(summary.get("dominant_llm_fallback_reason") or "")

    # Phase E (07-09): 5-case banner. The schema-failure and repairable
    # categories use a distinct message because the breaker opens but the
    # root cause is LLM contract violation, not gateway outage. The
    # operator's remediation is different (tighten prompt / retrain vs.
    # page gateway on-call).
    _SCHEMA_CATEGORIES = {
        "llm_schema_validation_failed",
        "llm_schema_repairable",
    }
    _GATEWAY_CATEGORIES = {
        "llm_transport_error",
        "llm_empty_response",
        "llm_rate_limited",
    }

    if state == "open":
        if dominant in _SCHEMA_CATEGORIES:
            if skipped > 0:
                return f"LLM：Schema/输出格式异常，已熔断，跳过 {skipped} 个品种；本批使用规则 SOP，禁止自动执行候选计划"
            return "LLM：Schema/输出格式异常，已熔断；本批使用规则 SOP，禁止自动执行候选计划"
        if dominant == "llm_config_error":
            if skipped > 0:
                return f"LLM：配置错误，已熔断，跳过 {skipped} 个品种；本批使用规则 SOP，禁止自动执行候选计划"
            return "LLM：配置错误，已熔断；本批使用规则 SOP，禁止自动执行候选计划"
        if dominant in _GATEWAY_CATEGORIES:
            if skipped > 0:
                return f"LLM：网关/模型空响应，已熔断，跳过 {skipped} 个品种；本批使用规则 SOP，禁止自动执行候选计划"
            return "LLM：网关/模型空响应，已熔断；本批使用规则 SOP，禁止自动执行候选计划"
        # Unknown dominant category - keep the legacy generic message but
        # avoid the misleading "配置/网关异常" wording (it groups two
        # distinct remediation paths). Surface the skip count if symbols
        # were skipped under the open breaker so the operator knows how
        # many symbols fell into the unknown-category bucket (design §4
        # case 5).
        if skipped > 0:
            return f"LLM：异常熔断，跳过 {skipped} 个品种（未知类别 {dominant}）；本批使用规则 SOP，禁止自动执行候选计划"
        return "LLM：异常熔断；本批使用规则 SOP，禁止自动执行候选计划"

    # Breaker closed but symbols were skipped - distinct from a clean run.
    if skipped > 0:
        if fallback_reason == "wall_clock_budget_exhausted":
            return f"LLM：重试预算耗尽，跳过 {skipped} 个品种；本批使用规则 SOP，禁止自动执行候选计划"
        # Skipped without an explicit budget reason - surface the count so
        # the operator can investigate. The breaker is closed so this is
        # NOT a breaker-skip; it's typically a per-symbol budget drop.
        return f"LLM：本批跳过 {skipped} 个品种（breaker 已关闭）；请检查单品种预算或重试配置"

    total = int(llm_health.get("total_attempts") or 0)
    ok = int(llm_health.get("successful") or 0)
    failed = int(llm_health.get("failed") or 0)
    retries = int(llm_health.get("total_retries") or 0)
    by_cat = llm_health.get("by_category") or {}
    parts: list[str] = []
    if isinstance(by_cat, dict):
        for cat, n in by_cat.items():
            try:
                count = int(n)
            except (TypeError, ValueError):
                continue
            if count > 0:
                parts.append(f"{_cat_short(cat)}={count}")
    breakdown = ", ".join(parts) if parts else ""
    line = f"LLM：{total} 个品种，成功 {ok}，失败 {failed}，重试 {retries}"
    if breakdown:
        line += f"；主要原因：{breakdown}"
    return line


def _select_latest_complete_batch(repo: CryptoGuardRepository, *, now_ms: int) -> dict[str, Any] | None:
    """Phase E (07-07) per design §9.4: pick the latest *complete* batch.

    A batch is complete when:
    - ``status='success'`` (not running / failed / partial_failed)
    - ``enabled_count > 0`` (the batch actually ran symbols)
    - ``completed_count == enabled_count`` (every enabled symbol finished)
    - matching GA decision count == enabled_count (decisions were persisted
      for every enabled symbol, guarding against a batch marked success with
      completed_symbols materialized but decisions missing/stale)

    Returns the batch dict (with parsed ``summary_json`` /
    ``completed_symbols_json`` / ``failed_symbols_json`` /
    ``enabled_symbols_json``) or ``None`` when no complete batch exists. The
    caller renders a degraded report and emits the
    ``HOURLY_REPORT_USED_PARTIAL_RUNNING_BATCH`` diagnostic when ``None``.
    """
    rows = repo.list_recent_analysis_batches(limit=5)
    for batch in rows:
        if str(batch.get("status") or "") != "success":
            continue
        summary = batch.get("summary_json") or {}
        if isinstance(summary, str):
            try:
                summary = json.loads(summary) or {}
            except Exception:
                summary = {}
        enabled_symbols = batch.get("enabled_symbols") or []
        if not isinstance(enabled_symbols, list):
            enabled_symbols = []
        enabled_count = len(enabled_symbols)
        if enabled_count <= 0:
            continue
        completed_symbols = batch.get("completed_symbols") or []
        if not isinstance(completed_symbols, list):
            completed_symbols = []
        completed_count = len(completed_symbols)
        if completed_count != enabled_count:
            continue
        # Verify GA decision count matches enabled count.
        batch_id = batch.get("batch_id")
        if not batch_id:
            continue
        decisions = repo.list_ga_decisions_for_batch(batch_id)
        if len(decisions) != enabled_count:
            continue
        return batch
    return None


# P1/P2 fix (07-09): known legacy schema-fail signatures that the 07-09
# alias-repair SOP is already handling (``entry_trigger_confirmation.type``
# alias normalization -> ``closed_candle_confirmation``). Historical
# agent_jobs rows within the 7-day ``recent_failed_jobs`` window would
# otherwise keep surfacing in every hourly report and drown out actionable
# current failures. These signatures are filtered out of the current
# risk-events list and counted in a legacy-audit line instead.
#
# P2 (07-09 R4): only the FULL ``no_edge fallback schema 校验失败`` prefix
# is matched. The bare ``analysis_time_utc' is a required property`` string
# is too broad — it would also match any FUTURE schema-fail that happens
# to surface the same required-property message via a different code path
# (e.g. a new SOP that doesn't include the ``no_edge fallback`` prefix).
# Future failures must NOT be archived — they are actionable.
_LEGACY_SCHEMA_FAIL_SIGNATURES = (
    "no_edge fallback schema 校验失败: 'analysis_time_utc'",
)


def _is_legacy_schema_fail_job(job: dict[str, Any]) -> bool:
    """Return True if the job's error_message matches a known legacy
    schema-fail signature that the 07-09 alias-repair SOP has already
    remediated. Used by ``_split_current_and_legacy_failed_jobs``.
    """
    msg = str(job.get("error_message") or "")
    return any(sig in msg for sig in _LEGACY_SCHEMA_FAIL_SIGNATURES)


def _split_current_and_legacy_failed_jobs(
    failed_jobs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """P2 (07-09 R3): split failed_jobs into ``(current_jobs,
    legacy_schema_fail_count)``.

    The system-status line ("最近失败 N 个") and the 九、风险事件 section
    must share the same filtering logic so the count shown at the top
    matches the number of items listed below. Before this helper, the
    system-status line counted all ``failed_jobs`` (including archived
    legacy schema-fail) while the risk-events section filtered them out,
    producing a mismatch like "最近失败 2 个" above a 1-item list.

    Returns:
        ``(current_jobs, legacy_schema_fail_count)`` where ``current_jobs``
        preserves the input order and ``legacy_schema_fail_count`` is the
        number of jobs whose error_message matches a known legacy
        schema-fail signature.
    """
    if not failed_jobs:
        return [], 0
    current = [j for j in failed_jobs if not _is_legacy_schema_fail_job(j)]
    legacy_count = len(failed_jobs) - len(current)
    return current, legacy_count


def _render_recent_failures(
    decisions: list[dict[str, Any]],
    *,
    now_ms: int,
    window_ms: int = 24 * 3600 * 1000,
) -> list[dict[str, Any]]:
    """Phase E (07-07) per design §9.3: filter the recent-failure list to a
    24h window and the current batch only.

    Failures older than 24h must NOT appear in the hourly report's recent
    failure section (AC17). They remain in the audit trail (DB rows) but
    are hidden from the hourly report so stale failures don't clutter the
    operator's view. The renderer calls this with the current batch's
    decisions plus any recent decisions within the window.

    The returned list preserves order (newest first) and only contains
    decisions with ``llm_status='failed'`` whose ``analysis_time`` falls
    within ``[now_ms - window_ms, now_ms]``.
    """
    cutoff = int(now_ms) - int(window_ms)
    recent: list[dict[str, Any]] = []
    for d in decisions or []:
        if str(d.get("llm_status") or "").lower() != "failed":
            continue
        try:
            at_ms = int(d.get("analysis_time") or 0)
        except (TypeError, ValueError):
            at_ms = 0
        if at_ms <= 0:
            continue
        if at_ms < cutoff:
            continue
        recent.append(d)
    return recent


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
    llm_status = str(decision.get("llm_status") or "")

    # R6/AC16: LLM failed/disabled — show degraded summary text.
    if llm_status in {"failed", "disabled"} and summary:
        return str(summary)

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


def _format_cd_reasons(reason_items: list[Any], max_items: int = 3) -> str:
    """Phase D (07-03): Format C/D reason text with a '前 N 项' truncation label.

    When the number of unique non-empty reasons exceeds ``max_items``, the
    output is prefixed with ``重点原因（前 {shown} 项，另有 {remaining} 项）：``
    so the user knows not all reasons are shown. When ``len <= max_items``,
    no label is added.

    R1-10 (07-03 final review): dedupe BEFORE counting so duplicate reasons
    are not double-counted in the "另有 M 项" label. Previously ``n`` was
    computed from the raw non-empty list, which inflated ``remaining``
    when duplicates existed. Now ``unique_count`` reflects only distinct
    reasons, and the label shows the actual number of unique items beyond
    what is displayed.

    Always calls ``_compact_items`` so both report paths share the same
    helper (design §6 requirement 4).
    """
    # Filter non-empty values, then dedupe preserving order, then count.
    raw_values = [str(x) for x in (reason_items or []) if x not in (None, "")]
    deduped = _dedupe(raw_values)
    unique_count = len(deduped)
    # _compact_items applies its own dedupe + truncation; pass the deduped
    # list so it does not re-process, but the shared-helper contract is
    # preserved (both report paths invoke _compact_items).
    rendered = _compact_items(deduped, max_items=max_items)
    if not rendered:
        return ""
    shown = min(max_items, unique_count)
    if unique_count > max_items:
        remaining = unique_count - shown
        return f"重点原因（前 {shown} 项，另有 {remaining} 项）：{rendered}"
    return rendered


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


from plugins.crypto_guard.notify.time_utils import format_event_time_cst as _format_time_utc8_compat


def _format_time_utc8(value: Any) -> str:
    """Format a timestamp to UTC+8 display string.

    Delegates to the shared formatter in notify/time_utils.py.
    """
    if value in (None, ""):
        return "-"
    result = _format_time_utc8_compat(value)
    if result == "不可用":
        try:
            return str(value)
        except Exception:
            return "-"
    return result


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
