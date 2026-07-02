"""Hourly report market-accuracy diagnostics.

Covers the ten P2 issue categories enumerated in the Hourly Report Market
Accuracy Fix PRD / research 00-summary. Pure-function helpers operate over
the already-pulled ga_decisions rows plus minimal repo lookups, so they can
be invoked from the report renderer and the state-consistency sweep alike.

Each checker returns a list of issue dicts with:
    {
        "type": <known_code>,
        "severity": "error" | "warning" | "info",
        "details": {...},
        "suggested_action": str,
    }
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from plugins.crypto_guard.storage.repository import CryptoGuardRepository

# Known issue codes — kept in sync with PRD ## P2 diagnostics.
HOURLY_REPORT_INCOMPLETE_BATCH = "hourly_report_incomplete_batch"
HOURLY_REPORT_STALE_DECISION = "hourly_report_stale_decision"
EXECUTABLE_WITHOUT_TRADE_PLAN = "executable_opportunity_without_trade_plan"
EXECUTABLE_RISK_REJECTED = "executable_opportunity_risk_rejected"
OPPORTUNITY_BELOW_CONFIDENCE = "opportunity_below_confidence_threshold"
SUMMARY_EXECUTION_CONFLICT = "summary_execution_state_conflict"
EXCESSIVE_GRADE_FLIP = "excessive_grade_flip"
DIRECTION_FLIP_NO_CLOSED_CANDLE = "direction_flip_without_closed_candle_confirmation"
INVALID_LIQUIDITY_SWEEP = "invalid_liquidity_sweep_semantics"
NEGATIVE_DRAWDOWN_DISPLAY = "negative_drawdown_display"


def diagnose_report_accuracy(repo: CryptoGuardRepository, *, batch_id: str | None = None) -> dict[str, Any]:
    """Run all hourly-report-accuracy diagnostics.

    Returns the standard state-consistency shape (ok / issues / summary /
    total_issues) so it can be merged into diagnose_state_consistency output
    or rendered standalone in the hourly report.

    FS-5: Issues are classified into three buckets:
      - ``error``: current R4-runtime violations (post-marker)
      - ``warning``: current R4-runtime warnings (post-marker)
      - ``legacy_info``: pre-marker audit findings preserved for traceability

    ``ok`` is True iff ``error_count == 0``. Warnings and legacy_info remain
    visible and must be explained, but do not fail the diagnostic.
    """
    issues: list[dict[str, Any]] = []
    issues.extend(_check_hourly_report_incomplete_batch(repo, batch_id))
    issues.extend(_check_hourly_report_stale_decision(repo, batch_id=batch_id))
    issues.extend(_check_executable_opportunity_without_trade_plan(repo))
    issues.extend(_check_executable_opportunity_risk_rejected(repo))
    issues.extend(_check_opportunity_below_confidence(repo))
    issues.extend(_check_summary_execution_state_conflict(repo))
    issues.extend(_check_excessive_grade_flip(repo))
    issues.extend(_check_direction_flip_without_closed_candle(repo))
    issues.extend(_check_invalid_liquidity_sweep_semantics(repo))
    issues.extend(_check_negative_drawdown_display(repo))

    # FS-5: re-classify pre-marker issues as legacy_info. The marker is the
    # R4 contract version timestamp written by the migration once the R4
    # postconditions (schema health, batch_symbol_status CHECK, etc.) hold.
    marker_ts = _get_r4_contract_marker_ts(repo)
    if marker_ts is not None:
        for issue in issues:
            decision_id = (issue.get("details") or {}).get("decision_id")
            if decision_id is None:
                continue
            decision_ts = _get_decision_created_ts(repo, decision_id)
            if decision_ts is not None and decision_ts < marker_ts:
                # Pre-marker decision — demote to legacy_info, preserve visibility.
                issue["severity"] = "legacy_info"

    error_count = sum(1 for i in issues if i["severity"] == "error")
    warning_count = sum(1 for i in issues if i["severity"] == "warning")
    legacy_info_count = sum(1 for i in issues if i["severity"] == "legacy_info")

    summary = {
        HOURLY_REPORT_INCOMPLETE_BATCH: _count(issues, HOURLY_REPORT_INCOMPLETE_BATCH),
        HOURLY_REPORT_STALE_DECISION: _count(issues, HOURLY_REPORT_STALE_DECISION),
        EXECUTABLE_WITHOUT_TRADE_PLAN: _count(issues, EXECUTABLE_WITHOUT_TRADE_PLAN),
        EXECUTABLE_RISK_REJECTED: _count(issues, EXECUTABLE_RISK_REJECTED),
        OPPORTUNITY_BELOW_CONFIDENCE: _count(issues, OPPORTUNITY_BELOW_CONFIDENCE),
        SUMMARY_EXECUTION_CONFLICT: _count(issues, SUMMARY_EXECUTION_CONFLICT),
        EXCESSIVE_GRADE_FLIP: _count(issues, EXCESSIVE_GRADE_FLIP),
        DIRECTION_FLIP_NO_CLOSED_CANDLE: _count(issues, DIRECTION_FLIP_NO_CLOSED_CANDLE),
        INVALID_LIQUIDITY_SWEEP: _count(issues, INVALID_LIQUIDITY_SWEEP),
        NEGATIVE_DRAWDOWN_DISPLAY: _count(issues, NEGATIVE_DRAWDOWN_DISPLAY),
        "error_count": error_count,
        "warning_count": warning_count,
        "legacy_info_count": legacy_info_count,
    }
    return {
        "ok": error_count == 0,
        "issues": issues,
        "summary": summary,
        "total_issues": len(issues),
        "error_count": error_count,
        "warning_count": warning_count,
        "legacy_info_count": legacy_info_count,
    }


# FS-5: R4 contract marker key in _migration_state. The marker is written
# only after the R4 migration postconditions succeed (schema health OK,
# batch_symbol_status CHECK constraint present, etc.). Decisions created
# before this marker are legacy audit findings, not current R4 errors.
R4_CONTRACT_MARKER_KEY = "hourly_report_accuracy_r4_contract_v1"


def _get_r4_contract_marker_ts(repo: CryptoGuardRepository) -> str | None:
    """Return the R4 contract marker's applied_at timestamp, or None."""
    try:
        row = repo.conn.execute(
            "SELECT applied_at FROM _migration_state WHERE key=?",
            (R4_CONTRACT_MARKER_KEY,),
        ).fetchone()
        if row and row["applied_at"]:
            return str(row["applied_at"])
    except Exception:
        return None
    return None


