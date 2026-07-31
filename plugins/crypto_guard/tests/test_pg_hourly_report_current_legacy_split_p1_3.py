# -*- coding: utf-8 -*-
"""终审返工 P1-3 (2026-07-27): historical fail-closed audit rows MUST NOT
pollute the per-hour current issues in the hourly report.

Codex final-review finding P1-3 on the Phase-2 P2-1 (07-27) work:
``diagnose_state_consistency`` may keep the full audit data in ``issues``, BUT
must clearly separate current issues from legacy (pre-fix) historical rows, and
the three hourly-report render paths (``render_ga_hourly_summary``,
``render_hourly_report_text``, and the Feishu-card text path inside
``render_hourly_report_text`` at the second state-consistency block) MUST render
ONLY current issues for "发现问题 N 个" and the detail first-10. Legacy issues
collapse to ONE summary line:

  "另有 N 条 fail-closed 修复部署前历史记录，已归档审计，不计入当前问题。"

and MUST NOT enter "发现问题 N 个" or the per-item detail.

Contract (verbatim from the authoritative review):
  - ``diagnose_state_consistency`` returns NEW keys ``current_issues`` /
    ``current_issue_count`` / ``legacy_issues`` alongside the existing
    ``issues`` / ``total_issues`` / ``legacy_info_count`` (kept for back-compat).
  - ``ok = len(error_issues) == 0`` is unchanged (legacy never fails the gate).
  - ``total_issues`` stays the ALL-issues count (option (b): render uses
    ``current_issue_count``, not ``total_issues``, for "发现问题 N 个").
  - The three render paths use ``current_issue_count`` for the count label and
    ``current_issues`` for the detail first-10, then append the legacy summary
    line when ``legacy_issues`` is non-empty.

RED-first + revert-fail:
  - RED: on the pre-fix code the render paths use ``total_issues`` (== 2, current
    + historical) for "发现问题 N 个" and the flat ``issues`` list for the detail
    first-10, so the rendered text shows "发现问题 2 个" AND the historical row's
    symbol/decision_id in the detail. The assertions below (count == 1, legacy
    summary line present, historical symbol NOT in detail) FAIL on that code.
  - GREEN: after the fix the render uses ``current_issue_count`` (== 1) and
    ``current_issues`` for the detail, so the text shows "发现问题 1 个", the
    legacy summary line, and the current row's identity only.

No production DB mutation, no marker write to production (``make_repo`` runs
``initialize_database`` into the scratch schema only), no service restart, no
commit/push/release.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.diagnostics.state_consistency import (
    diagnose_state_consistency,
    LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY,
)
from plugins.crypto_guard.notify import hourly_report


# ── helpers ─────────────────────────────────────────────────────────────────


def _marker_applied_at_dt(conn) -> datetime:
    """Read the fail-closed marker applied_at as an aware UTC datetime.

    Mirrors the P1-2 test helper. The marker is seeded by ``initialize_database``
    into the scratch schema. ``applied_at`` is ``TIMESTAMPTZ`` so psycopg returns
    an aware datetime; if somehow naive, assume UTC. Raises if absent.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applied_at FROM _migration_state WHERE key = %s LIMIT 1",
            (LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY,),
        )
        row = cur.fetchone()
    assert row and row["applied_at"], (
        "GREEN: initialize_database must seed "
        f"{LLM_FAILED_DIRECTION_FAIL_CLOSED_MARKER_KEY!r}"
    )
    dt = row["applied_at"]
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _seed_decision_row(
    repo, *,
    symbol: str,
    analysis_time_dt: datetime,
    llm_status: str,
    bias: str,
) -> int:
    """Insert one ``ga_decisions`` row with the given llm_status + market_bias.

    Mirrors the P1-2 / P2-1 helper shape so the diagnostic's row-scan picks it
    up. ``created_at`` is left to ``DEFAULT NOW()``; callers UPDATE it via
    ``_set_created_at`` to position the row before/after the marker cutoff.
    """
    decision = {
        "symbol": symbol,
        "analysis_time": _ms(analysis_time_dt),
        "analysis_time_utc": _iso(analysis_time_dt),
        "decision_type": "scheduled_analysis",
        "signal_grade": "D",
        "confidence": 0.3,
        "market_bias": bias,  # bullish/bearish: the leak signature
        "trend_stage": "range",
        "decision": "no_trade",
        "skill_result_refs": {"trend": 1},
        "evidence": [],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        "feishu_actions": [],
        "final_summary": "summary",
        "raw_llm_summary": "LLM TEXT",
        "rendered_summary": "canonical",
        "batch_id": None,
        "previous_grade": "D",
        "llm_status": llm_status,  # stored inside raw_decision_json
    }
    return repo.create_ga_decision(decision)


