# -*- coding: utf-8 -*-
"""08-12 P1 RED contract: durable once-ever daily-review push.

final-seal.md INVALIDATED_BY_OPEN_P1 record (2026-08-12). The daily
simulated-review producer pushed DUPLICATE Feishu messages: the external send
succeeded, then the in-transaction ``pushed_to_feishu`` update failed on a
PostgreSQL BOOLEAN datatype mismatch (``DatatypeMismatch`` — a Python int 1
bound against a BOOLEAN column, run_ga_workers.py:1722-1726), the whole
implicit business transaction rolled back (pg_db.get_conn INERROR rollback),
so the outbox/report "sent" state was rolled back while the external send was
not. The scheduler retried and sent again (production logs show >= 14
restarts of the same daily session).

The 8-point durable once-ever contract under test:

  1. external send + later finalize/business-txn failure -> the sender is
     still invoked exactly ONCE;
  2. re-enqueue of the same review_date -> never a second external send;
  3. two dispatchers claiming concurrently -> exactly one winner;
  4. a sent row is never reclaimable after a restart, a re-enqueue of its key
     returns the original row id (never a new pending row), and a stale
     ``sending`` row (crashed dispatcher) is reclaimed fail-closed to
     terminal ``failed`` WITHOUT re-sending;
  5. bounded retry of a failed pending row stays on ONE row and never exceeds
     max_attempts external attempts;
  6. pushed_to_feishu is written as a REAL PostgreSQL BOOLEAN (an int 1 write
     raises DatatypeMismatch — the production defect line);
  7. alert_outbox / pushed_to_feishu state inconsistency diagnostic;
  8. the ``daily_review:`` dedupe key is unique ACROSS all states (expression
     partial unique index) while non-daily_review keys keep their existing
     sent-history semantics.

RED-first: every test fails on the current tree (no cross-state index, no
atomic claim, no explicit finalize, no sending reclaim, no diagnostic, no
enqueue status seam). No production DB mutation, no marker write, no service
restart, no commit.
"""
from __future__ import annotations

from typing import Callable

import psycopg
import pytest
from psycopg import errors as pg_errors

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.notify.alert_delivery import (
    _deliver_alert,
    process_alert_outbox,
    send_markdown_alert,
)
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests import pg_fixtures as fx

REVIEW_DATE = "2026-08-11"
DEDUPE_KEY = f"daily_review:{REVIEW_DATE}"
PAYLOAD = {
    "receive_id": "chat_1",
    "receive_id_type": "chat_id",
    "msg_type": "interactive",
    "content": "{}",
    "fallback_text": "每日复盘",
}


def _ok_send(calls: list) -> Callable[..., bool]:
    def send(*args: object, **kwargs: object) -> bool:
        calls.append((args, kwargs))
        return True

    return send


def _fail_send(calls: list) -> Callable[..., bool]:
    def send(*args: object, **kwargs: object) -> bool:
        calls.append((args, kwargs))
        raise RuntimeError("feishu down")

    return send


