# -*- coding: utf-8 -*-
"""08-04 Phase 7.5: regression tests for every fresh-reviewer finding F1-F5.

The 08-04 fresh ``crypto-guard-reviewer`` returned 5 findings:
- F1 (P1): the fill push producer did not carry ``source_decision_id`` /
  ``slippage`` / ``position`` (contract A5 was render-only; the payload written
  by ``_fill_order`` lacked the mandated fields).
- F2 (P1): ``build_context_envelope`` was never wired into the production
  ``ContextBuilder``, so the D6 evidence-grounding gate saw an empty
  ``context_envelope`` in production.
- F3 (P1): the read-only ``AnalysisToolBroker`` verifier was never wired into
  the two production order-creation gates.
- F4 (P2): the A4 create/pending push render was untested.
- F5 (P2): the partial unique index ``idx_paper_orders_trigger_watch_once``
  DB-level rejection was untested.

Every test here drives the REAL production code (real repo, real broker, real
``_fill_order`` path, real render). The only seams are dependency-injection
points whose own behavior is covered elsewhere: ``_analyze`` (order-gate seam),
``resolve_report_target`` / ``send_markdown_alert`` (feishu delivery boundary),
and ``create_paper_order_from_ga_decision`` (order-creation boundary; the A4
test locks the RENDER contract, and F3's veto path never reaches it).

No production DB mutation, no marker write, no service restart, no commit.
"""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.tests.test_pg_08_04_watch_order_bridge_b import (
    _materialize_breakout_watch,
    _rejected_analyze,
)

_SYMBOL = "BTCUSDT"
_BASE = 1_700_000_000_000


# ── shared seed helpers (mirror _smoke_suite patterns, PG-native) ─────────────


