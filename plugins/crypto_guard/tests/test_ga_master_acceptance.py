from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class GAMasterAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("CRYPTO_GUARD_DB")
        self.old_llm = os.environ.get("CRYPTO_GUARD_LLM_ANALYSIS")
        self.old_redis_disabled = os.environ.get("CRYPTO_GUARD_REDIS_DISABLED")
        os.environ["CRYPTO_GUARD_DB"] = str(Path(self.tmp.name) / "crypto_guard.sqlite3")
        os.environ["CRYPTO_GUARD_LLM_ANALYSIS"] = "0"

        from plugins.crypto_guard.storage.migrations import initialize_database
        from plugins.crypto_guard.storage.repository import CryptoGuardRepository
        from plugins.crypto_guard.storage.sqlite_db import connect_db

        initialize_database()
        self.conn = connect_db(os.environ["CRYPTO_GUARD_DB"])
        self.repo = CryptoGuardRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        _restore_env("CRYPTO_GUARD_DB", self.old_db)
        _restore_env("CRYPTO_GUARD_LLM_ANALYSIS", self.old_llm)
        _restore_env("CRYPTO_GUARD_REDIS_DISABLED", self.old_redis_disabled)
        self.tmp.cleanup()

    def test_feishu_actions_follow_grade_and_risk_rules(self) -> None:
        from plugins.crypto_guard.ga_master.feishu_action_builder import build_feishu_actions

        self.assertEqual(build_feishu_actions({"signal_grade": "D"}, {}), ["add_to_watchlist", "ignore"])
        self.assertEqual(build_feishu_actions({"signal_grade": "C"}, {}), ["add_to_watchlist", "ignore"])
        self.assertEqual(build_feishu_actions({"signal_grade": "B", "opportunity_watch": {"watch_conditions": ["x"]}}, {}), ["create_opportunity_watch", "add_to_watchlist", "ignore"])
        self.assertNotIn("create_paper_order", build_feishu_actions({"signal_grade": "A", "trade_plan": None}, {"ok": True}))
        # A/S 级别需要 confidence >= 0.72 才能创建 paper order
        actions = build_feishu_actions({"signal_grade": "A", "confidence": 0.8, "has_trade_plan": True, "trade_plan": {"entry": 1}}, {"ok": True})
        self.assertEqual(actions[0], "create_paper_order")
        # confidence 不足时不能创建 paper order
        actions_low_conf = build_feishu_actions({"signal_grade": "A", "confidence": 0.65, "has_trade_plan": True, "trade_plan": {"entry": 1}}, {"ok": True})
        self.assertNotIn("create_paper_order", actions_low_conf)

    def test_ga_decision_persistence(self) -> None:
        from plugins.crypto_guard.ga_master.decision_schema import controller_decision_from_legacy

        decision = controller_decision_from_legacy(
            legacy={
                "symbol": "BTCUSDT",
                "decision": "no_edge",
                "signal_grade": "D",
                "confidence": 0.1,
                "summary": "store only",
                "risk_check": {"ok": False, "reasons": ["no edge"]},
            },
            decision_type="unit_acceptance",
            analysis_time=1_700_000_000_000,
            skill_result_refs={"price_action": 1},
            feishu_actions=["add_to_watchlist", "ignore"],
        )
        ga_decision_id = self.repo.create_ga_decision(decision)
        row = self.repo.get_ga_decision(ga_decision_id)
        self.assertEqual(row["created_by"], "ga_master_controller")
        self.assertEqual(row["skill_result_refs"], {"price_action": 1})

    def test_legacy_signal_paper_order_is_backed_by_ga_decision(self) -> None:
        from plugins.crypto_guard.paper.paper_broker import create_paper_order_from_signal

        snapshot_id = self.repo.save_market_snapshot(_risk_approved_snapshot("BTCUSDT"))
        signal_id = self.repo.create_signal(
            {
                "symbol": "BTCUSDT",
                "decision": "trade_plan_available",
                "signal_grade": "A",
                "confidence": 0.8,
                "summary": "legacy compatibility",
                "has_trade_plan": True,
                "risk_notes": [],
                "trade_plan": _trade_plan(),
            },
            snapshot_id,
        )
        result = create_paper_order_from_signal(self.repo, signal_id)
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["ga_decision_id"])
        order = self.conn.execute("SELECT ga_decision_id, source, risk_check_passed FROM paper_orders WHERE id=?", (result["order_id"],)).fetchone()
        self.assertEqual(order["source"], "ga_decision")
        self.assertEqual(int(order["risk_check_passed"]), 1)
        self.assertEqual(int(order["ga_decision_id"]), int(result["ga_decision_id"]))

    def test_parquet_merge_dedupe_and_duckdb_read(self) -> None:
        from plugins.crypto_guard.storage.duckdb_analytics import DuckDBAnalytics
        from plugins.crypto_guard.storage.parquet_archive import ParquetKlineArchive

        root = Path(self.tmp.name) / "parquet" / "klines" / "binance_um"
        archive = ParquetKlineArchive(root=root)
        candles = [
            _candle(close=1.1),
            _candle(close=1.2),
        ]
        result = archive.write_closed_klines(candles)
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["rows_written"], 1)

        analytics = DuckDBAnalytics(database_path=Path(self.tmp.name) / "duckdb" / "test.duckdb", parquet_root=root)
        health = analytics.health_check()
        if health.get("status") != "ok":
            self.skipTest(f"DuckDB unavailable: {health}")
        rows = analytics.query_klines("BTCUSDT", "5m")
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["close"]), 1.2)

    def test_temp_database_uses_sqlite_queue_fallback(self) -> None:
        from plugins.crypto_guard.storage.redis_adapter import should_use_redis_for_path

        self.assertFalse(should_use_redis_for_path(os.environ["CRYPTO_GUARD_DB"]))
        job_id = self.repo.enqueue_job("unit_job", 1, "test", "session", {})
        row = self.conn.execute("SELECT id, status FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "pending")

    def test_same_feishu_message_id_sends_ad_hoc_result_once(self) -> None:
        from plugins.crypto_guard.run_ga_workers import _maybe_send_feishu_result

        sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def fake_send(*args: object, **kwargs: object) -> str:
            sent.append((args, kwargs))
            return f"msg_{len(sent)}"

        payload = {"receive_id": "chat_1", "receive_id_type": "chat_id", "message_id": "om_acceptance_once"}
        result = {"card_json": "{\"schema\":\"2.0\",\"body\":{\"elements\":[]}}", "symbol": "BTCUSDT", "signal_id": 1}
        _maybe_send_feishu_result(self.repo, payload, result, fake_send)
        _maybe_send_feishu_result(self.repo, payload, result, fake_send)
        self.assertEqual(len(sent), 1)


def _restore_env(key: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


def _risk_approved_snapshot(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "analysis_time_utc": 1_700_000_000_000,
        "mode": "ad_hoc",
        "profiles": {
            "4h": {"market_structure": "bullish", "trend_stage": "middle", "momentum": "bullish", "candles_count": 80},
            "1h": {"market_structure": "bullish", "trend_stage": "middle", "momentum": "bullish", "candles_count": 80},
            "15m": {"market_structure": "bullish", "trend_stage": "early", "momentum": "bullish", "candles_count": 80},
            "5m": {"market_structure": "bullish", "trend_stage": "early", "momentum": "bullish", "candles_count": 80},
        },
        "modules": {"market_regime": {"regime": "normal", "extreme": False, "evolution_trigger_allowed": True}},
        "counter_evidence": {
            "bullish_evidence": ["higher timeframe supports long", "momentum aligned"],
            "bearish_evidence": [],
            "neutral_or_risk_evidence": [],
            "contradiction_level": "low",
        },
        "data_quality": {"closed_candles_only": True, "status": "complete"},
        "paper_context": {},
        "global_context": {"time_policy": "closed candles only"},
    }


def _trade_plan() -> dict[str, object]:
    return {
        "side": "LONG",
        "entry_type": "limit",
        "entry_price": 100.0,
        "trigger_price": None,
        "stop_loss": 95.0,
        "take_profits": [{"price": 110.0, "ratio": 1.0}],
        "risk_percent": 0.5,
        "invalid_condition": "below 95",
        "reason": "unit acceptance",
    }


def _candle(*, close: float) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "interval": "5m",
        "open_time": 1_779_795_000_000,
        "close_time": 1_779_795_299_999,
        "open": 1.0,
        "high": 1.4,
        "low": 0.9,
        "close": close,
        "volume": 100,
        "is_closed": True,
    }
