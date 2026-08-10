"""P1-4 (07-22 production review): release-audit terminal records must NOT
masquerade as recent failures / risk events.

CryptoGuard 2026-07-24 production review (Codex) found that terminal
release-audit ``agent_jobs`` rows written by the
``/trellis:crypto-guard-release`` R3 stale-release cleanup step
(``error_message='stale-release cleanup'`` and
``stale_snapshot_discarded_before_release``) were being surfaced in the
hourly report's "最近失败 N 个" system-status line and the 九、风险事件
section as if they were current production failures. A release-housekeeping
action is NOT a current failure; surfacing it there misrepresents a
deliberate maintenance action as a live risk event.

The fix has two layers:

1. ``recent_failed_jobs`` (repository.py) now EXCLUDES release-audit rows
   at the SQL layer (``NOT EXISTS (SELECT 1 FROM unnest(%s) ... WHERE
   error_message LIKE '%'||s||'%')``). A ``LIMIT 5`` ordered by ``id DESC``
   could otherwise be entirely consumed by recent release-audit rows,
   starving real current failures out of the list so "最近失败 N 个" reads
   0 while only archived release-audit rows are fetched. The companion
   ``recent_release_audit_jobs`` surfaces them separately.
2. ``_split_current_and_legacy_failed_jobs`` (hourly_report.py) is extended
   to a 3-way split ``(current_jobs, legacy_schema_fail_count,
   release_audit_count)`` so the report renders a SEPARATE archived-
   release-audit line ("另有 N 个发布清理审计记录已归档 ... 不计入当前风险事件")
   that distinguishes current failures from archived release audit.

Original ``agent_jobs`` rows are NEVER deleted — this is classification
only. The report view reclassifies; the audit history is preserved in the
DB.

These tests drive the REAL producer->consumer chain:
``recent_failed_jobs`` / ``recent_release_audit_jobs`` (real repository on
isolated PG) -> ``_split_current_and_legacy_failed_jobs`` (real
hourly_report classifier). No mocks of the functions under test.

Isolated PostgreSQL fixture only. No production DB mutation, no service
restart, no commit/push/finish-work.
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.notify.hourly_report import (
    _split_current_and_legacy_failed_jobs,
    _is_release_audit_job,
    _RELEASE_AUDIT_SIGNATURES,
)


def _seed_agent_job(
    conn, *, job_type: str, error_message: str, finished_at: str,
) -> int:
    """Insert one ``agent_jobs`` row with the given error_message and
    finished_at offset string (e.g. ``"NOW() - INTERVAL '1 hour'"``), then
    return its id. Mirrors the smoke-suite seeding pattern.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_jobs(job_type, priority, source, session_id, "
            "payload_json, status, started_at, finished_at, error_message) "
            "VALUES (%s, 5, 'test', %s, '{}'::jsonb, 'failed', "
            f"{finished_at}, {finished_at}, %s) RETURNING id",
            (job_type, f"session_{job_type}", error_message),
        )
        return int(cur.fetchone()["id"])


