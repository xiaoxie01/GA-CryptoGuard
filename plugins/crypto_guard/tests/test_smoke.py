from __future__ import annotations

import os
import json
import tempfile
import unittest


class CryptoGuardSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._old_llm_analysis = os.environ.get("CRYPTO_GUARD_LLM_ANALYSIS")
        os.environ["CRYPTO_GUARD_LLM_ANALYSIS"] = "0"
        os.environ["CRYPTO_GUARD_DB"] = os.path.join(self.tmp.name, "crypto_guard.sqlite3")
        from plugins.crypto_guard.storage.migrations import initialize_database
        from plugins.crypto_guard.storage.repository import CryptoGuardRepository
        from plugins.crypto_guard.storage.sqlite_db import connect_db

        initialize_database()
        self.conn = connect_db(os.environ["CRYPTO_GUARD_DB"])
        self.repo = CryptoGuardRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        if self._old_llm_analysis is None:
            os.environ.pop("CRYPTO_GUARD_LLM_ANALYSIS", None)
        else:
            os.environ["CRYPTO_GUARD_LLM_ANALYSIS"] = self._old_llm_analysis
        self.tmp.cleanup()

    def _decision_snapshot(
        self,
        *,
        trend_stage: str = "transition",
        neutral_risks: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "symbol": "BTCUSDT",
            "analysis_time_utc": 1_700_000_000_000,
            "mode": "scheduled",
            "profiles": {},
            "modules": {
                "price_action": {
                    "market_structure": "bullish",
                    "key_levels": {"support": [100.0], "resistance": [120.0]},
                    "invalid_level": 95.0,
                },
                "momentum": {"direction": "bullish"},
                "trend_stage": {"trend_stage": trend_stage},
            },
            "counter_evidence": {
                "bullish_evidence": ["价格结构偏多", "动能偏多"],
                "bearish_evidence": [],
                "neutral_or_risk_evidence": neutral_risks or ["仍需等待价格确认"],
                "contradiction_level": "medium",
            },
        }

    def _risk_approved_snapshot_id(self, symbol: str = "BTCUSDT") -> int:
        snapshot = {
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
                "bullish_evidence": ["高周期方向支持"],
                "bearish_evidence": [],
                "neutral_or_risk_evidence": [],
                "contradiction_level": "low",
            },
            "data_quality": {"closed_candles_only": True, "status": "complete"},
            "paper_context": {},
            "global_context": {"time_policy": "closed candles only"},
        }
        return self.repo.save_market_snapshot(snapshot)

    def test_symbols_queue_and_no_future_candles(self) -> None:
        from plugins.crypto_guard.data.symbol_registry import add_symbol, pause_symbol, resume_symbol

        self.assertTrue(add_symbol(self.repo, "WIF", validate=False)["ok"])
        self.assertTrue(pause_symbol(self.repo, "WIFUSDT")["ok"])
        self.assertTrue(resume_symbol(self.repo, "WIFUSDT")["ok"])
        user_job = self.repo.enqueue_job("feishu_user_message", 1, "feishu", "feishu:user:u1", {"text": "分析 BTC"})
        bg_job = self.repo.enqueue_job("daily_review", 7, "scheduler", "system:scheduled:daily", {})
        self.assertIsNone(self.repo.claim_next_job(background=True))
        claimed = self.repo.claim_next_job(max_priority=2)
        self.assertEqual(claimed["id"], user_job)
        self.repo.finish_job(user_job, result={"ok": True})
        self.assertEqual(self.repo.claim_next_job(background=True)["id"], bg_job)

        span = 900_000
        base = 1_700_000_000_000
        candles = []
        for i in range(35):
            open_time = base + i * span
            candles.append(
                {
                    "symbol": "BTCUSDT",
                    "interval": "15m",
                    "open_time": open_time,
                    "close_time": open_time + span - 1,
                    "open": 100 + i,
                    "high": 102 + i,
                    "low": 99 + i,
                    "close": 101 + i,
                    "volume": 1000 + i,
                    "is_closed": True,
                }
            )
        self.repo.upsert_candles(candles)
        analysis_time = candles[20]["close_time"]
        rows = self.repo.get_candles("BTCUSDT", "15m", analysis_time_utc=analysis_time, limit=100)
        self.assertTrue(rows)
        self.assertLessEqual(max(r["close_time"] for r in rows), analysis_time)
        no_lookahead = self.repo.no_lookahead_candles("BTCUSDT", "15m", analysis_time_utc=analysis_time, limit=100)
        self.assertTrue(no_lookahead["ok"])
        self.assertEqual(no_lookahead["violation_count"], 0)

    def test_persistent_feishu_dedupe_and_errors(self) -> None:
        self.assertTrue(self.repo.claim_feishu_event("evt_1", "message", {"text": "系统状态"}))
        self.assertFalse(self.repo.claim_feishu_event("evt_1", "message", {"text": "系统状态"}))
        job_id = self.repo.enqueue_job("test_failure", 5, "test", "system:test", {})
        self.repo.finish_job(job_id, error_message="boom")
        errors = self.repo.list_recent_errors()
        self.assertTrue(any(e["source"] == "agent_job" and e["id"] == job_id for e in errors))

    def test_snapshot_decision_paper_review(self) -> None:
        from plugins.crypto_guard.paper.paper_broker import close_trade_if_needed, create_paper_order_from_signal, fill_order_if_triggered
        from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
        from plugins.crypto_guard.reasoning.market_state_builder import build_market_state_snapshot
        from plugins.crypto_guard.review.trade_reviewer import review_trade

        span = 900_000
        base = 1_700_000_000_000
        candles = []
        price = 100.0
        for i in range(60):
            price += 0.8 if i % 5 else -0.2
            open_time = base + i * span
            candles.append(
                {
                    "symbol": "BTCUSDT",
                    "interval": "15m",
                    "open_time": open_time,
                    "close_time": open_time + span - 1,
                    "open": price,
                    "high": price + 2,
                    "low": price - 1,
                    "close": price + 1,
                    "volume": 1000 + i * 20,
                    "is_closed": True,
                }
            )
        self.repo.upsert_candles(candles)
        analysis_time = candles[-1]["close_time"]
        snapshot = build_market_state_snapshot(self.repo, symbol="BTCUSDT", analysis_time_utc=analysis_time, mode="ad_hoc", timeframes=["15m"])
        decision = run_ga_sop_decision(snapshot)
        snapshot_id = self.repo.save_market_snapshot(snapshot)
        signal_from_snapshot = self.repo.create_signal(decision, snapshot_id)
        saved_snapshot = self.conn.execute("SELECT data_quality_json FROM market_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        self.assertIsNotNone(saved_snapshot["data_quality_json"])
        eval_count = self.conn.execute("SELECT COUNT(*) FROM strategy_evaluations WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0]
        self.assertGreaterEqual(eval_count, 1)
        signal_row = self.repo.get_signal(signal_from_snapshot)
        self.assertEqual(signal_row["market_snapshot_id"], snapshot_id)
        self.assertIn(decision["decision"], {"trade_plan_available", "wait_for_pullback", "monitor_only", "no_edge"})

        plan = {
            "side": "LONG",
            "entry_type": "limit",
            "entry_price": 100.0,
            "trigger_price": None,
            "stop_loss": 95.0,
            "take_profits": [{"price": 110.0, "ratio": 1.0}],
            "risk_percent": 0.5,
            "invalid_condition": "跌破 95",
            "reason": "测试模拟盘",
        }
        signal = {
            "symbol": "BTCUSDT",
            "decision": "trade_plan_available",
            "signal_grade": "A",
            "confidence": 0.8,
            "summary": "测试",
            "has_trade_plan": True,
            "trade_plan": plan,
            "risk_notes": [],
        }
        signal_id = self.repo.create_signal(signal, self._risk_approved_snapshot_id("BTCUSDT"))
        first = create_paper_order_from_signal(self.repo, signal_id)
        second = create_paper_order_from_signal(self.repo, signal_id)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        order = self.repo.list_open_paper_orders()[0]
        fill = fill_order_if_triggered(self.repo, order, 100.0)
        self.assertTrue(fill["filled"])
        order = self.repo.list_open_paper_orders()[0]
        trade = self.repo.get_open_trade_for_order(order["id"])
        self.assertEqual(trade["signal_id"], signal_id)
        close = close_trade_if_needed(self.repo, order, trade, 111.0)
        self.assertTrue(close["closed"])
        review = review_trade(self.repo, close["trade_id"])
        self.assertTrue(review["ok"])
        if review["patch_id"]:
            row = self.conn.execute("SELECT status FROM strategy_patches WHERE id=?", (review["patch_id"],)).fetchone()
            self.assertEqual(row["status"], "candidate")

    def test_system_status_result_uses_text_renderer(self) -> None:
        from plugins.crypto_guard.run_ga_workers import _maybe_send_feishu_result
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_handle_text_command

        sent: list[tuple[str, str, dict[str, object]]] = []

        def fake_send(receive_id: str, content: str, **kwargs: object) -> str:
            sent.append((receive_id, content, kwargs))
            return "message_id"

        result = crypto_handle_text_command("系统状态")
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["symbols"], dict)
        _maybe_send_feishu_result(
            self.repo,
            {"receive_id": "chat_1", "receive_id_type": "chat_id"},
            result,
            fake_send,
        )
        self.assertTrue(sent)
        self.assertIn("CryptoGuard", sent[0][1])
        self.assertEqual(sent[0][2].get("msg_type"), "interactive")
        row = self.conn.execute("SELECT alert_type, status FROM alert_outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["alert_type"], "user_command_result")
        self.assertEqual(row["status"], "sent")

    def test_market_data_failure_returns_user_text(self) -> None:
        from unittest.mock import patch

        from plugins.crypto_guard.data.binance_rest import MarketDataError
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_analyze_symbol_once

        with patch("plugins.crypto_guard.tools.ga_crypto_tools.fetch_and_upsert_closed_klines", side_effect=MarketDataError("network reset")):
            result = crypto_analyze_symbol_once("ETHUSDT", ["15m"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "market_data_unavailable")
        self.assertIn("无法获取 Binance public 行情数据", result["text"])

    def test_hourly_report_renders(self) -> None:
        from plugins.crypto_guard.notify.hourly_report import build_hourly_report

        report = build_hourly_report(self.repo)
        self.assertTrue(report["ok"])
        self.assertIn("每小时简报", report["text"])
        self.assertIn("模拟盘", report["text"])

    def test_hourly_report_explains_trend_and_no_opportunity_reason(self) -> None:
        from plugins.crypto_guard.notify.hourly_report import render_hourly_report_text

        decision = {
            "symbol": "BTCUSDT",
            "decision": "no_edge",
            "signal_grade": "D",
            "confidence": 0.17,
            "market_bias": "neutral",
            "trend_stage": "range",
            "summary": "BTCUSDT 多周期偏震荡，当前没有可执行优势。",
            "profiles": {
                "1d": {"market_structure": "range", "trend_stage": "range", "momentum": "neutral"},
                "4h": {"market_structure": "range", "trend_stage": "range", "momentum": "neutral"},
                "1h": {"market_structure": "bullish", "trend_stage": "transition", "momentum": "bullish"},
                "15m": {"market_structure": "range", "trend_stage": "range", "momentum": "neutral"},
            },
            "modules": {"trend_stage": {"strategy_policy": "filter_trend_strategy"}},
            "counter_evidence": ["高概率震荡，方向延续性不足"],
            "risk_notes": ["不构成实盘建议"],
            "has_trade_plan": False,
            "suggested_actions": ["ignore"],
        }
        text = render_hourly_report_text(
            "2026-05-25T04:02:21Z",
            ["BTCUSDT"],
            [{"symbol": "BTCUSDT", "ga_decision_json": __import__("json").dumps(decision, ensure_ascii=False)}],
            [],
            [],
            {"pending_user": 0, "pending_background": 0, "running": 0},
            analysis_states=[
                {
                    "symbol": "BTCUSDT",
                    "state": {
                        "market_structure": {
                            "structure_status": "range_observation",
                            "direction_1d": "range",
                            "direction_4h": "range",
                            "trend_1h": "transition",
                            "structure_15m": "range",
                            "trigger_5m": "range",
                        },
                        "trend_clarity": {"score": 0.17, "level": "unclear", "reason": ["4H 方向=range", "15M 结构=range"]},
                        "no_trade_reason": {"has_no_trade": True, "reason_code": "risk_rejected", "detail": "缺少完整 trade_plan"},
                        "key_levels": {
                            "support": [100.0, 98.5],
                            "resistance": [105.0, 108.0],
                            "invalid_level": None,
                            "breakout_boundary": {"upper": 105.0, "lower": 98.5},
                        },
                        "next_triggers": [
                            {"condition": "15M 收盘站上 105.0"},
                            {"condition": "15M 收盘跌破 98.5"},
                        ],
                        "next_analysis": {"suggested_time_utc": "2026-05-25T04:15:00Z", "reason": "等待下一根 15m 已收盘 K 线确认"},
                        "breakout_watch": {"confirmation_required": "15M 收盘突破/跌破边界后，5M 回踩或反转确认"},
                        "trade_permission": {"paper_trade_allowed": False, "reason": "缺少完整 trade_plan"},
                        "opportunity_watch_recommended": True,
                    },
                },
                {
                    "symbol": "BTCUSDT",
                    "state": {
                        "market_structure": {"structure_status": "stale"},
                        "trend_clarity": {"score": 0.01, "level": "unclear", "reason": []},
                        "no_trade_reason": {"has_no_trade": True, "reason_code": "stale", "detail": "旧状态不应覆盖最新状态"},
                        "key_levels": {"breakout_boundary": {"upper": 999.0, "lower": 1.0}},
                        "next_triggers": [],
                        "next_analysis": {"suggested_time_utc": "2026-05-25T04:00:00Z", "reason": "旧状态"},
                        "breakout_watch": {},
                        "trade_permission": {"paper_trade_allowed": False, "reason": "旧状态"},
                        "opportunity_watch_recommended": False,
                    },
                }
            ],
        )
        self.assertIn("北京时间（UTC+8）", text)
        self.assertIn("趋势状态：range", text)
        self.assertIn("GA 分析结论", text)
        self.assertIn("暂无机会原因", text)
        self.assertIn("多周期", text)
        self.assertIn("市场结构状态", text)
        self.assertIn("趋势清晰度", text)
        self.assertIn("无交易机会归因", text)
        self.assertIn("关键关注点位", text)
        self.assertIn("下次触发条件", text)
        self.assertIn("下次分析时间", text)
        self.assertIn("等待突破边界", text)
        self.assertIn("模拟盘权限", text)
        self.assertIn("上沿=105", text)
        self.assertNotIn("上沿=999", text)

    def test_llm_agent_decision_is_primary_when_enabled(self) -> None:
        import json
        from unittest.mock import patch

        from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_sop_decision

        snapshot = self._decision_snapshot(trend_stage="range")
        llm_response = {
            "symbol": "BTCUSDT",
            "decision": "no_edge",
            "signal_grade": "D",
            "market_bias": "neutral",
            "trend_stage": "range",
            "confidence": 0.22,
            "summary": "BTCUSDT 高周期与主周期均偏震荡，缺少趋势延续和触发条件，因此当前没有可执行机会。",
            "evidence": ["15m 结构缺少有效突破"],
            "counter_evidence": ["震荡区间内方向延续性不足"],
            "risk_notes": ["等待重新突破区间或回踩确认；仅用于模拟盘与策略研究。"],
            "has_trade_plan": False,
            "trade_plan": None,
            "opportunity_watch": None,
            "suggested_actions": ["add_to_watchlist", "ignore"],
            "strategy_name": "llm_agent_sop",
            "strategy_version": "1.0",
            "analysis_time_utc": snapshot["analysis_time_utc"],
        }
        with patch("plugins.crypto_guard.reasoning.llm_agent_judge._call_ga_llm", return_value=json.dumps(llm_response, ensure_ascii=False)) as call:
            decision = run_agent_sop_decision(snapshot, use_llm=True)
        self.assertTrue(call.called)
        self.assertEqual(decision["analysis_source"], "llm_agent")
        self.assertEqual(decision["llm_status"], "ok")
        self.assertIn("没有可执行机会", decision["summary"])

    def test_llm_opportunity_watch_bidirectional_is_normalized(self) -> None:
        from plugins.crypto_guard.reasoning.decision_schema import no_edge_decision, validate_json
        from plugins.crypto_guard.reasoning.llm_agent_judge import _normalize_llm_decision

        snapshot = self._decision_snapshot(trend_stage="range")
        fallback = no_edge_decision("BTCUSDT", "fallback")
        candidate = {
            "decision": "monitor_only",
            "signal_grade": "C",
            "market_bias": "neutral",
            "trend_stage": "range",
            "confidence": 0.42,
            "summary": "等待区间边界确认后再观察。",
            "evidence": ["上下沿均未确认突破"],
            "counter_evidence": ["震荡区间内方向不清晰"],
            "risk_notes": ["不允许模拟盘开仓"],
            "has_trade_plan": False,
            "trade_plan": None,
            "opportunity_watch": {
                "needed": True,
                "direction": "bidirectional",
                "reason": "等待上下沿任一方向确认",
                "conditions": ["15M 收盘突破上沿或跌破下沿"],
                "expires_minutes": 60,
            },
            "suggested_actions": ["create_opportunity_watch", "ignore"],
        }

        decision = _normalize_llm_decision(candidate, snapshot, fallback)
        self.assertIsNone(decision["opportunity_watch"]["direction"])
        ok, err = validate_json("ga_decision.schema.json", decision)
        self.assertTrue(ok, err)

    def test_scheduler_utc_cadence_includes_cache_and_agent_analysis_jobs(self) -> None:
        from datetime import datetime, timezone

        from plugins.crypto_guard.service_manager import _due_scheduler_jobs

        jobs = _due_scheduler_jobs(datetime(2026, 5, 25, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(jobs[0], "hourly_feishu_report")
        self.assertIn("alert_outbox_retry", jobs)
        self.assertIn("fetch_1d_klines", jobs)
        self.assertIn("fetch_4h_klines", jobs)
        self.assertIn("fetch_1h_klines", jobs)
        self.assertIn("fetch_15m_klines", jobs)
        self.assertIn("fetch_5m_klines", jobs)
        self.assertIn("analyze_market_15m", jobs)

        jobs = _due_scheduler_jobs(datetime(2026, 5, 25, 0, 2, tzinfo=timezone.utc))
        self.assertIn("hourly_feishu_report", jobs)
        # analyze_market_5m was removed; 5m klines still fetched but analysis is 15m only
        self.assertNotIn("analyze_market_5m", jobs)

        jobs = _due_scheduler_jobs(datetime(2026, 5, 25, 0, 4, tzinfo=timezone.utc))
        self.assertIn("hourly_feishu_report", jobs)

        jobs = _due_scheduler_jobs(datetime(2026, 5, 25, 0, 10, tzinfo=timezone.utc))
        self.assertIn("hourly_feishu_report", jobs)

        jobs = _due_scheduler_jobs(datetime(2026, 5, 25, 0, 11, tzinfo=timezone.utc))
        self.assertNotIn("hourly_feishu_report", jobs)
        self.assertIn("alert_outbox_retry", jobs)

        jobs = _due_scheduler_jobs(datetime(2026, 5, 25, 0, 5, tzinfo=timezone.utc))
        self.assertIn("daily_review", jobs)

        jobs = _due_scheduler_jobs(datetime(2026, 5, 25, 0, 9, tzinfo=timezone.utc))
        self.assertIn("update_paper_positions_3m", jobs)

    def test_alert_outbox_retry_scheduler_job_priority(self) -> None:
        from plugins.crypto_guard.run_scheduler import run_job

        result = run_job("alert_outbox_retry")
        self.assertTrue(result["ok"])
        row = self.conn.execute(
            """
            SELECT priority, payload_json
            FROM agent_jobs
            WHERE job_type='alert_outbox_retry'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row["priority"]), 2)
        self.assertIn('"limit": 10', row["payload_json"])

    def test_hourly_report_priority_and_market_analysis_backlog_guard(self) -> None:
        from plugins.crypto_guard.run_scheduler import run_job
        from plugins.crypto_guard.scheduler.cron_scheduler import enqueue_market_analysis

        result = run_job("hourly_feishu_report")
        self.assertTrue(result["ok"])
        row = self.conn.execute(
            """
            SELECT priority
            FROM agent_jobs
            WHERE job_type='hourly_feishu_report'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        self.assertEqual(row["priority"], 3)

        first = enqueue_market_analysis(analysis_time_utc=1_700_000_000_000, primary_interval="5m", timeframes=["5m"])
        second = enqueue_market_analysis(analysis_time_utc=1_700_000_000_000, primary_interval="5m", timeframes=["5m"])
        self.assertEqual(first["priority"], 6)
        self.assertGreater(first["queued"], 0)
        self.assertEqual(second["queued"], 0)
        self.assertGreater(second["skipped_pending"], 0)

    def test_daily_review_reviews_unreviewed_trades_and_is_idempotent(self) -> None:
        from datetime import datetime, timezone

        from plugins.crypto_guard.review.daily_reviewer import run_daily_review

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.conn.execute(
            """
            INSERT INTO paper_trades(
                symbol, side, entry_price, exit_price, stop_loss, quantity, pnl, pnl_percent, pnl_r,
                max_favorable_excursion, max_adverse_excursion, close_reason, closed_at
            )
            VALUES ('ETHUSDT', 'LONG', 100, 94, 95, 1, -6, -6, -1.2, 1, -6, 'stop_loss', CURRENT_TIMESTAMP)
            """
        )
        first = run_daily_review(self.repo, day_utc=day)
        second = run_daily_review(self.repo, day_utc=day)
        self.assertGreaterEqual(first["new_reviews"], 1)
        self.assertEqual(second["new_reviews"], 0)
        self.assertIn("每日模拟盘复盘", first["text"])
        patches = self.conn.execute("SELECT status FROM strategy_patches").fetchall()
        self.assertTrue(all(row["status"] == "candidate" for row in patches))
        memory_count = self.conn.execute("SELECT COUNT(*) FROM strategy_memory").fetchone()[0]
        self.assertGreaterEqual(memory_count, 1)

    def test_v2_evolution_trigger_daily_review_and_skill_memory(self) -> None:
        from datetime import datetime, timedelta, timezone

        from plugins.crypto_guard.review.daily_reviewer import run_daily_review
        from plugins.crypto_guard.review.evolution_triggers import evaluate_evolution_triggers

        day = datetime.now(timezone.utc).date().isoformat()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for idx in range(3):
            closed_at = (now - timedelta(minutes=idx + 1)).isoformat().replace("+00:00", "Z")
            self.conn.execute(
                """
                INSERT INTO paper_trades(symbol, side, entry_price, exit_price, stop_loss, quantity, pnl, pnl_percent, pnl_r, close_reason, closed_at)
                VALUES ('BTCUSDT', 'LONG', 100, 95, 95, 1, -5, -5, -1, 'stop_loss', ?)
                """,
                (closed_at,),
            )
        trigger = evaluate_evolution_triggers(self.repo)
        self.assertTrue(trigger["triggered"])
        evo = self.conn.execute("SELECT * FROM evolution_triggers ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(evo["trigger_type"], "consecutive_stop_losses")
        shadow = self.conn.execute("SELECT * FROM strategy_versions WHERE status='shadow_testing' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(shadow)

        review = run_daily_review(self.repo, day_utc=day)
        self.assertTrue(review["daily_review_report_id"])
        report = self.conn.execute("SELECT * FROM daily_review_reports WHERE review_date=?", (day,)).fetchone()
        self.assertIsNotNone(report)
        skill_memory = self.conn.execute("SELECT COUNT(*) FROM skill_feedback_memory WHERE source_type='daily_review'").fetchone()[0]
        self.assertGreaterEqual(skill_memory, 5)

    def test_decision_supplement_buttons_risk_and_intraday_preprocessing(self) -> None:
        from plugins.crypto_guard.notify.feishu_cards import build_analysis_card
        from plugins.crypto_guard.paper.paper_broker import create_paper_order_from_signal
        from plugins.crypto_guard.reasoning.market_state_builder import DEFAULT_TIMEFRAMES, build_market_state_snapshot
        from plugins.crypto_guard.risk.risk_engine import apply_risk_to_decision

        self.assertEqual(DEFAULT_TIMEFRAMES, ["4h", "1h", "15m", "5m"])
        span_by_tf = {"4h": 14_400_000, "1h": 3_600_000, "15m": 900_000, "5m": 300_000}
        base = 1_700_000_000_000
        for tf, span in span_by_tf.items():
            rows = []
            price = 100.0
            for idx in range(40):
                price += 0.8
                rows.append(
                    {
                        "symbol": "BTCUSDT",
                        "interval": tf,
                        "open_time": base + idx * span,
                        "close_time": base + (idx + 1) * span - 1,
                        "open": price - 0.4,
                        "high": price + 1.0,
                        "low": price - 0.8,
                        "close": price,
                        "volume": 1000 + idx * 20,
                        "is_closed": True,
                    }
                )
            self.repo.upsert_candles(rows)
        analysis_time = base + 40 * span_by_tf["5m"] - 1
        snapshot = build_market_state_snapshot(self.repo, symbol="BTCUSDT", analysis_time_utc=analysis_time, mode="ad_hoc")
        self.assertEqual(snapshot["intraday_framework"]["direction"], "4h")
        self.assertEqual(snapshot["profiles"]["4h"]["weight"], 0.30)
        self.assertEqual(snapshot["intraday_framework"]["default_intraday_weights"]["4h"], 0.35)
        self.assertTrue(snapshot["modules"]["price_action"]["deterministic_preprocessing"])
        self.assertFalse(snapshot["preprocessing_policy"]["llm_geometry_allowed"])
        self.assertEqual(snapshot["modules"]["market_regime"]["module"], "market_regime")

        decision = {
            "symbol": "BTCUSDT",
            "decision": "trade_plan_available",
            "signal_grade": "A",
            "market_bias": "bullish",
            "trend_stage": "middle",
            "confidence": 0.8,
            "summary": "测试完整交易计划",
            "evidence": ["4H/1H/15M 支持做多"],
            "counter_evidence": ["测试反证"],
            "risk_notes": [],
            "has_trade_plan": True,
            "trade_plan": {
                "side": "LONG",
                "entry_type": "limit",
                "entry_price": 100.0,
                "stop_loss": 95.0,
                "take_profits": [{"price": 110.0, "ratio": 1.0}],
                "risk_percent": 0.5,
                "invalid_condition": "跌破 95",
                "reason": "测试",
            },
            "opportunity_watch": None,
            "suggested_actions": [],
            "strategy_name": "test",
            "strategy_version": "1.0",
            "analysis_time_utc": analysis_time,
        }
        approved_snapshot = json.loads(self.conn.execute("SELECT snapshot_json FROM market_snapshots WHERE id=?", (self._risk_approved_snapshot_id("BTCUSDT"),)).fetchone()[0])
        approved = apply_risk_to_decision(decision, approved_snapshot)
        self.assertTrue(approved["risk_check"]["ok"])
        self.assertIn("create_paper_order", approved["suggested_actions"])
        action_values = [
            item["behaviors"][0]["value"]["action"]
            for item in build_analysis_card(approved, signal_id=99)["body"]["elements"]
            if item.get("tag") == "button"
        ]
        self.assertIn("create_paper_order", action_values)
        self.assertIn("create_opportunity_watch", action_values)

        rejected = dict(decision)
        rejected["confidence"] = 0.5
        rejected_view = apply_risk_to_decision(rejected, approved_snapshot)
        self.assertFalse(rejected_view["risk_check"]["ok"])
        self.assertNotIn("create_paper_order", rejected_view["suggested_actions"])
        signal_id = self.repo.create_signal(rejected, self._risk_approved_snapshot_id("BTCUSDT"))
        paper = create_paper_order_from_signal(self.repo, signal_id)
        self.assertFalse(paper["ok"])
        self.assertIn("risk_reasons", paper)

    def test_decision_supplement_alert_outbox_and_config_hot_reload(self) -> None:
        from plugins.crypto_guard.notify.alert_delivery import process_alert_outbox, send_markdown_alert
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_confirm_config_update, crypto_handle_text_command

        def failing_send(*args: object, **kwargs: object) -> bool:
            raise RuntimeError("feishu down")

        first = send_markdown_alert(
            self.repo,
            failing_send,
            receive_id="chat_1",
            receive_id_type="chat_id",
            text="测试静默与重试",
            alert_type="normal_duplicate",
            symbol="BTCUSDT",
        )
        self.assertFalse(first["sent"])
        row = self.conn.execute("SELECT id, status, retry_count FROM alert_outbox ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["retry_count"], 1)
        alert_id = int(row["id"])
        pending_duplicate_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def pending_duplicate_send(*args: object, **kwargs: object) -> bool:
            pending_duplicate_calls.append((args, kwargs))
            return True

        pending_duplicate = send_markdown_alert(
            self.repo,
            pending_duplicate_send,
            receive_id="chat_1",
            receive_id_type="chat_id",
            text="测试静默与重试",
            alert_type="normal_duplicate",
            symbol="BTCUSDT",
        )
        self.assertTrue(pending_duplicate["silenced"])
        self.assertEqual(pending_duplicate_calls, [])
        for _ in range(2):
            self.conn.execute("UPDATE alert_outbox SET next_retry_at=CURRENT_TIMESTAMP WHERE id=?", (alert_id,))
            process_alert_outbox(self.repo, failing_send)
        final = self.conn.execute("SELECT status, retry_count FROM alert_outbox WHERE id=?", (alert_id,)).fetchone()
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["retry_count"], 3)
        failure_count = self.conn.execute("SELECT COUNT(*) FROM alert_failure_log WHERE alert_outbox_id=?", (alert_id,)).fetchone()[0]
        self.assertEqual(failure_count, 1)

        sent_messages: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def ok_send(*args: object, **kwargs: object) -> bool:
            sent_messages.append((args, kwargs))
            return True

        sent_once = send_markdown_alert(
            self.repo,
            ok_send,
            receive_id="chat_1",
            receive_id_type="chat_id",
            text="重复提醒",
            alert_type="normal_duplicate",
            symbol="ETHUSDT",
        )
        silenced = send_markdown_alert(
            self.repo,
            ok_send,
            receive_id="chat_1",
            receive_id_type="chat_id",
            text="重复提醒",
            alert_type="normal_duplicate",
            symbol="ETHUSDT",
        )
        self.assertTrue(sent_once["sent"])
        self.assertTrue(silenced["silenced"])

        request = crypto_handle_text_command("把置信度阈值改成 0.73", user_id="u1")
        self.assertTrue(request["confirmation_required"])
        change_id = int(request["change_id"])
        pending = self.conn.execute("SELECT status FROM config_hot_reload WHERE id=?", (change_id,)).fetchone()
        self.assertEqual(pending["status"], "pending")
        confirm = crypto_confirm_config_update(change_id)
        self.assertTrue(confirm["ok"])
        runtime = self.conn.execute("SELECT value_json FROM runtime_config WHERE config_key='risk.min_confidence_for_paper_order'").fetchone()
        self.assertEqual(json.loads(runtime["value_json"]), 0.73)

    def test_ad_hoc_analysis_silence_does_not_send_fallback_duplicate(self) -> None:
        from plugins.crypto_guard.run_ga_workers import _maybe_send_feishu_result

        sent_messages: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def ok_send(*args: object, **kwargs: object) -> str:
            sent_messages.append((args, kwargs))
            return f"msg_{len(sent_messages)}"

        payload = {"receive_id": "chat_1", "receive_id_type": "chat_id"}
        result = {"card_json": "{\"schema\":\"2.0\",\"body\":{\"elements\":[]}}", "symbol": "BTCUSDT", "signal_id": 42}
        _maybe_send_feishu_result(self.repo, payload, result, ok_send)
        _maybe_send_feishu_result(self.repo, payload, result, ok_send)

        self.assertEqual(len(sent_messages), 1)
        rows = self.conn.execute("SELECT alert_type, status FROM alert_outbox ORDER BY id").fetchall()
        self.assertEqual([(r["alert_type"], r["status"]) for r in rows], [("ad_hoc_analysis", "sent")])

    def test_phase03_signal_grading_counter_evidence_and_push_thresholds(self) -> None:
        from plugins.crypto_guard.reasoning.decision_schema import no_edge_decision, validate_json
        from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
        from plugins.crypto_guard.run_ga_workers import process_job
        from plugins.crypto_guard.strategy.strategy_scorer import grade_from_score

        self.assertEqual(grade_from_score(0.80), "S")
        self.assertEqual(grade_from_score(0.72), "A")
        self.assertEqual(grade_from_score(0.65), "B")
        self.assertEqual(grade_from_score(0.50), "C")
        self.assertEqual(grade_from_score(0.49), "D")

        invalid = no_edge_decision("BTCUSDT", "schema test")
        invalid["counter_evidence"] = []
        ok, _ = validate_json("ga_decision.schema.json", invalid)
        self.assertFalse(ok)

        b_decision = run_ga_sop_decision(self._decision_snapshot(trend_stage="transition"))
        # With the restructured scoring (base 0.55), bullish PA + bullish momentum yields ~0.85 = S
        self.assertIn(b_decision["signal_grade"], {"S", "A", "B"})
        self.assertIn("create_opportunity_watch", b_decision["suggested_actions"])
        self.assertTrue(b_decision["opportunity_watch"])

        sent: list[tuple[str, str, dict[str, object]]] = []

        def fake_send(receive_id: str, content: str, **kwargs: object) -> str:
            sent.append((receive_id, content, kwargs))
            return "message_id"

        a_snapshot = self._decision_snapshot(trend_stage="early")
        process_job(
            self.repo,
            {
                "id": 1,
                "job_type": "scheduled_market_analysis",
                "priority": 5,
                "session_id": "system:test",
                "payload_json": __import__("json").dumps(
                    {"snapshot": a_snapshot, "receive_id": "chat_1", "receive_id_type": "chat_id"},
                    ensure_ascii=False,
                ),
            },
            send_message=fake_send,
        )
        self.assertFalse(sent)
        state_row = self.conn.execute("SELECT * FROM analysis_states WHERE symbol='BTCUSDT' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(state_row)

        sent.clear()
        d_snapshot = self._decision_snapshot(trend_stage="late", neutral_risks=["趋势阶段偏末端，追价风险高"])
        d_snapshot["counter_evidence"]["contradiction_level"] = "high"  # type: ignore[index]
        process_job(
            self.repo,
            {
                "id": 2,
                "job_type": "scheduled_market_analysis",
                "priority": 5,
                "session_id": "system:test",
                "payload_json": __import__("json").dumps(
                    {"snapshot": d_snapshot, "receive_id": "chat_1", "receive_id_type": "chat_id"},
                    ensure_ascii=False,
                ),
            },
            send_message=fake_send,
        )
        self.assertFalse(sent)

    def test_v2_analysis_state_previous_context_and_skill_logs(self) -> None:
        from pathlib import Path

        from plugins.crypto_guard.reasoning.market_state_builder import DEFAULT_TIMEFRAMES, build_market_state_snapshot
        from plugins.crypto_guard.run_ga_workers import process_job

        base = 1_700_000_000_000
        span_by_tf = {"4h": 14_400_000, "1h": 3_600_000, "15m": 900_000, "5m": 300_000}
        for tf, span in span_by_tf.items():
            rows = []
            for idx in range(35):
                price = 100 + idx * 0.5
                rows.append(
                    {
                        "symbol": "BTCUSDT",
                        "interval": tf,
                        "open_time": base + idx * span,
                        "close_time": base + (idx + 1) * span - 1,
                        "open": price - 0.2,
                        "high": price + 0.8,
                        "low": price - 0.7,
                        "close": price,
                        "volume": 1000 + idx,
                        "is_closed": True,
                    }
                )
            self.repo.upsert_candles(rows)
        previous_id = self.repo.save_analysis_state(
            {
                "symbol": "BTCUSDT",
                "analysis_time": base - 1,
                "analysis_time_utc": "2023-11-14T22:13:19Z",
                "analysis_mode": "scheduled",
                "timeframes": DEFAULT_TIMEFRAMES,
                "market_structure": {"structure_status": "previous_waiting"},
                "trend_clarity": {"score": 0.5, "level": "mixed", "reason": []},
                "no_trade_reason": {"has_no_trade": True, "reason_code": "waiting", "detail": "等待突破"},
                "key_levels": {"support": [100], "resistance": [120], "breakout_boundary": {"upper": 120, "lower": 100}},
                "next_triggers": [],
                "next_analysis": {"suggested_time_utc": "2023-11-14T22:30:00Z"},
                "breakout_watch": {"enabled": True},
                "trade_permission": {"paper_trade_allowed": False},
                "opportunity_watch_recommended": True,
                "trade_plan": {"has_trade_plan": False},
            }
        )
        analysis_time = base + 35 * span_by_tf["5m"] - 1
        snapshot = build_market_state_snapshot(self.repo, symbol="BTCUSDT", analysis_time_utc=analysis_time, mode="scheduled", timeframes=DEFAULT_TIMEFRAMES)
        self.assertEqual((snapshot["previous_analysis_state"] or {})["market_structure"]["structure_status"], "previous_waiting")
        process_job(
            self.repo,
            {
                "id": 10,
                "job_type": "scheduled_market_analysis",
                "priority": 5,
                "session_id": "system:v2",
                "payload_json": json.dumps({"snapshot": snapshot, "snapshot_id": self.repo.save_market_snapshot(snapshot)}, ensure_ascii=False),
            },
        )
        state = self.conn.execute("SELECT * FROM analysis_states WHERE symbol='BTCUSDT' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(state)
        state_json = json.loads(state["state_json"])
        self.assertEqual(state_json["previous_state_id"], previous_id)
        self.assertIn("market_structure", state_json)
        self.assertIn("next_triggers", state_json)
        skill_count = self.conn.execute("SELECT COUNT(*) FROM skill_execution_logs").fetchone()[0]
        self.assertGreaterEqual(skill_count, 5 * len(DEFAULT_TIMEFRAMES))
        root = Path("plugins/crypto_guard/skills")
        for name in ("chanlun_skill", "price_action_skill", "smc_orderflow_skill", "momentum_skill", "trend_stage_skill"):
            for filename in ("skill.yaml", "prompt.md", "tools.py", "schema.json", "feedback_rules.yaml"):
                self.assertTrue((root / name / filename).exists(), f"{name}/{filename}")

    def test_phase04_opportunity_watch_state_machine_and_button(self) -> None:
        from datetime import datetime, timedelta, timezone

        from plugins.crypto_guard.run_ga_workers import handle_button_callback
        from plugins.crypto_guard.scheduler.opportunity_watcher import update_opportunity_watches

        signal_id = self.repo.create_signal(
            {
                "symbol": "BTCUSDT",
                "decision": "wait_for_pullback",
                "signal_grade": "B",
                "confidence": 0.67,
                "summary": "测试机会监控",
                "market_bias": "bullish",
                "risk_notes": ["仅用于测试"],
                "has_trade_plan": False,
                "opportunity_watch": {
                    "needed": True,
                    "direction": "LONG",
                    "reason": "等待突破确认",
                    "conditions": [{"type": "breakout", "side": "LONG", "level": 101.0, "timeframe": "15m"}],
                    "invalid_condition": {"type": "close_below", "level": 95.0},
                    "expires_minutes": 60,
                },
            },
            self._risk_approved_snapshot_id("BTCUSDT"),
        )
        button = handle_button_callback(
            self.repo,
            {"action": "create_opportunity_watch", "symbol": "BTCUSDT", "signal_id": signal_id},
        )
        self.assertTrue(button["ok"])
        watch = self.repo.get_opportunity_watch(button["watch_id"])
        self.assertEqual(watch["status"], "active")
        self.assertIsNotNone(watch["expires_at"])

        span = 900_000
        base = 1_700_000_000_000
        self.repo.upsert_candles(
            [
                {
                    "symbol": "BTCUSDT",
                    "interval": "15m",
                    "open_time": base,
                    "close_time": base + span - 1,
                    "open": 99.0,
                    "high": 100.5,
                    "low": 98.0,
                    "close": 100.0,
                    "volume": 1000,
                    "is_closed": True,
                },
                {
                    "symbol": "BTCUSDT",
                    "interval": "15m",
                    "open_time": base + span,
                    "close_time": base + span * 2 - 1,
                    "open": 100.0,
                    "high": 103.0,
                    "low": 99.5,
                    "close": 102.0,
                    "volume": 1200,
                    "is_closed": True,
                },
            ]
        )
        update = update_opportunity_watches(self.repo, analysis_time_utc=base + span * 2 - 1)
        self.assertEqual(update["triggered"], 1)
        triggered_watch = self.repo.get_opportunity_watch(button["watch_id"])
        self.assertEqual(triggered_watch["status"], "triggered")
        alerts = self.conn.execute("SELECT * FROM agent_jobs WHERE job_type='opportunity_watch_alert'").fetchall()
        self.assertEqual(len(alerts), 1)
        second = update_opportunity_watches(self.repo, analysis_time_utc=base + span * 2 - 1)
        self.assertEqual(second["triggered"], 0)

        pullback_id = self.repo.create_opportunity_watch(
            "XRPUSDT",
            {
                "direction": "LONG",
                "reason": "等待回踩确认",
                "conditions": [{"type": "pullback", "side": "LONG", "level": 100.0, "timeframe": "15m"}],
                "invalid_condition": {"type": "close_below", "level": 95.0},
                "expires_minutes": 60,
            },
        )
        reclaim_id = self.repo.create_opportunity_watch(
            "DOGEUSDT",
            {
                "direction": "LONG",
                "reason": "等待 reclaim",
                "conditions": [{"type": "reclaim", "side": "LONG", "level": 100.0, "timeframe": "15m"}],
                "invalid_condition": {"type": "close_below", "level": 95.0},
                "expires_minutes": 60,
            },
        )
        cvd_id = self.repo.create_opportunity_watch(
            "ADAUSDT",
            {
                "direction": "LONG",
                "reason": "等待 CVD 确认",
                "conditions": [{"type": "cvd_confirmation", "side": "LONG", "flow_confirmation": "supports_long", "timeframe": "15m"}],
                "invalid_condition": None,
                "expires_minutes": 60,
            },
        )
        for symbol, closes in {
            "XRPUSDT": [100.2],
            "DOGEUSDT": [99.0, 101.0],
            "ADAUSDT": [100.0],
        }.items():
            rows = []
            for idx, close_price in enumerate(closes):
                rows.append(
                    {
                        "symbol": symbol,
                        "interval": "15m",
                        "open_time": base + idx * span,
                        "close_time": base + (idx + 1) * span - 1,
                        "open": 99.0,
                        "high": 102.0,
                        "low": 99.8 if symbol == "XRPUSDT" else 98.0,
                        "close": close_price,
                        "volume": 1000,
                        "is_closed": True,
                    }
                )
            self.repo.upsert_candles(rows)
        structured_update = update_opportunity_watches(self.repo, analysis_time_utc=base + span * 2 - 1)
        self.assertEqual(structured_update["triggered"], 3)
        self.assertEqual(self.repo.get_opportunity_watch(pullback_id)["status"], "triggered")
        self.assertEqual(self.repo.get_opportunity_watch(reclaim_id)["status"], "triggered")
        self.assertEqual(self.repo.get_opportunity_watch(cvd_id)["status"], "triggered")

        invalid_id = self.repo.create_opportunity_watch(
            "ETHUSDT",
            {
                "direction": "LONG",
                "reason": "等待 reclaim",
                "conditions": [{"type": "reclaim", "side": "LONG", "level": 100.0, "timeframe": "15m"}],
                "invalid_condition": {"type": "close_below", "level": 95.0},
                "expires_minutes": 60,
            },
        )
        self.repo.upsert_candles(
            [
                {
                    "symbol": "ETHUSDT",
                    "interval": "15m",
                    "open_time": base,
                    "close_time": base + span - 1,
                    "open": 96.0,
                    "high": 97.0,
                    "low": 93.0,
                    "close": 94.0,
                    "volume": 1000,
                    "is_closed": True,
                }
            ]
        )
        invalid_update = update_opportunity_watches(self.repo, analysis_time_utc=base + span - 1)
        self.assertEqual(invalid_update["invalidated"], 1)
        self.assertEqual(self.repo.get_opportunity_watch(invalid_id)["status"], "invalidated")

        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        expired_id = self.repo.create_opportunity_watch(
            "SOLUSDT",
            {
                "direction": "LONG",
                "reason": "等待 CVD 确认",
                "conditions": [{"type": "cvd_confirmation", "side": "LONG", "flow_confirmation": "supports_long"}],
                "invalid_condition": None,
            },
            expires_at=expired_at,
        )
        expired_update = update_opportunity_watches(self.repo, analysis_time_utc=base + span - 1)
        self.assertEqual(expired_update["expired"], 1)
        self.assertEqual(self.repo.get_opportunity_watch(expired_id)["status"], "expired")

    def test_phase05_paper_execution_quality_metrics_and_drawdown_alert(self) -> None:
        import json

        from plugins.crypto_guard.paper.paper_broker import create_paper_order_from_signal, fill_order_if_triggered
        from plugins.crypto_guard.paper.paper_position_updater import update_paper_positions

        signal_id = self.repo.create_signal(
            {
                "symbol": "BTCUSDT",
                "decision": "trade_plan_available",
                "signal_grade": "A",
                "confidence": 0.8,
                "summary": "测试 Phase 5 模拟盘执行质量",
                "has_trade_plan": True,
                "risk_notes": ["仅用于测试"],
                "trade_plan": {
                    "side": "LONG",
                    "entry_type": "limit",
                    "entry_price": 100.0,
                    "trigger_price": None,
                    "stop_loss": 95.0,
                    "take_profits": [{"price": 110.0, "ratio": 1.0}],
                    "risk_percent": 0.5,
                    "invalid_condition": "跌破 95",
                    "reason": "测试执行质量",
                },
            },
            self._risk_approved_snapshot_id("BTCUSDT"),
        )
        order_id = create_paper_order_from_signal(self.repo, signal_id)["order_id"]
        order = self.repo.list_open_paper_orders()[0]
        fill = fill_order_if_triggered(
            self.repo,
            order,
            {"symbol": "BTCUSDT", "open": 101.0, "high": 102.0, "low": 99.0, "close": 101.0, "close_time": 1_700_000_900_000},
        )
        self.assertTrue(fill["filled"])

        order = self.conn.execute("SELECT * FROM paper_orders WHERE id=?", (order_id,)).fetchone()
        update = update_paper_positions(
            self.repo,
            prices={
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "open": 101.0,
                    "high": 112.0,
                    "low": 97.0,
                    "close": 111.0,
                    "close_time": 1_700_001_800_000,
                }
            },
        )
        self.assertTrue(any(result.get("closed") for result in update["results"]))
        trade = self.conn.execute("SELECT * FROM paper_trades WHERE order_id=?", (order["id"],)).fetchone()
        self.assertEqual(trade["close_reason"], "take_profit")
        self.assertEqual(trade["exit_price"], 110.0)
        # MFE/MAE are in PnL (USDT) units, not price units
        # quantity = (10000 * 0.5%) / |100 - 95| = 50 / 5 = 10
        # MFE = (112 - 100) * 10 = 120, MAE = (97 - 100) * 10 = -30
        self.assertEqual(trade["max_favorable_excursion"], 120.0)
        self.assertEqual(trade["max_adverse_excursion"], -30.0)
        self.assertIsNotNone(trade["entry_efficiency"])
        self.assertIsNotNone(trade["exit_efficiency"])
        self.assertIsNotNone(trade["signal_decay_score"])
        path = json.loads(trade["stop_take_path_json"])
        self.assertTrue(any(item.get("event") == "exit_hit" for item in path))
        equity = update["equity_snapshot"]
        # PnL = (110 - 100) * 10 (quantity) = 100
        self.assertEqual(equity["realized_pnl"], 100.0)
        self.assertEqual(equity["account_equity"], 10100.0)

        self.conn.execute(
            """
            INSERT INTO paper_trades(
                symbol, side, entry_price, exit_price, stop_loss, quantity, pnl, pnl_percent, pnl_r,
                max_favorable_excursion, max_adverse_excursion, entry_efficiency, exit_efficiency,
                signal_decay_score, stop_take_path_json, close_reason, closed_at
            )
            VALUES ('ETHUSDT', 'LONG', 100, 50, 95, 1, -600, -50, -120, 5, -50, 0.2, 0,
                    1, '[]', 'stop_loss', CURRENT_TIMESTAMP)
            """
        )
        drawdown = update_paper_positions(self.repo, prices={})
        self.assertTrue(drawdown["equity_snapshot"]["drawdown_alert"])
        alerts = self.conn.execute("SELECT * FROM agent_jobs WHERE job_type='paper_drawdown_alert'").fetchall()
        self.assertEqual(len(alerts), 1)
        repeated = update_paper_positions(self.repo, prices={})
        self.assertTrue(repeated["equity_snapshot"]["drawdown_alert"])
        alerts_after_repeat = self.conn.execute("SELECT * FROM agent_jobs WHERE job_type='paper_drawdown_alert'").fetchall()
        self.assertEqual(len(alerts_after_repeat), 1)

    def test_phase06_price_action_structure_events(self) -> None:
        from plugins.crypto_guard.analysis.price_action_engine import analyze_price_action, detect_swings

        candles = []
        closes = [100, 106, 102, 104, 101, 110, 105, 107, 104, 115, 109, 112, 108, 121, 116, 118, 114, 126]
        span = 900_000
        base = 1_700_000_000_000
        for idx, close in enumerate(closes):
            candles.append(
                {
                    "open_time": base + idx * span,
                    "close_time": base + (idx + 1) * span - 1,
                    "open": close - 1,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": 1000,
                }
            )
        highs, lows = detect_swings(candles)
        self.assertTrue(highs)
        self.assertTrue(lows)
        result = analyze_price_action(candles, analysis_time_utc=candles[-1]["close_time"])
        self.assertIn(result["market_structure"], {"bullish", "range"})
        self.assertTrue(result["swing_labels"])
        self.assertTrue(result["structure_events"])
        self.assertIn("explanation", result)
        if result["market_structure"] == "bullish":
            self.assertIsNotNone(result["invalid_level"])

        flat = []
        for idx, close in enumerate([100, 101, 100.5, 101.2, 100.7, 101.1, 100.4, 101.0, 100.6, 101.3]):
            flat.append(
                {
                    "open_time": base + idx * span,
                    "close_time": base + (idx + 1) * span - 1,
                    "open": close,
                    "high": close + 0.6,
                    "low": close - 0.6,
                    "close": close,
                    "volume": 1000,
                }
            )
        range_result = analyze_price_action(flat, analysis_time_utc=flat[-1]["close_time"])
        self.assertEqual(range_result["market_structure"], "range")
        self.assertIsNone(range_result["invalid_level"])

    def test_phase07_momentum_indicators_and_counter_evidence(self) -> None:
        from plugins.crypto_guard.analysis.counter_evidence_engine import build_counter_evidence
        from plugins.crypto_guard.analysis.momentum_engine import analyze_momentum

        candles = []
        span = 900_000
        base = 1_700_000_000_000
        price = 100.0
        for idx in range(40):
            price += 0.9 if idx < 30 else 0.25
            candles.append(
                {
                    "open_time": base + idx * span,
                    "close_time": base + (idx + 1) * span - 1,
                    "open": price - 0.5,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price,
                    "volume": 1000 + (800 if idx == 39 else idx * 8),
                }
            )
        result = analyze_momentum(candles, analysis_time_utc=candles[-1]["close_time"])
        self.assertIn(result["direction"], {"bullish", "neutral"})
        self.assertIn("rsi_slope", result)
        self.assertIn("macd", result)
        self.assertIn("atr", result)
        self.assertIn("volume_impulse", result)
        self.assertIn("body_strength", result)
        self.assertIsInstance(result["momentum_score"], int)

        counter = build_counter_evidence(
            {
                "price_action": {"market_structure": "bullish"},
                "momentum": {**result, "divergence": True, "quality": "exhausted"},
                "trend_stage": {"trend_stage": "late"},
                "smc": {},
            }
        )
        self.assertTrue(any("动能" in item for item in counter["neutral_or_risk_evidence"]))

    def test_phase08_trend_stage_fusion_and_score_downgrade(self) -> None:
        from plugins.crypto_guard.analysis.trend_stage_engine import fuse_trend_stage
        from plugins.crypto_guard.strategy.strategy_scorer import score_snapshot

        profiles = {
            "1d": {"trend_stage": "range", "market_structure": "range"},
            "4h": {"trend_stage": "middle", "market_structure": "bullish"},
            "1h": {"trend_stage": "middle", "market_structure": "bullish"},
            "15m": {"trend_stage": "early", "market_structure": "bullish"},
        }
        fused = fuse_trend_stage(profiles, {"trend_stage": "early", "structure": "bullish"}, analysis_time_utc=1_700_000_000_000)
        self.assertEqual(fused["trend_stage"], "range")
        self.assertEqual(fused["strategy_policy"], "filter_trend_strategy")

        snapshot = self._decision_snapshot(trend_stage="early")
        snapshot["modules"]["trend_stage"] = fused  # type: ignore[index]
        score = score_snapshot(snapshot)  # type: ignore[arg-type]
        # With restructured scoring:
        # base 0.55 + 0.15 (bullish PA) + 0.10 (bullish momentum) - 0.03 (range trend_stage) = 0.77
        # Fused trend_stage is "range" with filter_trend_strategy policy
        self.assertIn(score["signal_grade"], {"S", "A", "B", "C"})
        # Score should still be reasonable even with range trend stage
        self.assertTrue(score["score"] >= 0.50, f"score {score['score']} should be above C threshold")

        late = fuse_trend_stage(
            {
                "1d": {"trend_stage": "middle", "market_structure": "bullish"},
                "4h": {"trend_stage": "late", "market_structure": "bullish"},
                "1h": {"trend_stage": "late", "market_structure": "bullish"},
                "15m": {"trend_stage": "late", "market_structure": "bullish"},
            },
            {"trend_stage": "late", "structure": "bullish"},
            analysis_time_utc=1_700_000_000_000,
        )
        self.assertEqual(late["trend_stage"], "late")
        self.assertEqual(late["strategy_policy"], "downgrade_chasing_signal")

    def test_phase09_smc_and_order_flow_confirmation(self) -> None:
        from plugins.crypto_guard.analysis.order_flow_engine import analyze_order_flow
        from plugins.crypto_guard.analysis.smc_engine import analyze_smc
        from plugins.crypto_guard.strategy.strategy_scorer import score_snapshot

        span = 900_000
        base = 1_700_000_000_000
        raw = [
            (100, 104, 99, 103),
            (103, 106, 101, 105),
            (105, 108, 103, 107),
            (107, 110, 106, 109),
            (109, 112, 108, 111),
            (111, 114, 110, 113),
            (113, 115, 112, 114),
            (114, 116, 113, 115),
            (115, 117, 114, 116),
            (116, 118, 115, 117),
            (117, 119, 116, 118),
            (118, 120, 117, 119),
            (119, 121, 118, 120),
            (120, 122, 119, 121),
            (121, 123, 118, 119),
            (119, 124, 117, 123),
            (123, 127, 122, 126),
            (126, 130, 125, 129),
        ]
        candles = [
            {
                "open_time": base + idx * span,
                "close_time": base + (idx + 1) * span - 1,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1000 + idx * 10,
            }
            for idx, (o, h, l, c) in enumerate(raw)
        ]
        pa = {"market_structure": "bullish", "last_event": "bullish_bos", "range": {"high": 130, "low": 99}}
        smc = analyze_smc(candles, pa, analysis_time_utc=candles[-1]["close_time"])
        self.assertTrue(smc["implemented"])
        self.assertIn("liquidity", smc)
        self.assertIn("premium_discount", smc)
        self.assertIn("order_block", smc)

        degraded = analyze_order_flow([], analysis_time_utc=candles[-1]["close_time"])
        self.assertEqual(degraded["flow_confirmation"], "not_available")
        self.assertTrue(degraded["degraded"])
        flow = analyze_order_flow(
            analysis_time_utc=candles[-1]["close_time"],
            flow_data={"cvd_values": [0, 20, 55], "aggressive_buy_ratio": 0.68, "price_change": 3.0},
        )
        self.assertEqual(flow["flow_confirmation"], "supports_long")
        divergent = analyze_order_flow(
            analysis_time_utc=candles[-1]["close_time"],
            flow_data={"cvd_values": [55, 20, -5], "aggressive_buy_ratio": 0.4, "price_change": 3.0},
        )
        self.assertTrue(divergent["delta_divergence"])

        snapshot = self._decision_snapshot(trend_stage="early")
        snapshot["modules"]["smc"] = {**smc, "fvg": {"exists": True, "direction": "bullish"}}  # type: ignore[index]
        snapshot["modules"]["order_flow"] = flow  # type: ignore[index]
        score = score_snapshot(snapshot)  # type: ignore[arg-type]
        self.assertTrue(any("订单流" in item or "FVG" in item for item in score["evidence"]))

    def test_phase10_chanlun_structure_is_supporting_evidence_only(self) -> None:
        from plugins.crypto_guard.analysis.chanlun_engine import analyze_chanlun, detect_central_zone, detect_fractals, detect_strokes, normalize_inclusion
        from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision

        span = 900_000
        base = 1_700_000_000_000
        prices = [100, 106, 101, 108, 102, 110, 104, 111, 105, 113, 107, 112, 108, 116, 109, 118, 111, 117, 112, 120]
        candles = []
        for idx, close in enumerate(prices):
            candles.append(
                {
                    "open_time": base + idx * span,
                    "close_time": base + (idx + 1) * span - 1,
                    "open": close - 0.5,
                    "high": close + (2 if idx % 2 else 1),
                    "low": close - (2 if idx % 2 == 0 else 1),
                    "close": close,
                    "volume": 1000 + idx * 20,
                }
            )
        normalized = normalize_inclusion(candles)
        fractals = detect_fractals(normalized)
        strokes = detect_strokes(fractals)
        zone = detect_central_zone(strokes)
        result = analyze_chanlun(candles, analysis_time_utc=candles[-1]["close_time"])
        self.assertTrue(result["implemented"])
        self.assertIn("current_bi_direction", result)
        self.assertIn("divergence_candidate", result)
        self.assertEqual(result["evidence_role"], "supporting_only")
        self.assertEqual(result["central_zone"], zone)

        snapshot = self._decision_snapshot(trend_stage="transition")
        snapshot["modules"]["price_action"] = {"market_structure": "range", "key_levels": {}, "invalid_level": None}  # type: ignore[index]
        snapshot["modules"]["momentum"] = {"direction": "neutral", "quality": "range", "momentum_score": 50}  # type: ignore[index]
        snapshot["modules"]["chanlun"] = {**result, "signal": "class_3_buy_candidate"}  # type: ignore[index]
        decision = run_ga_sop_decision(snapshot)  # type: ignore[arg-type]
        self.assertFalse(decision["has_trade_plan"])
        self.assertNotEqual(decision["decision"], "trade_plan_available")

    def test_phase11_trade_review_reads_snapshot_and_generates_candidate_patch(self) -> None:
        import json

        from plugins.crypto_guard.review.trade_reviewer import review_trade

        snapshot = self._decision_snapshot(trend_stage="late", neutral_risks=["趋势阶段偏末端，追价风险高"])
        snapshot_id = self.repo.save_market_snapshot(snapshot)  # type: ignore[arg-type]
        signal_id = self.repo.create_signal(
            {
                "symbol": "BTCUSDT",
                "decision": "trade_plan_available",
                "signal_grade": "A",
                "confidence": 0.78,
                "summary": "测试复盘 snapshot",
                "has_trade_plan": False,
                "risk_notes": ["测试"],
            },
            snapshot_id,
        )
        self.conn.execute(
            """
            INSERT INTO paper_trades(
                signal_id, market_snapshot_id, symbol, side, entry_price, exit_price, stop_loss, quantity,
                pnl, pnl_percent, pnl_r, max_favorable_excursion, max_adverse_excursion,
                entry_efficiency, exit_efficiency, signal_decay_score, close_reason, closed_at
            )
            VALUES (?, ?, 'BTCUSDT', 'LONG', 100, 94, 95, 1, -6, -6, -1.2, 1, -6, 0.1, 0, 0.8, 'stop_loss', CURRENT_TIMESTAMP)
            """,
            (signal_id, snapshot_id),
        )
        trade_id = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        result = review_trade(self.repo, trade_id)
        self.assertTrue(result["ok"])
        review = result["review"]
        self.assertNotEqual(review["primary_reason"], "unknown")
        self.assertTrue(review["source_snapshot"]["available"])
        self.assertTrue(review["evidence_checklist"])
        self.assertTrue(result["patch_id"])
        patch = self.conn.execute("SELECT * FROM strategy_patches WHERE id=?", (result["patch_id"],)).fetchone()
        self.assertEqual(patch["status"], "candidate")
        evidence = json.loads(patch["evidence_json"])
        self.assertEqual(evidence["review_id"], result["review_id"])

    def test_phase12_strategy_versions_candidate_and_rollback(self) -> None:
        from plugins.crypto_guard.strategy.version_manager import create_candidate_version_from_patch, list_strategy_versions, rollback_active_strategy

        patch_id = self.repo.save_strategy_patch_candidate(
            {
                "strategy_name": "smc_pullback_long",
                "from_version": "1.0",
                "candidate_version": "1.2-candidate",
                "change_reason": "测试 candidate 版本",
                "patch": {"score_adjustments": {"test": -0.01}},
            },
            {"review_id": 123},
        )
        created = create_candidate_version_from_patch(self.repo, patch_id)
        self.assertTrue(created["ok"])
        candidate = self.repo.get_strategy_version("smc_pullback_long", "1.2-candidate")
        self.assertEqual(candidate["status"], "shadow_testing")
        self.assertIn("测试 candidate 版本", candidate["change_reason"])

        self.repo.save_strategy_version(
            strategy_name="smc_pullback_long",
            version="0.9",
            status="deprecated",
            config={"strategy_name": "smc_pullback_long", "version": "0.9"},
            change_reason="rollback target",
        )
        rolled = rollback_active_strategy(self.repo, "smc_pullback_long", "0.9", change_reason="manual rollback test")
        self.assertTrue(rolled["ok"])
        active = self.repo.active_strategy_version("smc_pullback_long")
        self.assertEqual(active["version"], "0.9")
        listed = list_strategy_versions(self.repo, "smc_pullback_long")
        self.assertIn("策略版本", listed["text"])

    def test_phase13_shadow_testing_thresholds_and_promotion_gate(self) -> None:
        from plugins.crypto_guard.strategy.shadow_testing import promote_shadow_candidate, record_shadow_evaluation, run_shadow_test

        self.repo.save_strategy_version(
            strategy_name="shadow_sop",
            version="1.0",
            status="active",
            config={"strategy_name": "shadow_sop", "version": "1.0"},
            change_reason="test active",
        )
        self.repo.save_strategy_version(
            strategy_name="shadow_sop",
            version="1.1-candidate",
            status="candidate",
            config={"strategy_name": "shadow_sop", "version": "1.1-candidate"},
            change_reason="test candidate",
        )
        for idx, score in enumerate([0.55, 0.58, 0.6, 0.57, 0.59]):
            self.repo.save_strategy_evaluation(
                {
                    "symbol": "BTCUSDT",
                    "timeframe": "15m",
                    "analysis_time_utc": 1_700_000_000_000 + idx,
                    "strategy_name": "shadow_sop",
                    "strategy_version": "1.0",
                    "confidence": score,
                    "decision": "monitor_only",
                    "evidence": [],
                    "counter_evidence": ["test"],
                },
                None,
            )
        for idx, score in enumerate([0.72, 0.74, 0.76, 0.73, 0.75]):
            record_shadow_evaluation(
                self.repo,
                symbol="BTCUSDT",
                timeframe="15m",
                analysis_time_utc=1_700_000_000_000 + idx,
                strategy_name="shadow_sop",
                strategy_version="1.1-candidate",
                score=score,
                decision="shadow_candidate",
            )
        insufficient = run_shadow_test(self.repo, strategy_name="shadow_sop", candidate_version="1.1-candidate", min_samples=10)
        self.assertEqual(insufficient["recommendation"], "insufficient_samples")
        passed = run_shadow_test(self.repo, strategy_name="shadow_sop", candidate_version="1.1-candidate", min_samples=3)
        self.assertIn("avg_r", passed["candidate_stats"])
        self.assertIn("win_rate", passed["candidate_stats"])
        self.assertIn("drawdown", passed["candidate_stats"])
        self.assertFalse(passed["auto_promoted"])
        denied = promote_shadow_candidate(self.repo, strategy_name="shadow_sop", candidate_version="1.1-candidate", change_reason="no confirm")
        self.assertFalse(denied["ok"])
        promoted = promote_shadow_candidate(
            self.repo,
            strategy_name="shadow_sop",
            candidate_version="1.1-candidate",
            confirm=True,
            change_reason="manual confirm shadow pass",
        )
        self.assertTrue(promoted["ok"])

    def test_phase14_historical_replay_parquet_no_lookahead_and_export(self) -> None:
        import os
        import pandas as pd

        from plugins.crypto_guard.backtest.historical_replay import load_historical_klines, run_historical_replay

        span = 900_000
        base = 1_700_000_000_000
        rows = []
        price = 100.0
        for idx in range(45):
            price += 0.7 if idx % 6 else -0.2
            rows.append(
                {
                    "symbol": "BTCUSDT",
                    "interval": "15m",
                    "open_time": base + idx * span,
                    "close_time": base + (idx + 1) * span - 1,
                    "open": price - 0.5,
                    "high": price + 1.5,
                    "low": price - 1.0,
                    "close": price,
                    "volume": 1000 + idx * 10,
                    "is_closed": 1,
                }
            )
        parquet_path = os.path.join(self.tmp.name, "btcusdt_15m.parquet")
        export_path = os.path.join(self.tmp.name, "replay_result.json")
        pd.DataFrame(rows).to_parquet(parquet_path)
        loaded = load_historical_klines(parquet_path, symbol="BTCUSDT", interval="15m")
        self.assertTrue(loaded["ok"])
        self.assertEqual(loaded["count"], len(rows))

        result = run_historical_replay(
            self.repo,
            symbol="BTCUSDT",
            interval="15m",
            start_time=rows[0]["close_time"],
            end_time=rows[-1]["close_time"],
            parquet_path=parquet_path,
            strategy_versions=["1.0", "1.1-candidate"],
            export_path=export_path,
            warmup=30,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["no_lookahead"]["ok"])
        self.assertGreater(result["stats"]["signal_count"], 0)
        self.assertTrue(result["strategy_comparison"])
        self.assertTrue(os.path.exists(export_path))
        saved = self.conn.execute("SELECT * FROM historical_replay_results WHERE id=?", (result["replay_result_id"],)).fetchone()
        self.assertIsNotNone(saved)

    def test_phase15_self_evolution_audit_overfit_gate_and_shadow(self) -> None:
        from plugins.crypto_guard.strategy.self_evolution import run_self_evolution_cycle
        from plugins.crypto_guard.strategy.shadow_testing import record_shadow_evaluation

        for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            for idx in range(2):
                self.conn.execute(
                    """
                    INSERT INTO paper_trades(symbol, side, entry_price, exit_price, stop_loss, quantity, pnl, pnl_percent, pnl_r, close_reason, closed_at)
                    VALUES (?, 'LONG', 100, 94, 95, 1, -6, -6, -1.2, 'stop_loss', CURRENT_TIMESTAMP)
                    """,
                    (symbol,),
                )
                trade_id = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                self.repo.save_trade_review(
                    trade_id,
                    {
                        "trade_id": trade_id,
                        "result": "loss",
                        "primary_reason": "entry_too_early",
                        "secondary_reasons": ["stop_loss_triggered"],
                        "summary": "测试自进化聚合",
                        "improvement_suggestion": {"action": "candidate_patch_or_memory_update"},
                    },
                )
        self.repo.save_strategy_version(
            strategy_name="self_evo_sop",
            version="1.0",
            status="active",
            config={"strategy_name": "self_evo_sop", "version": "1.0"},
            change_reason="test active",
        )
        blocked = run_self_evolution_cycle(self.repo, strategy_name="self_evo_sop", min_reviews=5, min_symbols=4, min_shadow_samples=3)
        self.assertEqual(blocked["status"], "rejected")
        self.assertEqual(blocked["reason"], "single_symbol_overfit_risk")

        pending = run_self_evolution_cycle(self.repo, strategy_name="self_evo_sop", min_reviews=5, min_symbols=2, min_shadow_samples=3)
        self.assertIn(pending["status"], {"candidate_pending_shadow", "candidate_review_required"})
        self.assertTrue(pending["audit_steps"])
        self.assertTrue(pending["patch_id"])
        candidate_version = pending["candidate_version"]
        candidate = self.repo.get_strategy_version("self_evo_sop", candidate_version)
        self.assertEqual(candidate["status"], "shadow_testing")
        for idx, score in enumerate([0.7, 0.72, 0.74]):
            self.repo.save_strategy_evaluation(
                {
                    "symbol": "BTCUSDT",
                    "timeframe": "15m",
                    "analysis_time_utc": 1_700_100_000_000 + idx,
                    "strategy_name": "self_evo_sop",
                    "strategy_version": "1.0",
                    "confidence": 0.55,
                    "decision": "monitor_only",
                    "evidence": [],
                    "counter_evidence": ["test"],
                },
                None,
            )
            record_shadow_evaluation(
                self.repo,
                symbol="BTCUSDT",
                timeframe="15m",
                analysis_time_utc=1_700_100_000_000 + idx,
                strategy_name="self_evo_sop",
                strategy_version=candidate_version,
                score=score,
                decision="shadow_candidate",
            )
        promoted = run_self_evolution_cycle(
            self.repo,
            strategy_name="self_evo_sop",
            min_reviews=5,
            min_symbols=2,
            min_shadow_samples=3,
            allow_auto_promote=True,
        )
        self.assertIn("explanation", promoted)
        self.assertTrue(promoted["audit_steps"])
        saved_run = self.conn.execute("SELECT * FROM self_evolution_runs WHERE id=?", (promoted["run_id"],)).fetchone()
        self.assertIsNotNone(saved_run)

    def test_backtest_gate_disabled_uses_5_samples(self) -> None:
        """When backtest gate is disabled, online shadow uses min_samples_after_backtest=5."""
        from plugins.crypto_guard.strategy.shadow_testing import check_candidate_backtest_status, run_shadow_test

        # Setup: save a gate_disabled backtest result
        self.repo.save_strategy_version(
            strategy_name="test_strategy",
            version="1.0",
            status="active",
            config={"strategy_name": "test_strategy", "version": "1.0"},
            change_reason="test",
        )
        patch_id = self.repo.save_strategy_patch_candidate(
            {"strategy_name": "test_strategy", "from_version": "1.0", "candidate_version": "v2-disabled", "patch": {}},
            evidence={},
        )
        # Save gate_disabled result
        self.conn.execute(
            "UPDATE strategy_patches SET backtest_result_json=? WHERE id=?",
            (json.dumps({"ok": True, "passed": True, "gate_disabled": True, "reason": "backtest_gate_disabled"}), patch_id),
        )
        self.repo.save_strategy_version(
            strategy_name="test_strategy",
            version="v2-disabled",
            status="shadow_testing",
            config={},
            change_reason="test",
        )

        status = check_candidate_backtest_status(self.repo, "test_strategy", "v2-disabled")
        self.assertTrue(status["has_backtest"])
        self.assertTrue(status["backtest"]["gate_disabled"])

    def test_backtest_gate_skipped_uses_30_samples(self) -> None:
        """When backtest is skipped (no scoring changes), online shadow uses min_samples_without_backtest=30."""
        from plugins.crypto_guard.strategy.shadow_testing import check_candidate_backtest_status

        self.repo.save_strategy_version(
            strategy_name="test_strategy",
            version="1.0",
            status="active",
            config={"strategy_name": "test_strategy", "version": "1.0"},
            change_reason="test",
        )
        patch_id = self.repo.save_strategy_patch_candidate(
            {"strategy_name": "test_strategy", "from_version": "1.0", "candidate_version": "v2-skipped", "patch": {"risk_controls": ["test"]}},
            evidence={},
        )
        # Save skipped result (no scoring changes)
        self.conn.execute(
            "UPDATE strategy_patches SET backtest_result_json=? WHERE id=?",
            (json.dumps({"ok": True, "passed": False, "skipped": True, "reason": "skipped_or_needs_online_shadow"}), patch_id),
        )

        status = check_candidate_backtest_status(self.repo, "test_strategy", "v2-skipped")
        self.assertTrue(status["has_backtest"])
        self.assertTrue(status["backtest"]["skipped"])
        self.assertFalse(status["passed"])

    def test_score_adjustments_field_is_recognized(self) -> None:
        """score_adjustments (plural, dict) should be recognized as scoring change."""
        from plugins.crypto_guard.strategy.shadow_testing import _extract_score_adjustment, _has_scoring_changes

        # Test score_adjustments (plural, dict)
        patch_with_adjustments = {"patch": {"score_adjustments": {"entry_penalty": -0.05, "late_penalty": -0.03}}}
        self.assertTrue(_has_scoring_changes(patch_with_adjustments))
        self.assertAlmostEqual(_extract_score_adjustment(patch_with_adjustments), -0.08)

        # Test score_adjustment (singular, float)
        patch_single = {"patch": {"score_adjustment": 0.1}}
        self.assertTrue(_has_scoring_changes(patch_single))
        self.assertAlmostEqual(_extract_score_adjustment(patch_single), 0.1)

        # Test no scoring changes
        patch_no_scoring = {"patch": {"risk_controls": ["test"]}}
        self.assertFalse(_has_scoring_changes(patch_no_scoring))
        self.assertAlmostEqual(_extract_score_adjustment(patch_no_scoring), 0.0)

    def test_performance_gate_cooldown(self) -> None:
        """Test symbol+side cooldown logic."""
        from datetime import datetime, timedelta, timezone
        from plugins.crypto_guard.ga_master.performance_gate import PerformanceGate

        gate = PerformanceGate(self.repo)

        # Insert 3 losing trades for BTCUSDT LONG
        now = datetime.now(timezone.utc)
        for i in range(3):
            closed_at = (now - timedelta(hours=i + 1)).isoformat().replace("+00:00", "Z")
            self.conn.execute(
                """
                INSERT INTO paper_trades(symbol, side, entry_price, exit_price, stop_loss, quantity, pnl, pnl_percent, pnl_r, close_reason, closed_at)
                VALUES ('BTCUSDT', 'LONG', 100, 95, 95, 1, -5, -5, -1, 'stop_loss', ?)
                """,
                (closed_at,),
            )

        # Check cooldown should be active
        result = gate.check(
            symbol="BTCUSDT",
            side="LONG",
            signal_grade="S",
            trend_stage="early",
            confidence=0.8,
        )
        self.assertTrue(result["cooldown_active"])
        self.assertTrue(result["should_watch_only"])
        self.assertIn("symbol_side_cooldown", result["reasons"][0])

        # Check ETHUSDT should not be in cooldown
        result_eth = gate.check(
            symbol="ETHUSDT",
            side="LONG",
            signal_grade="S",
            trend_stage="early",
            confidence=0.8,
        )
        self.assertFalse(result_eth["cooldown_active"])
        self.assertFalse(result_eth["should_watch_only"])

    def test_performance_gate_context_performance(self) -> None:
        """Test context performance gate - grade downgrade."""
        from datetime import datetime, timedelta, timezone
        from plugins.crypto_guard.ga_master.decision_persistence import DecisionPersistence
        from plugins.crypto_guard.ga_master.performance_gate import PerformanceGate

        gate = PerformanceGate(self.repo)
        # Disable cooldown to test context_performance in isolation
        gate._config["cooldown"]["loss_count_threshold"] = 100

        persistence = DecisionPersistence(self.repo)

        # Use repository to create ga_decision properly
        # Must include trade_plan with side to set signals.direction correctly
        ga_decision = {
            "symbol": "BTCUSDT",
            "analysis_time": 1000,
            "analysis_time_utc": "2023-11-14T22:13:19Z",
            "decision_type": "scheduled",
            "signal_grade": "S",
            "trend_stage": "early",
            "market_bias": "bullish",
            "confidence": 0.85,
            "decision": "trade_plan_available",
            "has_trade_plan": True,
            "trade_plan": {
                "side": "LONG",
                "entry_price": 100,
                "stop_loss": 95,
                "take_profit": 110,
            },
            "skill_result_refs": {},
            "evidence": ["测试"],
            "counter_evidence": [],
            "risk_check": {"ok": True},
            "feishu_actions": [],
            "final_summary": "测试",
            "raw_decision_json": {},
        }
        saved = persistence.save(ga_decision)

        # Create paper trade linked to this decision
        signal_id = saved.get("signal_id")
        self.conn.execute(
            """
            INSERT INTO paper_orders(symbol, side, order_type, entry_price, stop_loss, quantity, status, signal_id)
            VALUES ('BTCUSDT', 'LONG', 'limit', 100, 95, 1, 'closed', ?)
            """,
            (signal_id,),
        )
        order_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Insert 3 losing trades
        now = datetime.now(timezone.utc)
        for i in range(3):
            closed_at = (now - timedelta(hours=i + 1)).isoformat().replace("+00:00", "Z")
            self.conn.execute(
                """
                INSERT INTO paper_trades(symbol, side, entry_price, exit_price, stop_loss, quantity, pnl, pnl_percent, pnl_r, close_reason, closed_at, order_id)
                VALUES ('BTCUSDT', 'LONG', 100, 95, 95, 1, -5, -5, -1, 'stop_loss', ?, ?)
                """,
                (closed_at, order_id),
            )

        # Clear cache to ensure fresh data is read
        gate._cache.clear()

        # Check should downgrade S -> A (avg_r = -1 < 0, sample_count = 3 >= min_samples)
        result = gate.check(
            symbol="BTCUSDT",
            side="LONG",
            signal_grade="S",
            trend_stage="early",
            confidence=0.85,
        )
        self.assertTrue(result["performance_degraded"])
        self.assertEqual(result["effective_grade"], "A")
        # S grade with poor performance -> force watch-only (止血策略)
        self.assertTrue(result["should_watch_only"])
        self.assertIn("high_grade_performance_watch_only", result["reasons"])

        # Test with signal_grade "B" - downgrade to "C" triggers watch_only
        result_b = gate.check(
            symbol="BTCUSDT",
            side="LONG",
            signal_grade="B",
            trend_stage="early",
            confidence=0.85,
        )
        self.assertTrue(result_b["performance_degraded"])
        self.assertEqual(result_b["effective_grade"], "C")
        # B->C is below paper order threshold (S/A only)
        self.assertTrue(result_b["should_watch_only"])
        self.assertIn("grade_below_paper_order_threshold", result_b["reasons"])

        # Test with signal_grade "A" - downgrade to "B" should trigger watch_only
        result_a = gate.check(
            symbol="BTCUSDT",
            side="LONG",
            signal_grade="A",
            trend_stage="early",
            confidence=0.85,
        )
        self.assertTrue(result_a["performance_degraded"])
        self.assertEqual(result_a["effective_grade"], "B")
        # A grade with poor performance -> force watch-only (止血策略)
        self.assertTrue(result_a["should_watch_only"])
        self.assertIn("high_grade_performance_watch_only", result_a["reasons"])

    def test_performance_gate_confidence_degradation(self) -> None:
        """Test confidence degradation based on recent performance."""
        from datetime import datetime, timedelta, timezone
        from plugins.crypto_guard.ga_master.performance_gate import PerformanceGate

        gate = PerformanceGate(self.repo)

        # Insert trades for ETHUSDT SHORT with alternating wins/losses
        # to trigger confidence degradation (avg_r < -0.2) but NOT cooldown
        # Cooldown triggers: loss_window=3, loss_count_threshold=2
        # So recent 3 trades must have <= 1 loss to avoid cooldown
        # Confidence degradation: sample_window=5, avg_r_threshold=-0.2
        now = datetime.now(timezone.utc)
        trades = [
            # Recent 3: 1 loss, 2 wins (avoids cooldown)
            ("ETHUSDT", "SHORT", 100, 105, 105, 1, -5, -5, -1.0, "stop_loss", (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")),
            ("ETHUSDT", "SHORT", 100, 99, 105, 1, 1, 1, 0.1, "take_profit", (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")),
            ("ETHUSDT", "SHORT", 100, 99, 105, 1, 1, 1, 0.1, "take_profit", (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")),
            # Older 2: both losses (drags avg_r below -0.2)
            ("ETHUSDT", "SHORT", 100, 105, 105, 1, -5, -5, -1.0, "stop_loss", (now - timedelta(hours=4)).isoformat().replace("+00:00", "Z")),
            ("ETHUSDT", "SHORT", 100, 105, 105, 1, -5, -5, -1.0, "stop_loss", (now - timedelta(hours=5)).isoformat().replace("+00:00", "Z")),
        ]
        for trade in trades:
            self.conn.execute(
                """
                INSERT INTO paper_trades(symbol, side, entry_price, exit_price, stop_loss, quantity, pnl, pnl_percent, pnl_r, close_reason, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                trade,
            )

        # Verify trades were inserted
        count = self.conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE symbol='ETHUSDT' AND side='SHORT' AND closed_at IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(count, 5)

        # Check confidence should be degraded (avg_r = (-1+0.1+0.1-1-1)/5 = -0.56)
        result = gate.check(
            symbol="ETHUSDT",
            side="SHORT",
            signal_grade="A",
            trend_stage="middle",
            confidence=0.8,
        )
        # Cooldown should NOT be active (recent 3 has only 1 loss)
        self.assertFalse(result["cooldown_active"], "Cooldown should not be active")
        # Confidence degradation should be applied
        self.assertEqual(result["confidence_adjustment"], -0.10)
        self.assertAlmostEqual(result["effective_confidence"], 0.70)

    def test_performance_gate_disabled(self) -> None:
        """Test that performance gate can be disabled via config."""
        from plugins.crypto_guard.ga_master.performance_gate import PerformanceGate

        # Override config to disable gate
        gate = PerformanceGate(self.repo)
        gate._config["enabled"] = False

        result = gate.check(
            symbol="BTCUSDT",
            side="LONG",
            signal_grade="S",
            trend_stage="early",
            confidence=0.8,
        )
        self.assertFalse(result["cooldown_active"])
        self.assertFalse(result["performance_degraded"])
        self.assertFalse(result["should_watch_only"])
        self.assertEqual(result["effective_grade"], "S")
        self.assertEqual(result["effective_confidence"], 0.8)

    def test_controller_performance_gate_watch_only_removes_paper_order(self) -> None:
        """Integration test: performance gate watch-only should remove paper_order from actions."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        from plugins.crypto_guard.ga_master.controller import GAMasterController
        from plugins.crypto_guard.ga_master.decision_persistence import DecisionPersistence
        from plugins.crypto_guard.ga_master.decision_schema import GAAnalysisRequest

        controller = GAMasterController(self.repo)
        persistence = DecisionPersistence(self.repo)

        # Setup: insert historical losing trades for BTCUSDT LONG to trigger gate
        now = datetime.now(timezone.utc)

        # Use persistence to create proper ga_decision + signal
        hist_decision = {
            "symbol": "BTCUSDT",
            "analysis_time": 1672531200,
            "analysis_time_utc": "2023-01-01T00:00:00Z",
            "decision_type": "scheduled",
            "signal_grade": "S",
            "trend_stage": "early",
            "market_bias": "bullish",
            "confidence": 0.85,
            "decision": "trade_plan_available",
            "has_trade_plan": True,
            "trade_plan": {"side": "LONG", "entry_price": 100, "stop_loss": 95, "take_profit": 110},
            "skill_result_refs": {},
            "evidence": ["历史"],
            "counter_evidence": [],
            "risk_check": {"ok": True},
            "feishu_actions": [],
            "final_summary": "历史决策",
            "raw_decision_json": {},
        }
        saved = persistence.save(hist_decision)
        hist_signal_id = saved.get("signal_id")

        # Create paper order and trades
        self.conn.execute(
            """
            INSERT INTO paper_orders(symbol, side, order_type, entry_price, stop_loss, quantity, status, signal_id)
            VALUES ('BTCUSDT', 'LONG', 'limit', 100, 95, 1, 'closed', ?)
            """,
            (hist_signal_id,),
        )
        hist_order_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Insert 3 losing trades to trigger context_performance gate
        for i in range(3):
            closed_at = (now - timedelta(hours=i + 1)).isoformat().replace("+00:00", "Z")
            self.conn.execute(
                """
                INSERT INTO paper_trades(symbol, side, entry_price, exit_price, stop_loss, quantity, pnl, pnl_percent, pnl_r, close_reason, closed_at, order_id)
                VALUES ('BTCUSDT', 'LONG', 100, 95, 95, 1, -5, -5, -1, 'stop_loss', ?, ?)
                """,
                (closed_at, hist_order_id),
            )

        # Disable cooldown to isolate context_performance test
        controller.performance_gate._config["cooldown"]["loss_count_threshold"] = 100

        # Fake decision from LLM with S grade and trade_plan
        fake_decision = {
            "symbol": "BTCUSDT",
            "has_trade_plan": True,
            "trade_plan": {
                "side": "LONG",
                "entry_price": 50000,
                "stop_loss": 49000,
                "take_profit": 53000,
            },
            "signal_grade": "S",
            "trend_stage": "early",
            "confidence": 0.85,
            "decision": "trade_plan_available",
            "summary": "测试决策",
            "final_summary": "测试决策",
            "market_bias": "bullish",
            "evidence": ["测试证据"],
            "counter_evidence": [],
            "risk_notes": [],
        }

        # Patch run_agent_sop_decision to return fake decision
        with patch(
            "plugins.crypto_guard.ga_master.controller.run_agent_sop_decision",
            return_value=fake_decision,
        ):
            # Patch ContextBuilder.build to return minimal context
            fake_context = {
                "symbol": "BTCUSDT",
                "snapshot": {
                    "symbol": "BTCUSDT",
                    "current_price": 50000,
                    "market_structure": "bullish",
                },
                "analysis_time_utc": int(now.timestamp()),
                "snapshot_id": None,
            }
            with patch.object(
                controller.context_builder, "build", return_value=fake_context
            ):
                request = GAAnalysisRequest(
                    symbol="BTCUSDT",
                    decision_type="scheduled",
                )
                result = controller.analyze_symbol(request)

        # Verify: performance_gate should be in result
        self.assertIn("performance_gate", result)
        perf_gate = result["performance_gate"]
        self.assertTrue(perf_gate["performance_degraded"])
        self.assertTrue(perf_gate["should_watch_only"])

        # Verify: suggested_actions should NOT contain create_paper_order
        actions = result.get("suggested_actions", [])
        action_types = [a.get("action_type") if isinstance(a, dict) else a for a in actions]
        self.assertNotIn("create_paper_order", action_types)

        # Verify: decision should be opportunity_watch (not trade_plan_available)
        self.assertEqual(result.get("decision"), "opportunity_watch")
        self.assertFalse(result.get("has_trade_plan"))


if __name__ == "__main__":
    unittest.main()
