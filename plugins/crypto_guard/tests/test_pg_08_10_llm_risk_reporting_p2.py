# -*- coding: utf-8 -*-
"""08-10 Step 1 RED contract: LLM risk-governance hourly reporting (P2).

Contract under test (design.md §11, prd.md P2-1):

  - ``hourly_report._decision_row`` exposes the three risk-governance
    envelopes persisted on the decision (all inside ``raw_decision_json``):
      * ``entry_confirmation_lifecycle`` -- {status (valid/expired/invalidated/
        absent), origin (current_snapshot/carried_forward), timeframe, source,
        event_type, age_bars, ttl_bars, source_decision_id, source_snapshot_id,
        invalidation_reason}
      * ``llm_risk_proposal`` -- {proposal_status (none/ok/failed), verdict
        (approve_as_is/adjust/wait/reject), reason_codes, evidence_refs,
        counter_evidence_refs, adjustments}
      * ``risk_adjustment_verification`` -- {verification_ok, accepted,
        rejection_reasons, monetary_risk_delta, final_risk_check_ok,
        effective_order_allowed}
    plus ``llm_risk_scope`` (current/legacy) gated to the 08-10 marker window,
    exactly like ``execution_funnel_scope``.
  - ``hourly_report._aggregate_llm_risk_funnel(rows)`` computes the hourly
    risk-committee funnel over ONLY ``llm_risk_scope == "current"`` rows:
    confirmation statuses (current/carried/expired/invalidated/absent),
    proposal verdicts (approve_as_is/adjust/wait/reject/failed), verifier
    accepted vs rejected-by-reason, final_risk_pass and orders_created.
    Legacy rows contribute 0 to every bucket.
  - ``notify.order_notification.build_order_notification`` renders an order
    notification with original/adjusted entry+stop, effective risk %, quantity,
    TP list, confirmation source/timeframe/age, and the final risk checks; it
    FAILS CLOSED (ValueError) when there is no verifier success.
  - ``render_ga_hourly_summary`` accepts ``llm_risk_funnel_stats`` and renders
    a "**LLM 风控委员会（本小时）**" section; absent stats -> no section.
  - ``hourly_report._get_llm_risk_report_contract_marker_ts(repo)`` returns
    the MAX ``applied_at`` across the four 08-10 markers, or None when ANY
    marker is missing (function-local import so ``hourly_report`` stays
    importable in RED).
  - ``_format_opportunity_row`` appends a confirmation line (source /
    timeframe / age_bars / expiry reason) with NO raw JSON dump; status
    "absent" renders no line.

RED-first: none of these symbols exist yet -- ``_decision_row`` raises
KeyError on the new envelopes and TypeError on the new kwarg,
``render_ga_hourly_summary`` raises TypeError on ``llm_risk_funnel_stats``,
``_aggregate_llm_risk_funnel`` / ``_get_llm_risk_report_contract_marker_ts``
are absent, ``order_notification`` raises ModuleNotFoundError, and the
confirmation line never renders.

No production DB mutation, no marker write (``make_repo`` initializes only the
scratch schema), no service restart, no commit/push/release.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.notify import hourly_report
from plugins.crypto_guard.tests.pg_fixtures import make_repo

_T0 = 1_700_000_100_000  # exact 5m bar-close boundary (LTC 4985)
_T1 = _T0 + 300_000  # next closed 5m bar

# The four 08-10 contract markers (verbatim from design.md §12; the GREEN
# diagnostics module ``llm_risk_governance`` re-exports the same constants).
MARKERS = {
    "lifecycle": "entry_confirmation_lifecycle_contract_v1",
    "proposal": "llm_risk_proposal_contract_v1",
    "verifier": "risk_adjustment_verifier_contract_v1",
    "context": "llm_risk_context_isolation_contract_v1",
}


# ── envelope fixtures ─────────────────────────────────────────────────────────


def _lifecycle(*, status: str = "valid", origin: str = "current_snapshot",
               timeframe: str = "5m", source: str = "price_action",
               event_type: str = "BOS", age_bars: int = 0, ttl_bars: int = 3,
               source_decision_id: int | None = 1,
               source_snapshot_id: int | None = 1,
               invalidation_reason: str | None = None) -> dict:
    return {
        "status": status,
        "origin": origin,
        "timeframe": timeframe,
        "source": source,
        "event_type": event_type,
        "age_bars": age_bars,
        "ttl_bars": ttl_bars,
        "source_decision_id": source_decision_id,
        "source_snapshot_id": source_snapshot_id,
        "invalidation_reason": invalidation_reason,
    }


def _proposal(*, status: str = "ok", verdict: str = "approve_as_is",
              reason_codes: list[str] | None = None,
              evidence_refs: list[str] | None = None,
              counter_evidence_refs: list[str] | None = None,
              adjustments: dict | None = None) -> dict:
    return {
        "proposal_status": status,
        "verdict": verdict,
        "reason_codes": reason_codes or [],
        "evidence_refs": evidence_refs or [],
        "counter_evidence_refs": counter_evidence_refs or [],
        "adjustments": adjustments,
    }


def _verification(*, ok: bool = True, accepted: bool = True,
                  rejection_reasons: list[str] | None = None,
                  delta: float = 0.0, final_ok: bool = True,
                  order_allowed: bool = True) -> dict:
    return {
        "verification_ok": ok,
        "accepted": accepted,
        "rejection_reasons": rejection_reasons or [],
        "monetary_risk_delta": delta,
        "final_risk_check_ok": final_ok,
        "effective_order_allowed": order_allowed,
    }


def _row(*, created_at: str = "2026-08-10T13:00:00Z", lifecycle=None,
         proposal=None, verification=None, symbol: str = "LTCUSDT") -> dict:
    """A DB-shaped ``ga_decisions`` row whose ``raw_decision_json`` carries the
    three risk-governance envelopes plus the stable evidence set."""
    raw: dict = {
        "symbol": symbol,
        "risk_check": {"ok": True},
        "evidence_ids": ["ev_1", "ev_2"],
        "plan_execution_state": "confirmed",
    }
    if lifecycle is not None:
        raw["entry_confirmation_lifecycle"] = lifecycle
    if proposal is not None:
        raw["llm_risk_proposal"] = proposal
    if verification is not None:
        raw["risk_adjustment_verification"] = verification
    return {
        "symbol": symbol,
        "signal_grade": "A",
        "confidence": 0.8,
        "analysis_time": _T1,
        "created_at": created_at,
        "market_bias": "bearish",
        "trend_stage": "middle",
        "raw_decision_json": json.dumps(raw),
        "trade_plan_json": json.dumps({
            "side": "SHORT", "entry_price": 45.34, "stop_loss": 45.70,
            "risk_percent": 0.5,
        }),
        "risk_check_json": json.dumps({"ok": True}),
    }


def _llm_row(*, scope: str = "current", lifecycle=None, proposal=None,
             verification=None, paper_order_id: int | None = None) -> dict:
    """A hand-crafted ``_decision_row``-shaped row for the aggregate tests."""
    return {
        "llm_risk_scope": scope,
        "entry_confirmation_lifecycle": lifecycle,
        "llm_risk_proposal": proposal,
        "risk_adjustment_verification": verification,
        "paper_order_id": paper_order_id,
    }


# ── _decision_row exposes the three risk-governance envelopes ────────────────


class TestDecisionRowLLMRiskEnvelopes:
    """``_decision_row`` surfaces the three envelopes + ``llm_risk_scope``.
    RED: the keys do not exist yet -> KeyError / TypeError."""

    def test_exposes_entry_confirmation_lifecycle(self) -> None:
        out = hourly_report._decision_row(
            _row(lifecycle=_lifecycle(status="valid", origin="carried_forward",
                                      age_bars=2, ttl_bars=3)))
        lc = out["entry_confirmation_lifecycle"]  # RED: KeyError
        assert lc["status"] == "valid"
        assert lc["origin"] == "carried_forward"
        assert lc["timeframe"] == "5m"
        assert lc["age_bars"] == 2 and lc["ttl_bars"] == 3

    def test_exposes_llm_risk_proposal(self) -> None:
        out = hourly_report._decision_row(
            _row(proposal=_proposal(status="ok", verdict="adjust",
                                    reason_codes=["minimum_stop_distance"],
                                    adjustments={"stop_loss": 45.90})))
        prop = out["llm_risk_proposal"]  # RED: KeyError
        assert prop["proposal_status"] == "ok"
        assert prop["verdict"] == "adjust"
        assert prop["adjustments"] == {"stop_loss": 45.90}

    def test_exposes_risk_adjustment_verification(self) -> None:
        out = hourly_report._decision_row(
            _row(verification=_verification(ok=True, accepted=True,
                                            delta=-0.01, final_ok=True)))
        ver = out["risk_adjustment_verification"]  # RED: KeyError
        assert ver["verification_ok"] is True
        assert ver["monetary_risk_delta"] == -0.01
        assert ver["final_risk_check_ok"] is True

    def test_llm_risk_scope_current_after_cutoff(self) -> None:
        out = hourly_report._decision_row(
            _row(created_at="2026-08-10T13:00:00Z"),
            llm_risk_cutoff_utc="2026-08-10T12:00:00Z")  # RED: TypeError kwarg
        assert out["llm_risk_scope"] == "current"

    def test_llm_risk_scope_legacy_without_cutoff_fails_closed(self) -> None:
        out = hourly_report._decision_row(
            _row(created_at="2026-08-10T13:00:00Z"))
        # No 08-10 marker deployed -> FAIL CLOSED to legacy (never fail-open).
        assert out.get("llm_risk_scope") == "legacy"

    def test_existing_decision_row_fields_unchanged(self) -> None:
        """Back-compat: the existing key set keeps working (no regression)."""
        out = hourly_report._decision_row(_row())
        assert out["symbol"] == "LTCUSDT"
        assert out["plan_execution_state"] == "confirmed"


# ── hourly risk-committee funnel aggregate ───────────────────────────────────


class TestAggregateLLMRiskFunnel:
    """``_aggregate_llm_risk_funnel(rows)`` sums the risk-committee buckets
    over ONLY ``llm_risk_scope == "current"`` rows. RED: the helper does not
    exist -> AttributeError."""

    def _funnel(self, rows: list[dict]) -> dict:
        return hourly_report._aggregate_llm_risk_funnel(rows)

    def test_current_rows_bucket_everything(self) -> None:
        rows = [
            # 1. carried confirmation + adjust accepted (risk reduced) + order
            _llm_row(
                lifecycle=_lifecycle(status="valid", origin="carried_forward",
                                     age_bars=2, ttl_bars=3),
                proposal=_proposal(status="ok", verdict="adjust",
                                   adjustments={"stop_loss": 45.90}),
                verification=_verification(accepted=True, delta=-0.01,
                                           final_ok=True),
                paper_order_id=10,
            ),
            # 2. expired confirmation + approve_as_is accepted
            _llm_row(
                lifecycle=_lifecycle(status="expired", origin="current_snapshot"),
                proposal=_proposal(verdict="approve_as_is"),
                verification=_verification(accepted=True, delta=0.0,
                                           final_ok=True),
            ),
            # 3. invalidated confirmation + wait accepted
            _llm_row(
                lifecycle=_lifecycle(status="invalidated",
                                     invalidation_reason="opposite_structure"),
                proposal=_proposal(verdict="wait"),
                verification=_verification(accepted=True, delta=0.0,
                                           final_ok=True),
            ),
            # 4. absent confirmation + system-failed proposal + hard-gate reject
            _llm_row(
                lifecycle=None,
                proposal=_proposal(status="failed", verdict=None),
                verification=_verification(ok=False, accepted=False,
                                           rejection_reasons=[], final_ok=False),
            ),
            # 5. current/valid confirmation + reject accepted but final risk NO
            _llm_row(
                lifecycle=_lifecycle(status="valid", origin="current_snapshot"),
                proposal=_proposal(verdict="reject", reason_codes=["no_edge"]),
                verification=_verification(accepted=True, delta=0.0,
                                           final_ok=False),
            ),
            # 6. current/valid + approve rejected by a specific reason
            _llm_row(
                lifecycle=_lifecycle(status="valid", origin="current_snapshot"),
                proposal=_proposal(verdict="approve_as_is"),
                verification=_verification(ok=False, accepted=False,
                                           rejection_reasons=[
                                               "minimum_stop_distance"],
                                           final_ok=False),
            ),
            # 7. legacy row: MUST contribute 0 to every bucket
            _llm_row(
                scope="legacy",
                lifecycle=_lifecycle(status="valid", origin="carried_forward",
                                     age_bars=2, ttl_bars=3),
                proposal=_proposal(status="ok", verdict="adjust"),
                verification=_verification(accepted=True, delta=-0.01,
                                           final_ok=True),
                paper_order_id=99,
            ),
        ]
        stats = self._funnel(rows)
        assert stats["confirmation"] == {
            "current": 2, "carried": 1, "expired": 1,
            "invalidated": 1, "absent": 1,
        }, f"RED: got {stats['confirmation']!r}"
        assert stats["proposal_verdicts"] == {
            "approve_as_is": 2, "adjust": 1, "wait": 1,
            "reject": 1, "failed": 1,
        }, f"RED: got {stats['proposal_verdicts']!r}"
        assert stats["verifier_accepted"] == 4, (
            f"RED: expected 4 accepted verifications; got "
            f"{stats['verifier_accepted']!r}"
        )
        assert stats["verifier_rejected_by_reason"] == {
            "hard_gate": 1, "minimum_stop_distance": 1,
        }, f"RED: got {stats['verifier_rejected_by_reason']!r}"
        assert stats["final_risk_pass"] == 3, (
            f"RED: expected 3 final-risk passes; got {stats['final_risk_pass']!r}"
        )
        assert stats["orders_created"] == 1, (
            f"RED: expected 1 order from the current row; got "
            f"{stats['orders_created']!r}"
        )

    def test_legacy_rows_never_contribute(self) -> None:
        stats = self._funnel([
            _llm_row(scope="legacy", lifecycle=_lifecycle(),
                     proposal=_proposal(), verification=_verification(),
                     paper_order_id=1),
        ])
        assert stats["confirmation"] == {
            "current": 0, "carried": 0, "expired": 0,
            "invalidated": 0, "absent": 0,
        }
        assert stats["proposal_verdicts"] == {
            "approve_as_is": 0, "adjust": 0, "wait": 0,
            "reject": 0, "failed": 0,
        }
        assert stats["verifier_accepted"] == 0
        assert stats["verifier_rejected_by_reason"] == {}
        assert stats["final_risk_pass"] == 0
        assert stats["orders_created"] == 0

    def test_empty_rows_zero_counters(self) -> None:
        stats = self._funnel([])
        assert stats["confirmation"]["current"] == 0
        assert stats["proposal_verdicts"]["approve_as_is"] == 0
        assert stats["verifier_accepted"] == 0
        assert stats["final_risk_pass"] == 0
        assert stats["orders_created"] == 0

    def test_end_to_end_decision_row_to_funnel(self) -> None:
        """Wiring proof: ``_decision_row`` output feeds the aggregate directly."""
        rows = [
            hourly_report._decision_row(
                _row(created_at="2026-08-10T13:00:00Z",
                     lifecycle=_lifecycle(status="valid", origin="carried_forward",
                                          age_bars=2, ttl_bars=3),
                     proposal=_proposal(verdict="adjust",
                                        adjustments={"stop_loss": 45.90}),
                     verification=_verification(accepted=True, delta=-0.01,
                                                final_ok=True)),
                llm_risk_cutoff_utc="2026-08-10T12:00:00Z"),  # RED: TypeError
            hourly_report._decision_row(
                _row(created_at="2026-08-10T11:00:00Z",
                     lifecycle=_lifecycle(status="valid", origin="current_snapshot"),
                     proposal=_proposal(verdict="approve_as_is"),
                     verification=_verification(accepted=True, delta=0.0,
                                                final_ok=True)),
                llm_risk_cutoff_utc="2026-08-10T12:00:00Z"),
        ]
        stats = self._funnel(rows)
        assert stats["confirmation"] == {
            "current": 1, "carried": 1, "expired": 0,
            "invalidated": 0, "absent": 0,
        }
        assert stats["proposal_verdicts"]["adjust"] == 1
        assert stats["proposal_verdicts"]["approve_as_is"] == 1


# ── order notification builder (new module) ──────────────────────────────────


class TestOrderNotificationBuilder:
    """``build_order_notification`` renders the paper order with the full
    risk-governance context and FAILS CLOSED without verifier success.
    RED: the module does not exist -> ModuleNotFoundError."""

    def _inputs(self, *, verification=None) -> dict:
        return {
            "order": {
                "id": 1234,
                "symbol": "LTCUSDT",
                "side": "SHORT",
                "order_type": "limit",
                "entry_price": 45.34,
                "stop_loss": 45.90,
                "take_profit_json": json.dumps([
                    {"price": 44.40, "ratio": 0.5},
                    {"price": 43.90, "ratio": 0.5},
                ]),
                "created_at": "2026-08-10T13:00:00Z",
            },
            "candidate_plan": {
                "side": "SHORT", "entry_price": 45.34, "stop_loss": 45.90,
                "take_profits": [{"price": 44.40, "ratio": 0.5},
                                 {"price": 43.90, "ratio": 0.5}],
                "risk_percent": 0.5,
            },
            "adjusted_plan": {
                "side": "SHORT", "entry_price": 45.34, "stop_loss": 45.90,
                "take_profits": [{"price": 44.40, "ratio": 0.5},
                                 {"price": 43.90, "ratio": 0.5}],
                "risk_percent": 0.5,
            },
            "verification": verification or _verification(
                ok=True, accepted=True, delta=0.0, final_ok=True),
            "lifecycle": _lifecycle(status="valid", origin="current_snapshot",
                                    age_bars=0, ttl_bars=3),
            "account": {"equity": 10_000.0},
        }

    def test_renders_original_and_adjusted_geometry(self) -> None:
        from plugins.crypto_guard.notify.order_notification import (
            build_order_notification,
        )  # RED: ModuleNotFoundError

        text = build_order_notification(**self._inputs())
        assert "订单" in text, "RED: the notification must be order-shaped"
        assert "45.34" in text and "45.90" in text, (
            "RED: entry+stop must render (original and adjusted)"
        )
        assert "有效风险" in text and "0.50%" in text, (
            "RED: the effective risk percent must render"
        )
        assert "数量" in text, "RED: the computed quantity must render"
        assert "44.40" in text and "43.90" in text, (
            "RED: the TP list prices must render"
        )
        assert "price_action" in text and "5m" in text, (
            "RED: confirmation source+timeframe must render"
        )
        assert "最终风控" in text and "通过" in text, (
            "RED: the final risk check result must render"
        )

    def test_wider_stop_adjustment_renders_reduced_risk(self) -> None:
        from plugins.crypto_guard.notify.order_notification import (
            build_order_notification,
        )
        inputs = self._inputs()
        inputs["candidate_plan"]["stop_loss"] = 45.70
        inputs["adjusted_plan"]["stop_loss"] = 45.90
        inputs["adjusted_plan"]["risk_percent"] = 0.5 * 0.36 / 0.56
        inputs["verification"] = _verification(accepted=True, delta=-0.05,
                                               final_ok=True)
        text = build_order_notification(**inputs)
        assert "原始" in text and "调整" in text, (
            "RED: original vs adjusted must be visibly separated"
        )
        assert "45.70" in text and "45.90" in text, (
            "RED: both original stop 45.70 and adjusted stop 45.90 must render"
        )

    def test_fails_closed_without_verifier_success(self) -> None:
        from plugins.crypto_guard.notify.order_notification import (
            build_order_notification,
        )
        inputs = self._inputs(
            verification=_verification(ok=False, accepted=False,
                                       rejection_reasons=[
                                           "minimum_stop_distance"],
                                       final_ok=False),
        )
        with pytest.raises(ValueError):
            build_order_notification(**inputs)


# ── render the LLM risk-committee funnel section ──────────────────────────────


class TestRenderLLMRiskFunnelSection:
    """``render_ga_hourly_summary`` accepts ``llm_risk_funnel_stats`` and
    renders the risk-committee section. RED: unexpected kwarg -> TypeError."""

    def _render(self, *, stats: dict | None = None) -> str:
        return hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-08-10T12:00:00Z",
            active_symbols=["BTCUSDT"],
            ga_decisions=[],
            open_orders=[],
            active_watches=[],
            failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0,
                          "running": 0},
            llm_risk_funnel_stats=stats,  # RED: TypeError
        )

    def test_render_section_with_stats(self) -> None:
        text = self._render(stats={
            "confirmation": {"current": 1, "carried": 1, "expired": 0,
                             "invalidated": 1, "absent": 0},
            "proposal_verdicts": {"approve_as_is": 1, "adjust": 1, "wait": 0,
                                  "reject": 0, "failed": 0},
            "verifier_accepted": 2,
            "verifier_rejected_by_reason": {},
            "final_risk_pass": 2,
            "orders_created": 1,
        })
        assert "LLM 风控委员会" in text, (
            "RED: the risk-committee section header must render"
        )
        assert "已延续 1" in text, "RED: carried confirmation count must render"
        assert "已失效 1" in text, "RED: invalidated confirmation count must render"
        assert "调整 1" in text, "RED: adjust verdict count must render"
        assert "最终风控通过 2" in text, (
            "RED: final-risk-pass count must render"
        )
        assert "生成订单 1" in text, "RED: orders-created count must render"

    def test_no_section_when_stats_absent(self) -> None:
        """Negative control: omit the kwarg entirely (valid in BOTH phases) so
        the absence of a section is the contract, not a kwarg error."""
        text = hourly_report.render_ga_hourly_summary(
            generated_at_utc="2026-08-10T12:00:00Z",
            active_symbols=["BTCUSDT"],
            ga_decisions=[],
            open_orders=[],
            active_watches=[],
            failed_jobs=[],
            queue_counts={"pending_user": 0, "pending_background": 0,
                          "running": 0},
        )
        assert "LLM 风控委员会" not in text, (
            "GREEN: no risk-committee section when stats are absent"
        )


# ── 08-10 report-contract marker timestamp ────────────────────────────────────


class TestPgLLMRiskReportContractMarkerTs:
    """``hourly_report._get_llm_risk_report_contract_marker_ts(repo)`` returns
    the MAX ``applied_at`` across the four 08-10 markers, or None when ANY is
    missing (fail closed). RED: the helper does not exist -> AttributeError."""

    def test_marker_ts_max_and_missing_fail_closed(self) -> None:
        handle = make_repo()
        try:
            repo = handle.repo
            # Clear any pre-seeded 08-10 markers so the missing baseline is
            # deterministic across phases.
            for key in MARKERS.values():
                repo.conn.execute(
                    "DELETE FROM _migration_state WHERE key = %s", (key,))
            repo.conn.commit()
            ts = hourly_report._get_llm_risk_report_contract_marker_ts(repo)
            assert ts is None, (
                "GREEN: any missing marker must return None (fail closed); "
                f"got {ts!r}"
            )

            # Seed the four markers with staggered applied_at.
            base = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
            for i, key in enumerate(MARKERS.values()):
                repo.conn.execute(
                    "INSERT INTO _migration_state(key, applied_at) "
                    "VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (key, base + timedelta(minutes=i)),
                )
            repo.conn.commit()
            ts = hourly_report._get_llm_risk_report_contract_marker_ts(repo)
            assert ts is not None, "GREEN: all four markers present -> a ts"
            parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            max_dt = base + timedelta(minutes=3)
            assert abs((parsed - max_dt).total_seconds()) < 1.0, (
                "GREEN: the ts must be the MAX applied_at (index 3), not an "
                f"earlier one; got {ts!r}"
            )

            # Deleting ANY marker fails closed to None again.
            repo.conn.execute(
                "DELETE FROM _migration_state WHERE key = %s",
                (MARKERS["context"],),
            )
            repo.conn.commit()
            ts2 = hourly_report._get_llm_risk_report_contract_marker_ts(repo)
            assert ts2 is None, (
                "GREEN: one missing marker must return None (fail closed); "
                f"got {ts2!r}"
            )
        finally:
            handle.close()


# ── opportunity row confirmation line ────────────────────────────────────────


class TestOpportunityRowConfirmationLine:
    """``_format_opportunity_row`` appends a confirmation line (source /
    timeframe / age_bars / expiry reason), with NO raw JSON dump; absent
    confirmation renders no line. RED: the line never renders."""

    def _text(self, *, lifecycle: dict | None = None,
              status: str = "valid") -> str:
        row = _row(lifecycle=lifecycle)
        out = hourly_report._decision_row(row)
        if lifecycle is None:
            out["entry_confirmation_lifecycle"] = lifecycle
        return hourly_report._format_opportunity_row(
            out, {}, tier_label="可执行", market_data_degraded=False)

    def test_carried_confirmation_line(self) -> None:
        text = self._text(lifecycle=_lifecycle(
            status="valid", origin="carried_forward", source="price_action",
            timeframe="5m", age_bars=2, ttl_bars=3))
        assert "入场确认" in text, (
            "RED: the opportunity row must append a confirmation line"
        )
        assert "price_action" in text, "RED: confirmation source must render"
        assert "5m" in text, "RED: confirmation timeframe must render"
        assert "已延续 2" in text, "RED: carried age must render"
        assert "剩余 1" in text, "RED: remaining TTL must render"

    def test_invalidated_confirmation_shows_reason(self) -> None:
        text = self._text(lifecycle=_lifecycle(
            status="invalidated", origin="carried_forward",
            invalidation_reason="opposite_structure", source="price_action",
            timeframe="5m", age_bars=1, ttl_bars=3))
        assert "已失效" in text, "RED: the invalidated state must render"
        assert "opposite_structure" in text, (
            "RED: the structured invalidation reason must render"
        )

    def test_expired_confirmation_shows_expired(self) -> None:
        text = self._text(lifecycle=_lifecycle(
            status="expired", origin="carried_forward", source="price_action",
            timeframe="5m", age_bars=4, ttl_bars=3))
        assert "已过期" in text, "RED: the expired state must render"

    def test_absent_confirmation_renders_no_line(self) -> None:
        text = self._text(lifecycle=None)
        assert "入场确认" not in text, (
            "GREEN: absent confirmation renders no confirmation line"
        )