def _get_decision_created_ts(repo: CryptoGuardRepository, decision_id: int | None) -> str | None:
    """Return the created_at timestamp for a ga_decisions row, or None."""
    if decision_id is None:
        return None
    try:
        row = repo.conn.execute(
            "SELECT created_at FROM ga_decisions WHERE id=?",
            (int(decision_id),),
        ).fetchone()
        if row and row["created_at"]:
            return str(row["created_at"])
    except Exception:
        return None
    return None


def run_for_report(repo: CryptoGuardRepository, *, batch_id: str | None = None) -> dict[str, Any]:
    """Wrapper for render-time invocation; never raises.

    P1-11e: returns ok=False on error (fail-closed).
    """
    try:
        return diagnose_report_accuracy(repo, batch_id=batch_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "summary": {}, "total_issues": 0, "issues": []}


# ── issue codes omit "rate,"; for schema simplicity keep both forms documented. ──
def _count(issues: list[dict[str, Any]], code: str) -> int:
    return sum(1 for i in issues if i["type"] == code)


def _issue(code: str, severity: str, details: dict[str, Any], action: str) -> dict[str, Any]:
    return {"type": code, "severity": severity, "details": details, "suggested_action": action}


def _check_hourly_report_incomplete_batch(repo: CryptoGuardRepository, batch_id: str | None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    # P0-6: also check batches that are not 'running' — a 'success' batch
    # with pending symbols is still incomplete.
    rows = repo.conn.execute(
        """
        SELECT batch_id, primary_interval, analysis_time, status,
               enabled_symbols_json
        FROM analysis_batches
        ORDER BY started_at DESC
        LIMIT 10
        """
    ).fetchall()
    for row in rows:
        bid = row["batch_id"] if row["batch_id"] else None
        if batch_id and bid != batch_id:
            continue
        enabled = _json_list(row["enabled_symbols_json"])
        # P0-2/6: use batch_symbol_status for accurate counts
        completed_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=? AND status='completed'",
                (bid,),
            ).fetchall()
        ]
        failed_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=? AND status='failed'",
                (bid,),
            ).fetchall()
        ]
        pending_syms = [
            r["symbol"] for r in repo.conn.execute(
                "SELECT symbol FROM batch_symbol_status WHERE batch_id=? AND status='pending'",
                (bid,),
            ).fetchall()
        ]
        missing = sorted(set(enabled) - set(completed_syms) - set(failed_syms))
        if missing or pending_syms:
            issues.append(_issue(
                HOURLY_REPORT_INCOMPLETE_BATCH, "warning",
                {
                    "batch_id": bid, "primary_interval": row["primary_interval"],
                    "missing_symbols": missing, "failed_symbols": failed_syms,
                    "pending_symbols": pending_syms,
                },
                "等待批次完成或超时；标记 incomplete 并列 missing/failed/pending symbols",
            ))
    return issues


