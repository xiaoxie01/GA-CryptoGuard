"""P6: JSONB nested fields, time-window boundaries, NULL, epoch-ms on real PG.

Exercises the JSONB / time / epoch-ms semantics converted in P6 against the real
PG schema (initialized via ``initialize_database``). Settles the contract bugs
that static analysis cannot catch:

  J1: JSONB columns come back from psycopg as Python dict/list (NOT str). Reader
      methods that do ``json.loads(col)`` on an already-decoded dict raise
      ``TypeError`` and silently fall back to the default -> DATA LOSS. This is
      the central P6 defect: ``get_ga_decision``, ``latest_analysis_state``,
      ``get_analysis_batch``, ``list_recent_analysis_batches`` must handle the
      dict/list shape directly (json.loads only on a str).
  J2: nested-field round-trip - ``raw_decision_json.raw_llm_summary`` and
      ``trade_plan_json`` survive create->read (the audit/diagnostic path reads
      these nested fields; a silent ``{}`` default loses them).
  J3: NULL JSONB columns (``trade_plan_json``/``opportunity_watch_json`` are
      nullable) must read back as ``None`` (the documented default), not raise.
  T1: ``make_interval(mins => %s)`` time window - ``should_silence_alert``
      silences within the window, NOT past it (boundary correctness).
  T2: ``lease_until <= NOW()`` vs ``started_at <= NOW() - make_interval`` -
      ``recover_stale_running_jobs`` resets an expired-lease row but leaves a
      valid-lease row running (the ownership-liveness contract).
  T3: ``next_retry_at`` vs ``NOW()`` - ``claim_pending_alerts`` picks a due
      alert but skips a future-retry alert.
  E1: epoch-ms BIGINT comparison - ``latest_ga_decisions_by_symbol`` with
      ``min_analysis_time`` and ``list_recent_ga_decisions`` with ``since_ms``
      use plain integer comparison (no datetime wrapper) - boundary inclusive.

This is the "每完成一个业务域立即运行对应真实 PostgreSQL 测试" gate for P6.
NOT a mock; uses a real pooled conn.
"""

from __future__ import annotations

import os
import unittest

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import make_repo


def _set_app_dsn_env() -> str:
    from plugins.crypto_guard.tests._pg_bootstrap import app_dsn

    dsn = app_dsn()
    os.environ["CRYPTO_GUARD_DATABASE_URL"] = dsn
    pg_db.reset_pool()
    return dsn


def _ga_decision(symbol: str, analysis_time: int, *, batch_id: str | None = None) -> dict:
    """A minimal-but-complete ga_decisions row (all NOT NULL columns set)."""
    return {
        "symbol": symbol,
        "analysis_time": analysis_time,
        "analysis_time_utc": "2026-07-16T00:00:00Z",
        "decision_type": "analysis",
        "signal_grade": "B",
        "confidence": 0.7,
        "market_bias": "neutral",
        "trend_stage": "range",
        "decision": "no_trade",
        "skill_result_refs": {"trend": 1},
        "evidence": [{"k": "v"}],
        "counter_evidence": [],
        "risk_check": {"ok": True},
        # trade_plan / opportunity_watch omitted -> NULL columns (J3)
        "feishu_actions": [],
        "final_summary": "summary",
        "raw_llm_summary": "ORIGINAL LLM TEXT",  # nested inside raw_decision_json (J2)
        "rendered_summary": "canonical deterministic summary",
        "batch_id": batch_id,
        "previous_grade": "C",
    }


