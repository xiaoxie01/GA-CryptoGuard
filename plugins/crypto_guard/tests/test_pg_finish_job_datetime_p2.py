"""Phase-2 finish_job / finish_claimed_batch_symbol datetime serialization.

Production evidence (2026-07-22): bare ``json.dumps(result)`` in
``finish_job`` raised ``TypeError: Object of type datetime is not JSON
serializable`` after successful worker bodies (hourly_feishu_report /
update_paper_positions), breaking job terminalization.

These tests call the REAL repository methods on a real PostgreSQL scratch
schema. They do NOT mock finish_job.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.pg

import json
import os
import secrets
import unittest
from datetime import date, datetime, timezone

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.tests.pg_fixtures import make_repo


def _set_app_dsn_env() -> str:
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    os.environ["CRYPTO_GUARD_DATABASE_URL"] = dsn
    pg_db.reset_pool()
    return dsn


def _datetime_heavy_result() -> dict:
    """A realistic worker result containing every datetime shape that
    production payloads may carry after a PG row round-trip."""
    return {
        "ok": True,
        "updated_at": datetime(2026, 7, 22, 3, 0, 0, tzinfo=timezone.utc),
        "naive_ts": datetime(2026, 7, 22, 3, 0, 0),  # naive
        "as_of_date": date(2026, 7, 22),
        "nested": {
            "filled_at": datetime(2026, 7, 22, 2, 59, 0, tzinfo=timezone.utc),
            "events": [
                {"ts": datetime(2026, 7, 22, 2, 58, 0, tzinfo=timezone.utc), "kind": "fill"},
                {"ts": datetime(2026, 7, 22, 2, 57, 0), "kind": "open"},  # naive nested
            ],
        },
        "counts": {"updated": 1, "closed": 0},
    }


class TestPgFinishJobDatetimeP2(unittest.TestCase):
    def setUp(self) -> None:
        _set_app_dsn_env()
        self.handle = make_repo()
        self.conn = self.handle.conn
        self.repo = self.handle.repo

    def tearDown(self) -> None:
        self.handle.close()

    def _enqueue_running_job(self, *, job_type: str = "update_paper_positions") -> tuple[int, str]:
        token = secrets.token_hex(16)
        session = f"test:finish_dt:{secrets.token_hex(4)}"
        jid = self.repo.enqueue_job_once(
            job_type, 5, "test", session, {"seed": True},
        )
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agent_jobs
                SET status='running', started_at=NOW(), claim_token=%s,
                    lease_until=NOW() + INTERVAL '30 minutes'
                WHERE id=%s
                """,
                (token, int(jid)),
            )
        self.conn.commit()
        return int(jid), token

    def test_finish_job_accepts_datetime_result_and_terminalizes(self) -> None:
        jid, token = self._enqueue_running_job()
        result = _datetime_heavy_result()
        ok = self.repo.finish_job(jid, result=result, claim_token=token)
        self.assertTrue(ok, "finish_job must succeed with datetime-heavy result")
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT status, result_json, error_message FROM agent_jobs WHERE id=%s",
                (jid,),
            )
            row = cur.fetchone()
        self.assertEqual(row["status"], "success")
        self.assertIsNone(row["error_message"])
        # psycopg decodes jsonb to dict; also accept str and re-load.
        payload = row["result_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload.get("ok"))
        # datetime values must have been ISO-encoded, not dropped.
        self.assertIsInstance(payload.get("updated_at"), str)
        self.assertIn("2026-07-22", payload["updated_at"])
        self.assertIsInstance(payload["nested"]["filled_at"], str)
        self.assertIsInstance(payload["nested"]["events"][0]["ts"], str)
        self.assertIsInstance(payload.get("as_of_date"), str)

    def test_finish_claimed_batch_symbol_accepts_datetime_result(self) -> None:
        batch_id = f"15m:test_finish_dt_{secrets.token_hex(3)}"
        symbol = "BTCUSDT"
        token = secrets.token_hex(16)
        # Seed order: batch (unsealed) -> symbol status -> job -> then seal.
        # The production trigger rejects scheduled_market_analysis inserts
        # after sealed_at is set, so job membership must be written first.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_batches (
                  batch_id, primary_interval, analysis_time, status,
                  enabled_symbols_json, completed_symbols_json, failed_symbols_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    batch_id, "15m", 1_784_688_299_999, "running",
                    json.dumps([symbol]), json.dumps([]), json.dumps([]),
                ),
            )
            cur.execute(
                """
                INSERT INTO batch_symbol_status(batch_id, symbol, status, updated_at)
                VALUES (%s, %s, 'pending', NOW())
                """,
                (batch_id, symbol),
            )
            cur.execute(
                """
                INSERT INTO agent_jobs (
                  job_type, priority, source, session_id, payload_json, status,
                  scheduled_at, started_at, claim_token, lease_until,
                  batch_id, symbol
                ) VALUES (
                  'scheduled_market_analysis', 5, 'test', %s, %s, 'running',
                  NOW(), NOW(), %s, NOW() + INTERVAL '30 minutes',
                  %s, %s
                ) RETURNING id
                """,
                (
                    f"system:scheduled:15m:{symbol}:test",
                    json.dumps({
                        "batch_id": batch_id,
                        "symbol": symbol,
                        "primary_interval": "15m",
                        "snapshot": {"symbol": symbol},
                    }),
                    token, batch_id, symbol,
                ),
            )
            jid = int(cur.fetchone()["id"])
            cur.execute(
                """
                UPDATE analysis_batches
                SET sealed_at=NOW(), claim_ready_at=NOW()
                WHERE batch_id=%s
                """,
                (batch_id,),
            )
        self.conn.commit()

        result = _datetime_heavy_result()
        ok = self.repo.finish_claimed_batch_symbol(
            batch_id=batch_id,
            symbol=symbol,
            job_id=jid,
            claim_token=token,
            result=result,
        )
        self.assertTrue(ok, "finish_claimed_batch_symbol must accept datetime result")
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT status, result_json FROM agent_jobs WHERE id=%s", (jid,),
            )
            job = cur.fetchone()
            cur.execute(
                "SELECT status FROM batch_symbol_status WHERE batch_id=%s AND symbol=%s",
                (batch_id, symbol),
            )
            bss = cur.fetchone()
        self.assertEqual(job["status"], "success")
        self.assertEqual(bss["status"], "completed")
        payload = job["result_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.assertIsInstance(payload["updated_at"], str)

    def test_revert_to_bare_json_dumps_fails(self) -> None:
        """Revert-fail: if finish_job used bare json.dumps again, datetime
        results must raise TypeError. Proves the serializer is load-bearing.
        """
        jid, token = self._enqueue_running_job(job_type="hourly_feishu_report")
        result = _datetime_heavy_result()

        # Monkeypatch only the serializer used by finish_job to the bare form.
        import plugins.crypto_guard.storage.repository as repo_mod

        orig = repo_mod._json_dumps_payload

        def bare(payload):
            return json.dumps(payload, ensure_ascii=False)  # no default=

        repo_mod._json_dumps_payload = bare  # type: ignore[assignment]
        try:
            with self.assertRaises(TypeError):
                self.repo.finish_job(jid, result=result, claim_token=token)
        finally:
            repo_mod._json_dumps_payload = orig  # type: ignore[assignment]

    def test_process_job_update_paper_positions_finish_path(self) -> None:
        """Real worker path: process_job(update_paper_positions) then finish_job
        with a datetime-bearing result must terminalize success. Does NOT mock
        finish_job.
        """
        from plugins.crypto_guard.run_ga_workers import process_job

        jid, token = self._enqueue_running_job(job_type="update_paper_positions")
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM agent_jobs WHERE id=%s", (jid,))
            job = dict(cur.fetchone())
        # process_job body (empty book -> ok with counts)
        body = process_job(self.repo, job, send_message=None)
        self.assertIsInstance(body, dict)
        # Inject a datetime so the finish path is forced through the serializer
        # (production paper updater may include TIMESTAMPTZ-decoded values).
        body = dict(body)
        body["observed_at"] = datetime.now(timezone.utc)
        body["nested"] = {"ts": datetime(2026, 7, 22, 1, 2, 3)}
        ok = self.repo.finish_job(jid, result=body, claim_token=token)
        self.assertTrue(ok)
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, result_json FROM agent_jobs WHERE id=%s", (jid,))
            row = cur.fetchone()
        self.assertEqual(row["status"], "success")
        payload = row["result_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.assertIsInstance(payload.get("observed_at"), str)

    def test_process_job_hourly_report_finish_path_with_datetime(self) -> None:
        """Real worker path skeleton for hourly_feishu_report: process_job may
        requeue; whatever result dict it returns (with injected datetime) must
        finish without TypeError. Does NOT mock finish_job.
        """
        from plugins.crypto_guard.run_ga_workers import process_job

        jid, token = self._enqueue_running_job(job_type="hourly_feishu_report")
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM agent_jobs WHERE id=%s", (jid,))
            job = dict(cur.fetchone())
        # payload for hourly report
        job["payload_json"] = json.dumps({
            "batch_id": "15m:test_hourly",
            "retry_count": 0,
        })
        body = process_job(self.repo, job, send_message=None)
        self.assertIsInstance(body, dict)
        body = dict(body)
        body["finished_at"] = datetime.now(timezone.utc)
        body["deadline"] = datetime(2026, 7, 22, 4, 0, 0, tzinfo=timezone.utc)
        ok = self.repo.finish_job(jid, result=body, claim_token=token)
        self.assertTrue(ok)
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM agent_jobs WHERE id=%s", (jid,))
            self.assertEqual(cur.fetchone()["status"], "success")


class TestEffectiveGradeExceedsHtfCapP2(unittest.TestCase):
    """Two-way HTF-cap diagnostic contract on real PG persistence."""

    def setUp(self) -> None:
        _set_app_dsn_env()
        self.handle = make_repo()
        self.conn = self.handle.conn
        self.repo = self.handle.repo

    def tearDown(self) -> None:
        self.handle.close()

    def _insert(self, *, raw_grade: str, effective_grade: str, column_grade: str) -> int:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        raw_decision = {
            "symbol": "BTCUSDT",
            "analysis_time": now_ms,
            "raw_signal_grade": raw_grade,
            "effective_signal_grade": effective_grade,
            "signal_grade": column_grade,
            "market_bias": "bullish",
            "timeframe_context": {
                "1d": {"bias": "bullish"},
                "4h": {"bias": "bearish"},
                "1h": {"bias": "bearish"},
                "15m": {"bias": "bearish"},
            },
            "m5_bias": "bullish",  # Cap 4: only 5M supports -> max C
        }
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ga_decisions (
                  symbol, analysis_time, analysis_time_utc, decision_type,
                  signal_grade, confidence, market_bias, trend_stage, decision,
                  skill_result_refs_json, evidence_json, counter_evidence_json,
                  risk_check_json, feishu_actions_json, final_summary,
                  raw_decision_json, batch_id
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s
                ) RETURNING id
                """,
                (
                    "BTCUSDT", now_ms, "2026-07-22T00:00:00Z", "scheduled_analysis",
                    column_grade, 0.85, "bullish", "early", "trade_plan_available",
                    "[]", "[]", "[]", '{"ok":true}', "[]", "summary",
                    json.dumps(raw_decision), None,
                ),
            )
            did = int(cur.fetchone()["id"])
        self.conn.commit()
        return did

    def test_raw_above_cap_effective_at_cap_is_not_error(self) -> None:
        """raw=S, effective=C, cap=C → must NOT raise effective_grade_exceeds_htf_cap."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            EFFECTIVE_GRADE_EXCEEDS_HTF_CAP,
            RAW_GRADE_EXCEEDS_HTF_CAP,
            _check_effective_grade_exceeds_htf_cap,
            diagnose_report_accuracy,
        )

        self._insert(raw_grade="S", effective_grade="C", column_grade="C")
        issues = _check_effective_grade_exceeds_htf_cap(self.repo)
        codes = [i.get("type") for i in issues]
        self.assertNotIn(
            EFFECTIVE_GRADE_EXCEEDS_HTF_CAP, codes,
            "raw>cap with effective==cap is the legitimate audit shape; "
            "must NOT be an error",
        )
        self.assertNotIn(
            RAW_GRADE_EXCEEDS_HTF_CAP, codes,
            "deprecated raw_grade code must not be emitted either",
        )
        # Full diagnose_report_accuracy must also stay free of this error.
        report = diagnose_report_accuracy(self.repo)
        for issue in report.get("issues") or []:
            self.assertNotEqual(issue.get("type"), EFFECTIVE_GRADE_EXCEEDS_HTF_CAP)
            self.assertNotEqual(issue.get("type"), RAW_GRADE_EXCEEDS_HTF_CAP)

    def test_effective_above_cap_is_error(self) -> None:
        """raw=S, effective=S, cap=C → MUST raise effective_grade_exceeds_htf_cap."""
        from plugins.crypto_guard.diagnostics.report_diagnostics import (
            EFFECTIVE_GRADE_EXCEEDS_HTF_CAP,
            _check_effective_grade_exceeds_htf_cap,
            diagnose_report_accuracy,
        )

        did = self._insert(raw_grade="S", effective_grade="S", column_grade="S")
        issues = _check_effective_grade_exceeds_htf_cap(self.repo)
        codes = [i.get("type") for i in issues]
        self.assertIn(
            EFFECTIVE_GRADE_EXCEEDS_HTF_CAP, codes,
            "effective grade above HTF cap must fire effective_grade_exceeds_htf_cap",
        )
        hit = next(i for i in issues if i.get("type") == EFFECTIVE_GRADE_EXCEEDS_HTF_CAP)
        self.assertEqual(hit["details"]["effective_signal_grade"], "S")
        self.assertEqual(hit["details"]["max_allowed_grade"], "C")
        self.assertEqual(hit["details"]["raw_signal_grade"], "S")
        self.assertEqual(hit["details"]["decision_id"], did)
        # Full path
        report = diagnose_report_accuracy(self.repo)
        self.assertFalse(report.get("ok"))
        self.assertGreaterEqual(int(report.get("error_count") or 0), 1)
        self.assertGreaterEqual(
            int((report.get("summary") or {}).get(EFFECTIVE_GRADE_EXCEEDS_HTF_CAP) or 0),
            1,
        )


if __name__ == "__main__":
    unittest.main()