def _check_hourly_report_stale_decision(repo: CryptoGuardRepository, *, batch_id: str | None = None) -> list[dict[str, Any]]:
    """Flag ga_decisions whose analysis_time is older than one analysis cycle
    when the report renders them as fresh.

    P1-11a: only scan decisions matching the current report batch, not the
    most recent 120 historical records.
    """
    issues: list[dict[str, Any]] = []
    try:
        from plugins.crypto_guard.utils import latest_closed_close_time_ms, INTERVAL_MS, utc_ms
    except Exception:  # pragma: no cover
        return issues
    now_ms = utc_ms()
    cutoff = latest_closed_close_time_ms("15m", now_ms)
    span = INTERVAL_MS["15m"]
    # P1-11a: filter by batch_id if available; otherwise fall back to time window
    if batch_id:
        rows = repo.conn.execute(
            "SELECT id, symbol, analysis_time, signal_grade, batch_id "
            "FROM ga_decisions WHERE batch_id=? ORDER BY id DESC LIMIT 120",
            (batch_id,),
        ).fetchall()
    else:
        min_time = cutoff - span
        rows = repo.conn.execute(
            "SELECT id, symbol, analysis_time, signal_grade, batch_id "
            "FROM ga_decisions WHERE analysis_time >= ? ORDER BY id DESC LIMIT 120",
            (min_time,),
        ).fetchall()
    for r in rows:
        at = int(r["analysis_time"] or 0)
        if at == 0:
            continue
        age_ms = cutoff - at
        # stale if analysis_time is older than one 15m close (REPORTING cutoff)
        if age_ms > span:
            issues.append(_issue(
                HOURLY_REPORT_STALE_DECISION, "warning",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "analysis_time": at, "age_minutes": age_ms // 60000,
                    "grade": r["signal_grade"], "batch_id": r["batch_id"],
                },
                "该决策超过一个分析周期；不得进入可执行机会分类",
            ))
            # Limit noise: only flag the most recent 50 stale rows per run
            if len([i for i in issues if i["type"] == HOURLY_REPORT_STALE_DECISION]) >= 50:
                break
    return issues


def _check_executable_opportunity_without_trade_plan(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """P1-11b: Only flag decisions that claim create_paper_order or trade_plan_available
    but lack a trade plan — other decisions (monitor_only, etc.) are expected
    to not have one."""
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, confidence, decision,
               trade_plan_json, risk_check_json
        FROM ga_decisions
        WHERE signal_grade IN ('S','A','B')
          AND decision IN ('create_paper_order', 'trade_plan_available')
        ORDER BY id DESC LIMIT 120
        """
    ).fetchall()
    import json as _json
    for r in rows:
        plan = _safe_json(r["trade_plan_json"])
        if not isinstance(plan, dict) or not plan:
            issues.append(_issue(
                EXECUTABLE_WITHOUT_TRADE_PLAN, "warning",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "grade": r["signal_grade"], "decision": r["decision"],
                },
                "评级较高但 trade_plan 缺失；必须降级为观察候选",
            ))
    return issues


def _check_executable_opportunity_risk_rejected(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """P1-11b: Only flag decisions that claim create_paper_order or trade_plan_available
    but risk_check failed — other decisions (monitor_only, etc.) already know."""
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, risk_check_json
        FROM ga_decisions
        WHERE signal_grade IN ('S','A','B')
          AND decision IN ('create_paper_order', 'trade_plan_available')
        ORDER BY id DESC LIMIT 120
        """
    ).fetchall()
    for r in rows:
        risk = _safe_json(r["risk_check_json"]) or {}
        if isinstance(risk, dict) and risk.get("ok") is False:
            issues.append(_issue(
                EXECUTABLE_RISK_REJECTED, "warning",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "grade": r["signal_grade"], "decision": r["decision"],
                    "reasons": risk.get("reasons") or [],
                },
                "风控未通过对项目禁止说明'可执行/风控全部满足'",
            ))
    return issues


