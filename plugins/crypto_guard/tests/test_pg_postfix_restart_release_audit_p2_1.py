# -*- coding: utf-8 -*-
"""终审返工 Phase-2 P2-1 (2026-07-27): release-cleanup audit classification —
two postfix-restart terminal signatures (requirement E verbatim):

  "agent_jobs IDs 167-174 release-cleanup rows occupying recent-failed."

Production read-only evidence (phase2_step_a_evidence_probe.py) confirmed
agent_jobs IDs 167-174 are release-cleanup rows surfacing in
``recent_failed_jobs(limit=5, days=7)`` and re-triggering the hourly
risk-events line every hour. P1-4 (07-22) already classified the two
``stale-release cleanup`` / ``stale_snapshot_discarded_before_release``
signatures. The 07-27 release introduced TWO NEW postfix-restart release-
audit signatures that are NOT yet in
``RELEASE_AUDIT_ERROR_SIGNATURES``:

  - ``stale_batch_discarded_before_postfix_restart``
  - ``stale_maintenance_job_discarded_before_postfix_restart``

Requirement E verbatim: "Release cleanup audit classification
(storage/repository.py): add
``stale_batch_discarded_before_postfix_restart`` +
``stale_maintenance_job_discarded_before_postfix_restart`` to canonical
``RELEASE_AUDIT_ERROR_SIGNATURES``; ``recent_failed_jobs`` excludes BEFORE
LIMIT; ``recent_release_audit_jobs`` still queryable; no production row
deletion/modification."

This test drives the REAL producer→consumer chain on isolated PG:
``recent_failed_jobs`` / ``recent_release_audit_jobs`` (real repository) →
``_is_release_audit_job`` / ``_split_current_and_legacy_failed_jobs`` (real
hourly_report classifier). No mocks of the functions under test.

Isolated PostgreSQL fixture only. No production DB mutation, no service
restart, no commit/push/finish-work.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.notify.hourly_report import (
    _is_release_audit_job,
    _RELEASE_AUDIT_SIGNATURES,
    _split_current_and_legacy_failed_jobs,
)


# The two NEW postfix-restart signatures requirement E adds.
POSTFIX_RESTART_SIGS = (
    "stale_batch_discarded_before_postfix_restart",
    "stale_maintenance_job_discarded_before_postfix_restart",
)


def _seed_agent_job(
    conn, *, job_type: str, error_message: str, finished_at: str,
) -> int:
    """Insert one ``agent_jobs`` row with the given error_message and
    finished_at offset string, then return its id. Mirrors the P1-4 helper.
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


