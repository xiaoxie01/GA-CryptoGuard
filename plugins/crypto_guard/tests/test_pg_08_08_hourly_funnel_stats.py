# -*- coding: utf-8 -*-
"""08-08 P2 (PRD): hourly report execution-funnel stats + anti-bloat reminders.

The hourly report must surface the 8 execution-funnel counters so the operator
can see where the funnel drops (the production 55/55 watch-recheck drop):

  candidate / llm_confirmed / confirmation_available / risk_passed /
  final_executable / watches_triggered / recheck_rejected_by_reason /
  orders_created

The first five are decision-side (derived from the batch's ``ga_decisions``
rows, gated to the post-marker ``execution_funnel_scope == "current"`` window);
the last three are watch-side (real PostgreSQL aggregates over the last hour,
also gated to ``created_at >= execution_funnel_cutoff_utc``).

The current diagnostic reminders (section 十) must show each issue's ``type`` +
a short reason (not just the count), DEDUPE identical ``(type, reason)`` pairs,
cap the detail lines (10), and emit "另有 N 项" for the remainder — so a burst
of identical issues never bloats the report.

RED-first: pre-fix there is NO execution-funnel section in the report, no
``_aggregate_execution_funnel`` / ``_pg_execution_funnel_watch_stats`` helpers,
and the reminders render only "当前异常 N 项 · 提醒 N 项" with no per-issue
type/reason lines. All four test classes below FAIL on that code.

No production DB mutation, no marker write (``make_repo`` runs
``initialize_database`` into the scratch schema only), no service restart, no
commit/push/release.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.notify import hourly_report
from plugins.crypto_guard.tests.pg_fixtures import make_repo


# ── helpers ─────────────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _render(
    *,
    ga_decisions: list[dict] | None = None,
    report_accuracy_diagnostics: dict | None = None,
    execution_funnel_stats: dict | None = None,
) -> str:
    """Render the GA hourly summary with minimal empty inputs plus the
    optional P2 inputs under test."""
    return hourly_report.render_ga_hourly_summary(
        generated_at_utc="2026-08-08T12:00:00Z",
        active_symbols=["BTCUSDT"],
        ga_decisions=ga_decisions or [],
        open_orders=[],
        active_watches=[],
        failed_jobs=[],
        queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
        report_accuracy_diagnostics=report_accuracy_diagnostics,
        execution_funnel_stats=execution_funnel_stats,
    )


def _current_row(**aspects: bool) -> dict:
    """A ``_decision_row``-shaped row in the post-marker ``current`` scope with
    the given aspect booleans (default all True)."""
    base = {
        "symbol": "BTCUSDT",
        "signal_grade": "S",
        "confidence": 0.9,
        "analysis_time": 0,
        "created_at": None,
        "execution_funnel_scope": "current",
        "candidate": True,
        "llm_plan_confirmed": True,
        "confirmation_available": True,
        "risk_passed": True,
        "final_executable": True,
    }
    base.update(aspects)
    return base


def _legacy_row() -> dict:
    """A pre-marker row whose aspects are all ``None`` (scope ``legacy``) — it
    must NEVER contribute to the funnel counters."""
    return {
        "symbol": "ETHUSDT",
        "signal_grade": "S",
        "confidence": 0.9,
        "analysis_time": 0,
        "created_at": None,
        "execution_funnel_scope": "legacy",
        "candidate": None,
        "llm_plan_confirmed": None,
        "confirmation_available": None,
        "risk_passed": None,
        "final_executable": None,
    }


# ── P2: decision-side aggregate ──────────────────────────────────────────────


class TestAggregateExecutionFunnel:
    """``_aggregate_execution_funnel(rows)`` sums the five decision-side funnel
    counters over ONLY the ``execution_funnel_scope == "current"`` rows."""

    def test_aggregates_current_rows_only(self) -> None:
        """RED: ``_aggregate_execution_funnel`` does not exist pre-fix.
        GREEN: it sums candidate/llm_confirmed/confirmation_available/
        risk_passed/final_executable over current rows and ignores legacy rows."""
        rows = [
            _current_row(),  # all five True
            _current_row(candidate=True, llm_plan_confirmed=True,
                         confirmation_available=True, risk_passed=True,
                         final_executable=False),  # dropped at final gate
            _current_row(candidate=True, llm_plan_confirmed=False,
                         confirmation_available=False, risk_passed=False,
                         final_executable=False),  # dropped at llm confirm
            _legacy_row(),  # must be excluded
        ]
        stats = hourly_report._aggregate_execution_funnel(rows)
        assert stats == {
            "candidate": 3,
            "llm_confirmed": 2,
            "confirmation_available": 2,
            "risk_passed": 2,
            "final_executable": 1,
        }, f"P2 RED: expected the 5 decision-side counters; got {stats!r}"

    def test_empty_rows_zero_counters(self) -> None:
        """GREEN: no rows → all counters zero (never None)."""
        stats = hourly_report._aggregate_execution_funnel([])
        assert stats == {
            "candidate": 0,
            "llm_confirmed": 0,
            "confirmation_available": 0,
            "risk_passed": 0,
            "final_executable": 0,
        }, f"P2: empty rows must yield zero counters; got {stats!r}"


# ── P2: watch-side PostgreSQL aggregate ───────────────────────────────────────


class TestPgExecutionFunnelWatchStats:
    """``_pg_execution_funnel_watch_stats`` aggregates the three watch-side
    counters over the last hour AND ``created_at >= execution_funnel_cutoff_utc``
    (exclude-only: pre-marker / out-of-window history never counts)."""

    def _seed(self, repo, *, cutoff_dt: datetime, now_dt: datetime) -> None:
        """Seed one in-window recheck decision, two in-window rejected jobs
        (distinct reasons), one in-window order, plus one pre-marker decision
        and one pre-marker order that must be EXCLUDED."""
        # In-window recheck decision (post-marker, within last hour).
        in_win = now_dt - timedelta(minutes=10)
        in_win_id = repo.create_ga_decision({
            "symbol": "BTCUSDT",
            "analysis_time": _ms(in_win),
            "analysis_time_utc": _iso(in_win),
            "decision_type": "opportunity_watch_recheck",
            "signal_grade": "S",
            "confidence": 0.9,
            "market_bias": "bullish",
            "trend_stage": "early",
            "decision": "trade_plan_available",
            "skill_result_refs": {},
            "evidence": [],
            "counter_evidence": [],
            "risk_check": {"ok": True},
            "feishu_actions": [],
            "final_summary": "recheck",
            "raw_llm_summary": "recheck",
            "rendered_summary": "recheck",
            "batch_id": None,
            "previous_grade": "D",
            "llm_status": "ok",
        })
        # Pre-marker recheck decision (created_at BEFORE cutoff) → excluded.
        pre = cutoff_dt - timedelta(minutes=5)
        pre_id = repo.create_ga_decision({
            "symbol": "ETHUSDT",
            "analysis_time": _ms(pre),
            "analysis_time_utc": _iso(pre),
            "decision_type": "opportunity_watch_recheck",
            "signal_grade": "S",
            "confidence": 0.9,
            "market_bias": "bullish",
            "trend_stage": "early",
            "decision": "trade_plan_available",
            "skill_result_refs": {},
            "evidence": [],
            "counter_evidence": [],
            "risk_check": {"ok": True},
            "feishu_actions": [],
            "final_summary": "recheck",
            "raw_llm_summary": "recheck",
            "rendered_summary": "recheck",
            "batch_id": None,
            "previous_grade": "D",
            "llm_status": "ok",
        })
        # ``create_ga_decision`` defaults created_at to NOW() (real wall clock);
        # pin both to the fake window so the exclude-only cutoff/window filter is
        # exercised deterministically.
        repo.conn.execute(
            "UPDATE ga_decisions SET created_at=%s::timestamptz WHERE id=%s",
            (in_win.astimezone(timezone.utc), in_win_id),
        )
        repo.conn.execute(
            "UPDATE ga_decisions SET created_at=%s::timestamptz WHERE id=%s",
            (pre.astimezone(timezone.utc), pre_id),
        )
        # Two in-window rejected jobs with distinct reasons.
        import json as _json

        for reason in ("risk_ok=false", "grade=C"):
            repo.conn.execute(
                """
                INSERT INTO agent_jobs(job_type, priority, source, session_id,
                    payload_json, status, result_json, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "opportunity_watch_recheck", 5, "watch", f"sid-{reason}",
                    "{}", "success",
                    _json.dumps({"rejected": True, "reason": reason}),
                    in_win.astimezone(timezone.utc),
                ),
            )
        # One in-window order (source='watch_recheck').
        repo.conn.execute(
            """
            INSERT INTO paper_orders(signal_id, symbol, side, order_type,
                entry_price, stop_loss, source, risk_check_passed, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (None, "BTCUSDT", "LONG", "limit", 100.0, 95.0,
             "watch_recheck", True, in_win.astimezone(timezone.utc)),
        )
        # One pre-marker order (created_at BEFORE cutoff) → excluded.
        repo.conn.execute(
            """
            INSERT INTO paper_orders(signal_id, symbol, side, order_type,
                entry_price, stop_loss, source, risk_check_passed, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (None, "ETHUSDT", "LONG", "limit", 100.0, 95.0,
             "watch_recheck", True, pre.astimezone(timezone.utc)),
        )
        repo.conn.commit()

    def test_watch_stats_aggregate_and_exclude_pre_marker(self) -> None:
        """RED: ``_pg_execution_funnel_watch_stats`` does not exist pre-fix.
        GREEN: it returns watches_triggered / recheck_rejected_by_reason /
        orders_created over the last hour AND post-marker rows only."""
        handle = make_repo()
        try:
            repo = handle.repo
            now_dt = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
            cutoff_dt = now_dt - timedelta(hours=2)
            self._seed(repo, cutoff_dt=cutoff_dt, now_dt=now_dt)

            stats = hourly_report._pg_execution_funnel_watch_stats(
                repo,
                generated_at_utc=_iso(now_dt),
                execution_funnel_cutoff_utc=_iso(cutoff_dt),
            )
            assert stats.get("ok") is True, f"P2 RED: watch stats failed: {stats!r}"
            assert stats["watches_triggered"] == 1, (
                f"P2 RED: expected 1 in-window recheck decision; got "
                f"{stats['watches_triggered']!r}"
            )
            assert stats["recheck_rejected_by_reason"] == {
                "risk_ok=false": 1,
                "grade=C": 1,
            }, f"P2 RED: rejected-by-reason distribution wrong: {stats!r}"
            assert stats["orders_created"] == 1, (
                f"P2 RED: expected 1 in-window watch_recheck order; got "
                f"{stats['orders_created']!r}"
            )
        finally:
            handle.close()


# ── P2: render the 8 funnel stats ─────────────────────────────────────────────


class TestRenderFunnelSection:
    """``render_ga_hourly_summary`` renders the 8 execution-funnel counters when
    ``execution_funnel_stats`` is provided."""

    def test_render_all_eight_stats(self) -> None:
        """RED: pre-fix there is no execution-funnel section and no
        ``execution_funnel_stats`` param. GREEN: all 8 counters render."""
        stats = {
            "candidate": 10,
            "llm_confirmed": 8,
            "confirmation_available": 8,
            "risk_passed": 6,
            "final_executable": 5,
            "watches_triggered": 3,
            "recheck_rejected_by_reason": {"risk_ok=false": 2, "grade=C": 1},
            "orders_created": 1,
        }
        text = _render(execution_funnel_stats=stats)
        assert "执行漏斗" in text, (
            "P2 RED: the report must render an execution-funnel section"
        )
        assert "候选计划 10" in text, "P2: candidate counter must render"
        assert "LLM 确认 8" in text, "P2: llm_confirmed counter must render"
        assert "入场确认可用 8" in text, "P2: confirmation_available must render"
        assert "风控通过 6" in text, "P2: risk_passed counter must render"
        assert "最终可执行 5" in text, "P2: final_executable counter must render"
        assert "观察触发 3" in text, "P2: watches_triggered counter must render"
        assert "risk_ok=false=2" in text, "P2: rejected-by-reason must render"
        assert "grade=C=1" in text, "P2: rejected-by-reason must render"
        assert "生成订单 1" in text, "P2: orders_created counter must render"

    def test_no_funnel_section_when_stats_absent(self) -> None:
        """GREEN: when ``execution_funnel_stats`` is None (e.g. degraded path),
        no funnel section is rendered — no crash, no empty section."""
        text = _render()
        assert "执行漏斗" not in text, (
            "P2: no funnel section when execution_funnel_stats is absent"
        )


# ── P2: reminders show type + short reason, dedupe, cap, "另有 N 项" ──────────


class TestRemindersTypeAndReason:
    """The section-十 reminders must show each issue's ``type`` + a short reason
    (not just the count), DEDUPE identical ``(type, reason)`` pairs, cap the
    detail lines at 10, and emit "另有 N 项" for the remainder."""

    def _diag(self, issues: list[dict]) -> dict:
        errors = sum(1 for i in issues if i.get("severity") == "error")
        warnings = sum(1 for i in issues if i.get("severity") == "warning")
        return {
            "ok": True,
            "error_count": errors,
            "warning_count": warnings,
            "legacy_info_count": 0,
            "total_issues": len(issues),
            "issues": issues,
        }

    def _issue(self, code: str, severity: str, **details) -> dict:
        return {
            "type": code,
            "severity": severity,
            "layer": "current" if severity == "error" else "warning",
            "details": details,
            "suggested_action": "fix",
        }

    def test_reminders_show_type_and_short_reason(self) -> None:
        """RED: pre-fix the reminders render only "当前异常 N 项 · 提醒 N 项"
        with no per-issue type/reason lines. GREEN: each issue renders as a
        ``type | k=v`` line."""
        diag = self._diag([
            self._issue("watch_recheck_risk_shape_mismatch", "error",
                        decision_id=101, symbol="BTCUSDT"),
            self._issue("hourly_report_incomplete_batch", "warning",
                        batch_id="B1", missing_symbols=["ETHUSDT"]),
        ])
        text = _render(report_accuracy_diagnostics=diag)
        assert "当前异常 1 项 · 提醒 1 项" in text, (
            "P2: the count line must still render"
        )
        assert "watch_recheck_risk_shape_mismatch" in text, (
            "P2 RED: the reminder must show the issue type"
        )
        assert "decision_id=101" in text, (
            "P2 RED: the reminder must show a short reason (scalar detail)"
        )
        assert "hourly_report_incomplete_batch" in text, (
            "P2 RED: the warning issue type must render"
        )
        assert "batch_id=B1" in text, (
            "P2 RED: the warning short reason must render"
        )

    def test_reminders_dedupe_identical_pairs(self) -> None:
        """GREEN: two issues with the SAME (type, reason) render as ONE line."""
        diag = self._diag([
            self._issue("watch_recheck_risk_shape_mismatch", "error",
                        decision_id=101, symbol="BTCUSDT"),
            self._issue("watch_recheck_risk_shape_mismatch", "error",
                        decision_id=101, symbol="BTCUSDT"),
        ])
        text = _render(report_accuracy_diagnostics=diag)
        assert text.count("watch_recheck_risk_shape_mismatch") == 1, (
            "P2: identical (type, reason) pairs must be deduped to one line"
        )

    def test_reminders_cap_and_remainder(self) -> None:
        """GREEN: >10 unique (type, reason) pairs render the first 10 detail
        lines and a "另有 N 项" remainder line."""
        issues = [
            self._issue("watch_recheck_risk_shape_mismatch", "error",
                        decision_id=1000 + i, symbol="BTCUSDT")
            for i in range(12)
        ]
        diag = self._diag(issues)
        text = _render(report_accuracy_diagnostics=diag)
        # 12 unique pairs → 10 detail lines + "另有 2 项".
        assert "另有 2 项" in text, (
            "P2: the remainder must be summarized as '另有 2 项'"
        )
        # The first 10 decision_ids render; the last 2 do not.
        assert "decision_id=1000" in text, "P2: first detail line must render"
        assert "decision_id=1009" in text, "P2: 10th detail line must render"
        assert "decision_id=1010" not in text, (
            "P2: the 11th detail line must be capped out"
        )
        assert "decision_id=1011" not in text, (
            "P2: the 12th detail line must be capped out"
        )