def _check_opportunity_below_confidence(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """FS-5 #4: Only warn when structured state claims executable eligibility.

    A non-executable ``opportunity_watch`` decision (one whose ``decision`` is
    NOT ``create_paper_order`` / ``trade_plan_available``) is expected to be
    below the execution confidence threshold — that is the very reason it is
    classified as watch-only rather than executable. Warning on those rows
    produces noise that obscures real executable-threshold failures.

    Only warn when:
      - ``signal_grade IN ('S','A','B')`` AND
      - ``decision IN ('create_paper_order', 'trade_plan_available')`` AND
      - ``confidence < MIN_CONFIDENCE_FOR_PAPER_ORDER``
    """
    from plugins.crypto_guard.strategy.grade_config import MIN_CONFIDENCE_FOR_PAPER_ORDER
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, confidence, decision
        FROM ga_decisions
        WHERE signal_grade IN ('S','A','B')
          AND decision IN ('create_paper_order', 'trade_plan_available')
        ORDER BY id DESC LIMIT 120
        """
    ).fetchall()
    for r in rows:
        conf = float(r["confidence"] or 0)
        if conf < MIN_CONFIDENCE_FOR_PAPER_ORDER:
            issues.append(_issue(
                OPPORTUNITY_BELOW_CONFIDENCE, "warning",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "grade": r["signal_grade"], "confidence": conf,
                    "threshold": MIN_CONFIDENCE_FOR_PAPER_ORDER,
                    "decision": r["decision"],
                },
                f"置信度 {conf:.2f} 低于 min_confidence {MIN_CONFIDENCE_FOR_PAPER_ORDER:.2f}；不进入可执行",
            ))
    return issues



def _check_summary_execution_state_conflict(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    from plugins.crypto_guard.notify.report_consistency import FORBIDDEN_EXECUTABLE_PHRASES, is_valid_trade_plan
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, decision, final_summary,
               rendered_summary, risk_check_json, trade_plan_json
        FROM ga_decisions
        ORDER BY id DESC LIMIT 200
        """
    ).fetchall()
    for r in rows:
        risk = _safe_json(r["risk_check_json"]) or {}
        plan = _safe_json(r["trade_plan_json"])
        # P1-7 (Round 3): use is_valid_trade_plan instead of simple truthiness
        plan_ok = is_valid_trade_plan(plan)
        risk_ok = bool(isinstance(risk, dict) and risk.get("ok"))
        if risk_ok and plan_ok:
            continue
        text = (r["final_summary"] or "")
        hit = [p for p in FORBIDDEN_EXECUTABLE_PHRASES if p in text]
        rendered = r["rendered_summary"] or ""
        rendered_hit = [p for p in FORBIDDEN_EXECUTABLE_PHRASES if p in rendered]
        if hit or rendered_hit:
            issues.append(_issue(
                SUMMARY_EXECUTION_CONFLICT, "error",
                {
                    "decision_id": int(r["id"]), "symbol": r["symbol"],
                    "grade": r["signal_grade"], "decision": r["decision"],
                    "risk_ok": risk_ok, "has_trade_plan": plan_ok,
                    "forbidden_phrases_in_final_summary": hit,
                    "forbidden_phrases_in_rendered_summary": rendered_hit,
                },
                "summary 与结构化字段冲突；deterministic validator 必须覆盖文案",
            ))
    return issues