def _set_created_at(repo, ga_id: int, dt: datetime) -> None:
    """Override ``ga_decisions.created_at`` for one row (scratch schema only).

    Mirrors the P1-2 helper. ``created_at`` is ``TIMESTAMPTZ DEFAULT NOW()`` and
    cannot be set via ``create_ga_decision``; tests UPDATE it directly inside a
    transaction to position the row relative to the marker cutoff.
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    with repo.conn.transaction():
        with repo.conn.cursor() as cur:
            cur.execute(
                "UPDATE ga_decisions SET created_at = %s::timestamptz WHERE id = %s",
                (aware.astimezone(timezone.utc), int(ga_id)),
            )


def _seed_current_plus_historical(repo, conn) -> tuple[int, int]:
    """Seed BOTH a current ``failed``+bullish row (created_at AFTER marker →
    current warning) AND a historical ``failed``+bullish row (created_at BEFORE
    marker → legacy_info).

    Returns ``(current_ga_id, historical_ga_id)``. The two rows use DISTINCT
    symbols so the rendered-detail assertions can distinguish them.
    """
    cutoff = _marker_applied_at_dt(conn)
    # Current: created_at AFTER marker → current warning.
    cur_at = cutoff + timedelta(hours=1)
    cur_id = _seed_decision_row(
        repo, symbol="SOLUSDT",
        analysis_time_dt=cur_at,
        llm_status="failed", bias="bullish",
    )
    _set_created_at(repo, cur_id, cutoff + timedelta(hours=1))
    # Historical: created_at BEFORE marker → legacy_info.
    hist_at = cutoff - timedelta(hours=24)
    hist_id = _seed_decision_row(
        repo, symbol="ETHUSDT",
        analysis_time_dt=hist_at,
        llm_status="failed", bias="bullish",
    )
    _set_created_at(repo, hist_id, cutoff - timedelta(hours=24))
    return cur_id, hist_id


# ── P1-3: diagnose_state_consistency current/legacy split ───────────────────


class TestPgDiagnoseStateConsistencyCurrentLegacySplitP1_3:
    """P1-3: ``diagnose_state_consistency`` returns NEW keys
    ``current_issues`` / ``current_issue_count`` / ``legacy_issues`` alongside
    the existing ``issues`` / ``total_issues`` / ``legacy_info_count``.
    Legacy rows (``severity == "legacy_info"``) populate ``legacy_issues`` and
    MUST NOT inflate ``current_issue_count``; current rows (``severity`` in
    ``{error, warning}``) populate ``current_issues``. ``ok`` is unchanged
    (legacy never fails the gate). ``total_issues`` stays the ALL-issues count.
    """

    def test_current_and_legacy_lists_split_correctly(self) -> None:
        """RED→GREEN P1-3: seed one current + one historical failed+bias row;
        the returned dict splits them into ``current_issues`` / ``legacy_issues``
        with the right counts, while ``issues`` / ``total_issues`` /
        ``legacy_info_count`` are preserved (back-compat).
        """
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            cur_id, hist_id = _seed_current_plus_historical(repo, conn)
            result = diagnose_state_consistency(repo)

            # Back-compat: existing keys still present and correct.
            assert result["ok"] is True, result
            assert result["total_issues"] >= 2, result
            assert result["legacy_info_count"] >= 1, result
            assert result["error_count"] == 0, result
            assert result["warning_count"] >= 1, result

            # NEW keys present.
            assert "current_issues" in result, result
            assert "current_issue_count" in result, result
            assert "legacy_issues" in result, result

            # current_issues contains the current warning, NOT the historical.
            current_types = {i["type"] for i in result["current_issues"]}
            assert "deterministic_direction_from_failed_llm" in current_types, (
                "GREEN P1-3: current_issues must contain the current "
                "deterministic_direction_from_failed_llm warning."
            )
            current_ids = {
                int(i["details"]["decision_id"])
                for i in result["current_issues"]
                if i["type"] == "deterministic_direction_from_failed_llm"
            }
            assert cur_id in current_ids, (
                "GREEN P1-3: the current row's decision_id must be in "
                "current_issues."
            )
            assert hist_id not in current_ids, (
                "GREEN P1-3: the historical row's decision_id must NOT be in "
                "current_issues."
            )

            # legacy_issues contains the historical legacy_info, NOT the current.
            legacy_types = {i["type"] for i in result["legacy_issues"]}
            assert "deterministic_direction_from_failed_llm_historical" in legacy_types, (
                "GREEN P1-3: legacy_issues must contain the historical "
                "deterministic_direction_from_failed_llm_historical row."
            )
            legacy_ids = {
                int(i["details"]["decision_id"])
                for i in result["legacy_issues"]
                if i["type"] == "deterministic_direction_from_failed_llm_historical"
            }
            assert hist_id in legacy_ids, (
                "GREEN P1-3: the historical row's decision_id must be in "
                "legacy_issues."
            )
            assert cur_id not in legacy_ids, (
                "GREEN P1-3: the current row's decision_id must NOT be in "
                "legacy_issues."
            )

            # current_issue_count counts ONLY current (error + warning).
            assert result["current_issue_count"] == len(result["current_issues"]), (
                "current_issue_count must equal len(current_issues)."
            )
            # current_issue_count excludes the historical row: it is strictly
            # less than total_issues (which counts ALL).
            assert result["current_issue_count"] < result["total_issues"], (
                "GREEN P1-3: current_issue_count must exclude the historical "
                "row — it is strictly less than total_issues."
            )
            # legacy_info_count equals len(legacy_issues).
            assert result["legacy_info_count"] == len(result["legacy_issues"]), (
                "legacy_info_count must equal len(legacy_issues)."
            )
        finally:
            handle.close()


# ── P1-3: hourly report render paths show ONLY current issues ───────────────


class TestPgHourlyReportCurrentLegacyRenderSplitP1_3:
    """P1-3: the three hourly-report render paths render ONLY ``current_issues``
    for "发现问题 N 个" and the detail first-10; ``legacy_issues`` collapse to
    ONE summary line and MUST NOT appear per-item in the detail.

    RED-first + revert-fail: the assertions below FAIL on the pre-fix code, which
    used ``total_issues`` (== 2) for "发现问题 N 个" and the flat ``issues`` list
    for the detail first-10 — so the text showed "发现问题 2 个" and BOTH rows'
    symbols in the detail. After the fix the text shows "发现问题 1 个" (the
    CURRENT count), the legacy summary line, and ONLY the current row's symbol.
    """

    def _sc_with_current_plus_historical(self, cur_id: int, hist_id: int) -> dict:
        """A state_consistency dict carrying one current warning + one historical
        legacy_info, mirroring what ``diagnose_state_consistency`` returns after
        the P1-3 fix. Used by the render-path assertions so the render test does
        not depend on DB seeding for the render itself (the diagnose test above
        already proves the split at the diagnostic layer)."""
        return {
            "ok": True,
            "summary": {
                "active_eval_missing_ga_decision_id": 0,
                "paper_order_missing_active_eval": 0,
                "closed_trade_missing_active_real_pnl": 0,
                "duplicate_open_trades": 0,
                "orphan_patches": 0,
                "status_mismatches": 0,
                "duplicate_patches": 0,
                "stale_shadows": 0,
                "draft_limbo": 0,
                "shadow_candidate_legacy_only": 0,
                "stalled_candidate": 0,
                "deterministic_direction_from_failed_llm_historical": 1,
            },
            "total_issues": 2,  # ALL issues (current + historical) — unchanged.
            "error_count": 0,
            "warning_count": 1,  # only the current warning.
            "legacy_info_count": 1,  # the historical row.
            "current_issue_count": 1,  # NEW: current only.
            "current_issues": [
                {
                    "type": "deterministic_direction_from_failed_llm",
                    "severity": "warning",
                    "scope": {"decision_id": cur_id, "symbol": "SOLUSDT"},
                    "time_window": {"analysis_time_utc": "2026-07-27T10:00:00Z"},
                    "details": {
                        "decision_id": cur_id,
                        "symbol": "SOLUSDT",
                        "llm_status": "failed",
                        "market_bias": "bullish",
                        "signal_grade": "D",
                        "classification": "current",
                    },
                    "message": (
                        f"SOLUSDT GA 决策 {cur_id} llm_status=failed 但 "
                        f"market_bias=bullish（应为 unknown）。该行 created_at 晚于 "
                        f"fail-closed 契约 marker，确定性引擎在 LLM 失败时仍输出方向——当前违规。"
                    ),
                },
            ],
            "legacy_issues": [
                {
                    "type": "deterministic_direction_from_failed_llm_historical",
                    "severity": "legacy_info",
                    "scope": {"decision_id": hist_id, "symbol": "ETHUSDT"},
                    "time_window": {"analysis_time_utc": "2026-07-20T10:00:00Z"},
                    "details": {
                        "decision_id": hist_id,
                        "symbol": "ETHUSDT",
                        "llm_status": "failed",
                        "market_bias": "bullish",
                        "signal_grade": "D",
                        "classification": "historical",
                    },
                    "message": (
                        f"ETHUSDT GA 决策 {hist_id} llm_status=failed 但 "
                        f"market_bias=bullish（应为 unknown）。该行 created_at 早于 "
                        f"fail-closed 契约 marker，属 fail-closed 修复部署前的历史审计记录，"
                        f"不计入当前风险事件。"
                    ),
                },
            ],
            # Keep the flat ``issues`` list for back-compat (still the union).
            "issues": [],
        }

    def test_render_hourly_report_text_current_only_count_and_detail(self) -> None:
        """RED→GREEN P1-3 render path 1 (``render_hourly_report_text``): the
        state-consistency block shows "发现问题 1 个" (the CURRENT count, NOT 2),
        the detail first-10 contains the CURRENT row's symbol/decision_id but
        NOT the historical row's, and the legacy summary line appears once.

        Revert-fail: on the pre-fix code this dict (which sets
        ``total_issues=2`` and carries both rows) would render "发现问题 2 个"
        via ``total_issues`` and would iterate the flat ``issues`` list (empty
        here, but the real pre-fix path used the flat list with both rows). The
        load-bearing assertions are: count == 1 (NOT 2), legacy summary line
        present, and the historical symbol absent from the per-item detail.
        """
        handle = make_repo()
        try:
            cur_id = 9001
            hist_id = 9002
            sc = self._sc_with_current_plus_historical(cur_id, hist_id)
            # Populate the flat ``issues`` list as the pre-fix code would have
            # received it (current + historical together) so the revert-fail
            # control is realistic: pre-fix render iterates this flat list.
            sc["issues"] = sc["current_issues"] + sc["legacy_issues"]

            text = hourly_report.render_hourly_report_text(
                generated_at_utc="2026-07-27T12:00:00Z",
                active_symbols=["BTCUSDT"], signals=[], open_orders=[],
                failed_jobs=[],
                queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
                state_consistency=sc,
            )
            self._assert_render_split(text, cur_id, hist_id)
        finally:
            handle.close()

    def test_render_ga_hourly_summary_current_only_count(self) -> None:
        """RED→GREEN P1-3 render path 2 (``render_ga_hourly_summary``): the
        brief state-consistency block shows "发现问题 1 个" (the CURRENT count),
        NOT "发现问题 2 个". The legacy summary line appears once. This path
        does not render a per-item detail, so only the count + legacy line are
        asserted.

        Revert-fail: pre-fix used ``total_issues`` (== 2) for the count label.
        """
        handle = make_repo()
        try:
            cur_id = 9101
            hist_id = 9102
            sc = self._sc_with_current_plus_historical(cur_id, hist_id)
            sc["issues"] = sc["current_issues"] + sc["legacy_issues"]

            text = hourly_report.render_ga_hourly_summary(
                generated_at_utc="2026-07-27T12:00:00Z",
                active_symbols=["BTCUSDT"], ga_decisions=[], open_orders=[],
                active_watches=[], failed_jobs=[],
                queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
                state_consistency=sc,
            )
            # Current count label: "发现问题 1 个", NOT "发现问题 2 个".
            assert "发现问题 1 个" in text, (
                "GREEN P1-3: the GA brief path must show the CURRENT count "
                "(1), not the all-issues count (2)."
            )
            assert "发现问题 2 个" not in text, (
                "GREEN P1-3 revert-fail: the GA brief path must NOT show the "
                "all-issues count (2) — pre-fix used total_issues and would "
                "have rendered '发现问题 2 个'."
            )
            # Legacy summary line present exactly once (NOT iterated per-item).
            assert "另有 1 条 fail-closed 修复部署前历史记录" in text, (
                "GREEN P1-3: the GA brief path must append the legacy summary "
                "line when legacy_issues is non-empty."
            )
            # The historical row's symbol MUST NOT appear per-item.
            assert "ETHUSDT" not in text, (
                "GREEN P1-3: the historical row's symbol must NOT appear in "
                "the GA brief path (legacy is a single summary line, not "
                "per-item)."
            )
        finally:
            handle.close()

    def test_render_hourly_report_text_feishu_card_path_current_only(self) -> None:
        """RED→GREEN P1-3 render path 3 (the Feishu-card text path inside
        ``render_hourly_report_text`` — the second state-consistency block that
        renders the card-style "状态一致性诊断：" heading with the detail
        first-10). The card path shows "发现问题 1 个" (CURRENT count), the
        detail first-10 contains the CURRENT row's symbol/decision_id but NOT
        the historical row's, and the legacy summary line appears once.

        Revert-fail: pre-fix used ``total_issues`` (== 2) and the flat
        ``issues`` list for the card path's detail too.
        """
        handle = make_repo()
        try:
            cur_id = 9201
            hist_id = 9202
            sc = self._sc_with_current_plus_historical(cur_id, hist_id)
            sc["issues"] = sc["current_issues"] + sc["legacy_issues"]

            text = hourly_report.render_hourly_report_text(
                generated_at_utc="2026-07-27T12:00:00Z",
                active_symbols=["BTCUSDT"], signals=[], open_orders=[],
                failed_jobs=[],
                queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
                state_consistency=sc,
            )
            self._assert_render_split(text, cur_id, hist_id)
        finally:
            handle.close()

    def test_render_current_only_zero_current_with_legacy(self) -> None:
        """RED→GREEN P1-3 edge: when ``current_issue_count == 0`` AND
        ``legacy_issues`` is non-empty, the render must NOT show "发现问题 0 个"
        plus an empty detail — it shows the current-zero line and the legacy
        summary line. Legacy MUST NOT enter "发现问题 N 个" (the count stays 0).
        """
        handle = make_repo()
        try:
            hist_id = 9301
            sc = {
                "ok": True,
                "summary": {
                    "deterministic_direction_from_failed_llm_historical": 1,
                },
                "total_issues": 1,
                "error_count": 0,
                "warning_count": 0,
                "legacy_info_count": 1,
                "current_issue_count": 0,
                "current_issues": [],
                "legacy_issues": [
                    {
                        "type": "deterministic_direction_from_failed_llm_historical",
                        "severity": "legacy_info",
                        "scope": {"decision_id": hist_id, "symbol": "ETHUSDT"},
                        "details": {
                            "decision_id": hist_id,
                            "symbol": "ETHUSDT",
                            "classification": "historical",
                        },
                        "message": (
                            f"ETHUSDT GA 决策 {hist_id} 历史审计记录。"
                        ),
                    },
                ],
                "issues": [],
            }
            sc["issues"] = list(sc["legacy_issues"])

            text = hourly_report.render_hourly_report_text(
                generated_at_utc="2026-07-27T12:00:00Z",
                active_symbols=["BTCUSDT"], signals=[], open_orders=[],
                failed_jobs=[],
                queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
                state_consistency=sc,
            )
            # Legacy count MUST NOT inflate "发现问题 N 个": with current==0 the
            # count label is 0 (or the "全部正常" line), never 1.
            assert "发现问题 1 个" not in text, (
                "GREEN P1-3 edge: with current==0 + 1 legacy, the count label "
                "must NOT be 1 (legacy must not pollute the current count)."
            )
            # The legacy summary line appears.
            assert "另有 1 条 fail-closed 修复部署前历史记录" in text, (
                "GREEN P1-3 edge: the legacy summary line must appear when "
                "legacy_issues is non-empty even if current==0."
            )
            # The historical symbol MUST NOT appear per-item in the detail.
            assert "ETHUSDT" not in text, (
                "GREEN P1-3 edge: the historical symbol must NOT appear "
                "per-item (legacy is a single summary line)."
            )
        finally:
            handle.close()

    def _assert_render_split(self, text: str, cur_id: int, hist_id: int) -> None:
        """Shared assertions for the two detail-bearing render paths."""
        # Current count label: "发现问题 1 个", NOT "发现问题 2 个".
        assert "发现问题 1 个" in text, (
            "GREEN P1-3: the render must show the CURRENT count (1), not the "
            "all-issues count (2)."
        )
        assert "发现问题 2 个" not in text, (
            "GREEN P1-3 revert-fail: the render must NOT show the all-issues "
            "count (2) — pre-fix used total_issues and would have rendered "
            "'发现问题 2 个'."
        )
        # Legacy summary line present (NOT iterated per-item).
        assert "另有 1 条 fail-closed 修复部署前历史记录" in text, (
            "GREEN P1-3: the render must append the legacy summary line when "
            "legacy_issues is non-empty."
        )
        # The CURRENT row's symbol/decision_id appear in the detail first-10.
        assert "SOLUSDT" in text, (
            "GREEN P1-3: the current row's symbol must appear in the detail "
            "first-10 (current_issues)."
        )
        assert str(cur_id) in text, (
            "GREEN P1-3: the current row's decision_id must appear in the "
            "detail first-10."
        )
        # The HISTORICAL row's symbol MUST NOT appear per-item in the detail.
        # The historical row's symbol is "ETHUSDT" — but the legacy summary line
        # does NOT name it, so its absence proves the detail used current_issues
        # only. (active_symbols=["BTCUSDT"] so ETHUSDT only appears if the
        # historical issue was iterated per-item.)
        assert "ETHUSDT" not in text, (
            "GREEN P1-3 revert-fail: the historical row's symbol must NOT "
            "appear in the rendered detail — pre-fix iterated the flat issues "
            "list and would have rendered the historical row per-item."
        )


# ── P1-3: end-to-end diagnose → render (real DB) ─────────────────────────────


class TestPgDiagnoseThenRenderCurrentLegacySplitP1_3:
    """P1-3 end-to-end: seed real rows on the scratch schema, run
    ``diagnose_state_consistency``, feed the result to
    ``render_hourly_report_text``, and assert the rendered text shows the
    current count (NOT the all-issues count), the legacy summary line, and the
    current row's symbol (NOT the historical row's). This proves the full
    diagnostic→render chain honours the split.
    """

    def test_diagnose_then_render_shows_current_only(self) -> None:
        """RED→GREEN P1-3 e2e: real ``diagnose_state_consistency`` output fed
        directly to ``render_hourly_report_text`` renders ONLY current issues.
        """
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            cur_id, hist_id = _seed_current_plus_historical(repo, conn)
            result = diagnose_state_consistency(repo)
            # The diagnostic returns the NEW keys.
            assert result["current_issue_count"] == 1, result
            assert result["legacy_info_count"] == 1, result
            assert result["total_issues"] == 2, result

            text = hourly_report.render_hourly_report_text(
                generated_at_utc="2026-07-27T12:00:00Z",
                active_symbols=[], signals=[], open_orders=[],
                failed_jobs=[],
                queue_counts={"pending_user": 0, "pending_background": 0, "running": 0},
                state_consistency=result,
            )
            # Current count label is 1 (NOT 2).
            assert "发现问题 1 个" in text, (
                "GREEN P1-3 e2e: the rendered count must be the CURRENT count "
                "(1), not total_issues (2)."
            )
            assert "发现问题 2 个" not in text, (
                "GREEN P1-3 e2e revert-fail: total_issues (2) must NOT drive "
                "the count label."
            )
            # Legacy summary line present.
            assert "另有 1 条 fail-closed 修复部署前历史记录" in text, (
                "GREEN P1-3 e2e: the legacy summary line must appear."
            )
            # The current row's symbol appears in the detail.
            assert "SOLUSDT" in text, (
                "GREEN P1-3 e2e: the current row's symbol must appear in the "
                "rendered detail."
            )
            # The historical row's symbol MUST NOT appear per-item.
            assert "ETHUSDT" not in text, (
                "GREEN P1-3 e2e revert-fail: the historical row's symbol must "
                "NOT appear per-item in the detail."
            )
        finally:
            handle.close()