def _seed_agent_job_null_finished(
    conn, *, job_type: str, error_message: str, started_at: str,
) -> int:
    """Insert one ``agent_jobs`` row with ``finished_at = NULL`` and a recent
    ``started_at`` offset string. Exercises the
    ``COALESCE(finished_at, started_at)`` time-window branch that the regular
    helper (which sets both columns) never reaches.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_jobs(job_type, priority, source, session_id, "
            "payload_json, status, started_at, finished_at, error_message) "
            "VALUES (%s, 5, 'test', %s, '{}'::jsonb, 'failed', "
            f"{started_at}, NULL, %s) RETURNING id",
            (job_type, f"session_{job_type}", error_message),
        )
        return int(cur.fetchone()["id"])



class TestPgRecentFailedJobsReleaseAuditP1_4:
    """P1-4: release-audit terminal records are excluded from
    ``recent_failed_jobs`` (no LIMIT-slot starvation), surfaced separately
    by ``recent_release_audit_jobs``, reclassified by
    ``_split_current_and_legacy_failed_jobs`` into a separate archived
    count, and rendered as a distinct archived-release-audit line. Original
    ``agent_jobs`` rows are preserved (no deletion)."""

    def test_release_audit_rows_excluded_from_recent_failed_jobs(self) -> None:
        """A real current failure + two release-audit rows (stale-release
        cleanup / stale_snapshot_discarded_before_release) + one old failure
        (>7d). ``recent_failed_jobs`` returns ONLY the real current failure
        — the release-audit rows do NOT occupy the LIMIT slot, so a current
        failure is never starved out, and the old failure is excluded by the
        7-day window.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            with conn.transaction():
                _seed_agent_job(
                    conn, job_type="release_cleanup_stale",
                    error_message="stale-release cleanup",
                    finished_at="NOW() - INTERVAL '30 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="release_cleanup_snapshot",
                    error_message="stale_snapshot_discarded_before_release",
                    finished_at="NOW() - INTERVAL '45 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="real_current_failure",
                    error_message="recent failure",
                    finished_at="NOW() - INTERVAL '1 hour'",
                )
                _seed_agent_job(
                    conn, job_type="old_failure_outside_window",
                    error_message="old failure",
                    finished_at="NOW() - INTERVAL '8 days'",
                )
            recent = repo.recent_failed_jobs(limit=5)
            types = [r.get("job_type") for r in recent]
            # The real current failure MUST appear.
            assert "real_current_failure" in types, (
                "P1-4: a real current failure must appear in recent_failed_jobs"
            )
            # Release-audit rows MUST NOT appear (classified out, not
            # starved by LIMIT 5).
            assert "release_cleanup_stale" not in types, (
                "P1-4: stale-release cleanup must NOT appear in recent_failed_jobs "
                "— it is an archived release-audit record, not a current failure"
            )
            assert "release_cleanup_snapshot" not in types, (
                "P1-4: stale_snapshot_discarded_before_release must NOT appear in "
                "recent_failed_jobs — it is an archived release-audit record"
            )
            # Old failure excluded by the 7-day window (unchanged behavior).
            assert "old_failure_outside_window" not in types
        finally:
            handle.close()

    def test_release_audit_rows_starve_protection_limit(self) -> None:
        """P1-4 slot-starvation guard: with 3 recent release-audit rows and
        1 real current failure, a naive ``LIMIT 3`` ordered by ``id DESC``
        would return ONLY the 3 release-audit rows (starving the current
        failure out). The SQL-layer exclusion guarantees the current
        failure appears even under a tight LIMIT.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            with conn.transaction():
                _seed_agent_job(
                    conn, job_type="real_current_failure_tight",
                    error_message="recent failure",
                    finished_at="NOW() - INTERVAL '10 minutes'",
                )
                for i in range(3):
                    _seed_agent_job(
                        conn, job_type=f"release_audit_{i}",
                        error_message="stale-release cleanup",
                        finished_at="NOW() - INTERVAL '5 minutes'",
                    )
            recent = repo.recent_failed_jobs(limit=3)
            types = [r.get("job_type") for r in recent]
            assert "real_current_failure_tight" in types, (
                "P1-4: the real current failure must appear even under "
                "LIMIT 3 alongside 3 release-audit rows — the SQL-layer "
                "exclusion prevents release-audit rows from starving the slot"
            )
            assert all("release_audit_" not in t for t in types), (
                "P1-4: no release-audit row may occupy the LIMIT slot"
            )
        finally:
            handle.close()

    def test_recent_release_audit_jobs_surfaces_archived_rows(self) -> None:
        """``recent_release_audit_jobs`` surfaces ONLY the release-audit
        rows within the 7-day window, so the report can render a separate
        archived-release-audit count line. Old (>7d) release-audit rows are
        excluded by the window.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            with conn.transaction():
                _seed_agent_job(
                    conn, job_type="release_cleanup_stale",
                    error_message="stale-release cleanup",
                    finished_at="NOW() - INTERVAL '30 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="release_cleanup_snapshot",
                    error_message="stale_snapshot_discarded_before_release",
                    finished_at="NOW() - INTERVAL '45 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="real_current_failure",
                    error_message="recent failure",
                    finished_at="NOW() - INTERVAL '1 hour'",
                )
                _seed_agent_job(
                    conn, job_type="old_release_audit_outside_window",
                    error_message="stale-release cleanup",
                    finished_at="NOW() - INTERVAL '8 days'",
                )
            audit_rows = repo.recent_release_audit_jobs()
            types = [r.get("job_type") for r in audit_rows]
            assert "release_cleanup_stale" in types
            assert "release_cleanup_snapshot" in types
            assert "real_current_failure" not in types, (
                "P1-4: recent_release_audit_jobs must NOT surface a non-audit failure"
            )
            assert "old_release_audit_outside_window" not in types, (
                "P1-4: an 8-day-old release-audit row must be excluded by the 7-day window"
            )
        finally:
            handle.close()

    def test_release_audit_row_null_finished_at_windowed_by_started_at(self) -> None:
        """Reviewer evidence-gap #1: a release-audit row with
        ``finished_at = NULL`` and a recent ``started_at`` is still subject
        to the ``COALESCE(finished_at, started_at)`` time window. It must be
        EXCLUDED from ``recent_failed_jobs`` AND INCLUDED in
        ``recent_release_audit_jobs`` (the combined NULL-finished_at +
        audit-signature predicate, which the regular helper that always sets
        finished_at never reaches).
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            with conn.transaction():
                # Release-audit row, NULL finished_at, recent started_at -> in window.
                _seed_agent_job_null_finished(
                    conn, job_type="release_audit_null_finished",
                    error_message="stale-release cleanup",
                    started_at="NOW() - INTERVAL '20 minutes'",
                )
                # Old release-audit row, NULL finished_at, old started_at -> out of window.
                _seed_agent_job_null_finished(
                    conn, job_type="release_audit_null_finished_old",
                    error_message="stale_snapshot_discarded_before_release",
                    started_at="NOW() - INTERVAL '8 days'",
                )
                # A real current failure for contrast (both columns set).
                _seed_agent_job(
                    conn, job_type="real_current_failure",
                    error_message="recent failure",
                    finished_at="NOW() - INTERVAL '1 hour'",
                )
            recent = repo.recent_failed_jobs(limit=5)
            recent_types = [r.get("job_type") for r in recent]
            assert "release_audit_null_finished" not in recent_types, (
                "P1-4: a NULL-finished_at release-audit row must NOT appear in "
                "recent_failed_jobs — the EXISTS signature clause excludes it "
                "regardless of the COALESCE window branch"
            )
            assert "release_audit_null_finished_old" not in recent_types
            assert "real_current_failure" in recent_types, (
                "P1-4: the real current failure must still surface"
            )
            audit_rows = repo.recent_release_audit_jobs()
            audit_types = [r.get("job_type") for r in audit_rows]
            assert "release_audit_null_finished" in audit_types, (
                "P1-4: a NULL-finished_at release-audit row with a recent "
                "started_at must be surfaced by recent_release_audit_jobs — "
                "COALESCE(finished_at, started_at) keeps it inside the 7-day window"
            )
            assert "release_audit_null_finished_old" not in audit_types, (
                "P1-4: an 8-day-old NULL-finished_at release-audit row must be "
                "excluded by the COALESCE window"
            )
            assert "real_current_failure" not in audit_types, (
                "P1-4: recent_release_audit_jobs must NOT surface a non-audit failure"
            )
        finally:
            handle.close()

    def test_classifier_three_way_split(self) -> None:
        """``_split_current_and_legacy_failed_jobs`` returns a 3-tuple
        ``(current_jobs, legacy_schema_fail_count, release_audit_count)``.
        Given a mix of current failure + release-audit rows, ``current_jobs``
        contains ONLY the current failure and ``release_audit_count`` is the
        number of release-audit rows.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            with conn.transaction():
                _seed_agent_job(
                    conn, job_type="release_cleanup_stale",
                    error_message="stale-release cleanup",
                    finished_at="NOW() - INTERVAL '30 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="release_cleanup_snapshot",
                    error_message="stale_snapshot_discarded_before_release",
                    finished_at="NOW() - INTERVAL '45 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="real_current_failure",
                    error_message="recent failure",
                    finished_at="NOW() - INTERVAL '1 hour'",
                )
            # Producer fetches current failures (SQL-excludes release-audit)
            # then appends the release-audit rows so the shared classifier
            # counts them — exactly the production path in build_hourly_report.
            current = repo.recent_failed_jobs(limit=5)
            audit = repo.recent_release_audit_jobs()
            failed_jobs = current + audit
            cur_jobs, legacy_count, release_audit_count = (
                _split_current_and_legacy_failed_jobs(failed_jobs)
            )
            assert len(cur_jobs) == 1, (
                f"P1-4: current_jobs must contain only the real current failure; "
                f"got {len(cur_jobs)}"
            )
            assert cur_jobs[0]["job_type"] == "real_current_failure"
            assert legacy_count == 0
            assert release_audit_count == 2, (
                f"P1-4: release_audit_count must be 2; got {release_audit_count}"
            )
        finally:
            handle.close()

    def test_is_release_audit_job_and_signatures(self) -> None:
        """``_is_release_audit_job`` matches both release-audit signatures
        and the shared signature tuple is the canonical terminal release-
        audit message set. A non-audit failure returns False.

        P2-1 (07-27) requirement E: the set was extended from two to four
        signatures — the two original (stale-release cleanup /
        stale_snapshot_discarded_before_release) plus the two postfix-restart
        signatures (stale_batch_discarded_before_postfix_restart /
        stale_maintenance_job_discarded_before_postfix_restart). See
        ``test_pg_postfix_restart_release_audit_p2_1.py`` for the postfix-
        restart coverage.
        """
        assert _is_release_audit_job({"error_message": "stale-release cleanup"}) is True
        assert _is_release_audit_job(
            {"error_message": "stale_snapshot_discarded_before_release"}
        ) is True
        # substring match within a longer message still matches.
        assert _is_release_audit_job(
            {"error_message": "R3 stale-release cleanup: discarded 3 snapshots"}
        ) is True
        # A real failure does NOT match.
        assert _is_release_audit_job({"error_message": "recent failure"}) is False
        assert _is_release_audit_job({"error_message": ""}) is False
        assert _is_release_audit_job({}) is False
        assert set(_RELEASE_AUDIT_SIGNATURES) == {
            "stale-release cleanup",
            "stale_snapshot_discarded_before_release",
            "stale_batch_discarded_before_postfix_restart",
            "stale_maintenance_job_discarded_before_postfix_restart",
        }, (
            "P1-4 + P2-1: the release-audit signature tuple must be exactly "
            "the four terminal release-audit messages (two original + two "
            "postfix-restart per requirement E)"
        )

    def test_original_agent_jobs_rows_preserved_no_deletion(self) -> None:
        """P1-4 boundary: classification is report-view only. The original
        ``agent_jobs`` rows are NEVER deleted — after fetching both lists,
        all seeded rows still exist in the table (verified by direct
        count). No production history is erased.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            with conn.transaction():
                _seed_agent_job(
                    conn, job_type="release_cleanup_stale",
                    error_message="stale-release cleanup",
                    finished_at="NOW() - INTERVAL '30 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="release_cleanup_snapshot",
                    error_message="stale_snapshot_discarded_before_release",
                    finished_at="NOW() - INTERVAL '45 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="real_current_failure",
                    error_message="recent failure",
                    finished_at="NOW() - INTERVAL '1 hour'",
                )
            _ = repo.recent_failed_jobs(limit=5)
            _ = repo.recent_release_audit_jobs()
            _ = _split_current_and_legacy_failed_jobs(
                repo.recent_failed_jobs(limit=5) + repo.recent_release_audit_jobs()
            )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM agent_jobs WHERE status='failed'"
                )
                total = int(cur.fetchone()["c"])
            assert total == 3, (
                f"P1-4: all 3 agent_jobs rows must be preserved (no deletion); "
                f"found {total}"
            )
        finally:
            handle.close()

    def test_rendered_archived_release_audit_line_present(self) -> None:
        """The rendered report must distinguish current failures from
        archived release audit. The hourly_report source must contain the
        distinct archived-release-audit line string (mirrors the P1-10
        source-inspection pattern). This pins the report expression so a
        future refactor cannot silently merge it back into the current-
        failures list.
        """
        from plugins.crypto_guard.notify import hourly_report
        src = inspect.getsource(hourly_report)
        assert "发布清理审计记录已归档" in src, (
            "P1-4: report must render a distinct archived-release-audit line "
            "('另有 N 个发布清理审计记录已归档') distinguishing current failures "
            "from archived release audit"
        )
        assert "stale_snapshot_discarded_before_release" in src, (
            "P1-4: the archived-release-audit line must name both terminal "
            "signatures so the operator can identify the release-housekeeping action"
        )
        assert "不计入当前风险事件" in src, (
            "P1-4: the archived-release-audit line must explicitly state these "
            "are NOT counted as current risk events"
        )