def _check_excessive_grade_flip(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Detect S→D→S style jumps within the last 4 hours per symbol."""
    issues: list[dict[str, Any]] = []
    cutoff_ms = int((datetime.now(timezone.utc).timestamp() - 4 * 3600) * 1000)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, analysis_time, signal_grade, previous_grade
        FROM ga_decisions
        WHERE analysis_time >= ?
        ORDER BY symbol, analysis_time ASC
        """,
        (cutoff_ms,),
    ).fetchall()
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append({
            "id": int(r["id"]), "grade": r["signal_grade"],
            "previous_grade": r["previous_grade"], "ts": int(r["analysis_time"]),
        })
    for symbol, seq in by_symbol.items():
        grades = [g["grade"] for g in seq]
        # Detect wild swing: at least one grade S/A followed by D/C within the window.
        saw_top = any(gr in {"S", "A"} for gr in grades)
        saw_bottom = any(gr in {"D", "C"} for gr in grades)
        if saw_top and saw_bottom and len(grades) >= 2:
            issues.append(_issue(
                EXCESSIVE_GRADE_FLIP, "warning",
                {"symbol": symbol, "grade_sequence": grades,
                 "window_hours": 4, "decision_ids": [g["id"] for g in seq]},
                "短时间内高/低评级跳变；按 grade hysteresis 做迟滞并记录 previous_grade",
            ))
    return issues