def _seed_ga_decision(conn, *, symbol: str = _SYMBOL, analysis_time: int = _BASE) -> int:
    """Insert a minimal but fill-recheck-valid ga_decisions row; return its id."""
    from datetime import datetime, timezone

    at = int(analysis_time)
    at_utc = datetime.fromtimestamp(at / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan_json = json.dumps({
        "side": "LONG", "entry_type": "market", "entry_price": 100.0,
        "stop_loss": 95.0, "take_profits": [{"price": 110.0}],
        "risk_percent": 0.5, "invalid_condition": "15m 收盘跌破 94.0", "reason": "F1 fill test",
    })
    risk_json = json.dumps({"ok": True, "reasons": [], "metrics": {}})
    evidence_json = json.dumps({"bias": "bullish", "confidence": 0.85})
    raw_json = json.dumps({"llm_status": "ok", "symbol": symbol})
    cur = conn.execute(
        "INSERT INTO ga_decisions (symbol, analysis_time, analysis_time_utc, decision_type, "
        "  signal_grade, confidence, market_bias, trend_stage, decision, skill_result_refs_json, "
        "  evidence_json, counter_evidence_json, risk_check_json, feishu_actions_json, "
        "  final_summary, raw_decision_json, trade_plan_json, rendered_summary, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id",
        (symbol, at, at_utc, "scheduled_analysis", "A", 0.85, "bullish", "middle",
         "trade_plan_available", "[]", evidence_json, "[]", risk_json, '["create_paper_order"]',
         "test", raw_json, plan_json, "", at_utc),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return int(new_id)


def _insert_order(
    conn,
    *,
    symbol: str = _SYMBOL,
    side: str = "LONG",
    order_type: str = "market",
    entry_price: float = 100.0,
    stop_loss: float = 95.0,
    ga_decision_id: int | None = None,
    status: str = "pending",
    trigger_watch_id: int | None = None,
) -> int:
    """Insert a paper_orders row with minimal NOT NULL columns; return its id."""
    from datetime import datetime, timezone

    created_at = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO paper_orders(symbol, side, order_type, entry_price, stop_loss, quantity, "
        "  status, created_at, expires_at, ga_decision_id, trigger_watch_id) "
        "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s) RETURNING id",
        (symbol, side, order_type, entry_price, stop_loss, status, created_at, created_at,
         ga_decision_id, trigger_watch_id),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return int(new_id)


def _paper_event_alert_jobs(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT payload_json FROM agent_jobs WHERE job_type='paper_event_alert' ORDER BY id"
    ).fetchall()
    out = []
    for row in rows:
        payload = row["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        out.append(payload)
    return out


# ── F1 (P1): fill push producer carries source_decision_id / slippage / position ─


class TestFillPushProducerCarriesA5Fields:
    """F1: drive the REAL ``_fill_order`` path and assert the enqueued
    ``paper_event_alert`` payload (not just the render) carries the mandated
    A5 fields, and that the push render shows them."""

    def test_fill_enqueues_payload_with_source_decision_slippage_position(self) -> None:
        from unittest import mock

        from plugins.crypto_guard.paper.paper_broker import fill_order_if_triggered

        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            ga_id = _seed_ga_decision(conn, symbol=_SYMBOL, analysis_time=_BASE)
            order_id = _insert_order(
                conn, symbol=_SYMBOL, side="LONG", order_type="market",
                entry_price=100.0, stop_loss=95.0, ga_decision_id=ga_id,
            )
            row = conn.execute("SELECT * FROM paper_orders WHERE id=%s", (order_id,)).fetchone()
            order = dict(row)
            market = {"open": 100.0, "high": 100.5, "low": 99.0, "close": 100.5, "prev_close": 99.0}
            event_time = _BASE + 7_200_000  # 2h after the GA decision
            with mock.patch(
                "plugins.crypto_guard.paper.paper_broker._should_check_market_data_health_for_fill",
                return_value=False,
            ):
                result = fill_order_if_triggered(repo, order, market, event_time=event_time)
            assert result.get("filled") is True, result

            jobs = _paper_event_alert_jobs(conn)
            assert jobs, "F1 RED: a paper_event_alert job must be enqueued on fill"
            payload = jobs[-1]
            assert payload.get("event_type") == "paper_order_filled", payload
            # F1 mandated payload fields.
            assert payload.get("source_decision_id") == ga_id, (
                f"F1 RED: fill payload must carry source_decision_id; {payload}"
            )
            assert payload.get("slippage") is not None, (
                f"F1 RED: fill payload must carry price-level slippage; {payload}"
            )
            pos = payload.get("position")
            assert isinstance(pos, dict), f"F1 RED: fill payload must carry position; {payload}"
            assert str(pos.get("side") or "").upper() == "LONG", pos
            assert pos.get("quantity") not in (None, 0), pos
            assert pos.get("avg_price") is not None, pos

            # The real push handler renders 滑点 / 持仓 / 决策ID from the payload.
            from plugins.crypto_guard.run_ga_workers import handle_paper_event_alert

            rendered = handle_paper_event_alert(repo, payload, send_message=None)
            text = rendered["text"]
            assert "滑点" in text, "F1: fill push must render 滑点"
            assert "持仓" in text, "F1: fill push must render 持仓"
            assert f"{ga_id}" in text, "F1: fill push must render the source 决策ID"
        finally:
            handle.close()


# ── F2 (P1): context envelope wired into production ContextBuilder ───────────


class TestContextEnvelopeWiredInProduction:
    """F2: the production ``ContextBuilder`` must attach the versioned context
    envelope to the in-memory snapshot so the D6 evidence-grounding gate
    (``_build_allowed_evidence_ids``) sees provenance-tagged evidence."""

    def test_builder_attaches_envelope_and_grounds_allowed_evidence(self) -> None:
        from plugins.crypto_guard.ga_master.context_builder import ContextBuilder
        from plugins.crypto_guard.ga_master.decision_schema import GAAnalysisRequest
        from plugins.crypto_guard.reasoning.llm_agent_judge import _build_allowed_evidence_ids

        handle = make_repo()
        try:
            repo = handle.repo
            # momentum module deliberately carries NO timeframe/as_of so it does
            # NOT contribute a module-derived evidence_id — only the envelope's
            # derived_evidence path can ground it (isolates the F2 wiring).
            snapshot = {
                "symbol": _SYMBOL,
                "analysis_time_utc": _BASE,
                "mode": "ad_hoc",
                "modules": {
                    "market_regime": {"regime": "normal", "extreme": False},
                    "momentum": {"direction": "bullish"},
                },
                "data_quality": {"status": "complete", "closed_candles_only": True},
            }
            request = GAAnalysisRequest(
                symbol=_SYMBOL,
                decision_type="scheduled_market_analysis",
                analysis_time_utc=_BASE,
                timeframes=["15m"],
                snapshot=snapshot,
            )
            ctx = ContextBuilder(repo).build(request)
            env = ctx["snapshot"].get("context_envelope")
            assert env is not None, "F2 RED: production snapshot must carry context_envelope"
            assert env.get("envelope_version") == "1.0", env
            for section in ("trusted_facts", "derived_evidence", "bounded_memory", "execution_state"):
                assert section in env, f"F2 RED: envelope must carry '{section}'"
            derived_ids = [
                item.get("evidence_id")
                for item in env.get("derived_evidence", [])
                if isinstance(item, dict)
            ]
            assert any(
                isinstance(did, str) and did.startswith("momentum:BTCUSDT:15m:")
                for did in derived_ids
            ), f"F2 RED: envelope derived_evidence must carry an evidence_id; {derived_ids}"

            # D6 gate must now see the envelope-derived evidence id.
            allowed = _build_allowed_evidence_ids(ctx["snapshot"])
            assert any(
                isinstance(eid, str) and eid.startswith("momentum:BTCUSDT:15m:")
                for eid in allowed
            ), (
                "F2 RED: _build_allowed_evidence_ids must include the envelope-derived "
                f"evidence_id; allowed={allowed}"
            )
        finally:
            handle.close()


# ── F3 (P1): analysis broker verifier wired into both order-creation gates ───


class TestBrokerVerifierWiredIntoProductionGates:
    """F3: the read-only broker's VETO-ONLY verifier runs inside the two
    production order-creation gates and a veto blocks the order."""

    def test_broker_verifier_vetoes_on_concentration_breach(self) -> None:
        from plugins.crypto_guard.run_ga_workers import _broker_verifier_allows

        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            # >5 concurrent open orders across the whole account = breach.
            for i in range(6):
                _insert_order(conn, symbol=f"ALT{i:02d}", status="open")
            allowed, detail = _broker_verifier_allows(
                repo, symbol=_SYMBOL, timeframe="15m",
                analysis_time_utc=_BASE, deterministic_risk_ok=True,
            )
            assert allowed is False, (
                "F3 RED: concentration breach must VETO the order; " f"{detail}"
            )
            assert detail.get("reason") == "broker_verifier", detail
            assert detail.get("verdict") == "veto", detail
        finally:
            handle.close()

    def test_broker_verifier_approves_clean_account(self) -> None:
        from plugins.crypto_guard.run_ga_workers import _broker_verifier_allows

        handle = make_repo()
        try:
            allowed, detail = _broker_verifier_allows(
                handle.repo, symbol=_SYMBOL, timeframe="15m",
                analysis_time_utc=_BASE, deterministic_risk_ok=True,
            )
            assert allowed is True, f"F3: a clean account must approve; {detail}"
            assert detail.get("verdict") == "approve", detail
        finally:
            handle.close()

    def test_broker_verifier_fail_open_on_missing_seams(self) -> None:
        from types import SimpleNamespace

        from plugins.crypto_guard.run_ga_workers import _broker_verifier_allows

        shim = SimpleNamespace()  # no broker read seams -> fail-open
        allowed, detail = _broker_verifier_allows(
            shim, symbol=_SYMBOL, timeframe="15m",
            analysis_time_utc=_BASE, deterministic_risk_ok=True,
        )
        assert allowed is True, f"F3: missing seams must fail open; {detail}"
        assert detail.get("reason") == "broker_seams_missing_skip", detail

    def test_post_decision_effects_veto_blocks_auto_order(self, monkeypatch) -> None:
        """F3: the REAL broker veto runs inside ``_post_decision_effects`` and
        a concentration breach must stop the auto-order (real veto, real gate).
        The order-creation boundary is stubbed to a recorder so the test is
        deterministic: the assertion is that the veto PREVENTED creation."""
        from plugins.crypto_guard import run_ga_workers
        from plugins.crypto_guard.paper import paper_broker

        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            for i in range(6):
                _insert_order(conn, symbol=f"ALT{i:02d}", status="open")

            calls: list[tuple] = []
            orig = run_ga_workers._broker_verifier_allows
            create_calls: list[int] = []

            def recorder(*args, **kwargs):
                calls.append((args, kwargs))
                return orig(*args, **kwargs)

            def record_create(repo_, ga_decision_id):
                create_calls.append(int(ga_decision_id))
                return {"ok": True, "created": True, "order_id": 4242}

            monkeypatch.setattr(run_ga_workers, "_broker_verifier_allows", recorder)
            monkeypatch.setattr(
                paper_broker, "create_paper_order_from_ga_decision", record_create,
            )
            decision = {
                "signal_id": 999,
                "symbol": _SYMBOL,
                "signal_grade": "A",
                "confidence": 0.85,
                "has_trade_plan": True,
                "trade_plan": {
                    "side": "LONG", "entry_type": "limit", "entry_price": 100.0,
                    "stop_loss": 95.0, "take_profits": [{"price": 108.0}], "risk_percent": 0.5,
                },
                "risk_check": {"ok": True, "risk_percent": 0.5},
                "ga_decision_id": 7777,
            }
            run_ga_workers._post_decision_effects(
                repo, decision, {"allow_realtime_signal_alert": False}, send_message=None,
            )
            assert calls, "F3 RED: _post_decision_effects must consult the broker verifier"
            assert calls[0][1]["symbol"] == _SYMBOL
            assert create_calls == [], (
                "F3 RED: the broker veto must prevent order creation; " f"{create_calls}"
            )
            assert repo.list_open_paper_orders_for_symbol(_SYMBOL) == [], (
                "F3 RED: the broker veto must block the auto order"
            )
        finally:
            handle.close()

    def test_watch_recheck_veto_blocks_order(self, monkeypatch) -> None:
        """F3: the REAL broker veto runs inside ``handle_opportunity_watch_recheck``;
        a concentration breach must reject the bridge before any order. The
        order-creation boundary is stubbed to a recorder so the test is
        deterministic: the assertion is that the veto PREVENTED creation."""
        from plugins.crypto_guard import run_ga_workers
        from plugins.crypto_guard.paper import paper_broker

        create_calls: list[int] = []

        def record_create(repo_, ga_decision_id):
            create_calls.append(int(ga_decision_id))
            return {"ok": True, "created": True, "order_id": 4242}

        monkeypatch.setattr(
            paper_broker, "create_paper_order_from_ga_decision", record_create,
        )
        handle = make_repo()
        try:
            repo = handle.repo
            conn = handle.conn
            watch = repo.get_opportunity_watch(_materialize_breakout_watch(repo)["watch_id"])
            watch_id = int(watch["id"])
            for i in range(6):
                _insert_order(conn, symbol=f"ALT{i:02d}", status="open")

            result = run_ga_workers.handle_opportunity_watch_recheck(
                repo, {"watch_id": watch_id}, send_message=None,
                _analyze=_rejected_analyze(grade="A", side="LONG"),
            )
            assert result.get("ok") is True, result
            assert result.get("rejected") is True, (
                f"F3 RED: the broker veto must reject the recheck bridge; {result}"
            )
            assert result.get("reason") == "broker_verifier_veto", result
            assert create_calls == [], (
                "F3 RED: the veto must prevent order creation in the recheck "
                f"bridge; {create_calls}"
            )
            orders = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert orders == [], f"F3 RED: no order may be bridged under a broker veto; {orders}"
            fresh = repo.get_opportunity_watch(watch_id)
            assert fresh["recheck_status"] == "recheck_rejected", fresh
        finally:
            handle.close()


# ── F4 (P2): A4 create/pending push render carries all mandated fields ───────


class TestA4CreatePushRenderMandatedFields:
    """F4: the create/pending push rendered by ``_post_decision_effects`` must
    carry order_id / symbol / side / order_type / entry / SL / TP /
    quantity-or-risk / expiry / source_decision_id. Delivery-boundary seams
    (feishu target + send + order-creation) are stubbed; the render template
    under test runs for real."""

    def test_create_push_render_has_all_mandated_fields(self, monkeypatch) -> None:
        from plugins.crypto_guard import run_ga_workers
        from plugins.crypto_guard.paper import paper_broker

        captured: dict = {}
        order_created: list = []

        def fake_target(repo, payload=None):
            return {"receive_id": "oc_test", "receive_id_type": "chat_id"}

        def fake_send(repo, send_message, *, receive_id, receive_id_type, text,
                      alert_type, symbol=None, priority=5, dedupe_key=None):
            captured["text"] = text
            return {"ok": True, "sent": True}

        def fake_create(repo, ga_decision_id):
            order_created.append(int(ga_decision_id))
            return {"ok": True, "created": True, "order_id": 4242}

        monkeypatch.setattr(run_ga_workers, "resolve_report_target", fake_target)
        monkeypatch.setattr(run_ga_workers, "send_markdown_alert", fake_send)
        monkeypatch.setattr(paper_broker, "create_paper_order_from_ga_decision", fake_create)

        handle = make_repo()
        try:
            repo = handle.repo
            decision = {
                "signal_id": 999,
                "symbol": _SYMBOL,
                "signal_grade": "A",
                "confidence": 0.85,
                "has_trade_plan": True,
                "trade_plan": {
                    "side": "LONG", "entry_type": "limit", "entry_price": 100.0,
                    "stop_loss": 95.0,
                    "take_profits": [{"price": 108.0}, {"price": 115.0}],
                    "risk_percent": 0.5,
                },
                "risk_check": {"ok": True, "risk_percent": 0.5},
                "ga_decision_id": 7777,
            }
            run_ga_workers._post_decision_effects(
                repo, decision, {"allow_realtime_signal_alert": False}, send_message=lambda **k: True,
            )
            assert order_created == [7777], "the auto-order gate must reach order creation"
            text = captured.get("text")
            assert text, "F4 RED: the create/pending push must be rendered"
            # A4 mandated fields.
            assert "订单号：4242" in text, "F4: order_id"
            assert "产品：BTCUSDT" in text, "F4: symbol"
            assert "做多" in text, "F4: side"
            assert "待成交挂单" in text, "F4: order_type (limit -> 待成交挂单)"
            assert "入场价：100.0" in text, "F4: entry"
            assert "止损价：95.0" in text, "F4: SL"
            assert "止盈价：108.0, 115.0" in text, "F4: TP"
            assert "数量/风险：0.5% 风险" in text, "F4: quantity-or-risk"
            assert "有效期：" in text, "F4: expiry"
            assert "决策ID：7777" in text, "F4: source_decision_id"
        finally:
            handle.close()


# ── F5 (P2): partial unique index rejects a second live order per watch ───────


class TestPartialUniqueIndexRejectsDuplicate:
    """F5: the DB itself must reject a second live paper order for the same
    trigger_watch_id (the index is the last line of defense behind the task
    lock + idempotency check)."""

    def test_second_live_order_for_same_watch_raises_unique_violation(self) -> None:
        from psycopg import errors as pg_errors

        handle = make_repo()
        try:
            conn = handle.conn
            watch_id = 987654
            _insert_order(conn, trigger_watch_id=watch_id, status="open")
            # A second live order (pending) for the SAME watch must be rejected
            # by the partial unique index.
            with pytest.raises(pg_errors.UniqueViolation):
                _insert_order(conn, trigger_watch_id=watch_id, status="pending")
            conn.rollback()
            rows = conn.execute(
                "SELECT * FROM paper_orders WHERE trigger_watch_id=%s", (watch_id,),
            ).fetchall()
            assert len(rows) == 1, (
                "F5 RED: only the first live order may survive; " f"{rows}"
            )
        finally:
            handle.close()