class TestPgJsonbAndTimeP6(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    # ── J1/J2/J3: JSONB nested-field round-trip via create->read ───────────

    def test_ga_decision_jsonb_nested_roundtrip(self) -> None:
        """J1+J2: raw_decision_json nested field + trade_plan_json survive read."""
        dec = _ga_decision("BTCUSDT", 1_000_000, batch_id="b1")
        dec["trade_plan"] = {"entry": 50000.0, "stop": 49000.0}  # non-NULL trade_plan
        dec["opportunity_watch"] = {"watch": True}
        gid = self.repo.create_ga_decision(dec)
        self.assertGreater(gid, 0)

        read = self.repo.get_ga_decision(gid)
        self.assertIsNotNone(read)
        # J2: nested field inside raw_decision_json preserved (audit path reads it).
        self.assertEqual(read["raw_decision"]["raw_llm_summary"], "ORIGINAL LLM TEXT")
        self.assertEqual(read["raw_decision"]["symbol"], "BTCUSDT")
        # J1: trade_plan_json (JSONB) read back as a dict, not a str needing parse.
        self.assertEqual(read["trade_plan"], {"entry": 50000.0, "stop": 49000.0})
        self.assertEqual(read["opportunity_watch"], {"watch": True})
        # J1: evidence_json (JSONB NOT NULL) read back as a list.
        self.assertEqual(read["evidence"], [{"k": "v"}])
        self.assertEqual(read["risk_check"], {"ok": True})

    def test_ga_decision_null_jsonb_columns(self) -> None:
        """J3: nullable trade_plan_json / opportunity_watch_json read back as None."""
        dec = _ga_decision("ETHUSDT", 1_000_001)
        # No trade_plan / opportunity_watch -> NULL columns.
        gid = self.repo.create_ga_decision(dec)
        read = self.repo.get_ga_decision(gid)
        self.assertIsNotNone(read)
        self.assertIsNone(read["trade_plan"])  # documented default is None
        self.assertIsNone(read["opportunity_watch"])

    def test_analysis_state_jsonb_nested_roundtrip(self) -> None:
        """J1: state_json + market_structure_json (nested dict) survive read."""
        state = {
            "symbol": "SOLUSDT",
            "analysis_time": 1_000_002,
            "analysis_time_utc": "2026-07-16T00:00:00Z",
            "analysis_mode": "4h",
            "timeframes": ["4h", "1d"],
            "market_structure": {"trend": "up", "phase": "breakout"},
            "trend_clarity": {"score": 8},
            "no_trade_reason": {},
            "key_levels": {"support": [100, 110]},
            "next_triggers": [],
            "next_analysis": {},
            "breakout_watch": {},
            "trade_permission": {"paper_trade_allowed": True},
            "trade_plan": {},
            "opportunity_watch_recommended": False,
            "extra_nested": {"deep": {"value": 42}},
        }
        sid = self.repo.save_analysis_state(state)
        self.assertGreater(sid, 0)

        read = self.repo.latest_analysis_state("SOLUSDT")
        self.assertIsNotNone(read)
        # J1: state_json (JSONB) read back as a dict; nested field preserved.
        self.assertEqual(read["state"]["extra_nested"]["deep"]["value"], 42)
        self.assertEqual(read["state"]["symbol"], "SOLUSDT")
        # The top-level market_structure_json column also round-trips as dict.
        self.assertEqual(read["market_structure_json"]["trend"], "up")

    def test_analysis_batch_jsonb_arrays_roundtrip(self) -> None:
        """J1: enabled_symbols_json / summary_json (JSONB arrays/objects) read back."""
        bid = self.repo.start_analysis_batch(
            batch_id="batch-p6", primary_interval="4h", analysis_time=1_000_003,
            enabled_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        )
        self.assertGreater(bid, 0)
        read = self.repo.get_analysis_batch("batch-p6")
        self.assertIsNotNone(read)
        # J1: enabled_symbols_json read back as a list (json.loads on a list
        # would TypeError -> fallback to [] -> this assertion catches that).
        self.assertEqual(set(read["enabled_symbols"]), {"BTCUSDT", "ETHUSDT", "SOLUSDT"})

    # ── T1: make_interval time window (should_silence_alert) ───────────────

    def test_should_silence_alert_boundary(self) -> None:
        """T1: an alert created NOW is silenced within the window; the boundary
        is evaluated via NOW() - make_interval(mins => %s)."""
        self.repo.enqueue_alert(
            alert_type="bound_probe", payload={}, symbol="BTCUSDT",
            priority=5, dedupe_key="bound-1",
        )
        # Within a 60-minute window -> silenced.
        self.assertTrue(self.repo.should_silence_alert(
            alert_type="bound_probe", symbol="BTCUSDT",
            quiet_minutes=60, never_silence=set(),
        ))
        # A different alert_type is NOT silenced (window is per-type+symbol).
        self.assertFalse(self.repo.should_silence_alert(
            alert_type="other_type", symbol="BTCUSDT",
            quiet_minutes=60, never_silence=set(),
        ))

    # ── T2: lease_until vs started_at age (recover_stale_running_jobs) ──────

    def test_recover_stale_running_jobs_expired_lease_reset(self) -> None:
        """T2: a running job with an EXPIRED lease_until is reset to pending."""
        jid = self.repo.enqueue_job(
            job_type="stale_probe", priority=5, source="test",
            session_id="stale-1", payload={"k": "v"},
        )
        # Manually mark it running with an expired lease (1 minute in the past).
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_jobs SET status='running', started_at=NOW() - make_interval(mins => 60), "
                    "lease_until=NOW() - make_interval(mins => 1) WHERE id=%s",
                    (jid,),
                )
        affected = self.repo.recover_stale_running_jobs(older_than_minutes=30)
        self.assertGreaterEqual(affected, 1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, lease_until FROM agent_jobs WHERE id=%s", (jid,))
            row = dict(cur.fetchone())
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["lease_until"])

    def test_recover_stale_running_jobs_valid_lease_left_running(self) -> None:
        """T2: a running job with a VALID (future) lease is LEFT running."""
        jid = self.repo.enqueue_job(
            job_type="valid_probe", priority=5, source="test",
            session_id="valid-1", payload={"k": "v"},
        )
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_jobs SET status='running', started_at=NOW(), "
                    "lease_until=NOW() + make_interval(mins => 10) WHERE id=%s",
                    (jid,),
                )
        self.repo.recover_stale_running_jobs(older_than_minutes=30)
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM agent_jobs WHERE id=%s", (jid,))
            row = cur.fetchone()
        self.assertEqual(row["status"], "running")  # NOT recovered

    # ── T3: next_retry_at vs NOW (claim_pending_alerts) ────────────────────

    def test_claim_pending_alerts_skips_future_retry(self) -> None:
        """T3: a due alert is claimed; a future-retry alert is skipped."""
        due_id = self.repo.enqueue_alert(
            alert_type="due_probe", payload={"i": 1}, symbol="BTCUSDT",
            priority=5, dedupe_key="due-1",
        )
        # A second alert scheduled 60 minutes in the future.
        future_id = self.repo.enqueue_alert(
            alert_type="future_probe", payload={"i": 2}, symbol="BTCUSDT",
            priority=5, dedupe_key="future-1",
        )
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE alert_outbox SET next_retry_at=NOW() + make_interval(mins => 60) WHERE id=%s",
                    (future_id,),
                )
        claimed = self.repo.claim_pending_alerts(limit=10)
        claimed_types = {c["alert_type"] for c in claimed}
        self.assertIn("due_probe", claimed_types)
        self.assertNotIn("future_probe", claimed_types)  # future retry NOT claimed

    # ── E1: epoch-ms BIGINT comparison (min_analysis_time / since_ms) ───────

    def test_latest_ga_decisions_min_analysis_time_boundary(self) -> None:
        """E1: min_analysis_time is a plain BIGINT epoch-ms inclusive filter.

        ``latest_ga_decisions_by_symbol`` returns the latest row per symbol
        (ROW_NUMBER rn=1). Use two distinct symbols so the boundary filter is
        genuinely exercised: a symbol whose only decision is below the cutoff
        disappears; one at-or-above stays.
        """
        self.repo.create_ga_decision(_ga_decision("XRPUSDT", 5_000_000))
        self.repo.create_ga_decision(_ga_decision("ADAUSDT", 5_000_001))
        # Boundary inclusive: min_analysis_time=5_000_000 keeps both symbols.
        both = self.repo.latest_ga_decisions_by_symbol(limit=10, min_analysis_time=5_000_000)
        syms_below = {r["symbol"] for r in both}
        self.assertEqual(syms_below, {"XRPUSDT", "ADAUSDT"})
        # min_analysis_time=5_000_001 drops XRPUSDT (its only decision is below).
        one = self.repo.latest_ga_decisions_by_symbol(limit=10, min_analysis_time=5_000_001)
        syms_above = {r["symbol"] for r in one}
        self.assertEqual(syms_above, {"ADAUSDT"})
        self.assertEqual(one[0]["analysis_time"], 5_000_001)

    def test_list_recent_ga_decisions_since_ms_boundary(self) -> None:
        """E1: since_ms is a plain BIGINT epoch-ms inclusive filter."""
        self.repo.create_ga_decision(_ga_decision("DOTUSDT", 6_000_000))
        self.repo.create_ga_decision(_ga_decision("DOTUSDT", 6_000_005))
        since_5m = self.repo.list_recent_ga_decisions(limit=50, since_ms=6_000_000)
        self.assertEqual(len(since_5m), 2)
        since_5m1 = self.repo.list_recent_ga_decisions(limit=50, since_ms=6_000_005)
        self.assertEqual(len(since_5m1), 1)
        self.assertEqual(since_5m1[0]["analysis_time"], 6_000_005)

    def test_risk_summary_accepts_psycopg_jsonb_dict(self) -> None:
        """A JSONB dict must not be fed back through json.loads()."""
        from plugins.crypto_guard.risk.risk_engine import risk_summary_from_signal

        expected = {"ok": True, "reasons": [], "metrics": {"rr": 2.4}}
        actual = risk_summary_from_signal({"ga_decision_json": {"risk_check": expected}})
        self.assertEqual(actual, expected)

    def test_cleanup_uses_analysis_states_created_at(self) -> None:
        """Retention deletes old analysis states through the real PG column."""
        from plugins.crypto_guard.storage.cleanup import clean_old_data

        sid = self.repo.save_analysis_state({
            "symbol": "CLEANUSDT",
            "analysis_time": 9_000_000,
            "analysis_time_utc": "2026-07-16T00:00:00Z",
            "analysis_mode": "scheduled",
            "timeframes": [],
            "market_structure": {},
            "trend_clarity": {},
            "no_trade_reason": {},
            "key_levels": {},
            "next_triggers": [],
            "next_analysis": {},
            "breakout_watch": {},
            "trade_permission": {},
            "trade_plan": {},
            "opportunity_watch_recommended": False,
        })
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE analysis_states SET created_at=NOW() - INTERVAL '40 days' WHERE id=%s",
                (sid,),
            )
        self.conn.commit()
        deleted = clean_old_data(retention_days=30)
        self.assertEqual(deleted.get("analysis_states"), 1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM analysis_states WHERE id=%s", (sid,))
            self.assertEqual(int(cur.fetchone()["c"]), 0)


if __name__ == "__main__":
    unittest.main()