def _check_direction_flip_without_closed_candle(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag symbol-level direction flips that lack a closed candle
    breakthrough evidence.

    FR-5: Uses structured evidence from the snapshot's module results.
    A valid confirmation requires an event dict with:
      - matching symbol and snapshot
      - event_type in the canonical structural-break set
      - supported non-empty timeframe
      - strict closed status
      - parseable event/close time (seconds, milliseconds, or ISO UTC)
      - event_time after previous decision and not after current decision
      - direction matching the new side
    Text evidence is NEVER accepted.
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int((datetime.now(timezone.utc).timestamp() - 4 * 3600) * 1000)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, analysis_time, market_bias, counter_evidence_json,
               trade_plan_json, evidence_json, snapshot_id
        FROM ga_decisions
        WHERE analysis_time >= ?
        ORDER BY symbol, analysis_time ASC
        """,
        (cutoff_ms,),
    ).fetchall()
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append({
            "id": int(r["id"]), "bias": r["market_bias"],
            "ts": int(r["analysis_time"]),
            "counter": _safe_json(r["counter_evidence_json"]) or [],
            "side": (_safe_json(r["trade_plan_json"]) or {}).get("side"),
            "evidence": _safe_json(r["evidence_json"]) or [],
            "snapshot_id": r["snapshot_id"],
            "symbol": r["symbol"],
        })
    for symbol, seq in by_symbol.items():
        for prev, cur in zip(seq, seq[1:]):
            prev_side = prev.get("side") or _bias_side(prev.get("bias"))
            cur_side = cur.get("side") or _bias_side(cur.get("bias"))
            if prev_side and cur_side and prev_side != cur_side:
                # FR-5: structured evidence from snapshot's module results
                structural_confirmation = _has_structured_confirmation(
                    repo, cur, cur_side, prev_ts=prev.get("ts", 0),
                )
                if not structural_confirmation:
                    issues.append(_issue(
                        DIRECTION_FLIP_NO_CLOSED_CANDLE, "warning",
                        {
                            "symbol": symbol, "prev_side": prev_side, "cur_side": cur_side,
                            "prev_decision_id": prev["id"], "cur_decision_id": cur["id"],
                        },
                        "方向翻转必须以已收盘 K 线突破作为证据；缺突破证据时记录诊断",
                    ))
    return issues


# Canonical structural-break event types for direction flip confirmation.
# All upper-case — comparison uses .upper() on the event_type value.
_STRUCTURAL_BREAK_TYPES = frozenset({
    "BOS", "BREAK_OF_STRUCTURE",
    "CHOCH", "CHANGE_OF_CHARACTER",
    "BREAKOUT", "BREAKDOWN",
})

# Supported timeframes for structured evidence
_SUPPORTED_TIMEFRAMES = frozenset({
    "1m", "5m", "15m", "1h", "4h", "1d",
})


def _has_structured_confirmation(
    repo: CryptoGuardRepository, cur: dict[str, Any], new_side: str, *, prev_ts: int = 0,
) -> bool:
    """FR-5: Check for structured event confirmation of a direction flip.

    Text evidence is NEVER accepted. A valid confirmation must come from
    structured events associated with the decision's snapshot/module result
    in the database. Inline evidence from ga_decisions.evidence_json /
    counter_evidence_json is NOT trusted — it could be fabricated by the LLM.

    Every accepted event must have:
    - matching symbol and snapshot (looked up from module_analysis_results)
    - event_type in the canonical structural-break set
    - supported non-empty timeframe (from module_analysis_results.timeframe)
    - strict closed status (snapshot events are by definition closed)
    - required parseable event/close time (from module_analysis_results.analysis_time)
    - event_time after previous decision and not after current decision
    - direction matching the new side
    """
    snapshot_id = cur.get("snapshot_id")
    symbol = cur.get("symbol")
    analysis_time = cur.get("ts", 0)

    # FR-5 (P1 fix): ONLY events looked up from the snapshot's module results
    # are accepted. Inline evidence from cur["evidence"]/cur["counter"] is
    # explicitly rejected — it lives in ga_decisions JSON columns that the
    # LLM could populate with arbitrary dicts.
    events = _lookup_snapshot_events(repo, snapshot_id, symbol)

    for event in events:
        # Must have a structural-break event_type (mapped from production shape)
        event_type = event.get("event_type", "")
        if not event_type:
            continue
        if str(event_type).upper() not in _STRUCTURAL_BREAK_TYPES:
            continue

        # FR-5: must have supported non-empty timeframe
        timeframe = str(event.get("timeframe", "")).lower().strip()
        if not timeframe or timeframe not in _SUPPORTED_TIMEFRAMES:
            continue

        # FR-5: strict closed status — snapshot events are by definition on
        # closed candles; explicit closed=False rejects, otherwise accepted.
        closed = event.get("closed", True)
        if closed is not True and str(closed).lower().strip() not in {"true", "1", "yes"}:
            continue

        # FR-5: required parseable event/close time
        event_time = _parse_event_time(event)
        if event_time is None:
            continue

        # FR-5: event_time must be after previous decision and not after current
        event_time_ms = int(event_time)
        if prev_ts > 0 and event_time_ms <= prev_ts:
            continue
        if analysis_time > 0 and event_time_ms > analysis_time:
            continue

        # FR-5: direction must match new side
        direction = str(event.get("direction", "")).lower().strip()
        side_lower = new_side.lower()
        direction_match = (
            (side_lower == "long" and direction in {"bullish", "long", "up", "多"})
            or (side_lower == "short" and direction in {"bearish", "short", "down", "空"})
        )
        if direction_match:
            return True

    return False


# Production modules that emit structural-break events. The legacy
# `smc_orderflow` module has 0 rows in production — real modules are
# `price_action` (with `structure_events` list) and `smc`.
_SNAPSHOT_EVENT_MODULES: tuple[str, ...] = ("price_action", "smc", "smc_orderflow")

# Map production event names to canonical structural-break types.
# Production `price_action.structure_events[].event` uses names like
# `bullish_bos`, `bearish_choch`. The `type` field is `BOS`/`CHoCH`/`none`.
_EVENT_NAME_TO_TYPE: dict[str, str] = {
    "bullish_bos": "BOS",
    "bearish_bos": "BOS",
    "bullish_choch": "CHOCH",
    "bearish_choch": "CHOCH",
    "bullish_breakout": "BREAKOUT",
    "bearish_breakout": "BREAKOUT",
    "bullish_breakdown": "BREAKDOWN",
    "bearish_breakdown": "BREAKDOWN",
}


def _normalize_snapshot_event(raw: dict[str, Any], *, timeframe: str, analysis_time: int) -> dict[str, Any] | None:
    """FS-1 / FR-5: Map a production-shape event dict to the canonical shape.

    Production `price_action.structure_events` rows emitted by
    ``price_action_engine._structure_events()`` look like:
        {"event": "bullish_bos", "type": "BOS", "event_type": "BOS",
         "direction": "bullish", "timeframe": "1h",
         "reference_high": 6.267, "reference_low": 5.957, "close": 6.308,
         "close_time": <source candle close_time>, "closed": True}

    Canonical shape required by ``_has_structured_confirmation``:
        {"event_type": "BOS", "timeframe": "1h", "closed": True,
         "time": <close_time_ms>, "direction": "bullish"}

    FS-1: The event time MUST come from the source event's ``close_time``
    (the actual candle close time written by ``price_action_engine``). The
    module row's ``analysis_time`` MUST NOT be used as an event-time
    fallback — module analysis time is when the analyzer ran, not when the
    candle closed. Repeated analysis of the same higher-timeframe candle
    therefore retains the same event time.

    FS-1: The ``closed`` flag MUST come from the source event. It MUST NOT
    be invented as ``True`` when the source event does not prove it.
    """
    event_name = str(raw.get("event", "")).lower().strip()
    # Direct event_type field wins if present
    direct_type = raw.get("event_type")
    if direct_type:
        event_type = str(direct_type).upper()
    elif event_name in _EVENT_NAME_TO_TYPE:
        event_type = _EVENT_NAME_TO_TYPE[event_name]
    else:
        # Fall back to the `type` field (BOS / CHoCH / none)
        type_field = str(raw.get("type", "")).upper().strip()
        if not type_field or type_field == "NONE":
            return None
        event_type = type_field

    # Derive direction from event_name prefix or explicit direction field
    direction = ""
    if event_name.startswith("bullish") or event_name.startswith("bos_bull") or event_name.startswith("choch_bull"):
        direction = "bullish"
    elif event_name.startswith("bearish") or event_name.startswith("bos_bear") or event_name.startswith("choch_bear"):
        direction = "bearish"
    elif raw.get("direction"):
        direction = str(raw.get("direction")).lower().strip()

    # FS-1: event time MUST come from the source event. NEVER fall back to
    # module analysis_time — that is when the analyzer ran, not when the
    # candle closed.
    event_time = raw.get("close_time")
    if event_time is None:
        event_time = raw.get("time")
    if event_time is None:
        event_time = raw.get("event_time")
    if event_time is None:
        # FS-1: no real event time — reject instead of substituting analysis_time
        return None

    # FS-1: closed flag MUST come from the source event. NEVER invent True.
    closed_raw = raw.get("closed")
    if closed_raw is None:
        # Source event did not prove closed status — reject.
        return None
    if closed_raw is True or str(closed_raw).lower().strip() in {"true", "1", "yes"}:
        closed = True
    else:
        # Explicit closed=False — reject.
        return None

    return {
        "event_type": event_type,
        "timeframe": timeframe,
        "closed": closed,
        "time": event_time,
        "direction": direction,
    }


def _lookup_snapshot_events(
    repo: CryptoGuardRepository, snapshot_id: int | None, symbol: str,
) -> list[dict[str, Any]]:
    """FR-5: Look up structured events from the snapshot's module results.

    Production modules with structural-break events:
    - `price_action`: result_json.structure_events (list of dicts)
    - `smc`: result_json.events / structure_breaks (list of dicts)
    - `smc_orderflow`: legacy module (0 rows in production as of 2026-06-29)

    Each returned event is normalized to the canonical shape with event_type,
    timeframe, closed, time, direction fields populated from the module row.
    """
    if snapshot_id is None:
        return []
    try:
        rows = repo.conn.execute(
            """
            SELECT module, timeframe, analysis_time, result_json
            FROM module_analysis_results
            WHERE snapshot_id=? AND symbol=? AND module IN (?, ?, ?)
            ORDER BY timeframe
            """,
            (int(snapshot_id), symbol, *_SNAPSHOT_EVENT_MODULES),
        ).fetchall()
    except Exception:
        return []

    events: list[dict[str, Any]] = []
    for row in rows:
        result = _safe_json(row["result_json"]) or {}
        if not isinstance(result, dict):
            continue
        timeframe = str(row["timeframe"] or "").lower().strip()
        analysis_time = int(row["analysis_time"] or 0)
        # price_action: structure_events list; smc: events/structure_breaks
        items: list[Any] = []
        for key in ("structure_events", "events", "structure_breaks", "breakouts", "breakdowns"):
            v = result.get(key)
            if isinstance(v, list):
                items.extend(v)
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_snapshot_event(item, timeframe=timeframe, analysis_time=analysis_time)
            if normalized is not None:
                events.append(normalized)
    return events


def _parse_event_time(event: dict[str, Any]) -> int | None:
    """FR-5: Parse event time from seconds, milliseconds, or ISO UTC.

    Returns milliseconds since epoch, or None if unparseable.
    Threshold: values < 1e12 are seconds, >= 1e12 are milliseconds.
    """
    # Try multiple time field names
    for field in ("event_time", "close_time", "time", "timestamp", "candle_close_time"):
        raw = event.get(field)
        if raw is None:
            continue

        # Integer/float: distinguish seconds from milliseconds
        if isinstance(raw, (int, float)):
            val = int(raw)
            if val <= 0:
                continue
            if val < 1_000_000_000_000:
                # Seconds — convert to milliseconds
                return val * 1000
            else:
                # Already milliseconds
                return val

        # String: try ISO UTC format
        if isinstance(raw, str):
            raw_str = raw.strip()
            if not raw_str:
                continue
            try:
                dt = datetime.fromisoformat(raw_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except (ValueError, TypeError):
                # Try as numeric string
                try:
                    val = int(float(raw_str))
                    if val <= 0:
                        continue
                    if val < 1_000_000_000_000:
                        return val * 1000
                    else:
                        return val
                except (ValueError, TypeError):
                    continue

    return None


def _check_invalid_liquidity_sweep_semantics(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Only used in unit-test contexts: validate smc_engine direction semantics
    on a known candle sequence. The diagnostic runner asserts the mapping is
    exercised (sweep_low → sell-side → bullish) by querying recent ga_decisions'
    smc evidence for inconsistent wording."""
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, final_summary, raw_decision_json
        FROM ga_decisions
        ORDER BY id DESC LIMIT 50
        """
    ).fetchall()
    for r in rows:
        text = (r["final_summary"] or "")
        # P1-11d: sell_side sweep sweeps low (price goes down) which is
        # the NORMAL direction for a sell-side sweep. "向下" (downward)
        # with sell_side is correct behavior — only flag explicit direction
        # contradictions like "看空" (bearish conviction) with sell_side.
        # Similarly, buy_side sweeps high ("向上" = upward) is normal —
        # only flag "看多" (bullish conviction) with buy_side.
        if "sell_side" in text and ("看空" in text or "bearish" in text.lower()):
            issues.append(_issue(
                INVALID_LIQUIDITY_SWEEP, "warning",
                {"decision_id": int(r["id"]), "symbol": r["symbol"], "snippet": text[:200]},
                "sell_side liquidity sweep 应映射 bullish reclaim；勿反向解读",
            ))
        elif "buy_side" in text and ("看多" in text or "bullish" in text.lower()):
            issues.append(_issue(
                INVALID_LIQUIDITY_SWEEP, "warning",
                {"decision_id": int(r["id"]), "symbol": r["symbol"], "snippet": text[:200]},
                "buy_side liquidity sweep 应映射 bearish reclaim；勿反向解读",
            ))
    return issues


def _check_negative_drawdown_display(repo: CryptoGuardRepository) -> list[dict[str, Any]]:
    """Flag paper_equity_snapshots whose stored drawdown_percent is positive
    when account equity is below initial — internal sign convention must be
    <= 0 (loss negative)."""
    issues: list[dict[str, Any]] = []
    # Get initial_balance from paper_accounts for relative comparison
    try:
        init_row = repo.conn.execute(
            "SELECT initial_balance FROM paper_accounts ORDER BY id LIMIT 1"
        ).fetchone()
        initial_balance = float(init_row["initial_balance"]) if init_row and init_row["initial_balance"] else 0.0
    except Exception:
        initial_balance = 0.0
    rows = repo.conn.execute(
        """
        SELECT id, account_equity, unrealized_pnl, realized_pnl, snapshot_json
        FROM paper_equity_snapshots
        ORDER BY id DESC LIMIT 50
        """
    ).fetchall()
    import json as _json
    for r in rows:
        snap = _safe_json(r["snapshot_json"]) or {}
        dd = snap.get("drawdown_percent")
        if dd is None:
            continue
        try:
            dd = float(dd)
        except (TypeError, ValueError):
            continue
        # positive drawdown_display while equity shows loss below initial is the bug pattern
        equity = float(r["account_equity"] or 0)
        is_loss = equity < initial_balance if initial_balance > 0 else equity < 10000
        if dd > 0 and is_loss:
            issues.append(_issue(
                NEGATIVE_DRAWDOWN_DISPLAY, "warning",
                {"snapshot_id": int(r["id"]), "account_equity": equity,
                 "initial_balance": initial_balance,
                 "drawdown_percent_internal": dd},
                "对外显示回撤需统一为非负幅度；内部保留 sign 语义",
            ))
    return issues


# ── helpers ─────────────────────────────────────────────────────────────────
def _json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        import json
        data = json.loads(raw)
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def _safe_json(raw: Any) -> Any:
    if raw is None:
        return None
    import json
    try:
        return json.loads(raw)
    except Exception:
        return None


def _bias_side(bias: str | None) -> str | None:
    b = (bias or "").lower()
    if b in ("bullish", "long", "多"):
        return "LONG"
    if b in ("bearish", "short", "空"):
        return "SHORT"
    return None