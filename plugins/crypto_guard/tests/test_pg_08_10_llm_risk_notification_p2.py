# -*- coding: utf-8 -*-
"""08-10 P2-4 RED regression: production order notification renders
original-vs-adjusted via ``build_order_notification``.

Reviewer finding (verbatim): "``notify/order_notification.py:57`` — defined
but called only by tests. The production paper-order notification at
``run_ga_workers.py:355-372`` renders only the single (original) plan fields
and never original-vs-adjusted. **Exact fix:** Call ``build_order_notification``
from the production paper-order creation/fill path (run_ga_workers.py:355-372
and paper_broker.py order routes) with the persisted original candidate and
verified adjusted plan once P1-2 is fixed."

The regression test (the exact-fix regression): drive the PRODUCTION
``_post_decision_effects`` create/pending push with a VERIFIED paper_bounded
decision — the in-memory ``risk_advisory`` envelope carries
``proposal_status="ok"`` + ``verification_ok`` + ``final_risk_check_ok`` plus
the P2-4 ``candidate_plan`` (ORIGINAL, pre-adjustment) and
``entry_confirmation_lifecycle``; ``decision.trade_plan`` is the P1-2 ADJUSTED
plan (wider stop 45.90, risk scaled 0.5*0.36/0.56). The emitted notification
MUST contain original and adjusted entry/stop values, the effective risk
percent, the computed quantity, the TP list, the confirmation source/timeframe,
and the final risk checks.

RED-first: the P2-4 wiring (``_render_auto_order_notification``'s verified-
envelope branch inside ``_post_decision_effects``) does not exist yet — the
create/pending push renders the legacy 08-04 A4 block and never carries the
builder markers (``**订单**``, ``原始 ... 调整 ...``, ``有效风险``,
``入场确认 ... 最终风控：通过``).

No production DB mutation, no marker write (``make_repo`` initializes only the
scratch schema), no service restart, no commit/push/release.
"""
from __future__ import annotations

import pytest

from plugins.crypto_guard.tests.pg_fixtures import make_repo

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

_SYMBOL = "LTCUSDT"

# Candidate (ORIGINAL, pre-adjustment): entry 45.34 / stop 45.70 (SL distance
# 0.36, 0.794% — below the 0.8% engine floor). Adjusted (P1-2, verifier
# scenario-8 wider stop): entry 45.34 / stop 45.90 (distance 0.56), risk scaled
#   risk_percent = 0.5 * 0.36 / 0.56 = 0.32142857142857145
#   qty = equity * (risk_percent/100) / |stop-entry|
#       = 10000 * (0.32142857142857145/100) / 0.56 = 57.39795918367347
_ADJ_RISK_PCT = 0.5 * 0.36 / 0.56
_ADJ_QTY = 10_000.0 * (_ADJ_RISK_PCT / 100.0) / (45.90 - 45.34)


def _verified_envelope() -> dict:
    return {
        "mode": "paper_bounded",
        "proposal_status": "ok",
        "verification_ok": True,
        "final_risk_check_ok": True,
        # 08-10 P2-4: the ORIGINAL candidate rides the in-memory envelope so
        # the notification can render original-vs-adjusted geometry.
        "candidate_plan": {
            "side": "SHORT",
            "entry_type": "limit",
            "entry_price": 45.34,
            "trigger_price": 45.34,
            "stop_loss": 45.70,
            "take_profits": [{"price": 44.40, "ratio": 0.5},
                             {"price": 43.90, "ratio": 0.5}],
            "risk_percent": 0.5,
            "invalid_condition": "5m 收盘站回 45.80",
        },
        "entry_confirmation_lifecycle": {
            "status": "valid",
            "origin": "current_snapshot",
            "timeframe": "5m",
            "source": "price_action",
            "event_type": "BOS",
            "age_bars": 0,
            "ttl_bars": 3,
            "source_decision_id": 1,
            "source_snapshot_id": 1,
            "invalidation_reason": None,
        },
    }