class TestPgPostfixRestartReleaseAuditP2_1:
    """Requirement E: the two postfix-restart release-audit signatures are
    classified the same as the existing two release-audit signatures —
    excluded from ``recent_failed_jobs`` BEFORE the LIMIT, queryable via
    ``recent_release_audit_jobs``, matched by ``_is_release_audit_job``, and
    counted into the archived-release-audit line. No production row
    deletion/modification."""

    def test_postfix_restart_sigs_in_canonical_signature_tuple(self) -> None:
        """RED→GREEN: both postfix-restart signatures MUST be in the
        canonical ``RELEASE_AUDIT_ERROR_SIGNATURES`` tuple (the single
        source of truth) and in the hourly_report classifier's
        ``_RELEASE_AUDIT_SIGNATURES``.
        """
        sigs_repo = set(CryptoGuardRepository.RELEASE_AUDIT_ERROR_SIGNATURES)
        sigs_report = set(_RELEASE_AUDIT_SIGNATURES)
        for sig in POSTFIX_RESTART_SIGS:
            assert sig in sigs_repo, (
                f"GREEN: {sig!r} must be in "
                f"RELEASE_AUDIT_ERROR_SIGNATURES (requirement E); got "
                f"{sorted(sigs_repo)}"
            )
            assert sig in sigs_report, (
                f"GREEN: {sig!r} must be in hourly_report "
                f"_RELEASE_AUDIT_SIGNATURES (lock-step with repository); "
                f"got {sorted(sigs_report)}"
            )

    def test_is_release_audit_job_matches_postfix_restart_sigs(self) -> None:
        """RED→GREEN: ``_is_release_audit_job`` classifies a row whose
        error_message contains a postfix-restart signature as a release-
        audit job (substring match, like the existing two signatures).
        """
        for sig in POSTFIX_RESTART_SIGS:
            assert _is_release_audit_job({"error_message": sig}) is True, (
                f"GREEN: _is_release_audit_job must match {sig!r} (req E)"
            )
            # substring within a longer message still matches.
            assert _is_release_audit_job({
                "error_message": f"R3 postfix cleanup: {sig}: discarded 1 batch"
            }) is True, (
                f"GREEN: _is_release_audit_job substring match must work for "
                f"{sig!r}"
            )
        # A real failure does NOT match.
        assert _is_release_audit_job({"error_message": "recent failure"}) is False
        assert _is_release_audit_job({"error_message": ""}) is False
        assert _is_release_audit_job({}) is False

    def test_postfix_restart_rows_excluded_from_recent_failed_jobs_no_starvation(
        self,
    ) -> None:
        """RED→GREEN: two postfix-restart release-audit rows + one real
        current failure. ``recent_failed_jobs(limit=5)`` returns ONLY the
        real current failure — the postfix-restart rows do NOT occupy the
        LIMIT slot (excluded BEFORE LIMIT), so a current failure is never
        starved out. This is the symptom #4 fix: IDs 167-174 were occupying
        the recent-failed list.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            with conn.transaction():
                _seed_agent_job(
                    conn, job_type="postfix_batch_cleanup",
                    error_message="stale_batch_discarded_before_postfix_restart",
                    finished_at="NOW() - INTERVAL '30 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="postfix_maint_cleanup",
                    error_message="stale_maintenance_job_discarded_before_postfix_restart",
                    finished_at="NOW() - INTERVAL '45 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="real_current_failure",
                    error_message="recent failure: daily_review crashed",
                    finished_at="NOW() - INTERVAL '1 hour'",
                )
            with conn.transaction():
                rows = repo.recent_failed_jobs(limit=5, days=7)
            types = {r["job_type"] for r in rows}
            assert "real_current_failure" in types, (
                "GREEN: the real current failure must appear in "
                "recent_failed_jobs (req E — current failure not starved)"
            )
            assert "postfix_batch_cleanup" not in types, (
                "GREEN: stale_batch_discarded_before_postfix_restart must NOT "
                "appear in recent_failed_jobs — it is an archived release-audit "
                "record (req E, symptom #4)"
            )
            assert "postfix_maint_cleanup" not in types, (
                "GREEN: stale_maintenance_job_discarded_before_postfix_restart "
                "must NOT appear in recent_failed_jobs (req E, symptom #4)"
            )
        finally:
            handle.close()

    def test_postfix_restart_rows_queryable_via_recent_release_audit_jobs(
        self,
    ) -> None:
        """RED→GREEN: the two postfix-restart rows ARE queryable via
        ``recent_release_audit_jobs`` (requirement E: "still queryable"). The
        original ``agent_jobs`` rows are NEVER deleted — classification is
        report-view only.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            with conn.transaction():
                id1 = _seed_agent_job(
                    conn, job_type="postfix_batch_cleanup",
                    error_message="stale_batch_discarded_before_postfix_restart",
                    finished_at="NOW() - INTERVAL '30 minutes'",
                )
                id2 = _seed_agent_job(
                    conn, job_type="postfix_maint_cleanup",
                    error_message="stale_maintenance_job_discarded_before_postfix_restart",
                    finished_at="NOW() - INTERVAL '45 minutes'",
                )
            with conn.transaction():
                rows = repo.recent_release_audit_jobs(days=7)
            msgs = {r["error_message"] for r in rows}
            assert "stale_batch_discarded_before_postfix_restart" in msgs, (
                "GREEN: postfix-restart rows must be queryable via "
                "recent_release_audit_jobs (req E)"
            )
            assert "stale_maintenance_job_discarded_before_postfix_restart" in msgs, (
                "GREEN: postfix-restart rows must be queryable via "
                "recent_release_audit_jobs (req E)"
            )
            # Original rows preserved (no deletion): both ids still exist.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS c FROM agent_jobs WHERE id = ANY(%s)",
                    ([id1, id2],),
                )
                assert int(cur.fetchone()["c"]) == 2, (
                    "GREEN: original agent_jobs rows preserved (req E — no "
                    "production row deletion/modification)"
                )
        finally:
            handle.close()

    def test_split_classifies_postfix_restart_into_release_audit_count(self) -> None:
        """RED→GREEN: ``_split_current_and_legacy_failed_jobs`` (the hourly
        report classifier) routes postfix-restart rows into the
        ``release_audit_count`` (the archived line), NOT into
        ``current_jobs`` (the current risk-events list). This is the report-
        view side of requirement E.
        """
        handle = make_repo()
        try:
            conn = handle.conn
            repo = handle.repo
            with conn.transaction():
                _seed_agent_job(
                    conn, job_type="postfix_batch_cleanup",
                    error_message="stale_batch_discarded_before_postfix_restart",
                    finished_at="NOW() - INTERVAL '30 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="postfix_maint_cleanup",
                    error_message="stale_maintenance_job_discarded_before_postfix_restart",
                    finished_at="NOW() - INTERVAL '45 minutes'",
                )
                _seed_agent_job(
                    conn, job_type="real_current_failure",
                    error_message="recent failure",
                    finished_at="NOW() - INTERVAL '1 hour'",
                )
            with conn.transaction():
                failed_jobs = repo.recent_failed_jobs(limit=20, days=7)
                # NOTE: recent_failed_jobs EXCLUDES release-audit rows, so
                # failed_jobs here is ONLY the real current failure. The
                # classifier is fed the union via recent_release_audit_jobs
                # in production; here we feed it both lists to exercise the
                # 3-way split.
                audit_jobs = repo.recent_release_audit_jobs(days=7)
                combined = list(failed_jobs) + list(audit_jobs)
            current_jobs, legacy_count, release_audit_count = (
                _split_current_and_legacy_failed_jobs(combined)
            )
            current_types = {j["job_type"] for j in current_jobs}
            assert "real_current_failure" in current_types, (
                "GREEN: real current failure routed to current_jobs"
            )
            assert "postfix_batch_cleanup" not in current_types, (
                "GREEN: postfix-restart rows must NOT be in current_jobs "
                "(req E — not a current risk event)"
            )
            assert "postfix_maint_cleanup" not in current_types, (
                "GREEN: postfix-restart rows must NOT be in current_jobs (req E)"
            )
            assert release_audit_count >= 2, (
                f"GREEN: both postfix-restart rows must be counted in "
                f"release_audit_count (the archived-release-audit line); "
                f"got {release_audit_count}"
            )
        finally:
            handle.close()