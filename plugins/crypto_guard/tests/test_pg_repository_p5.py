"""P5 repository.py exercise on real PostgreSQL.

Exercises the business-domain methods converted in P5 against the real PG
schema (initialized via ``initialize_database``). Proves the write/read
templates (``with self.conn.transaction(): cur.execute(... RETURNING id)`` /
read-only ``with self.conn.cursor():``) actually execute and persist, and
that JSONB / BOOLEAN / TIMESTAMPTZ / IDENTITY-RETURNING adaptation works end
to end across the paper-trade, alert, strategy-memory, config-hot-reload,
daily-review, and strategy-version domains.

This is the "每完成一个业务域立即运行对应真实 PostgreSQL 测试" gate for the
domains converted in this session. NOT a mock; uses a real pooled conn.
"""

from __future__ import annotations

import json
import unittest

from plugins.crypto_guard.tests.pg_fixtures import make_repo


class TestPgRepositoryP5(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()

    def test_upsert_symbol_returns_dict(self) -> None:
        sym = self.repo.upsert_symbol("TESTUSDT", category="custom", enabled=True,
                                      timeframes=["1d", "4h"], notes="probe")
        self.assertEqual(sym["symbol"], "TESTUSDT")
        self.assertEqual(sym["base_asset"], "TEST")
        self.assertTrue(sym["enabled"])  # BOOLEAN -> Python bool
        # default_timeframes is a TEXT column holding a JSON string (not JSONB),
        # so it round-trips as a str; parse to verify the content.
        self.assertEqual(json.loads(sym["default_timeframes"]), ["1d", "4h"])

    def test_ensure_paper_account_upsert_idempotent(self) -> None:
        a1 = self.repo.ensure_paper_account()
        a2 = self.repo.ensure_paper_account()
        self.assertEqual(int(a1["id"]), int(a2["id"]))  # ON CONFLICT DO NOTHING -> same row
        self.assertEqual(float(a1["initial_balance"]), 10000.0)

    def test_log_paper_trade_event_returns_real_id(self) -> None:
        # log_paper_trade_event uses RETURNING id (was last_insert_rowid).
        event_id = self.repo.log_paper_trade_event(
            event_type="test_event",
            symbol="BTCUSDT",
            side="long",
            price=50000.0,
            quantity=1.0,
            reason="probe",
            event={"k": "v"},
            dedupe_key="probe-1",
        )
        self.assertGreater(event_id, 0)
        # Round-trip via raw cursor: JSONB column comes back as dict.
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_trade_logs WHERE id=%s", (event_id,))
            row = dict(cur.fetchone())
        self.assertEqual(row["event_type"], "test_event")
        self.assertEqual(row["symbol"], "BTCUSDT")
        # JSONB column comes back as a Python dict (psycopg adapter), not a str.
        self.assertEqual(row["event_json"]["k"], "v")

    def test_enqueue_alert_on_conflict_dedupe(self) -> None:
        # First enqueue creates a row.
        aid1 = self.repo.enqueue_alert(
            alert_type="probe_alert", payload={"x": 1}, symbol="BTCUSDT",
            priority=3, dedupe_key="dup-1",
        )
        self.assertGreater(aid1, 0)
        # Second enqueue with the same pending dedupe_key -> returns the existing id.
        aid2 = self.repo.enqueue_alert(
            alert_type="probe_alert", payload={"x": 2}, symbol="BTCUSDT",
            priority=3, dedupe_key="dup-1",
        )
        self.assertEqual(aid1, aid2)
        # mark_alert_sent then a new pending enqueue with the same key is allowed.
        self.repo.mark_alert_sent(aid1)
        aid3 = self.repo.enqueue_alert(
            alert_type="probe_alert", payload={"x": 3}, symbol="BTCUSDT",
            priority=3, dedupe_key="dup-1",
        )
        self.assertGreater(aid3, aid1)  # sent row no longer dedupes -> new pending row

    def test_should_silence_alert_time_window(self) -> None:
        # Enqueue a pending alert, then a silence probe within the window.
        self.repo.enqueue_alert(
            alert_type="silence_probe", payload={}, symbol="ETHUSDT",
            priority=5, dedupe_key="silence-1",
        )
        silenced = self.repo.should_silence_alert(
            alert_type="silence_probe", symbol="ETHUSDT",
            quiet_minutes=60, never_silence=set(),
        )
        self.assertTrue(silenced)
        # A different alert_type is NOT silenced.
        silenced_other = self.repo.should_silence_alert(
            alert_type="other_type", symbol="ETHUSDT",
            quiet_minutes=60, never_silence=set(),
        )
        self.assertFalse(silenced_other)

    def test_strategy_memory_upsert_and_increment(self) -> None:
        # First sample inserts.
        self.repo.update_strategy_memory_from_review(
            strategy_name="strat_a", condition_hash="h1", result="win",
            pnl_r=2.0, notes="first",
        )
        top = self.repo.strategy_memory_top(limit=10)
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["strategy_name"], "strat_a")
        self.assertEqual(int(top[0]["sample_count"]), 1)
        self.assertEqual(int(top[0]["win_count"]), 1)
        # Second sample updates averages.
        self.repo.update_strategy_memory_from_review(
            strategy_name="strat_a", condition_hash="h1", result="loss",
            pnl_r=-1.0, notes="second",
        )
        top2 = self.repo.strategy_memory_top(limit=10)
        self.assertEqual(int(top2[0]["sample_count"]), 2)
        self.assertEqual(int(top2[0]["win_count"]), 1)
        self.assertEqual(int(top2[0]["loss_count"]), 1)

    def test_strategy_version_upsert_and_active(self) -> None:
        sid = self.repo.save_strategy_version(
            strategy_name="strat_b", version="v1", status="active",
            config={"k": 1}, change_reason="init",
        )
        self.assertGreater(sid, 0)
        # Upsert same key -> same id.
        sid2 = self.repo.save_strategy_version(
            strategy_name="strat_b", version="v1", status="candidate",
            config={"k": 2}, change_reason="tweak",
        )
        self.assertEqual(sid, sid2)
        active = self.repo.active_strategy_version("strat_b")
        # After upsert to candidate there is no active -> None.
        self.assertIsNone(active)
        # Re-save active.
        self.repo.save_strategy_version(
            strategy_name="strat_b", version="v1", status="active",
            config={"k": 3}, change_reason="reactivate",
        )
        active2 = self.repo.active_strategy_version("strat_b")
        self.assertIsNotNone(active2)
        self.assertEqual(active2["status"], "active")

    def test_save_strategy_patch_candidate_and_mark_duplicates(self) -> None:
        # Two patches for the same trigger_id + candidate_version.
        pid1 = self.repo.save_strategy_patch_candidate(
            {"strategy_name": "strat_c", "from_version": "v1", "candidate_version": "v2",
             "patch": {"a": 1}, "change_reason": "r1"},
            evidence={"e": 1}, trigger_id=100, status="draft",
        )
        pid2 = self.repo.save_strategy_patch_candidate(
            {"strategy_name": "strat_c", "from_version": "v1", "candidate_version": "v2",
             "patch": {"a": 2}, "change_reason": "r2"},
            evidence={"e": 2}, trigger_id=100, status="draft",
        )
        self.assertGreater(pid1, 0)
        self.assertGreater(pid2, pid1)
        # mark_duplicate_patches_rejected uses cur.rowcount (was changes()).
        result = self.repo.mark_duplicate_patches_rejected()
        self.assertGreaterEqual(result["rejected_duplicates"], 1)

    def test_save_daily_review_report_upsert(self) -> None:
        rid1 = self.repo.save_daily_review_report(
            review_date="2026-07-16", summary={"k": 1}, ga_report="r1",
            skill_updates=[], evolution_actions={}, pushed_to_feishu=False,
        )
        rid2 = self.repo.save_daily_review_report(
            review_date="2026-07-16", summary={"k": 2}, ga_report="r2",
            skill_updates=[], evolution_actions={}, pushed_to_feishu=True,
        )
        self.assertEqual(rid1, rid2)  # ON CONFLICT(review_date) -> same row

    def test_config_hot_reload_request_apply_confirm(self) -> None:
        change_id = self.repo.request_config_hot_reload(
            config_key="probe.cfg", new_value="v1", requested_by="tester",
            request_text="set v1", confirmation_required=False,
        )
        self.assertGreater(change_id, 0)
        # confirmation_required=False -> apply directly.
        res = self.repo.apply_config_hot_reload(change_id)
        self.assertTrue(res["ok"])
        self.assertEqual(res["change_id"], change_id)

    def test_create_evolution_trigger_dedupe(self) -> None:
        tid1 = self.repo.create_evolution_trigger(
            trigger_type="drawdown", trigger_value=0.1, threshold_value=0.08,
            related_trade_ids=[1, 2], strategy_name="strat_d", symbol="BTCUSDT",
            evolution_allowed=True, status="pending",
        )
        # Same trigger_type + pending status + symbol -> dedup returns existing.
        tid2 = self.repo.create_evolution_trigger(
            trigger_type="drawdown", trigger_value=0.2, threshold_value=0.08,
            related_trade_ids=[3], strategy_name="strat_d", symbol="BTCUSDT",
            evolution_allowed=True, status="pending",
        )
        self.assertEqual(tid1, tid2)

    def test_list_open_paper_trades_and_sum_pnl(self) -> None:
        trades = self.repo.list_open_paper_trades()
        self.assertEqual(trades, [])
        self.assertEqual(self.repo.sum_closed_realized_pnl(), 0.0)


if __name__ == "__main__":
    unittest.main()