def _adjusted_plan() -> dict:
    return {
        "side": "SHORT",
        "entry_type": "limit",
        "entry_price": 45.34,
        "trigger_price": 45.34,
        "stop_loss": 45.90,
        "take_profits": [{"price": 44.40, "ratio": 0.5},
                         {"price": 43.90, "ratio": 0.5}],
        "risk_percent": _ADJ_RISK_PCT,
        "invalid_condition": "5m 收盘站回 45.80",
    }


def _decision() -> dict:
    return {
        "signal_id": 999,
        "symbol": _SYMBOL,
        "signal_grade": "A",
        "confidence": 0.85,
        "has_trade_plan": True,
        "trade_plan": _adjusted_plan(),
        "risk_check": {"ok": True, "risk_percent": _ADJ_RISK_PCT},
        "risk_advisory": _verified_envelope(),
        "ga_decision_id": 7777,
    }


def _harness(monkeypatch):
    """Delivery-boundary seams stubbed exactly like F4 (feishu target + send +
    order-creation); the render template under test runs for real."""
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
    monkeypatch.setattr(paper_broker,
                        "create_paper_order_from_ga_decision", fake_create)
    return captured, order_created


class TestP2_4OrderNotificationWiring:
    """The production create/pending push must render original-vs-adjusted
    geometry through ``build_order_notification`` for a VERIFIED envelope."""

    def test_verified_envelope_renders_original_vs_adjusted(self, monkeypatch) -> None:
        captured, order_created = _harness(monkeypatch)
        from plugins.crypto_guard import run_ga_workers

        handle = make_repo()
        try:
            repo = handle.repo
            run_ga_workers._post_decision_effects(
                repo, _decision(),
                {"allow_realtime_signal_alert": False},
                send_message=lambda **k: True,
            )
            assert order_created == [7777], (
                "the auto-order gate must reach order creation"
            )
            text = captured.get("text")
            assert text, "P2-4 RED: the create/pending push must be rendered"
            # builder header + computed quantity
            assert f"**订单** {_SYMBOL} SHORT limit · 数量 {_ADJ_QTY:.2f}" in text, text
            # adjusted geometry (what actually fills)
            assert "- 入场 45.34 · 止损 45.90" in text, text
            # original-vs-adjusted (the whole point of P2-4)
            assert "- 原始 45.34/45.70 · 调整 45.34/45.90" in text, text
            # effective risk percent + the TP list
            assert (
                f"- 有效风险 {_ADJ_RISK_PCT:.2f}% · 止盈 44.40 · 43.90" in text
            ), text
            # confirmation source / timeframe / remaining TTL + final risk checks
            assert (
                "- 入场确认 price_action 5m · 已延续 0 · 剩余 3 · 最终风控：通过"
                in text
            ), text
        finally:
            handle.close()

    def test_legacy_decision_keeps_a4_render(self, monkeypatch) -> None:
        """Positive control: a decision WITHOUT a verified envelope keeps the
        08-04 contract-A render (F4 mandated fields intact) and carries NONE of
        the builder markers."""
        captured, order_created = _harness(monkeypatch)
        from plugins.crypto_guard import run_ga_workers

        handle = make_repo()
        try:
            repo = handle.repo
            decision = _decision()
            decision.pop("risk_advisory")  # no verified envelope
            run_ga_workers._post_decision_effects(
                repo, decision,
                {"allow_realtime_signal_alert": False},
                send_message=lambda **k: True,
            )
            assert order_created == [7777]
            text = captured.get("text")
            assert text
            # 08-04 A4 mandated fields, byte-compatible. The legacy render uses
            # raw ``str()``/f-string formatting, so 45.90 -> "45.9", 44.40 ->
            # "44.4" (only the builder's ``{:.2f}`` keeps two decimals).
            assert "订单号：4242" in text
            assert "产品：LTCUSDT" in text
            assert "做空" in text  # SHORT side
            assert "待成交挂单" in text
            assert "入场价：45.34" in text
            assert "止损价：45.9" in text
            assert "止盈价：44.4, 43.9" in text
            assert "决策ID：7777" in text
            # the legacy render must NOT carry the builder markers
            assert "**订单**" not in text, text
            assert "最终风控：通过" not in text, text
        finally:
            handle.close()
