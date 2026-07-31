# -*- coding: utf-8 -*-
"""终审返工 H (2026-07-27): hourly report per-symbol block must NOT
duplicate the candidate-state sentence AND the blocker narrative, and every
rendered full sentence must end with the Chinese full stop "。".

Finding H on the Phase-2 (07-27) work concerns the per-symbol block rendered by
``_format_opportunity_row`` (the render path used by
``render_ga_hourly_summary`` and, via the shared text, the Feishu card). The
pre-fix code appends BOTH:

  - ``_trade_plan_summary(row)`` -> e.g.
    "候选计划详情（LONG 入场 100 止损 95），阻断原因：风控未通过（RR不足）"
  - ``_render_plan_state_label(row)`` -> e.g.
    "候选计划已生成，但风控未通过"

Both lines say "候选计划已生成/详情" AND both say "风控未通过". The blocker
reason is therefore stated TWICE — the "重复句子" (repeated-sentence) defect.
The ``not in result`` guard only suppresses an EXACT string match; the two
strings differ, so both append.

Fix (Option A): when the candidate carries a structured blocker narrative
(candidate_trade_plan is a dict AND plan_blockers is non-empty OR llm_status in
{failed, disabled} OR plan_status == "withheld" — i.e. _trade_plan_summary
returns a "候选计划详情（...），阻断原因：..." line), append ONLY
``_trade_plan_summary`` and SKIP ``_render_plan_state_label``. When there are
NO structured blockers (clean confirmed / unconfirmed / risk_rejected-without-
detail / invalidated-without-detail / no_candidate), append
``_render_plan_state_label`` so the 5-branch state wording still shows. The two
lines are mutually exclusive: EITHER the detailed candidate+blocker line OR
the concise state-label line, never both.

Punctuation: every rendered FULL SENTENCE ends with "。".
``_render_plan_state_label`` returns end with "。"; the "候选计划详情...
阻断原因：..." lines of ``_trade_plan_summary`` end with "。". The compact
key-value display line (``f"{side} {entry_type}，入场 ... 风控=..."``) stays
as-is (it is not a full sentence). The "暂无完整交易计划。" /
"LLM 失败..." / "候选计划被 LLM 失败阻断执行。" lines already end with "。".

The three render paths that must stay semantically identical:
  1. ``render_ga_hourly_summary`` -> ``_format_opportunity_row`` (the dedup
     site). Fixing this one function fixes the GA brief text AND the Feishu
     card text (the card reuses the GA/legacy text via ``build_hourly_report``).
  2. ``render_hourly_report_text`` -> ``_signal_report_lines`` -> only calls
     ``_trade_plan_summary`` (never ``_render_plan_state_label``), so there is
     NO duplication on this path — only the punctuation fix applies here.

Already-correct regressions proven here (DO NOT change production):
  - LLM-success (llm_status=ok, plan_execution_state=confirmed,
    plan_origin=llm_confirmed) renders "候选计划已生成（LLM 已确认）" and does
    NOT render "LLM 已禁用" anywhere in the per-symbol block.
  - no_candidate (plan_execution_state=no_candidate, no candidate_trade_plan)
    renders "无候选计划，本轮仅观察" and does NOT render "LLM 未确认".
  - Release-cleanup ("发布清理审计记录已归档") is rendered ONLY as an archived
    audit line prefixed "另有 N 个 ... 已归档（...不计入当前风险事件）" and NEVER
    enters the current-failed-jobs list (the "#id job_type: error" items).

RED-first + revert-fail:
  - RED: on the pre-fix code the risk_rejected row renders BOTH
    "候选计划详情" AND "候选计划已生成" (duplicate) and "风控未通过" TWICE;
    the dedup test and the revert-fail control FAIL.
  - GREEN: after the fix the risk_rejected row renders ONLY
    "候选计划详情（...），阻断原因：风控未通过（RR不足）。" (one line) and the
    state-label line is suppressed; the blocker surfaces exactly once.

No production DB mutation, no marker write to production (``make_repo`` runs
``initialize_database`` into the scratch schema only), no service restart, no
commit/push/release.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.notify import hourly_report
from plugins.crypto_guard.tests.pg_fixtures import make_repo


# ── helpers ─────────────────────────────────────────────────────────────────


def _risk_rejected_row() -> dict:
    """A per-symbol row mirroring ``_decision_row`` output for a risk_rejected
    candidate with a structured blocker. ``_format_opportunity_row`` reads
    ``candidate_trade_plan`` / ``plan_blockers`` / ``plan_execution_state`` /
    ``plan_origin`` directly off the row dict.
    """
    return {
        "symbol": "BTCUSDT",
        "signal_grade": "B",
        "confidence": 0.7,
        "analysis_time": 0,
        "created_at": None,
        "candidate_trade_plan": {
            "side": "LONG",
            "entry_price": 100,
            "stop_loss": 95,
            "entry_type": "limit",
        },
        "plan_blockers": [
            {"code": "risk_rejected", "detail": "RR不足", "stage": "risk"},
        ],
        "plan_execution_state": "risk_rejected",
        "plan_origin": "deterministic_fallback",
        "has_trade_plan": False,
        "trade_plan": None,
        "risk_check": {"ok": False, "reasons": ["RR不足"]},
        "llm_status": "ok",
        "plan_status": "withheld",
        "_blockers": [],  # gate blockers (unused for candidate dedup)
    }


def _confirmed_llm_row() -> dict:
    """A per-symbol row for a confirmed LLM-success decision (clean state)."""
    return {
        "symbol": "BTCUSDT",
        "signal_grade": "A",
        "confidence": 0.9,
        "analysis_time": 0,
        "created_at": None,
        "candidate_trade_plan": None,
        "plan_blockers": [],
        "plan_execution_state": "confirmed",
        "plan_origin": "llm_confirmed",
        "has_trade_plan": True,
        "trade_plan": {
            "side": "LONG",
            "entry_type": "limit",
            "entry_price": 100,
            "stop_loss": 95,
            "take_profits": [{"price": 110}],
        },
        "risk_check": {"ok": True},
        "llm_status": "ok",
        "plan_status": "executable",
        "_blockers": [],
    }


def _no_candidate_row() -> dict:
    """A per-symbol row for a no_candidate decision (clean observation)."""
    return {
        "symbol": "BTCUSDT",
        "signal_grade": "D",
        "confidence": 0.3,
        "analysis_time": 0,
        "created_at": None,
        "candidate_trade_plan": None,
        "plan_blockers": [],
        "plan_execution_state": "no_candidate",
        "plan_origin": None,
        "has_trade_plan": False,
        "trade_plan": None,
        "risk_check": {"ok": True},
        "llm_status": "ok",
        "plan_status": "no_plan",
        "_blockers": [],
    }


# ── H: dedup candidate-state + blocker narrative ────────────────────────────


class TestPgHourlyReportRenderHDedup:
    """H: the per-symbol block MUST NOT duplicate the candidate-state sentence
    with the blocker narrative. When the candidate carries a structured
    blocker, ONLY the detailed "候选计划详情（...），阻断原因：..." line is
    appended; the concise "_render_plan_state_label" line is suppressed. For
    clean states the state-label line is appended and "候选计划详情" does NOT
    appear.
    """

    def test_h_candidate_with_blocker_no_duplicate_state_label(self) -> None:
        """RED→GREEN H: a risk_rejected candidate with a structured blocker
        renders EXACTLY ONE of {"候选计划详情", "候选计划已生成"} — not both —
        and the blocker "风控未通过" appears exactly ONCE. The detail "RR不足"
        is still surfaced.

        Revert-fail: pre-fix code appends BOTH the candidate-detail line
        ("候选计划详情（...），阻断原因：风控未通过（RR不足）") AND the state-label
        line ("候选计划已生成，但风控未通过"), so BOTH "候选计划详情" AND
        "候选计划已生成" appear and "风控未通过" appears TWICE.
        """
        handle = make_repo()
        try:
            row = _risk_rejected_row()
            out = hourly_report._format_opportunity_row(
                row, {}, tier_label="观察候选"
            )
            # Dedup: NOT both "候选计划详情" AND "候选计划已生成".
            has_detail = "候选计划详情" in out
            has_label = "候选计划已生成" in out
            assert not (has_detail and has_label), (
                "GREEN H dedup: the per-symbol block must NOT contain BOTH "
                "'候选计划详情' AND '候选计划已生成' (duplicate candidate "
                "sentence). Pre-fix appends both."
            )
            # The blocker "风控未通过" appears exactly ONCE (not twice).
            assert out.count("风控未通过") == 1, (
                "GREEN H dedup: the blocker '风控未通过' must appear EXACTLY "
                "ONCE. Pre-fix states it in both the detail line and the "
                "state-label line."
            )
            # The blocker detail is still surfaced.
            assert "RR不足" in out, (
                "GREEN H: the blocker detail 'RR不足' must still appear in "
                "the per-symbol block."
            )
        finally:
            handle.close()

    def test_h_clean_confirmed_state_shows_label(self) -> None:
        """RED→GREEN H: a clean confirmed LLM-success decision renders the
        state label "候选计划已生成（LLM 已确认）" and does NOT duplicate with
        "候选计划详情" (clean states use the label, not the detail).
        """
        handle = make_repo()
        try:
            row = _confirmed_llm_row()
            out = hourly_report._format_opportunity_row(
                row, {}, tier_label="可执行"
            )
            assert "候选计划已生成（LLM 已确认）" in out, (
                "GREEN H: a clean confirmed LLM-success decision must render "
                "the state label '候选计划已生成（LLM 已确认）'."
            )
            assert "候选计划详情" not in out, (
                "GREEN H: a clean confirmed state must NOT also render "
                "'候选计划详情' (no structured blocker, so only the label "
                "line is shown)."
            )
        finally:
            handle.close()

    def test_h_llm_success_does_not_show_disabled(self) -> None:
        """RED→GREEN H regression: an LLM-success decision (llm_status=ok,
        plan_execution_state=confirmed, plan_origin=llm_confirmed) renders
        "LLM 已确认" and does NOT render "LLM 已禁用" anywhere in the
        per-symbol block.

        This is ALREADY correct pre-fix (the "LLM 已禁用" string only appears
        on the deterministic_sop confirmed path and the llm_disabled blocker
        code, neither of which fire for llm_status=ok). The test locks the
        contract so a future change cannot regress it.
        """
        handle = make_repo()
        try:
            row = _confirmed_llm_row()
            out = hourly_report._format_opportunity_row(
                row, {}, tier_label="可执行"
            )
            assert "LLM 已确认" in out, (
                "GREEN H: an LLM-success decision must render 'LLM 已确认'."
            )
            assert "LLM 已禁用" not in out, (
                "GREEN H: an LLM-success decision must NOT render 'LLM 已禁用' "
                "anywhere in the per-symbol block."
            )
        finally:
            handle.close()

    def test_h_no_candidate_does_not_show_not_confirmed(self) -> None:
        """RED→GREEN H regression: a no_candidate decision
        (plan_execution_state=no_candidate, no candidate_trade_plan) renders
        "无候选计划，本轮仅观察" and does NOT render "LLM 未确认".

        This is ALREADY correct pre-fix (the "LLM 未确认" wording is on the
        unconfirmed + deterministic_fallback branch, which the no_candidate
        path never hits). The test locks the contract.
        """
        handle = make_repo()
        try:
            row = _no_candidate_row()
            out = hourly_report._format_opportunity_row(
                row, {}, tier_label="观察候选"
            )
            assert "无候选计划，本轮仅观察" in out, (
                "GREEN H: a no_candidate decision must render '无候选计划，"
                "本轮仅观察'."
            )
            assert "LLM 未确认" not in out, (
                "GREEN H: a no_candidate decision must NOT render 'LLM 未确认' "
                "(that wording is exclusive to the unconfirmed + "
                "deterministic_fallback branch)."
            )
        finally:
            handle.close()

    def test_h_p2a_empty_blockers_llm_failed_shows_detail_not_label(self) -> None:
        """P2-A: candidate dict + empty plan_blockers + llm_status=failed.

        Pre-fix gated detail on truthy plan_blockers, so this row only got
        the state label. Post-fix must emit 候选计划详情 + LLM 失败 and must
        NOT also append the unconfirmed state-label line.
        """
        handle = make_repo()
        try:
            row = {
                "symbol": "ETHUSDT",
                "signal_grade": "B",
                "confidence": 0.6,
                "analysis_time": 0,
                "created_at": None,
                "candidate_trade_plan": {
                    "side": "LONG",
                    "entry_price": 2000,
                    "stop_loss": 1900,
                    "entry_type": "limit",
                },
                "plan_blockers": [],
                "plan_execution_state": "unconfirmed",
                "plan_origin": "deterministic_fallback",
                "has_trade_plan": False,
                "trade_plan": None,
                "risk_check": {"ok": True},
                "llm_status": "failed",
                "plan_status": "withheld",
                "_blockers": [],
            }
            out = hourly_report._format_opportunity_row(
                row, {}, tier_label="观察候选"
            )
            assert "候选计划详情" in out, (
                "P2-A: empty blockers + llm_status=failed must still render "
                "候选计划详情 via _trade_plan_summary"
            )
            assert "LLM 失败" in out, (
                "P2-A: empty blockers + llm_status=failed must surface LLM 失败"
            )
            assert "规则候选计划已生成，LLM 未确认，禁止执行" not in out, (
                "P2-A: state-label must be suppressed when detail line is present"
            )
        finally:
            handle.close()

    def test_h_p2a_empty_blockers_withheld_shows_detail(self) -> None:
        """P2-A: candidate + empty blockers + plan_status=withheld + llm ok."""
        handle = make_repo()
        try:
            row = {
                "symbol": "ETHUSDT",
                "signal_grade": "B",
                "confidence": 0.6,
                "analysis_time": 0,
                "created_at": None,
                "candidate_trade_plan": {
                    "side": "SHORT",
                    "entry_price": 2000,
                    "stop_loss": 2100,
                    "entry_type": "limit",
                },
                "plan_blockers": [],
                "plan_execution_state": "unconfirmed",
                "plan_origin": "deterministic_fallback",
                "has_trade_plan": False,
                "trade_plan": None,
                "risk_check": {"ok": True},
                "llm_status": "ok",
                "plan_status": "withheld",
                "_blockers": [],
            }
            out = hourly_report._format_opportunity_row(
                row, {}, tier_label="观察候选"
            )
            assert "候选计划详情" in out
            assert "执行门禁未通过" in out
            assert "规则候选计划已生成，LLM 未确认，禁止执行" not in out
        finally:
            handle.close()

    def test_h_p2b_llm_not_confirmed_humanized(self) -> None:
        """P2-B: path1 blocker code llm_not_confirmed renders Chinese LLM 未确认.

        Must not show raw ``llm_not_confirmed`` or false ``LLM 已禁用``.
        """
        handle = make_repo()
        try:
            row = {
                "symbol": "ETHUSDT",
                "signal_grade": "B",
                "confidence": 0.7,
                "analysis_time": 0,
                "created_at": None,
                "candidate_trade_plan": {
                    "side": "LONG",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "entry_type": "limit",
                },
                "plan_blockers": [
                    {
                        "code": "llm_not_confirmed",
                        "stage": "llm_synthesis",
                        "detail": "LLM succeeded without confirming plan",
                    }
                ],
                "plan_execution_state": "unconfirmed",
                "plan_origin": "deterministic_fallback",
                "has_trade_plan": False,
                "trade_plan": None,
                "risk_check": {"ok": True},
                "llm_status": "ok",
                "plan_status": "withheld",
                "_blockers": [],
            }
            out = hourly_report._format_opportunity_row(
                row, {}, tier_label="观察候选"
            )
            assert "LLM 未确认" in out, (
                "P2-B: llm_not_confirmed blocker must humanize to LLM 未确认"
            )
            assert "LLM 已禁用" not in out
            assert "llm_not_confirmed" not in out, (
                "P2-B: raw code must not appear in operator-facing text"
            )
            assert "候选计划详情" in out
        finally:
            handle.close()


# ── H: punctuation terminator ────────────────────────────────────────────────


class TestPgHourlyReportRenderHPunctuation:
    """H: every rendered FULL SENTENCE in the candidate-blocker narrative and
    the state label ends with the Chinese full stop "。".

    The compact key-value display line ("LONG limit，入场 100...风控=通过")
    is NOT a full sentence and stays as-is. The "暂无完整交易计划。" /
    "LLM 失败..." / "候选计划被 LLM 失败阻断执行。" lines already end with "。".
    """

    def test_h_punctuation_terminator(self) -> None:
        """RED→GREEN H: the rendered candidate-blocker line
        ("候选计划详情（...），阻断原因：风控未通过（RR不足）。") and the
        state-label line ("候选计划已生成（LLM 已确认）。") each end with "。"
        when they are full sentences.
        """
        handle = make_repo()
        try:
            # The candidate+blocker line (full sentence) must end with "。".
            row = _risk_rejected_row()
            summary = hourly_report._trade_plan_summary(row)
            assert summary.endswith("。"), (
                "GREEN H punctuation: the candidate+blocker line must end "
                f"with '。'. Got: {summary!r}"
            )
            # The state-label lines (full sentences) must end with "。".
            for state, origin in [
                ("confirmed", "llm_confirmed"),
                ("unconfirmed", "deterministic_fallback"),
                ("risk_rejected", None),
                ("invalidated", None),
                ("no_candidate", None),
                ("confirmed", "deterministic_sop"),
            ]:
                label = hourly_report._render_plan_state_label(
                    {"plan_execution_state": state, "plan_origin": origin}
                )
                assert label.endswith("。"), (
                    f"GREEN H punctuation: the state-label for "
                    f"({state}, {origin}) must end with '。'. Got: {label!r}"
                )
            # The compact key-value display line is NOT a sentence and stays
            # WITHOUT a trailing "。" (it is rendered as "  - 交易计划：{plan}"
            # which provides the sentence frame).
            compact_row = {
                "has_trade_plan": True,
                "trade_plan": {
                    "side": "LONG",
                    "entry_type": "limit",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profits": [{"price": 110}],
                },
                "risk_check": {"ok": True},
            }
            compact = hourly_report._trade_plan_summary(compact_row)
            assert not compact.endswith("。"), (
                "GREEN H punctuation: the compact key-value display line must "
                f"NOT end with '。' (it is not a full sentence). Got: "
                f"{compact!r}"
            )
        finally:
            handle.close()


# ── H: revert-fail control ───────────────────────────────────────────────────


class TestPgHourlyReportRenderHRevertFail:
    """H revert-fail control: documents and asserts that the pre-fix code
    WOULD have produced BOTH "候选计划详情" AND "候选计划已生成" (duplicate) —
    i.e. the dedup test would FAIL against pre-fix code.

    This is a CONTROL test: it asserts the post-fix contract holds (dedup)
    AND documents the pre-fix failure mode in the docstring so a reviewer can
    reproduce the revert-fail by checking out the pre-fix code.
    """

    def test_h_revert_fail_control(self) -> None:
        """Revert-fail control: the dedup contract holds post-fix.

        Pre-fix behavior (documented, reproducible by reverting the fix):
          - ``_format_opportunity_row`` appends BOTH
            ``_trade_plan_summary(row)`` ("候选计划详情（...），阻断原因：
            风控未通过（RR不足）") AND ``_render_plan_state_label(row)``
            ("候选计划已生成，但风控未通过") for a risk_rejected candidate.
          - Therefore the rendered text contained BOTH "候选计划详情" AND
            "候选计划已生成", and "风控未通过" appeared TWICE.
          - ``test_h_candidate_with_blocker_no_duplicate_state_label`` would
            FAIL against pre-fix code (the ``not (has_detail and has_label)``
            assertion would fire, and ``out.count("风控未通过") == 1`` would
            fail with count == 2).

        Post-fix: the dedup holds — only one of the two narratives appears.
        """
        handle = make_repo()
        try:
            row = _risk_rejected_row()
            out = hourly_report._format_opportunity_row(
                row, {}, tier_label="观察候选"
            )
            has_detail = "候选计划详情" in out
            has_label = "候选计划已生成" in out
            # Post-fix: dedup holds.
            assert not (has_detail and has_label), (
                "Revert-fail control: post-fix the dedup must hold — the "
                "per-symbol block must NOT contain BOTH '候选计划详情' AND "
                "'候选计划已生成'. If this fires, the dedup fix was reverted."
            )
            assert out.count("风控未通过") == 1, (
                "Revert-fail control: post-fix '风控未通过' must appear "
                "exactly once. If count != 1, the dedup fix was reverted."
            )
        finally:
            handle.close()


# ── H: release-cleanup only in archived audit line ──────────────────────────


class TestPgHourlyReportRenderHReleaseCleanupAudit:
    """H requirement 5: the release-cleanup ("发布清理审计记录已归档") text is
    rendered ONLY as an archived audit line (prefixed "另有 N 个 ... 已归档"
    with the "不计入当前风险事件" qualifier) and NEVER enters the current
    failed-jobs list (the "#id job_type: error" per-item items).

    This is ALREADY correct post-P1-4 (the release-audit rows are split out via
    ``_split_current_and_legacy_failed_jobs`` into ``release_audit_count`` and
    rendered as a separate archived-audit line, NOT as current failed jobs).
    The test locks the contract so a future change cannot regress it.
    """

    def test_h_release_cleanup_only_in_archived_audit_line(self) -> None:
        """RED→GREEN H: when failed_jobs contains a mix of current failures AND
        release-audit terminal records, the rendered "风险事件" section shows
        the current failures as "#id job_type: error" per-item lines AND the
        release-cleanup count as a SEPARATE "另有 N 个发布清理审计记录已归档"
        archived line. The release-audit rows do NOT appear as "#id" per-item
        current failures.
        """
        handle = make_repo()
        try:
            # Build failed_jobs: one current failure + two release-audit
            # terminal records. The release-audit classification matches the
            # job's ``error_message`` against
            # ``RELEASE_AUDIT_ERROR_SIGNATURES`` (defined on
            # CryptoGuardRepository): "stale-release cleanup" and
            # "stale_snapshot_discarded_before_release". The ``job_type`` is
            # irrelevant to the classifier — only ``error_message`` is read.
            current_job = {
                "id": 5001,
                "job_type": "scheduled_analysis",
                "error_message": "current failure",
            }
            release_audit_jobs = [
                {
                    "id": 5002,
                    "job_type": "maintenance",
                    "error_message": "stale-release cleanup: discarded batch",
                },
                {
                    "id": 5003,
                    "job_type": "maintenance",
                    "error_message": (
                        "stale_snapshot_discarded_before_release: id=900"
                    ),
                },
            ]
            failed_jobs = [current_job] + release_audit_jobs

            text = hourly_report.render_hourly_report_text(
                generated_at_utc="2026-07-27T12:00:00Z",
                active_symbols=["BTCUSDT"],
                signals=[],
                open_orders=[],
                failed_jobs=failed_jobs,
                queue_counts={
                    "pending_user": 0,
                    "pending_background": 0,
                    "running": 0,
                },
                state_consistency=None,
            )

            # The current failure appears as a "#id" per-item line.
            assert "#5001" in text, (
                "GREEN H: the current failure must appear as a '#id' per-item "
                "line in the risk-events section."
            )
            # The release-cleanup archived line is present.
            assert "发布清理审计记录已归档" in text, (
                "GREEN H: the release-cleanup archived-audit line must be "
                "present when release-audit rows exist."
            )
            assert "不计入当前风险事件" in text, (
                "GREEN H: the release-cleanup archived-audit line must carry "
                "the '不计入当前风险事件' qualifier."
            )
            # The release-audit rows do NOT appear as "#id" per-item current
            # failures. Their ids (5002, 5003) must NOT be rendered as
            # per-item lines.
            assert "#5002" not in text, (
                "GREEN H: the release-audit row id 5002 must NOT appear as a "
                "'#id' per-item current failure (it is archived, not current)."
            )
            assert "#5003" not in text, (
                "GREEN H: the release-audit row id 5003 must NOT appear as a "
                "'#id' per-item current failure (it is archived, not current)."
            )
        finally:
            handle.close()