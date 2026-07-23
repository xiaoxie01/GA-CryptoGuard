"""P8-4 review-domain real-PG gate.

This is the ``每完成一个业务域立即运行对应真实 PostgreSQL 测试`` gate for the
review/ domain migrated in P8-4 (``trade_reviewer.py``,
``evolution_triggers.py``, ``daily_reviewer.py``). It drives the REAL
review-domain functions against the real PG schema (initialized via
``initialize_database``), seeding the full production chain:

  snapshot -> signal -> paper_order -> paper_trade -> (close) -> trade_review
  -> strategy_patch + version -> backtest gate -> shadow_testing transition.

The headline defect classes this gate proves (each was a real cutover risk):

1. **JSONB-returned-as-dict**: ``trade_reviewer._load_review_json``,
   ``_snapshot_context``, ``_enrich_trade_with_regime_context``,
   ``_derive_strategy_name_from_trade`` and ``daily_reviewer._parse_json_field``
   all read JSONB columns that psycopg decodes to dict/list already. Pre-cutover
   ``json.loads(dict)`` raised ``TypeError``. The cutover replaced those with
   ``_decode_json``/``_parse_json_field`` (isinstance guards). This gate seeds
   real JSONB ``ga_review_json``/``snapshot_json``/``raw_decision_json`` rows and
   asserts the reads do not crash.

2. **Snapshot chain rewrite**: under PG ``paper_trades`` has NO
   ``signal_id``/``market_snapshot_id`` (normalized away).
   ``_snapshot_context`` was rewritten to resolve the snapshot via
   ``trade.order_id -> paper_orders.signal_id -> signals.market_snapshot_id ->
   market_snapshots``. This gate seeds that exact chain and asserts the review
   reports ``source_snapshot.available=True``.

3. **Raw-write transaction wraps + dropped bare commits**: ``review_trade``
   wraps patch+version+cap in ``with repo.conn.transaction():``;
   ``_run_backtest_for_candidate`` wraps the backtest_result + status-transition
   UPDATEs in one ``with repo.conn.transaction():`` with bare ``commit()`` calls
   dropped. ``daily_reviewer`` wraps the force-archive and false-review-memory
   archive UPDATEs. This gate asserts the writes actually commit (status flips,
   backtest_result_json non-null) without a bare ``commit()``.

4. **`backtest_result_json` JSONB column** (added to greenfield PG
   ``strategy_patches`` schema): ``_run_backtest_for_candidate`` and
   ``evaluate_evolution_triggers`` UPDATE this column. The gate asserts the
   write survives as a JSONB dict (psycopg decodes it).

The LLM boundary (``run_agent_json_task``) is mocked off via
``CRYPTO_GUARD_LLM_ANALYSIS=0`` (returns the deterministic fallback - no
network), mirroring the production-shaped path. The backtest gate is mocked to
``skipped`` so the candidate transitions to ``shadow_testing`` without needing
candle data. Everything else runs against the real PG schema.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from plugins.crypto_guard.review.daily_reviewer import run_daily_review
from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers
from plugins.crypto_guard.review.trade_reviewer import review_trade
from plugins.crypto_guard.tests.pg_fixtures import make_repo


def _now_ms() -> int:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return int(base.timestamp() * 1000)


class TestPgReviewDomainP8(unittest.TestCase):
    def setUp(self) -> None:
        # Deterministic fallback path: no LLM network call.
        self._saved_llm = os.environ.get("CRYPTO_GUARD_LLM_ANALYSIS")
        os.environ["CRYPTO_GUARD_LLM_ANALYSIS"] = "0"
        self._repo_handle = make_repo()
        self.conn = self._repo_handle.conn
        self.repo = self._repo_handle.repo

    def tearDown(self) -> None:
        self._repo_handle.close()
        if self._saved_llm is not None:
            os.environ["CRYPTO_GUARD_LLM_ANALYSIS"] = self._saved_llm
        else:
            os.environ.pop("CRYPTO_GUARD_LLM_ANALYSIS", None)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _seed_candle(self, symbol: str, close: float, close_time_ms: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO candles(symbol, open, high, low, close, volume,
                                    close_time, open_time, interval)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (symbol, close, close, close, close, 0.0,
                 close_time_ms, close_time_ms - 60_000, "1m"),
            )
        self.conn.commit()

    def _seed_active_strategy(self) -> None:
        """Seed an active strategy_version + symbol so the backtest gate has a
        baseline to diff against (avoids no_active_version noise)."""
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO strategy_versions(strategy_name, version, status, config_json, change_reason) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s) "
                    "ON CONFLICT(strategy_name, version) DO NOTHING",
                    ("smc_pullback_long", "1.0", "active", "{}", "seed"),
                )
                cur.execute(
                    "INSERT INTO symbols(symbol, enabled) VALUES (%s, TRUE) "
                    "ON CONFLICT(symbol) DO NOTHING",
                    ("TESTUSDT",),
                )

    def _seed_snapshot_signal_order_trade(
        self, symbol: str, *, entry_price: float, stop_loss: float,
        close_price: float, close_reason: str, close_time_ms: int,
        trend_stage: str = "mid",
    ) -> int:
        """Seed the full production chain and a CLOSED losing trade. Returns trade id.

        snapshot -> signal -> paper_order (signal_id, ga_decision_id) ->
        paper_trade (create_paper_trade) -> close_paper_trade (loss)."""
        from plugins.crypto_guard.utils import iso_utc_from_ms

        analysis_time_ms = close_time_ms - 120_000
        # A real market_snapshot with modules.market_regime so _snapshot_context
        # resolves trend_stage / market_regime / evolution_trigger_allowed.
        snapshot = {
            "symbol": symbol,
            "analysis_time_utc": analysis_time_ms,
            "analysis_time": analysis_time_ms,
            "mode": "scheduled",
            "modules": {
                "trend_stage": {"trend_stage": trend_stage},
                "price_action": {"last_event": "breakout"},
                "momentum": {"direction": "up"},
                "market_regime": {"regime": "normal", "evolution_trigger_allowed": True},
            },
            "counter_evidence": {},
        }
        snapshot_id = self.repo.save_market_snapshot(snapshot)

        # Seed a ga_decision so _derive_strategy_name_from_trade and
        # _enrich_trade_with_regime_context resolve via the order chain.
        ga_id = self.repo.create_ga_decision({
            "symbol": symbol,
            "analysis_time": analysis_time_ms,
            "analysis_time_utc": iso_utc_from_ms(analysis_time_ms),
            "decision_type": "scheduled",
            "signal_grade": "A",
            "confidence": 0.78,
            "market_bias": "bullish",
            "trend_stage": trend_stage,
            "decision": "enter",
            "risk_check": {"ok": True},
            "trade_plan": {"side": "LONG", "strategy_name": "smc_pullback_long", "strategy_version": "1.0"},
            "final_summary": "test summary",
            "raw_decision_json": {
                "strategy_name": "smc_pullback_long",
                "strategy_version": "1.0",
                "market_regime_gate_json": {"market_regime": {"regime": "normal"}},
            },
        })

        signal_id = self.repo.create_signal(
            {
                "symbol": symbol,
                "decision": "trade_plan_available",
                "signal_grade": "A",
                "confidence": 0.78,
                "summary": "test snapshot",
                "has_trade_plan": True,
                "trade_plan": {"side": "LONG"},
                "risk_notes": ["test"],
            },
            snapshot_id,
            ga_decision_id=ga_id,
        )

        from plugins.crypto_guard.paper.pending_order_manager import compute_expires_at

        exp = compute_expires_at("market")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_orders(symbol, side, order_type, entry_price, stop_loss,
                                             initial_stop_loss, take_profit_json, quantity,
                                             risk_percent, source, risk_check_passed, status,
                                             filled_at, fill_method, expires_at, signal_id, ga_decision_id)
                    VALUES (%s, 'LONG', 'market', %s, %s, %s, %s, 0.1, 1.0,
                            'test_seed', TRUE, 'filled', %s, 'market', %s, %s, %s)
                    RETURNING id
                    """,
                    (symbol, entry_price, stop_loss, stop_loss, "[]", exp, exp, signal_id, ga_id),
                )
                order_id = int(cur.fetchone()["id"])
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_orders WHERE id=%s", (order_id,))
            order = dict(cur.fetchone())
        self.conn.commit()

        trade_id = self.repo.create_paper_trade(
            order, entry_price, fill_method="market",
            event_time=close_time_ms - 60_000, allow_wall_clock=False,
        )
        # Close as a loss (stop_loss): entry=100 stop=95 exit=94 -> pnl_r=-1.2.
        self.repo.close_paper_trade(
            trade_id,
            exit_price=close_price,
            close_reason=close_reason,
            pnl=(close_price - entry_price),
            pnl_percent=((close_price - entry_price) / entry_price) * 100.0,
            pnl_r=(close_price - entry_price) / abs(entry_price - stop_loss),
            mfe=0.0,
            mae=abs(close_price - entry_price),
            event_time=close_time_ms,
            allow_wall_clock=False,
        )
        return trade_id

    # ── P8-4-D: review_trade full chain (snapshot resolve + patch + version + backtest) ──

    def test_review_trade_resolves_snapshot_chain_and_creates_shadow_patch(self) -> None:
        """P8-4-D: ``review_trade`` must resolve the snapshot via the PG
        order->signal->snapshot chain (no signal_id/market_snapshot_id on
        paper_trades), read JSONB columns without crashing, create a
        strategy_patch + version, run the (mocked-skipped) backtest gate, and
        transition the patch to ``shadow_testing`` - all via ``with
        repo.conn.transaction():`` wraps with no bare ``commit()``."""
        self._seed_active_strategy()
        self._seed_candle("TESTUSDT", close=100.0, close_time_ms=_now_ms())
        trade_id = self._seed_snapshot_signal_order_trade(
            "TESTUSDT", entry_price=100.0, stop_loss=95.0,
            close_price=94.0, close_reason="stop_loss", close_time_ms=_now_ms(),
        )
        self.conn.commit()

        with patch(
            "plugins.crypto_guard.strategy.shadow_testing.run_backtest_gate",
            return_value={"ok": True, "passed": False, "reason": "skipped", "skipped": True},
        ):
            result = review_trade(self.repo, trade_id)

        self.assertTrue(result.get("ok"), f"review_trade failed: {result}")
        review = result["review"]
        # Snapshot resolved through the rewritten PG chain.
        self.assertTrue(
            review["source_snapshot"]["available"],
            "P8-4-D: _snapshot_context must resolve the snapshot via order->signal->snapshot. "
            f"Got {review.get('source_snapshot')!r}",
        )
        # JSONB read survived: evidence_checklist parsed from snapshot modules.
        self.assertTrue(review["evidence_checklist"])
        # Patch + version created, transitioned to shadow_testing (backtest skipped).
        self.assertTrue(result["patch_id"], "P8-4-D: a candidate patch must be created.")
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, backtest_result_json FROM strategy_patches WHERE id=%s",
                        (result["patch_id"],))
            row = cur.fetchone()
        self.assertIsNotNone(row, "P8-4-D: strategy_patches row must exist.")
        self.assertEqual(
            row["status"], "shadow_testing",
            "P8-4-D: skipped-backtest candidate must transition to shadow_testing (transaction wrap).",
        )
        # backtest_result_json committed as JSONB (psycopg decodes to dict).
        self.assertIsInstance(
            row["backtest_result_json"], dict,
            "P8-4-D: backtest_result_json must commit as JSONB dict (no bare commit).",
        )
        self.assertTrue(row["backtest_result_json"].get("skipped"))

    # ── P8-4-E: evaluate_evolution_triggers candidate + reuse path ───────────

    def test_evolution_triggers_creates_candidate_and_reuses_on_second_call(self) -> None:
        """P8-4-E: ``evaluate_evolution_triggers`` fires ``daily_loss_threshold``
        after 3 stop-loss trades, creating an evolution_trigger + strategy_patch
        + version (backtest mocked-skipped -> shadow_testing). A second call
        REUSES the existing trigger (no new patch) and updates
        latest_related_trade_ids / latest_triggered_at - proving the reuse-path
        transaction wrap and JSONB ``related_trade_ids`` reads/writes survive."""
        self._seed_active_strategy()
        close_time_ms = _now_ms()
        self._seed_candle("TESTUSDT", close=100.0, close_time_ms=close_time_ms)
        # Three stop-loss trades on the same day -> daily_loss_threshold fires.
        tids = []
        for i in range(3):
            tid = self._seed_snapshot_signal_order_trade(
                "TESTUSDT", entry_price=100.0, stop_loss=95.0,
                close_price=94.0, close_reason="stop_loss",
                close_time_ms=close_time_ms + i * 60_000,
            )
            tids.append(tid)
        self.conn.commit()

        with patch(
            "plugins.crypto_guard.strategy.shadow_testing.run_backtest_gate",
            return_value={"ok": True, "passed": False, "reason": "skipped", "skipped": True},
        ):
            first = evaluate_evolution_triggers(self.repo)
            second = evaluate_evolution_triggers(self.repo)

        self.assertTrue(first.get("triggered"), f"P8-4-E: first call must trigger. Got {first}")
        first_action = first["actions"][0]
        self.assertEqual(first_action["status"], "shadow_testing")
        first_trigger_id = first_action["trigger_id"]
        first_patch_id = first_action["patch_id"]
        self.assertIsNotNone(first_patch_id, "P8-4-E: a candidate patch must be created.")

        # Second call reuses the existing trigger (no new patch).
        self.assertTrue(second.get("triggered"), "P8-4-E: second call must still report triggered.")
        second_action = second["actions"][0]
        self.assertEqual(
            second_action["status"], "existing_trigger_reused",
            "P8-4-E: second call must reuse the existing trigger, not create a new patch.",
        )
        self.assertEqual(second_action["trigger_id"], first_trigger_id)
        # latest_triggered_at updated (transaction wrap commit).
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT latest_triggered_at, latest_related_trade_ids FROM evolution_triggers WHERE id=%s",
                (first_trigger_id,),
            )
            row = cur.fetchone()
        self.assertIsNotNone(row["latest_triggered_at"], "P8-4-E: latest_triggered_at must commit on reuse.")
        # JSONB/list related_trade_ids survived the write (isinstance guard).
        self.assertIsInstance(row["latest_related_trade_ids"], (list, str))

    # ── P8-4-F: run_daily_review idempotency + JSONB summary read + report save ──

    def test_daily_review_saves_report_and_idempotent_reread(self) -> None:
        """P8-4-F: ``run_daily_review`` builds the deterministic report from a
        closed losing trade, writes ``daily_review_reports`` (JSONB summary_json
        + ga_report), and a second call returns idempotent=True reading the
        JSONB ``summary_json`` back as a dict (no ``json.loads(dict)`` crash).
        Also exercises ``_evolution_status_for_report`` (JSONB
        backtest_result_json parse + ``is_shadow=TRUE`` strategy_evaluations
        COUNT) and ``_cleanup_false_review_error_memories``
        (``created_at::date`` archive)."""
        self._seed_active_strategy()
        # Use an explicit day so the review window is deterministic.
        day_utc = "2024-01-01"
        close_time_ms = _now_ms()
        self._seed_candle("TESTUSDT", close=100.0, close_time_ms=close_time_ms)
        self._seed_snapshot_signal_order_trade(
            "TESTUSDT", entry_price=100.0, stop_loss=95.0,
            close_price=94.0, close_reason="stop_loss", close_time_ms=close_time_ms,
        )
        self.conn.commit()

        with patch(
            "plugins.crypto_guard.strategy.shadow_testing.run_backtest_gate",
            return_value={"ok": True, "passed": False, "reason": "skipped", "skipped": True},
        ):
            first = run_daily_review(self.repo, day_utc=day_utc)

        self.assertTrue(
            "daily_review_report_id" in first and first["daily_review_report_id"],
            f"P8-4-F: first daily review must save a report. Got {first}",
        )
        self.assertIn("text", first)
        report_id = first["daily_review_report_id"]

        # Second call: idempotent, reads the JSONB summary_json back as dict.
        second = run_daily_review(self.repo, day_utc=day_utc)
        self.assertTrue(second.get("idempotent"), "P8-4-F: second call must be idempotent.")
        self.assertEqual(second["daily_review_report_id"], report_id)
        # summary_json read back as a dict (JSONB-as-dict survival).
        self.assertIsInstance(second["summary"], dict)
        self.assertIn("paper_summary", second["summary"])
        # ga_report text persisted.
        with self.conn.cursor() as cur:
            cur.execute("SELECT ga_report, pushed_to_feishu FROM daily_review_reports WHERE id=%s",
                        (report_id,))
            row = cur.fetchone()
        self.assertTrue(row["ga_report"], "P8-4-F: ga_report must persist.")
        self.assertFalse(row["pushed_to_feishu"])

    # ── P8-4-G: force rebuild archives old skill_feedback_memory ─────────────

    def test_daily_review_force_archives_old_skill_memory(self) -> None:
        """P8-4-G: ``run_daily_review(force=True)`` on a day with an existing
        report archives old ``skill_feedback_memory`` rows whose finding matches
        the review date. The archive UPDATE is wrapped in ``with
        repo.conn.transaction():`` (no bare ``commit()``). Asserts the rows
        flip to ``status='archived'``."""
        self._seed_active_strategy()
        day_utc = "2024-01-01"
        close_time_ms = _now_ms()
        self._seed_candle("TESTUSDT", close=100.0, close_time_ms=close_time_ms)
        self._seed_snapshot_signal_order_trade(
            "TESTUSDT", entry_price=100.0, stop_loss=95.0,
            close_price=94.0, close_reason="stop_loss", close_time_ms=close_time_ms,
        )
        self.conn.commit()

        with patch(
            "plugins.crypto_guard.strategy.shadow_testing.run_backtest_gate",
            return_value={"ok": True, "passed": False, "reason": "skipped", "skipped": True},
        ):
            run_daily_review(self.repo, day_utc=day_utc)  # builds skill memory rows
            # Seed a stale matching skill_feedback_memory row to be archived.
            self.repo.save_skill_feedback_memory(
                skill_name="price_action",
                feedback_type="daily_review",
                source_type="daily_review",
                finding=f"每日复盘：stale entry for {day_utc}",
                suggested_adjustment={"loss_count": 0, "evolution_triggered": False},
            )
            self.conn.commit()
            forced = run_daily_review(self.repo, day_utc=day_utc, force=True)

        self.assertTrue(forced.get("daily_review_report_id"), "P8-4-F: force rebuild must still save a report.")
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM skill_feedback_memory "
                "WHERE source_type='daily_review' AND finding LIKE %s AND status='archived'",
                (f"每日复盘：%{day_utc}%",),
            )
            archived = int(cur.fetchone()["cnt"])
        self.assertGreater(
            archived, 0,
            "P8-4-G: force=True must archive old skill_feedback_memory rows (transaction wrap).",
        )


if __name__ == "__main__":
    unittest.main()
