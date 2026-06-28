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
    }
    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "summary": summary,
        "total_issues": len(issues),
    }


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
    from plugins.crypto_guard.strategy.grade_config import MIN_CONFIDENCE_FOR_PAPER_ORDER
    issues: list[dict[str, Any]] = []
    rows = repo.conn.execute(
        """
        SELECT id, symbol, signal_grade, confidence
        FROM ga_decisions
        WHERE signal_grade IN ('S','A','B')
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

    P1-11c: also checks counter_evidence and evidence (BOS/CHoCH) for
    confirmation, not just counter_evidence.

    P1-6 (Round 3): market_bias flip is the TRIGGER (the flip happened)
    but NOT the CONFIRMATION (the flip was structurally justified).
    Only closed-candle tokens and BOS/CHoCH events count as confirmation.
    """
    issues: list[dict[str, Any]] = []
    cutoff_ms = int((datetime.now(timezone.utc).timestamp() - 4 * 3600) * 1000)
    rows = repo.conn.execute(
        """
        SELECT id, symbol, analysis_time, market_bias, counter_evidence_json,
               trade_plan_json, evidence_json
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
        })
    for symbol, seq in by_symbol.items():
        for prev, cur in zip(seq, seq[1:]):
            prev_side = prev.get("side") or _bias_side(prev.get("bias"))
            cur_side = cur.get("side") or _bias_side(cur.get("bias"))
            if prev_side and cur_side and prev_side != cur_side:
                # P1-11c: check multiple confirmation sources
                # P1-6 (Round 3): market_bias flip is NOT confirmation.
                # Only closed-candle tokens and BOS/CHoCH events count.
                merged_counter = " ".join(str(x) for x in (cur["counter"] or []))
                merged_evidence = " ".join(str(x) for x in (cur["evidence"] or []))
                combined = merged_counter + " " + merged_evidence
                structural_confirmation = any(
                    token in combined
                    for token in ("收盘突破", "收盘跌破", "收盘站上", "收盘站回",
                                  "closed candle", "closed_candle",
                                  "BOS", "CHoCH", "Break of Structure",
                                  "Change of Character")
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