class TestDailyReviewOnceEverPush:
    """8 real-PostgreSQL tests for the durable once-ever daily-review push."""

    def test_external_send_survives_later_txn_failure_sender_once(self) -> None:
        """Contract 1: the external send succeeded; a later finalize /
        business-txn failure must NOT trigger a second send."""
        h = fx.make_repo()
        try:
            repo = h.repo
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True}, ga_report="text"
            )
            calls: list = []
            alert_id = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            # Dispatcher boundary: the enqueue INSERT is committed before the
            # outbox cycle (the fixed implementation commits each claim before
            # the external send). The defect under test is the post-send
            # finalize/business-txn failure — NOT an uncommitted INSERT.
            repo.conn.commit()
            first = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert first["sent"] == 1
            assert len(calls) == 1
            # Simulate the post-send finalize/business-txn failure: the exact
            # production defect line (Python int 1 -> BOOLEAN column) then a
            # pg_db.get_conn-style INERROR rollback.
            with pytest.raises(pg_errors.DatatypeMismatch):
                repo.conn.execute(
                    "UPDATE daily_review_reports SET pushed_to_feishu=%s WHERE review_date=%s",
                    (1, REVIEW_DATE),
                )
            repo.conn.rollback()
            # Scheduler retry: the row must not be sendable again.
            second = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert second["processed"] == 0
            assert len(calls) == 1  # once-ever: exactly one external send
            row = repo.conn.execute(
                "SELECT status FROM alert_outbox WHERE id=%s", (alert_id,)
            ).fetchone()
            assert row["status"] == "sent"
        finally:
            h.close()

    def test_same_review_date_reenqueue_never_sends_twice(self) -> None:
        """Contract 2: re-enqueue of the same review_date (scheduler restart /
        re-dispatch) must never produce a second external send.

        Reviewer Recommended-1: the once-ever contract holds only through the
        outbox QUEUE (claim -> send -> finalize). A direct
        send_markdown_alert(send_message=...) bypasses the claim entirely, so
        this test drives delivery through process_alert_outbox (the production
        path); the second dispatch is the producer path (send_message=None),
        which must dedupe without sending."""
        h = fx.make_repo()
        try:
            repo = h.repo
            calls: list = []
            text = "每日复盘文本"
            # delivery happens ONLY through the outbox queue
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY,
            )
            repo.conn.commit()  # dispatcher txn boundary (claim is committed)
            first = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert first["sent"] == 1
            assert len(calls) == 1
            # Producer re-dispatch (scheduler restart / re-run of the same
            # review_date): send_message=None + the SAME dedupe_key. A
            # different symbol keeps the silence/lock seams out of the way;
            # the dedupe_key (the once-ever identity) is identical.
            second = send_markdown_alert(
                repo,
                None,
                receive_id="chat_2",
                receive_id_type="chat_id",
                text=text,
                alert_type="daily_review",
                priority=5,
                symbol="ETHUSDT",
                dedupe_key=DEDUPE_KEY,
            )
            assert second["deduped"] is True
            assert second["sent"] is False
            assert len(calls) == 1  # never a second external send
        finally:
            h.close()

    def test_concurrent_claim_single_winner(self) -> None:
        """Contract 3: two dispatchers claiming concurrently -> exactly one
        winner; the loser sees no pending row and sends nothing."""
        h = fx.make_repo()
        try:
            repo = h.repo
            alert_id = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.commit()  # dispatcher txn boundary (claim is committed)
            calls: list = []
            conn2 = fx.direct_conn(h.schema)
            try:
                repo2 = CryptoGuardRepository(conn2)
                rows1 = repo.claim_pending_alerts(limit=10)
                rows2 = repo2.claim_pending_alerts(limit=10)
                assert len(rows1) == 1
                assert rows2 == []  # exactly one winner
                for row in rows1:
                    _deliver_alert(repo, int(row["id"]), row["payload_json"], _ok_send(calls))
            finally:
                conn2.close()
            assert len(calls) == 1
            row = repo.conn.execute(
                "SELECT status FROM alert_outbox WHERE id=%s", (alert_id,)
            ).fetchone()
            assert row["status"] in ("sent", "sending")
        finally:
            h.close()

    def test_sent_never_reclaimed_after_restart_and_stale_sending_reclaimed(self) -> None:
        """Contract 4: a sent row is never reclaimable after a restart; a
        re-enqueue of its key returns the original row id; a stale ``sending``
        row (crashed dispatcher) is reclaimed fail-closed to terminal failed
        WITHOUT any external send."""
        h = fx.make_repo()
        try:
            repo = h.repo
            calls: list = []
            alert_id = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            result = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert result["sent"] == 1
            assert len(calls) == 1
            # Dispatcher txn boundary: the outbox cycle committed the send;
            # a restart/re-enqueue happens on a committed snapshot (without
            # this the uncommitted INSERT would deadlock a second connection
            # on the same dedupe_key — psycopg3 savepoint-in-implicit-txn).
            repo.conn.commit()
            conn2 = fx.direct_conn(h.schema)
            try:
                repo2 = CryptoGuardRepository(conn2)
                # restart: sent row is not pending anymore
                assert repo2.claim_pending_alerts(limit=10) == []
                # re-enqueue of the same key -> the ORIGINAL id, never a new row
                again = repo2.enqueue_alert(
                    alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
                )
                assert again == alert_id
                assert repo2.claim_pending_alerts(limit=10) == []
            finally:
                conn2.close()
            assert len(calls) == 1
            # crashed dispatcher residue: sending + stale -> fail-closed reclaim
            repo.conn.execute(
                "UPDATE alert_outbox SET status='sending', "
                "updated_at=NOW() - interval '16 minutes' WHERE id=%s",
                (alert_id,),
            )
            repo.conn.commit()
            crash_calls: list = []
            again2 = process_alert_outbox(repo, _ok_send(crash_calls), limit=10)
            assert again2["processed"] == 0
            assert crash_calls == []  # never re-send a reclaimed row
            row = repo.conn.execute(
                "SELECT status FROM alert_outbox WHERE id=%s", (alert_id,)
            ).fetchone()
            assert row["status"] == "failed"
        finally:
            h.close()

    def test_bounded_retry_single_row_never_breaks_once_ever(self) -> None:
        """Contract 5: a failed pending row retries per the explicit policy
        (max_attempts, backoff) on the SAME row; two dispatchers racing the
        retry must not multiply external attempts."""
        h = fx.make_repo()
        try:
            repo = h.repo
            alert_id = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.commit()
            calls: list = []
            conn2 = fx.direct_conn(h.schema)
            try:
                repo2 = CryptoGuardRepository(conn2)
                for _attempt in range(3):
                    repo.conn.commit()
                    rows1 = repo.claim_pending_alerts(limit=10)
                    rows2 = repo2.claim_pending_alerts(limit=10)
                    for row in rows1:
                        _deliver_alert(repo, int(row["id"]), row["payload_json"], _fail_send(calls))
                        # finalize = its own short transaction (dispatcher
                        # boundary); without it the uncommitted failure-flag
                        # UPDATE deadlocks the racing second connection.
                        repo.conn.commit()
                    for row in rows2:
                        _deliver_alert(repo2, int(row["id"]), row["payload_json"], _fail_send(calls))
                        repo2.conn.commit()
                    repo.conn.commit()
                    # push the backoff window open for the next attempt
                    repo.conn.execute(
                        "UPDATE alert_outbox SET next_retry_at=CURRENT_TIMESTAMP WHERE id=%s",
                        (alert_id,),
                    )
                    repo.conn.commit()
            finally:
                conn2.close()
            rows = repo.conn.execute(
                "SELECT status, retry_count FROM alert_outbox WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            ).fetchall()
            assert len(rows) == 1  # once-ever: retries stay on ONE row
            assert rows[0]["status"] == "failed"
            assert rows[0]["retry_count"] == 3
            assert len(calls) == 3  # bounded: one external attempt per retry
            assert repo.claim_pending_alerts(limit=10) == []
        finally:
            h.close()

    def test_pushed_to_feishu_real_boolean_write(self) -> None:
        """Contract 6: pushed_to_feishu is written as a REAL PostgreSQL
        BOOLEAN. The production defect line (int 1) raises DatatypeMismatch;
        the once-ever finalize writes TRUE and the row reads back as bool."""
        h = fx.make_repo()
        try:
            repo = h.repo
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True}, ga_report="text"
            )
            # production defect line: int 1 against a BOOLEAN column
            with pytest.raises(pg_errors.DatatypeMismatch):
                repo.conn.execute(
                    "UPDATE daily_review_reports SET pushed_to_feishu=%s WHERE review_date=%s",
                    (1, REVIEW_DATE),
                )
            repo.conn.rollback()
            calls: list = []
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            result = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert result["sent"] == 1
            row = repo.conn.execute(
                "SELECT pushed_to_feishu FROM daily_review_reports WHERE review_date=%s",
                (REVIEW_DATE,),
            ).fetchone()
            assert row["pushed_to_feishu"] is True
            assert len(calls) == 1
        finally:
            h.close()

    def test_outbox_pushed_marker_inconsistency_diagnostic(self) -> None:
        """Contract 7: a diagnostic surfaces alert_outbox /
        pushed_to_feishu state inconsistencies in both directions."""
        h = fx.make_repo()
        try:
            repo = h.repo
            # orphan marker: report flagged pushed but no outbox send evidence
            repo.save_daily_review_report(
                review_date="2026-08-10", summary={"ok": True}, ga_report="t"
            )
            repo.conn.execute(
                "UPDATE daily_review_reports SET pushed_to_feishu=TRUE WHERE review_date='2026-08-10'"
            )
            # marker missing: outbox row sent but report not flagged
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True}, ga_report="t"
            )
            sent_id = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.mark_alert_sent(sent_id)
            repo.conn.commit()
            diag = repo.diagnose_daily_review_push_consistency()
            assert diag["ok"] is True
            kinds = {d["kind"] for d in diag["inconsistencies"]}
            assert "orphan_marker" in kinds
            assert "marker_missing" in kinds
        finally:
            h.close()

    def test_daily_review_dedupe_key_unique_across_states(self) -> None:
        """Contract 8: the ``daily_review:`` dedupe key is unique across ALL
        states (sent + pending collide), while non-daily_review keys keep
        their sent-history semantics (sent + new pending coexist)."""
        h = fx.make_repo()
        try:
            repo = h.repo
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.execute(
                "UPDATE alert_outbox SET status='sent' WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            )
            repo.conn.commit()
            # cross-state unique index must reject the second row
            with pytest.raises(psycopg.Error):
                repo.conn.execute(
                    "INSERT INTO alert_outbox(alert_type, symbol, priority, "
                    "payload_json, next_retry_at, dedupe_key) "
                    "VALUES ('daily_review', NULL, 5, %s, NOW(), %s)",
                    ('{"a":1}', DEDUPE_KEY),
                )
            repo.conn.rollback()
            # control: non-daily_review keys keep their existing semantics
            other = "BTCUSDT:trend_alert"
            repo.enqueue_alert(
                alert_type="trend_alert", symbol="BTCUSDT",
                payload=PAYLOAD, dedupe_key=other,
            )
            repo.conn.execute(
                "UPDATE alert_outbox SET status='sent' WHERE dedupe_key=%s", (other,)
            )
            repo.conn.commit()
            repo.enqueue_alert(
                alert_type="trend_alert", symbol="BTCUSDT",
                payload=PAYLOAD, dedupe_key=other,
            )
            count = repo.conn.execute(
                "SELECT COUNT(*) AS c FROM alert_outbox WHERE dedupe_key=%s", (other,)
            ).fetchone()["c"]
            assert count == 2  # sent history + fresh pending row
        finally:
            h.close()

    # ---- reviewer-finding repairs (08-12) ---------------------------------
    # Four RED-first tests for the independent reviewer's findings on the
    # original 8-point fix: P0-1 (production producer must only enqueue, never
    # inline-send inside the run_once business transaction), P1-1 (a send that
    # SUCCEEDED externally but whose finalize failed must fail closed to
    # terminal 'failed', never recycle to 'pending' for a second external
    # send), P2-1 (INSERT-conflict re-read for a daily_review:<date> key must
    # look across ALL states, not only 'pending'), P2-2 (the push-consistency
    # diagnostic must be registered inside diagnose_state_consistency).

    def test_daily_review_producer_enqueues_only_never_inline_sends(self, monkeypatch) -> None:
        """Reviewer P0-1: the production producer (process_job daily_review
        branch) must ONLY persist the outbox intent — process_job must NEVER
        invoke send_message inside the run_once business transaction (a
        post-send failure would roll the whole implicit transaction back and
        the scheduler would re-run the job and send again)."""
        import json as _json

        import plugins.crypto_guard.run_ga_workers as workers

        h = fx.make_repo()
        try:
            repo = h.repo
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True}, ga_report="text"
            )
            monkeypatch.setattr(
                workers, "resolve_report_target",
                lambda repo, payload: {"receive_id": "chat_1", "receive_id_type": "chat_id"},
            )
            monkeypatch.setattr(
                workers, "_build_evolution_status_text", lambda repo: "evolution-status"
            )
            send_spy: list = []
            job = {
                "id": 9_000_001,
                "job_type": "daily_review",
                "payload_json": _json.dumps(
                    {"day_utc": "2026-08-11", "loss_count": 0}
                ),
                "priority": 7,
                "session_id": "test-t9",
            }
            result = workers.process_job(repo, job, send_message=_ok_send(send_spy))
            # producer NEVER invokes the external sender
            assert send_spy == []
            assert result["sent"] is False
            assert result["queued"] is True
            assert result["deduped"] is False
            # the outbox intent is durable on a SECOND connection (the fix
            # explicitly commits the enqueue before process_job returns)
            conn2 = fx.direct_conn(h.schema)
            try:
                row = conn2.execute(
                    "SELECT status FROM alert_outbox WHERE dedupe_key=%s",
                    (DEDUPE_KEY,),
                ).fetchone()
                assert row is not None
                assert row["status"] == "pending"
            finally:
                conn2.close()
        finally:
            h.close()

    def test_finalize_failure_after_send_never_retries(self, monkeypatch) -> None:
        """Reviewer P1-1: a send that SUCCEEDED externally but whose finalize
        (mark_alert_sent) failed must be fail-closed to terminal 'failed' with
        an alert_failure_log entry — never recycled to 'pending' for a second
        external send (the external side effect already happened)."""
        h = fx.make_repo()
        try:
            repo = h.repo
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.commit()
            calls: list = []

            def _finalize_boom(alert_id: int, *, daily_review_date: str | None = None) -> None:
                raise RuntimeError("finalize boom")

            monkeypatch.setattr(repo, "mark_alert_sent", _finalize_boom)
            first = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert first["failed"] == 1
            assert len(calls) == 1
            row = repo.conn.execute(
                "SELECT status, retry_count FROM alert_outbox WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            ).fetchone()
            assert row["status"] == "failed"  # terminal, never 'pending'
            assert int(row["retry_count"]) == 1
            # a second dispatcher cycle must not re-send the row
            second = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert second["processed"] == 0
            assert len(calls) == 1  # once-ever: exactly one external send
        finally:
            h.close()

    def test_concurrent_reenqueue_returns_winner_id_after_sent(self) -> None:
        """Reviewer P2-1: a concurrent enqueue of the same daily_review:<date>
        key that lands AFTER the winner's row is already 'sent' takes the
        INSERT-conflict path (not the pre-read); the loser must receive the
        original winner row id — never RuntimeError."""
        import threading
        import time as _time

        h = fx.make_repo()
        try:
            repo = h.repo
            # conn1: explicit transaction keeps the winner row uncommitted so
            # conn2's pre-read (READ COMMITTED) sees no row and reaches the
            # INSERT-conflict branch; the row commits as 'sent'.
            repo.conn.execute("BEGIN")
            alert_id = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.execute(
                "UPDATE alert_outbox SET status='sent' WHERE id=%s", (alert_id,)
            )
            conn2 = fx.direct_conn(h.schema)
            outcome: dict = {}
            try:
                repo2 = CryptoGuardRepository(conn2)

                def _race() -> None:
                    try:
                        outcome["id"] = repo2.enqueue_alert(
                            alert_type="daily_review",
                            payload=PAYLOAD,
                            dedupe_key=DEDUPE_KEY,
                        )
                    except Exception as exc:  # noqa: BLE001 - recorded below
                        outcome["exc"] = exc

                t = threading.Thread(target=_race, daemon=True)
                t.start()
                # conn2's cross-state pre-read (enqueue_alert's FIRST statement,
                # ~µs) must run BEFORE conn1 commits, otherwise the pre-read
                # itself resolves the key and the INSERT-conflict branch is
                # never exercised. Deterministic proof (reviewer Recommended-2,
                # no sleep-based guessing): poll pg_stat_activity until conn2's
                # backend is ACTIVE and WAITING ON A LOCK — i.e. its INSERT has
                # hit the unique index and is blocked on conn1's uncommitted
                # row. Only then release conn1. If the conflict branch were
                # removed (re-read that only sees pending rows), conn2 would
                # raise once the row commits as 'sent' — RED.
                deadline = _time.monotonic() + 15
                blocked = False
                while _time.monotonic() < deadline:
                    pid = conn2.info.backend_pid
                    state_row = repo.conn.execute(
                        "SELECT state, wait_event_type FROM pg_stat_activity "
                        "WHERE pid=%s",
                        (pid,),
                    ).fetchone()
                    if state_row and state_row["state"] == "active" and state_row["wait_event_type"] == "Lock":
                        blocked = True
                        break
                    _time.sleep(0.02)
                if not blocked:
                    raise AssertionError(
                        "conn2 never blocked on the unique index — the "
                        "INSERT-conflict branch was not reached"
                    )
                repo.conn.commit()  # winner row lands committed as 'sent'
                t.join(timeout=15)
                assert not t.is_alive()
                assert "exc" not in outcome, (
                    f"enqueue must not raise: {outcome.get('exc')!r}"
                )
                assert outcome.get("id") == alert_id
            finally:
                conn2.close()
        finally:
            h.close()

    def test_daily_review_push_diagnostic_registered_in_state_consistency(self) -> None:
        """Reviewer P2-2: the alert_outbox / pushed_to_feishu push-consistency
        diagnostic is registered inside the production
        diagnose_state_consistency aggregation (the entrypoint the hourly
        report calls)."""
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )

        h = fx.make_repo()
        try:
            repo = h.repo
            # marker_missing: a sent outbox row whose report is not flagged
            repo.save_daily_review_report(
                review_date="2026-08-09", summary={"ok": True}, ga_report="t"
            )
            sent_id = repo.enqueue_alert(
                alert_type="daily_review",
                payload=PAYLOAD,
                dedupe_key="daily_review:2026-08-09",
            )
            repo.mark_alert_sent(sent_id)
            repo.conn.commit()
            result = diagnose_state_consistency(repo)
            assert result["ok"] is True
            types = {issue["type"] for issue in result["issues"]}
            assert "daily_review_push_inconsistency" in types
        finally:
            h.close()

    # ---- Codex review round (08-12): at-most-once send semantics -----------
    # Codex P1-1: a daily_review external send whose outcome is UNKNOWN (the
    # provider accepted the request, then the client raised / timed out) is
    # at-most-once: TERMINAL failed with reason
    # daily_review_send_outcome_unknown_no_retry — never recycled to 'pending',
    # never re-dispatched, exactly one sender call total (there is no upstream
    # provider idempotency key, so at-most-once and exactly-once cannot be
    # jointly held; duplicate daily pushes must be avoided first). Other alert
    # types keep the existing bounded-retry policy.
    # Codex P1-2: the push-consistency diagnostic reports delivery-outcome-
    # unknown rows (reason codes + long-stale 'sending') with alert_outbox_id /
    # review_date / status / reason; the production state_consistency
    # aggregation surfaces type daily_review_delivery_outcome_unknown and must
    # never auto-flip pushed_to_feishu.
    # Codex P2-1: REAL PostgreSQL finalize failure (BEFORE UPDATE trigger, no
    # monkeypatch) + the send_message=None / dedupe-hit process_job return
    # semantics (deduped=True, queued=False).

    def test_daily_review_send_timeout_at_most_once(self) -> None:
        """Codex P1-1: daily_review send outcome unknown (sender recorded the
        request, then raised TimeoutError) -> TERMINAL 'failed' with reason
        daily_review_send_outcome_unknown_no_retry; a second dispatcher cycle
        and a stale-recovery pass must never invoke the sender again."""
        h = fx.make_repo()
        try:
            repo = h.repo
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.commit()
            calls: list = []

            def _timeout_send(*args: object, **kwargs: object) -> bool:
                calls.append((args, kwargs))  # provider accepted the request
                raise TimeoutError("feishu read timeout")

            first = process_alert_outbox(repo, _timeout_send, limit=10)
            assert first["failed"] == 1
            assert len(calls) == 1
            row = repo.conn.execute(
                "SELECT id, status, retry_count, last_error FROM alert_outbox "
                "WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            ).fetchone()
            assert row["status"] == "failed"  # TERMINAL, never 'pending'
            assert "daily_review_send_outcome_unknown_no_retry" in (row["last_error"] or "")
            # a second dispatcher cycle must not invoke the sender again
            second = process_alert_outbox(repo, _timeout_send, limit=10)
            assert second["processed"] == 0
            assert len(calls) == 1
            # restart / stale recovery: the row stays terminal, never reclaimed
            repo.conn.execute(
                "UPDATE alert_outbox SET status='sending', "
                "updated_at=NOW() - interval '16 minutes' WHERE id=%s",
                (row["id"],),
            )
            repo.conn.commit()
            third = process_alert_outbox(repo, _timeout_send, limit=10)
            assert third["processed"] == 0
            assert len(calls) == 1
        finally:
            h.close()

    def test_non_daily_review_send_failure_keeps_retry_policy(self) -> None:
        """Codex P1-1 boundary: OTHER alert types keep the existing bounded
        retry policy — a send exception recycles to 'pending' with backoff and
        no daily_review reason code."""
        h = fx.make_repo()
        try:
            repo = h.repo
            alert_id = repo.enqueue_alert(
                alert_type="trend_alert", symbol="BTCUSDT",
                payload=PAYLOAD, dedupe_key="BTCUSDT:trend_alert",
            )
            repo.conn.commit()
            calls: list = []
            first = process_alert_outbox(repo, _fail_send(calls), limit=10)
            assert first["failed"] == 1
            row = repo.conn.execute(
                "SELECT status, last_error FROM alert_outbox WHERE id=%s",
                (alert_id,),
            ).fetchone()
            assert row["status"] == "pending"  # retryable per the explicit policy
            assert "daily_review_send_outcome_unknown_no_retry" not in (row["last_error"] or "")
        finally:
            h.close()

    def test_stale_sending_recovery_reason_visible(self) -> None:
        """Codex P1-1/P1-2: a daily_review row stranded in 'sending' by a
        crashed dispatcher is terminalized with the recover_stale_sending_alerts
        reason (never re-sent) so the new diagnostic can surface it."""
        h = fx.make_repo()
        try:
            repo = h.repo
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True}, ga_report="t"
            )
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.commit()
            repo.conn.execute(
                "UPDATE alert_outbox SET status='sending', "
                "updated_at=NOW() - interval '16 minutes' WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            )
            repo.conn.commit()
            calls: list = []
            result = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert result["processed"] == 0  # never re-sent
            assert calls == []
            row = repo.conn.execute(
                "SELECT status, last_error FROM alert_outbox WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            ).fetchone()
            assert row["status"] == "failed"
            assert "recover_stale_sending_alerts" in (row["last_error"] or "")
        finally:
            h.close()

    def test_real_pg_finalize_failure_via_trigger_never_retries(self) -> None:
        """Codex P2-1: REAL PostgreSQL finalize failure — a BEFORE UPDATE
        trigger on alert_outbox rejecting status='sent' (no monkeypatch). The
        external send succeeded once; the finalize UPDATE fails inside the DB
        itself; the row fails closed to terminal 'failed' +
        finalize_failed_after_send; a second dispatcher cycle must not invoke
        the sender again."""
        h = fx.make_repo()
        try:
            repo = h.repo
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.commit()
            repo.conn.execute(
                "CREATE OR REPLACE FUNCTION _t16_finalize_boom() RETURNS trigger AS $$"
                " BEGIN RAISE EXCEPTION 'finalize_boom_real_pg'; END"
                " $$ LANGUAGE plpgsql"
            )
            repo.conn.execute(
                "CREATE TRIGGER _t16_finalize_boom_trg BEFORE UPDATE ON alert_outbox"
                " FOR EACH ROW WHEN (NEW.status = 'sent')"
                " EXECUTE FUNCTION _t16_finalize_boom()"
            )
            repo.conn.commit()
            calls: list = []
            first = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert first["failed"] == 1
            assert len(calls) == 1
            row = repo.conn.execute(
                "SELECT status, retry_count, last_error FROM alert_outbox "
                "WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            ).fetchone()
            assert row["status"] == "failed"  # terminal, never 'pending'
            assert "finalize_failed_after_send" in (row["last_error"] or "")
            assert "finalize_boom_real_pg" in (row["last_error"] or "")
            second = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert second["processed"] == 0
            assert len(calls) == 1  # once-ever: exactly one external send
        finally:
            h.close()

    def test_delivery_outcome_unknown_reported_by_diagnostic(self) -> None:
        """Codex P1-2: the push-consistency diagnostic reports delivery-
        outcome-unknown daily_review rows — terminal failed with
        finalize_failed_after_send / daily_review_send_outcome_unknown_no_retry
        / recover_stale_sending_alerts reasons and long-stale 'sending' rows —
        each carrying alert_outbox_id / review_date / status / reason; the
        production state_consistency aggregation surfaces type
        daily_review_delivery_outcome_unknown and never auto-flips the marker."""
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )

        h = fx.make_repo()
        try:
            repo = h.repo
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True}, ga_report="t"
            )
            # (a) send outcome unknown (P1-1 terminal)
            id_a = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.execute(
                "UPDATE alert_outbox SET status='failed', "
                "last_error='daily_review_send_outcome_unknown_no_retry: feishu read timeout' "
                "WHERE id=%s",
                (id_a,),
            )
            # (b) finalize failed after send (P2-1 terminal)
            id_b = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD,
                dedupe_key="daily_review:2026-08-10",
            )
            repo.conn.execute(
                "UPDATE alert_outbox SET status='failed', "
                "last_error='finalize_failed_after_send: boom' WHERE id=%s",
                (id_b,),
            )
            # (c) dispatcher crash recovered
            id_c = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD,
                dedupe_key="daily_review:2026-08-09",
            )
            repo.conn.execute(
                "UPDATE alert_outbox SET status='failed', "
                "last_error='recover_stale_sending_alerts: dispatcher crashed mid-send; "
                "fail-closed, never re-sent' WHERE id=%s",
                (id_c,),
            )
            # (d) long-stale sending, not yet recovered (diagnostic sees it live)
            id_d = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD,
                dedupe_key="daily_review:2026-08-08",
            )
            repo.conn.execute(
                "UPDATE alert_outbox SET status='sending', "
                "updated_at=NOW() - interval '30 minutes' WHERE id=%s",
                (id_d,),
            )
            # (e) unclassified terminal 'failed' (reviewer P2-1): legacy
            # residue / manual edit / future reason code — its delivery outcome
            # is equally unknown and must be surfaced, never silently hidden
            id_e = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD,
                dedupe_key="daily_review:2026-01-01",
            )
            repo.conn.execute(
                "UPDATE alert_outbox SET status='failed', "
                "last_error='unknown other cause' WHERE id=%s",
                (id_e,),
            )
            repo.conn.commit()
            diag = repo.diagnose_daily_review_push_consistency()
            assert diag["ok"] is True
            unknown = {d["alert_outbox_id"]: d for d in diag["delivery_unknown"]}
            assert set(unknown) == {id_a, id_b, id_c, id_d, id_e}
            by_reason = {d["reason"] for d in diag["delivery_unknown"]}
            assert "send_outcome_unknown_no_retry" in by_reason
            assert "finalize_failed_after_send" in by_reason
            assert "dispatcher_crashed_mid_send" in by_reason
            assert "stale_sending_unrecovered" in by_reason
            assert "unclassified_terminal_failed" in by_reason
            entry = unknown[id_a]
            assert entry["review_date"] == REVIEW_DATE
            assert entry["status"] == "failed"
            # production aggregation surfaces the new issue type
            result = diagnose_state_consistency(repo)
            assert result["ok"] is True
            types = {issue["type"] for issue in result["issues"]}
            assert "daily_review_delivery_outcome_unknown" in types
            delivery = [
                i for i in result["issues"]
                if i["type"] == "daily_review_delivery_outcome_unknown"
            ]
            assert len(delivery) == 5
            details = {i["details"]["alert_outbox_id"] for i in delivery}
            assert details == {id_a, id_b, id_c, id_d, id_e}
            # reviewer round 2 (P2): the summary dict carries a per-type count
            # key for the new daily_review types, like every other type.
            assert result["summary"]["daily_review_delivery_outcome_unknown"] == 5
            assert result["summary"]["daily_review_push_inconsistency"] == 0
            # never auto-flip the marker
            marker = repo.conn.execute(
                "SELECT pushed_to_feishu FROM daily_review_reports WHERE review_date=%s",
                (REVIEW_DATE,),
            ).fetchone()
            assert marker["pushed_to_feishu"] in (None, False)
        finally:
            h.close()

    def test_process_job_reenqueue_deduped_semantics_when_already_consumed(self, monkeypatch) -> None:
        """Codex P2-1: with send_message=None and an existing NON-pending
        daily_review:<date> outbox row (delivery already attempted, report
        marker not set because finalize failed), process_job must report
        deduped=True / queued=False — the send_message=None path must not
        claim a queue slot it can never fill."""
        import json as _json

        import plugins.crypto_guard.run_ga_workers as workers

        h = fx.make_repo()
        try:
            repo = h.repo
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True}, ga_report="t"
            )
            # consume the key with a REAL finalize failure: outbox row terminal
            # 'failed' (finalize_failed_after_send) while the report marker is
            # still FALSE — the state that makes a re-dispatch ambiguous
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.commit()
            repo.conn.execute(
                "UPDATE alert_outbox SET status='failed', retry_count=1, "
                "last_error='finalize_failed_after_send: boom' WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            )
            repo.conn.commit()
            monkeypatch.setattr(
                workers, "resolve_report_target",
                lambda repo, payload: {"receive_id": "chat_1", "receive_id_type": "chat_id"},
            )
            monkeypatch.setattr(
                workers, "_build_evolution_status_text", lambda repo: "evolution-status"
            )
            job = {
                "id": 9_000_002,
                "job_type": "daily_review",
                "payload_json": _json.dumps(
                    {"day_utc": "2026-08-11", "loss_count": 0}
                ),
                "priority": 7,
                "session_id": "test-t18",
            }
            result = workers.process_job(repo, job, send_message=None)
            assert result["queued"] is False  # RED today: queued=True
            assert result["deduped"] is True  # RED today: deduped=False
            # no second outbox row; the terminal state is unchanged
            rows = repo.conn.execute(
                "SELECT status FROM alert_outbox WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
        finally:
            h.close()

    def test_direct_daily_review_send_rejected_at_api_boundary(self) -> None:
        """Reviewer Recommended-1: the once-ever contract is enforced by the
        claim -> send -> finalize sequence INSIDE process_alert_outbox. A
        direct send_markdown_alert(send_message=...) with a daily_review:<date>
        key bypasses the claim entirely (a crash between send and finalize
        leaves the row claimable again -> a SECOND external send), so the
        public API must reject it BEFORE enqueue. No outbox row may be left
        behind by a rejected call."""
        h = fx.make_repo()
        try:
            repo = h.repo
            calls: list = []
            with pytest.raises(ValueError):
                send_markdown_alert(
                    repo,
                    _ok_send(calls),
                    receive_id="chat_1",
                    receive_id_type="chat_id",
                    text="每日复盘文本",
                    alert_type="daily_review",
                    priority=5,
                    dedupe_key=DEDUPE_KEY,
                )
            assert calls == []  # sender never invoked
            count = repo.conn.execute(
                "SELECT COUNT(*) AS c FROM alert_outbox WHERE dedupe_key=%s",
                (DEDUPE_KEY,),
            ).fetchone()["c"]
            assert count == 0  # rejected BEFORE enqueue: no intent row left
        finally:
            h.close()

    def test_dirty_duplicate_daily_review_rows_block_migration_actionably(self) -> None:
        """Reviewer P2-2: a dirty historical DB that already holds TWO rows for
        the same daily_review:<date> key (the old code had no cross-state
        constraint) must fail initialize_database with an ACTIONABLE preflight
        error — duplicate keys listed, human-merge guidance — BEFORE the
        once-ever unique index DDL would raise a raw UniqueViolation, and the
        transaction must leave the rows untouched (zero deletion, fail-closed).
        Runs the real dirty-DB upgrade branch (unhealthy schema -> test-owner
        migration DDL path, identical to production is_test_owner)."""
        from plugins.crypto_guard.storage.migrations import initialize_database

        h = fx.make_repo()
        try:
            repo = h.repo
            # simulate a dirty historical pre-08-12 DB: the once-ever index
            # does not exist yet (its absence also makes the schema unhealthy,
            # so initialize_database takes the dirty-DB migration branch)
            repo.conn.execute("DROP INDEX idx_alert_outbox_daily_review_once_ever")
            # two rows for the same once-ever key, exactly as a dirty
            # historical DB would hold them. The old code had no CROSS-STATE
            # constraint on daily_review:<date> keys: the pending-only dedupe
            # index never saw them because both rows are terminal (sent /
            # failed). A second terminal row for the same date is precisely
            # the state the once-ever index forbids.
            for _ in range(2):
                row_id = repo.conn.execute(
                    "INSERT INTO alert_outbox(alert_type, symbol, priority, "
                    "payload_json, next_retry_at, dedupe_key) "
                    "VALUES ('daily_review', NULL, 5, %s, NOW(), %s) "
                    "RETURNING id",
                    ('{"a": 1}', "daily_review:2026-01-01"),
                ).fetchone()["id"]
                repo.conn.execute(
                    "UPDATE alert_outbox SET status='failed' WHERE id=%s",
                    (row_id,),
                )
            repo.conn.commit()
            with pytest.raises(RuntimeError) as ei:
                initialize_database()
            msg = str(ei.value)
            assert "daily_review:2026-01-01 (x2)" in msg
            assert "alert_outbox" in msg
            # fail-closed: nothing deleted, nothing rewritten
            count = repo.conn.execute(
                "SELECT COUNT(*) AS c FROM alert_outbox WHERE dedupe_key=%s",
                ("daily_review:2026-01-01",),
            ).fetchone()["c"]
            assert count == 2
        finally:
            h.close()

    # ---- reviewer round 3 (08-12): 1 P2 + 3 Recommended --------------------
    # P2-1: save_daily_review_report's ON CONFLICT DO UPDATE erased the
    #   pushed_to_feishu marker on a force-rebuild (default pushed_to_feishu=
    #   False overwrote TRUE) -> marker_missing false positive + producer
    #   already_pushed gate re-triggered. The marker must be MONOTONIC.
    # Recommended-1: a raw-SQL writer bypassing initialize_database can commit
    #   a duplicate daily_review:<date> row in the precheck -> DDL window; the
    #   CREATE UNIQUE INDEX then raises a UniqueViolation wrapped as an
    #   index-name-only RuntimeError. The DDL step must be savepointed so the
    #   catch path re-runs the precheck and raises the actionable key list.
    # Recommended-2: fail-closed query errors (diagnostic_query_failed) of ANY
    #   check must be visible in the summary dict like every other type.
    # Recommended-3: a falsy send_message return EXPLICITLY refused the message
    #   (provider never accepted it -> retrying is duplicate-safe). Only the
    #   EXCEPTION path is outcome-unknown; a daily_review falsy return must
    #   keep the bounded retry policy, not terminate with
    #   daily_review_send_outcome_unknown_no_retry.

    def test_force_rebuild_never_clears_pushed_marker(self) -> None:
        """Reviewer round 3 P2-1: save_daily_review_report on an existing
        review_date (run_daily_review(force=True) / manual re-run, default
        pushed_to_feishu=False) must NOT erase the pushed_to_feishu marker of a
        delivery that durably happened — the marker is monotonic once TRUE
        (mark_alert_sent commits outbox 'sent' + marker in ONE transaction).
        Erasing it would false-positive marker_missing and re-trigger the
        producer's already_pushed gate."""
        h = fx.make_repo()
        try:
            repo = h.repo
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True}, ga_report="t"
            )
            calls: list = []
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            result = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert result["sent"] == 1
            marker = repo.conn.execute(
                "SELECT pushed_to_feishu FROM daily_review_reports WHERE review_date=%s",
                (REVIEW_DATE,),
            ).fetchone()
            assert marker["pushed_to_feishu"] is True
            # force-rebuild: same review_date, default pushed_to_feishu=False
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True, "rebuild": 1},
                ga_report="rebuilt text",
            )
            marker = repo.conn.execute(
                "SELECT pushed_to_feishu FROM daily_review_reports WHERE review_date=%s",
                (REVIEW_DATE,),
            ).fetchone()
            assert marker["pushed_to_feishu"] is True  # RED: cleared to False
            # no marker_missing inconsistency is manufactured by the rebuild
            diag = repo.diagnose_daily_review_push_consistency()
            kinds = {d["kind"] for d in diag["inconsistencies"]}
            assert "marker_missing" not in kinds
            # the once-ever row is still sent: no second delivery
            second = process_alert_outbox(repo, _ok_send(calls), limit=10)
            assert second["processed"] == 0
            assert len(calls) == 1
        finally:
            h.close()

    def test_ddl_window_duplicate_raises_actionable_precheck_error(self, monkeypatch) -> None:
        """Reviewer round 3 Recommended-1: a raw-SQL writer that bypasses
        initialize_database can commit a SECOND daily_review:<date> row in the
        window between the precheck SELECT and the CREATE UNIQUE INDEX (the
        index build scans live data, not the precheck snapshot). The DDL then
        raises a UniqueViolation naming only the index — the fixed
        implementation savepoints the DDL step, re-runs the precheck from the
        savepoint and raises the ACTIONABLE key-list error instead. The
        transaction rolls back fully: no window residue survives."""
        from plugins.crypto_guard.storage import migrations

        h = fx.make_repo()
        try:
            repo = h.repo
            dup_key = "daily_review:2026-02-02"
            # dirty-DB branch: the once-ever index missing makes the schema
            # unhealthy, exactly like a legacy pre-08-12 database
            repo.conn.execute("DROP INDEX idx_alert_outbox_daily_review_once_ever")
            repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=dup_key
            )
            repo.conn.execute(
                "UPDATE alert_outbox SET status='failed' WHERE dedupe_key=%s",
                (dup_key,),
            )
            repo.conn.commit()
            orig_precheck = migrations._precheck_daily_review_once_ever_duplicates

            def windowed_precheck(cur) -> None:
                # the preflight sees a clean single row...
                orig_precheck(cur)
                # ...then a raw-SQL writer (bypassing initialize_database)
                # commits a duplicate in the precheck -> DDL window
                cur.connection.execute(
                    "INSERT INTO alert_outbox(alert_type, symbol, priority, "
                    "payload_json, next_retry_at, dedupe_key) "
                    "VALUES ('daily_review', NULL, 5, %s, NOW(), %s)",
                    ('{"a": 1}', dup_key),
                )

            monkeypatch.setattr(
                migrations, "_precheck_daily_review_once_ever_duplicates",
                windowed_precheck,
            )
            with pytest.raises(RuntimeError) as ei:
                migrations.initialize_database()
            msg = str(ei.value)
            # ACTIONABLE: the merge-guidance message with the exact key + count
            # (a raw UniqueViolation / index-name-only wrap never contains it)
            assert f"{dup_key} (x2)" in msg
            assert "must be merged manually" in msg
            # fail-closed: the window insert rolled back; the original row is
            # untouched (zero deletion, zero rewrite)
            rows = repo.conn.execute(
                "SELECT status FROM alert_outbox WHERE dedupe_key=%s",
                (dup_key,),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
        finally:
            h.close()

    def test_diagnostic_query_failed_surfaces_in_summary(self, monkeypatch) -> None:
        """Reviewer round 3 Recommended-2: a check that fails closed
        (diagnostic_query_failed) must be observable from the summary dict,
        like every other issue type — a silently-absent key would make the
        fail-closed check look like it never ran."""
        from plugins.crypto_guard.diagnostics import state_consistency

        h = fx.make_repo()
        try:
            repo = h.repo

            def _boom(repo) -> list:
                raise RuntimeError("injected query failure")

            monkeypatch.setattr(state_consistency, "_check_orphan_patches", _boom)
            result = state_consistency.diagnose_state_consistency(repo)
            assert result["summary"]["diagnostic_query_failed"] == 1
            failed = [
                i for i in result["issues"]
                if i["type"] == "diagnostic_query_failed"
            ]
            assert len(failed) == 1
            assert failed[0]["severity"] == "error"
            assert failed[0]["details"]["check"] == "_check_orphan_patches"
        finally:
            h.close()

    def test_falsy_send_keeps_bounded_retry_even_for_daily_review(self) -> None:
        """Reviewer round 3 Recommended-3: a send_message returning falsy
        EXPLICITLY refused the message — the provider never accepted it, so a
        bounded retry is duplicate-safe (only the EXCEPTION path is outcome-
        unknown). A daily_review row must recycle to 'pending' with
        retry_count=1 and write NO alert_failure_log row; the next dispatch
        must respect the backoff, never a second send within it."""
        h = fx.make_repo()
        try:
            repo = h.repo
            repo.save_daily_review_report(
                review_date=REVIEW_DATE, summary={"ok": True}, ga_report="t"
            )
            alert_id = repo.enqueue_alert(
                alert_type="daily_review", payload=PAYLOAD, dedupe_key=DEDUPE_KEY
            )
            repo.conn.commit()
            calls: list = []

            def _falsy_send(*args: object, **kwargs: object) -> bool:
                calls.append((args, kwargs))
                return False

            result = process_alert_outbox(repo, _falsy_send, limit=10)
            assert result["failed"] == 1
            assert len(calls) == 1
            row = repo.conn.execute(
                "SELECT status, retry_count, last_error FROM alert_outbox WHERE id=%s",
                (alert_id,),
            ).fetchone()
            assert row["status"] == "pending"  # bounded retry, NOT terminal
            assert int(row["retry_count"]) == 1
            assert "daily_review_send_outcome_unknown_no_retry" not in (row["last_error"] or "")
            log_count = repo.conn.execute(
                "SELECT COUNT(*) AS c FROM alert_failure_log WHERE alert_outbox_id=%s",
                (alert_id,),
            ).fetchone()["c"]
            assert log_count == 0  # no terminal failure logged
            # backoff respected: the next dispatch does not claim the row
            second = process_alert_outbox(repo, _falsy_send, limit=10)
            assert second["processed"] == 0
            assert len(calls) == 1
        finally:
            h.close()

    def test_ddl_window_pending_dedupe_duplicate_raises_actionable_precheck_error(
        self, monkeypatch
    ) -> None:
        """Reviewer round 4 R-1 (Recommended): the 23505 catch path must be
        actionable for EVERY unique index, not just the once-ever
        daily_review one. A raw-SQL writer committing a SECOND pending row for
        one dedupe_key (idx_alert_outbox_dedupe_unique, partial unique on
        status='pending') in the precheck -> DDL window must surface the
        business key + count + merge guidance — not the index-name-only wrap
        (same defect class the once-ever precheck already fixes for
        daily_review:<date> keys)."""
        from plugins.crypto_guard.storage import migrations

        h = fx.make_repo()
        try:
            repo = h.repo
            dup_key = "-:hourly_summary"
            # dirty-DB branch: the once-ever index missing makes the schema
            # unhealthy, exactly like a legacy pre-08-12 database
            repo.conn.execute("DROP INDEX idx_alert_outbox_daily_review_once_ever")
            # The window writer must NOT hit the ALREADY-LIVE partial unique
            # idx_alert_outbox_dedupe_unique (both rows are pending): an
            # insert violating it aborts the connection OUTSIDE the DDL
            # savepoint (InFailedSqlTransaction, sqlstate 25P02 — not the
            # 23505 catch branch). Dropping it first simulates a dirty-DB
            # window writer; the schema DDL rebuild scans the two live pending
            # rows for the same key -> UniqueViolation 23505 inside the
            # savepoint -> the actionable-precheck catch path.
            repo.conn.execute("DROP INDEX idx_alert_outbox_dedupe_unique")
            repo.enqueue_alert(
                alert_type="hourly_summary", payload=PAYLOAD, dedupe_key=dup_key
            )
            repo.conn.commit()
            # Round-5 P2 (reviewer): patch the PENDING precheck (the second
            # preflight call, and the re-run inside the 23505 catch), NOT the
            # once-ever one. A window row inserted from the once-ever precheck
            # is already visible to the pending precheck's SELECT (same open
            # init transaction), so the pending PREFLIGHT raises (x2) and the
            # DDL savepoint - and therefore the catch path under test - never
            # executes. Patching the pending precheck instead: its preflight
            # invocation runs the REAL check (clean single row, no raise) and
            # inserts the window row; the schema DDL then genuinely raises
            # UniqueViolation 23505; the catch re-runs BOTH prechecks, where
            # the patched pending precheck runs the REAL check first and
            # raises (x2). Removing the catch wiring (migrations.py:421)
            # makes the message fall back to the index-name-only wrap and
            # this test goes RED.
            orig_precheck = migrations._precheck_pending_dedupe_key_duplicates

            def windowed_precheck(cur) -> None:
                # the preflight sees a clean single pending row (real check
                # passes, nothing raised)...
                orig_precheck(cur)
                # ...then a raw-SQL writer (bypassing initialize_database)
                # commits a DUPLICATE pending row for the SAME dedupe_key in
                # the precheck -> DDL window; the partial unique
                # idx_alert_outbox_dedupe_unique (status='pending') then
                # raises UniqueViolation 23505
                cur.connection.execute(
                    "INSERT INTO alert_outbox(alert_type, symbol, priority, "
                    "payload_json, next_retry_at, dedupe_key) "
                    "VALUES ('hourly_summary', NULL, 5, %s, NOW(), %s)",
                    ('{"a": 1}', dup_key),
                )

            monkeypatch.setattr(
                migrations, "_precheck_pending_dedupe_key_duplicates",
                windowed_precheck,
            )
            with pytest.raises(RuntimeError) as ei:
                migrations.initialize_database()
            msg = str(ei.value)
            # ACTIONABLE: the merge-guidance message with the exact key + count
            # (the index-name-only wrap never contains the (x2) form)
            assert f"{dup_key} (x2)" in msg
            assert "must be merged manually" in msg
            # fail-closed: the window insert rolled back; the original row is
            # untouched (zero deletion, zero rewrite)
            rows = repo.conn.execute(
                "SELECT status FROM alert_outbox WHERE dedupe_key=%s",
                (dup_key,),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["status"] == "pending"
        finally:
            h.close()

    def test_preflight_pending_dedupe_duplicate_raises_actionable_precheck_error(
        self, monkeypatch
    ) -> None:
        """Reviewer round 5 P2: the PREFLIGHT wiring of the pending-dedupe
        precheck (migrations.py:396) gets an EXPLICIT test, not coverage by
        accident. A dirty DB already holding TWO pending rows for one
        non-daily_review dedupe_key (both unique indexes missing, like a
        legacy pre-08-12 database) must fail closed at the PREFLIGHT precheck
        with the actionable (x2) key-list message - before the DDL savepoint
        ever runs. The DDL-savepoint marker (psycopg.Connection.transaction
        patched) proves the raise came from the preflight, not from the 23505
        catch: the catch only re-runs the prechecks AFTER the savepoint
        starts, so a catch-satisfied raise would see the marker populated.
        Removing the preflight wiring (migrations.py:396) makes the DDL
        savepoint start and this test goes RED."""
        import psycopg

        from plugins.crypto_guard.storage import migrations

        h = fx.make_repo()
        try:
            repo = h.repo
            dup_key = "-:hourly_summary"
            repo.conn.execute("DROP INDEX idx_alert_outbox_daily_review_once_ever")
            repo.conn.execute("DROP INDEX idx_alert_outbox_dedupe_unique")
            for _ in range(2):
                repo.conn.execute(
                    "INSERT INTO alert_outbox(alert_type, symbol, priority, "
                    "payload_json, next_retry_at, dedupe_key) "
                    "VALUES ('hourly_summary', NULL, 5, %s, NOW(), %s)",
                    ('{"a": 1}', dup_key),
                )
            repo.conn.commit()
            ddl_attempted: list[bool] = []
            orig_transaction = psycopg.Connection.transaction

            def counting_transaction(self, *args, **kwargs):
                ddl_attempted.append(True)
                return orig_transaction(self, *args, **kwargs)

            monkeypatch.setattr(
                psycopg.Connection, "transaction", counting_transaction
            )
            with pytest.raises(RuntimeError) as ei:
                migrations.initialize_database()
            msg = str(ei.value)
            # ACTIONABLE: the merge-guidance message with the exact key + count
            assert f"{dup_key} (x2)" in msg
            assert "must be merged manually" in msg
            # The raise came from the PREFLIGHT precheck: the DDL savepoint
            # (psycopg Connection.transaction) never started. A catch-path
            # raise would have populated the marker first.
            assert ddl_attempted == [], (
                "expected the preflight precheck to fail closed BEFORE the "
                "DDL savepoint; the DDL savepoint was entered, so the raise "
                "came from the 23505 catch path, not the preflight wiring "
                "under test"
            )
            # fail-closed: both original rows remain, zero deleted/rewritten
            rows = repo.conn.execute(
                "SELECT status FROM alert_outbox WHERE dedupe_key=%s",
                (dup_key,),
            ).fetchall()
            assert len(rows) == 2
            assert all(r["status"] == "pending" for r in rows)
        finally:
            h.close()
