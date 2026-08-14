# -*- coding: utf-8 -*-
"""08-10 Step 1 RED contract: LLM risk-governance diagnostics (P2).

Contract under test (design.md §12, prd.md P2-2):

  - New module ``diagnostics/llm_risk_governance.py`` exposes
    ``diagnose_llm_risk_governance(repo, *, batch_id=None) -> dict`` whose
    ``issues`` entries mirror the ``report_diagnostics._issue`` shape
    ``{type, severity, layer, details, suggested_action}``.
  - The four 08-10 contract markers gate every check (fail closed):
      * a MISSING marker raises ``{key}_contract_marker_missing`` (error,
        details.issue="marker_absent");
      * an unparseable ``applied_at`` is detected by the parser helper
        ``_parse_marker_applied_at`` (fail closed, mirrors the 08-08
        ``marker_corrupt`` defensive branch);
      * every detection check runs on SQL lower bound
        ``created_at >= marker.applied_at`` and SELF-SKIPS when the marker is
        missing (no partial window, no fail-open).
  - Detection checks (all error severity unless noted):
      * ``carried_confirmation_without_provenance`` -- lifecycle origin
        ``carried_forward`` but no ``source_decision_id``;
      * ``confirmation_survived_expiry`` -- lifecycle status ``valid`` with
        ``age_bars > ttl_bars``;
      * ``llm_proposal_immutable_change`` -- proposal ``adjustments`` carry an
        immutable key (symbol/side/candidate_fingerprint/
        entry_trigger_confirmation/order_id/database_action/
        notification_action/ttl_bars/quantity/leverage/risk_check);
      * ``llm_proposal_unknown_evidence`` -- ``evidence_refs`` not contained in
        the decision's stable ``evidence_ids`` set;
      * ``accepted_adjustment_increases_monetary_risk`` -- verification
        ``accepted`` with ``monetary_risk_delta > 0``;
      * ``order_without_final_verifier_success`` -- a paper order whose linked
        decision carries a risk-advisory envelope in shadow/paper_bounded but
        NOT (verification_ok AND final_risk_check_ok); the legacy ``off`` path
        is out of scope; 08-12 P2-2 also fires when the order-side
        ``paper_orders.risk_advisory_mode`` claims governance ran but the
        decision lost the audit row (persist-loss false negative);
      * ``llm_risk_review_starvation`` (warning) -- >= 3 system ``failed``
        proposals in the window (legitimate ``reject``/``wait`` do not count).

RED-first: ``diagnostics/llm_risk_governance.py`` does not exist; every import
raises ModuleNotFoundError.

No production DB mutation, no marker write (``make_repo`` initializes only the
scratch schema), no service restart, no commit/push/release.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.tests.pg_fixtures import make_repo

_T0 = 1_700_000_100_000

MARKERS = {
    "lifecycle": "entry_confirmation_lifecycle_contract_v1",
    "proposal": "llm_risk_proposal_contract_v1",
    "verifier": "risk_adjustment_verifier_contract_v1",
    "context": "llm_risk_context_isolation_contract_v1",
}

IMMUTABLE_KEYS = {
    "symbol", "side", "candidate_fingerprint", "entry_trigger_confirmation",
    "order_id", "database_action", "notification_action", "ttl_bars",
    "quantity", "leverage", "risk_check",
}


# ── seeding helpers (mirror the 08-08 marker-test pattern) ───────────────────


def _marker_ts(conn, key: str) -> datetime | None:
    row = conn.execute(
        "SELECT applied_at FROM _migration_state WHERE key = %s", (key,),
    ).fetchone()
    if row is None or row["applied_at"] is None:
        return None
    return row["applied_at"]


def _seed_marker(conn, key: str, applied_at) -> None:
    conn.execute(
        "INSERT INTO _migration_state(key, applied_at) VALUES (%s, %s) "
        "ON CONFLICT (key) DO NOTHING",
        (key, applied_at),
    )
    conn.commit()


def _seed_all_markers(conn, *, at: datetime) -> None:
    for key in MARKERS.values():
        _seed_marker(conn, key, at)


def _delete_all_markers(conn) -> None:
    for key in MARKERS.values():
        conn.execute(
            "DELETE FROM _migration_state WHERE key = %s", (key,))
    conn.commit()


def _after_marker(handle, days: int = 0) -> datetime:
    """A timestamp strictly inside the marker window: ANY present marker +
    N days (tests seed only the marker the check is gated on)."""
    ts = None
    for key in MARKERS.values():
        candidate = _marker_ts(handle.conn, key)
        if candidate is not None:
            ts = candidate
            break
    assert ts is not None, "GREEN: at least one 08-10 marker must be seeded"
    return ts + timedelta(days=days + 1)


def _lifecycle(*, status: str = "valid", origin: str = "current_snapshot",
               age_bars: int = 0, ttl_bars: int = 3,
               source_decision_id: int | None = 1,
               invalidation_reason: str | None = None) -> dict:
    return {
        "status": status, "origin": origin, "timeframe": "5m",
        "source": "price_action", "event_type": "BOS",
        "age_bars": age_bars, "ttl_bars": ttl_bars,
        "source_decision_id": source_decision_id,
        "source_snapshot_id": 1, "invalidation_reason": invalidation_reason,
    }


def _proposal(*, status: str = "ok", verdict: str = "approve_as_is",
              reason_codes: list[str] | None = None,
              evidence_refs: list[str] | None = None,
              adjustments: dict | None = None) -> dict:
    return {
        "proposal_status": status, "verdict": verdict,
        "reason_codes": reason_codes or [], "evidence_refs": evidence_refs or [],
        "counter_evidence_refs": [], "adjustments": adjustments,
    }


def _verification(*, ok: bool = True, accepted: bool = True,
                  delta: float = 0.0, final_ok: bool = True) -> dict:
    return {
        "verification_ok": ok, "accepted": accepted,
        "rejection_reasons": [], "monetary_risk_delta": delta,
        "final_risk_check_ok": final_ok, "effective_order_allowed": True,
    }


def _insert_decision(handle, *, created_at: datetime, symbol: str = "LTCUSDT",
                     lifecycle=None, proposal=None, verification=None,
                     risk_advisory: dict | None = None,
                     evidence_ids: list[str] | None = None) -> int:
    """Persist a decision with the optional risk-governance envelopes; the
    ``created_at`` is positioned relative to the marker window via UPDATE."""
    decision: dict = {
        "symbol": symbol,
        "analysis_time": _T0,
        "analysis_time_utc": "2033-05-18T08:33:20Z",
        "decision_type": "opportunity_watch_recheck",
        "signal_grade": "A",
        "confidence": 0.8,
        "decision": "enter_short",
        "market_bias": "bearish",
        "trend_stage": "middle",
        "risk_check": {"ok": True},
        "evidence": [],
        "counter_evidence": [],
        "skill_result_refs": {},
        "feishu_actions": [],
        "final_summary": "test",
        "summary": "test",
        "effective_signal_grade": "A",
        "has_trade_plan": True,
        "trade_plan": {"side": "SHORT", "entry_price": 45.34,
                       "stop_loss": 45.90, "risk_percent": 0.5},
        "plan_execution_state": "confirmed",
        "plan_status": "executable",
        "entry_trigger_confirmation": {
            "type": "closed_candle_confirmation", "timeframe": "5m",
            "event_type": "BOS", "direction": "bearish",
            "candle_close_time": _T0, "price": 45.34,
            "source": "price_action", "symbol": symbol,
        },
    }
    if evidence_ids is not None:
        decision["evidence_ids"] = evidence_ids
    if lifecycle is not None:
        decision["entry_confirmation_lifecycle"] = lifecycle
    if proposal is not None:
        decision["llm_risk_proposal"] = proposal
    if verification is not None:
        decision["risk_adjustment_verification"] = verification
    if risk_advisory is not None:
        decision["risk_advisory"] = risk_advisory

    ga_id = CryptoGuardRepository(handle.conn).create_ga_decision(decision)
    with handle.conn.transaction():
        handle.conn.execute(
            "UPDATE ga_decisions SET created_at = %s::timestamptz "
            "WHERE id = %s",
            (created_at.isoformat(), ga_id),
        )
    return ga_id


def _insert_paper_order(handle, ga_decision_id: int,
                        risk_advisory_mode: str | None = None) -> int:
    if risk_advisory_mode is None:
        with handle.conn.transaction():
            cur = handle.conn.execute(
                "INSERT INTO paper_orders"
                "  (ga_decision_id, symbol, side, order_type, status, entry_price,"
                "   stop_loss, risk_percent)"
                " VALUES (%s, %s, 'SHORT', 'limit', 'open', 45.34, 45.90, 0.5)"
                " RETURNING id",
                (ga_decision_id, "LTCUSDT"),
            )
            return cur.fetchone()["id"]
    with handle.conn.transaction():
        cur = handle.conn.execute(
            "INSERT INTO paper_orders"
            "  (ga_decision_id, symbol, side, order_type, status, entry_price,"
            "   stop_loss, risk_percent, risk_advisory_mode)"
            " VALUES (%s, %s, 'SHORT', 'limit', 'open', 45.34, 45.90, 0.5, %s)"
            " RETURNING id",
            (ga_decision_id, "LTCUSDT", risk_advisory_mode),
        )
        return cur.fetchone()["id"]


def _diagnose(handle) -> dict:
    from plugins.crypto_guard.diagnostics.llm_risk_governance import (
        diagnose_llm_risk_governance,
    )  # RED: ModuleNotFoundError
    return diagnose_llm_risk_governance(handle.repo)


def _issue_types(result: dict) -> set[str]:
    return {i["type"] for i in result["issues"]}


def _issue_count(result: dict, kind: str) -> int:
    return sum(1 for i in result["issues"] if i["type"] == kind)


# ── marker gating: missing / corrupt / window ─────────────────────────────────


class TestMarkerMissingFailClosed:
    """Every 08-10 marker missing -> ``{key}_contract_marker_missing`` error;
    the detection checks self-skip (no fail-open)."""

    def test_each_marker_missing_raises(self) -> None:
        handle = make_repo()
        try:
            _delete_all_markers(handle.conn)
            result = _diagnose(handle)
            expected = {f"{name}_contract_marker_missing"
                        for name in MARKERS}
            assert expected == _issue_types(result), (
                f"GREEN: expected exactly {sorted(expected)}; got "
                f"{sorted(_issue_types(result))!r}"
            )
            for issue in result["issues"]:
                assert issue["severity"] == "error", issue
                assert issue["details"]["issue"] == "marker_absent", issue
                assert "suggested_action" in issue, issue
        finally:
            handle.close()

    def test_detection_checks_self_skip_when_marker_missing(self) -> None:
        handle = make_repo()
        try:
            _delete_all_markers(handle.conn)
            # Even a violating carried-forward confirmation in the window must
            # NOT fire a detection issue while the marker is missing.
            _insert_decision(handle, created_at=datetime.now(timezone.utc),
                             lifecycle=_lifecycle(status="valid",
                                                  origin="carried_forward",
                                                  source_decision_id=None))
            result = _diagnose(handle)
            types = _issue_types(result)
            assert "carried_confirmation_without_provenance" not in types, (
                "GREEN: detection checks must self-skip on a missing marker"
            )
            assert "order_without_final_verifier_success" not in types
        finally:
            handle.close()

    def test_corrupt_applied_at_parse_fails_closed(self) -> None:
        """``applied_at`` that cannot be parsed -> the parser returns None
        (fail closed, mirrors the 08-08 marker_corrupt defensive branch)."""
        from plugins.crypto_guard.diagnostics.llm_risk_governance import (
            _parse_marker_applied_at,
        )  # RED: ModuleNotFoundError
        assert _parse_marker_applied_at("not-a-timestamp") is None, (
            "GREEN: an unparseable applied_at must fail closed to None"
        )
        assert _parse_marker_applied_at("2026-08-10T12:00:00+00:00") is not None

    def test_window_excludes_legacy_rows(self) -> None:
        """A violating row BEFORE the marker must NOT fire (SQL lower bound
        ``created_at >= marker.applied_at``, exclude-only)."""
        handle = make_repo()
        try:
            _seed_all_markers(handle.conn,
                              at=datetime(2026, 8, 1, tzinfo=timezone.utc))
            # legacy rows BEFORE the marker, carrying every violation
            _insert_decision(
                handle, created_at=_after_marker(handle, days=-2),
                lifecycle=_lifecycle(status="valid", origin="carried_forward",
                                     source_decision_id=None),
                proposal=_proposal(verdict="adjust",
                                   evidence_refs=["ev_nonexistent"],
                                   adjustments={"symbol": "BTCUSDT"}),
                verification=_verification(accepted=True, delta=1.0),
                risk_advisory={"mode": "paper_bounded", "proposal_status": "ok",
                               "verification_ok": True,
                               "final_risk_check_ok": True})
            bad_ga = _insert_decision(
                handle, created_at=_after_marker(handle, days=-2),
                risk_advisory={"mode": "paper_bounded", "proposal_status": "ok",
                               "verification_ok": False,
                               "final_risk_check_ok": False})
            assert _insert_paper_order(handle, bad_ga) is not None
            result = _diagnose(handle)
            types = _issue_types(result)
            for kind in ("carried_confirmation_without_provenance",
                         "llm_proposal_immutable_change",
                         "llm_proposal_unknown_evidence",
                         "accepted_adjustment_increases_monetary_risk",
                         "order_without_final_verifier_success"):
                assert kind not in types, (
                    f"GREEN: legacy rows before the marker must be excluded "
                    f"from {kind}"
                )
        finally:
            handle.close()


# ── detection checks ─────────────────────────────────────────────────────────


class TestCarriedConfirmationProvenance:
    def _seed(self, handle, *, source_decision_id=None) -> int:
        _delete_all_markers(handle.conn)
        _seed_marker(handle.conn, MARKERS["lifecycle"],
                     datetime(2026, 8, 1, tzinfo=timezone.utc))
        return _insert_decision(
            handle, created_at=_after_marker(handle),
            lifecycle=_lifecycle(status="valid", origin="carried_forward",
                                 age_bars=2, ttl_bars=3,
                                 source_decision_id=source_decision_id))

    def test_carried_forward_without_provenance_fires(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, source_decision_id=None)
            result = _diagnose(handle)
            assert _issue_count(result,
                                "carried_confirmation_without_provenance") == 1
            issue = next(i for i in result["issues"]
                         if i["type"] == "carried_confirmation_without_provenance")
            assert issue["severity"] == "error", issue
            assert "source_decision_id" in json.dumps(issue["details"],
                                                      ensure_ascii=False)
        finally:
            handle.close()

    def test_carried_forward_with_provenance_clean(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, source_decision_id=77)
            result = _diagnose(handle)
            assert _issue_count(result,
                                "carried_confirmation_without_provenance") == 0
        finally:
            handle.close()


class TestConfirmationSurvivedExpiry:
    def _seed(self, handle, *, age_bars: int, ttl_bars: int) -> int:
        _delete_all_markers(handle.conn)
        _seed_marker(handle.conn, MARKERS["lifecycle"],
                     datetime(2026, 8, 1, tzinfo=timezone.utc))
        return _insert_decision(
            handle, created_at=_after_marker(handle),
            lifecycle=_lifecycle(status="valid", origin="current_snapshot",
                                 age_bars=age_bars, ttl_bars=ttl_bars))

    def test_age_over_ttl_fires(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, age_bars=4, ttl_bars=3)
            result = _diagnose(handle)
            assert _issue_count(result, "confirmation_survived_expiry") == 1
            issue = next(i for i in result["issues"]
                         if i["type"] == "confirmation_survived_expiry")
            assert issue["details"]["age_bars"] == 4
            assert issue["details"]["ttl_bars"] == 3
        finally:
            handle.close()

    def test_age_within_ttl_clean(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, age_bars=2, ttl_bars=3)
            result = _diagnose(handle)
            assert _issue_count(result, "confirmation_survived_expiry") == 0
        finally:
            handle.close()


class TestProposalImmutableChange:
    def _seed(self, handle, *, adjustments: dict) -> int:
        _delete_all_markers(handle.conn)
        _seed_marker(handle.conn, MARKERS["proposal"],
                     datetime(2026, 8, 1, tzinfo=timezone.utc))
        return _insert_decision(
            handle, created_at=_after_marker(handle),
            proposal=_proposal(verdict="adjust", adjustments=adjustments))

    def test_immutable_key_in_adjustments_fires(self) -> None:
        handle = make_repo()
        try:
            for key in sorted(IMMUTABLE_KEYS):
                self._seed(handle, adjustments={key: "x"})
            result = _diagnose(handle)
            fired = {
                i["details"]["immutable_keys"][0]
                for i in result["issues"]
                if i["type"] == "llm_proposal_immutable_change"
            }
            assert fired == IMMUTABLE_KEYS, (
                f"GREEN: every immutable key must fire; missing "
                f"{sorted(IMMUTABLE_KEYS - fired)!r}"
            )
        finally:
            handle.close()

    def test_mutable_adjustment_clean(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, adjustments={"stop_loss": 45.90,
                                            "risk_percent": 0.4})
            result = _diagnose(handle)
            assert _issue_count(result, "llm_proposal_immutable_change") == 0
        finally:
            handle.close()


class TestProposalUnknownEvidence:
    def _seed(self, handle, *, evidence_ids, evidence_refs) -> int:
        _delete_all_markers(handle.conn)
        _seed_marker(handle.conn, MARKERS["proposal"],
                     datetime(2026, 8, 1, tzinfo=timezone.utc))
        return _insert_decision(
            handle, created_at=_after_marker(handle),
            proposal=_proposal(verdict="approve_as_is",
                               evidence_refs=evidence_refs),
            evidence_ids=evidence_ids)

    def test_unknown_evidence_ref_fires(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, evidence_ids=["ev_1", "ev_2"],
                       evidence_refs=["ev_1", "ev_999"])
            result = _diagnose(handle)
            assert _issue_count(result, "llm_proposal_unknown_evidence") == 1
            details = next(
                i["details"] for i in result["issues"]
                if i["type"] == "llm_proposal_unknown_evidence")
            assert details["unknown_refs"] == ["ev_999"], details
        finally:
            handle.close()

    def test_all_refs_in_evidence_set_clean(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, evidence_ids=["ev_1", "ev_2"],
                       evidence_refs=["ev_1", "ev_2"])
            result = _diagnose(handle)
            assert _issue_count(result, "llm_proposal_unknown_evidence") == 0
        finally:
            handle.close()

    def test_no_evidence_ids_fails_closed(self) -> None:
        """A proposal with refs against a decision with NO stable evidence set
        is treated as unknown (fail closed)."""
        handle = make_repo()
        try:
            self._seed(handle, evidence_ids=[], evidence_refs=["ev_1"])
            result = _diagnose(handle)
            assert _issue_count(result, "llm_proposal_unknown_evidence") == 1
        finally:
            handle.close()


class TestAcceptedAdjustmentMonetaryRisk:
    def _seed(self, handle, *, accepted: bool, delta: float) -> int:
        _delete_all_markers(handle.conn)
        _seed_marker(handle.conn, MARKERS["verifier"],
                     datetime(2026, 8, 1, tzinfo=timezone.utc))
        return _insert_decision(
            handle, created_at=_after_marker(handle),
            proposal=_proposal(verdict="adjust", adjustments={"stop_loss": 46.5}),
            verification=_verification(accepted=accepted, delta=delta))

    def test_accepted_with_positive_delta_fires(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, accepted=True, delta=0.5)
            result = _diagnose(handle)
            assert _issue_count(
                result, "accepted_adjustment_increases_monetary_risk") == 1
            details = next(
                i["details"] for i in result["issues"]
                if i["type"] == "accepted_adjustment_increases_monetary_risk")
            assert details["monetary_risk_delta"] == 0.5, details
        finally:
            handle.close()

    def test_accepted_with_negative_or_zero_delta_clean(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, accepted=True, delta=-0.1)
            self._seed(handle, accepted=True, delta=0.0)
            result = _diagnose(handle)
            assert _issue_count(
                result, "accepted_adjustment_increases_monetary_risk") == 0
        finally:
            handle.close()

    def test_rejected_with_positive_delta_clean(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, accepted=False, delta=0.5)
            result = _diagnose(handle)
            assert _issue_count(
                result, "accepted_adjustment_increases_monetary_risk") == 0
        finally:
            handle.close()


class TestOrderWithoutFinalVerifierSuccess:
    def _seed(self, handle, *, verification_ok: bool, final_ok: bool,
              mode: str = "paper_bounded") -> tuple[int, int]:
        _delete_all_markers(handle.conn)
        _seed_marker(handle.conn, MARKERS["verifier"],
                     datetime(2026, 8, 1, tzinfo=timezone.utc))
        ga_id = _insert_decision(
            handle, created_at=_after_marker(handle),
            risk_advisory={"mode": mode, "proposal_status": "ok",
                           "verification_ok": verification_ok,
                           "final_risk_check_ok": final_ok})
        order_id = _insert_paper_order(handle, ga_id)
        return ga_id, order_id

    def test_order_without_verifier_success_fires(self) -> None:
        handle = make_repo()
        try:
            expected_orders: set[int] = set()
            for verification_ok, final_ok in ((False, False), (True, False),
                                              (False, True)):
                _, order_id = self._seed(handle,
                                         verification_ok=verification_ok,
                                         final_ok=final_ok)
                expected_orders.add(order_id)
            result = _diagnose(handle)
            assert _issue_count(
                result, "order_without_final_verifier_success") == 3, (
                f"RED: every partial-verification order must fire; got "
                f"{_issue_count(result, 'order_without_final_verifier_success')}"
            )
            fired_orders = {
                i["details"]["paper_order_id"]
                for i in result["issues"]
                if i["type"] == "order_without_final_verifier_success"
            }
            assert fired_orders == expected_orders, (
                f"GREEN: the fired orders {sorted(fired_orders)!r} must equal "
                f"the seeded {sorted(expected_orders)!r}"
            )
        finally:
            handle.close()

    def test_order_with_full_verifier_success_clean(self) -> None:
        handle = make_repo()
        try:
            _, order_id = self._seed(handle, verification_ok=True, final_ok=True)
            result = _diagnose(handle)
            assert _issue_count(result,
                                "order_without_final_verifier_success") == 0
        finally:
            handle.close()

    def test_legacy_off_path_out_of_scope(self) -> None:
        """mode=off has no verifier by design; it must NOT fire."""
        handle = make_repo()
        try:
            _, order_id = self._seed(handle, verification_ok=False,
                                     final_ok=False, mode="off")
            result = _diagnose(handle)
            assert _issue_count(result,
                                "order_without_final_verifier_success") == 0
        finally:
            handle.close()

    def test_mode_governance_but_decision_lost_audit_row_fires(self) -> None:
        """08-12 P2-2 persist-loss false negative: the order-side mode says
        governance ran but the decision lost the ``risk_advisory`` audit row
        (always-stamp persist-swallow in ``_attach_risk_governance``) -- must
        fire. The old decision-side-only join ``continue``d and was invisible.
        """
        handle = make_repo()
        try:
            _delete_all_markers(handle.conn)
            _seed_marker(handle.conn, MARKERS["verifier"],
                         datetime(2026, 8, 1, tzinfo=timezone.utc))
            ga_id = _insert_decision(
                handle, created_at=_after_marker(handle),
                # NO risk_advisory envelope -- the audit row was lost.
                risk_advisory=None)
            order_id = _insert_paper_order(handle, ga_id,
                                           risk_advisory_mode="paper_bounded")
            result = _diagnose(handle)
            assert _issue_count(
                result, "order_without_final_verifier_success") == 1, (
                f"RED: a governance-created order with a lost audit row must "
                f"fire; got "
                f"{_issue_count(result, 'order_without_final_verifier_success')}"
            )
            details = next(
                i["details"] for i in result["issues"]
                if i["type"] == "order_without_final_verifier_success")
            assert details["paper_order_id"] == order_id, details
            assert details["mode"] == "paper_bounded", details
            assert details["verification_ok"] is None, details
        finally:
            handle.close()

    def test_mode_null_decision_without_audit_row_clean(self) -> None:
        """A legacy order (``risk_advisory_mode IS NULL``) whose decision never
        carried an envelope must NOT fire -- only orders whose mode CLAIMS
        governance ran are in scope."""
        handle = make_repo()
        try:
            _delete_all_markers(handle.conn)
            _seed_marker(handle.conn, MARKERS["verifier"],
                         datetime(2026, 8, 1, tzinfo=timezone.utc))
            ga_id = _insert_decision(
                handle, created_at=_after_marker(handle),
                risk_advisory=None)
            _insert_paper_order(handle, ga_id)  # mode IS NULL
            result = _diagnose(handle)
            assert _issue_count(
                result, "order_without_final_verifier_success") == 0
        finally:
            handle.close()

    def test_mode_off_without_audit_row_clean(self) -> None:
        """An order whose mode is 'off' (governance explicitly disabled) with a
        decision that lost the audit row is the legacy path -- must NOT fire."""
        handle = make_repo()
        try:
            _delete_all_markers(handle.conn)
            _seed_marker(handle.conn, MARKERS["verifier"],
                         datetime(2026, 8, 1, tzinfo=timezone.utc))
            ga_id = _insert_decision(
                handle, created_at=_after_marker(handle),
                risk_advisory=None)
            _insert_paper_order(handle, ga_id, risk_advisory_mode="off")
            result = _diagnose(handle)
            assert _issue_count(
                result, "order_without_final_verifier_success") == 0
        finally:
            handle.close()


class TestLLMRiskReviewStarvation:
    def _seed(self, handle, *, proposals: list[dict]) -> None:
        _delete_all_markers(handle.conn)
        _seed_marker(handle.conn, MARKERS["proposal"],
                     datetime(2026, 8, 1, tzinfo=timezone.utc))
        for proposal in proposals:
            _insert_decision(handle, created_at=_after_marker(handle),
                             proposal=proposal)

    def test_three_system_failures_fires_warning(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, proposals=[
                _proposal(status="failed"), _proposal(status="failed"),
                _proposal(status="failed"),
            ])
            result = _diagnose(handle)
            assert _issue_count(result, "llm_risk_review_starvation") == 1
            issue = next(i for i in result["issues"]
                         if i["type"] == "llm_risk_review_starvation")
            assert issue["severity"] == "warning", issue
            assert issue["details"]["failed_count"] == 3, issue
        finally:
            handle.close()

    def test_legitimate_rejections_do_not_count(self) -> None:
        """reject/wait verdicts are legitimate outcomes, never starvation."""
        handle = make_repo()
        try:
            self._seed(handle, proposals=[
                _proposal(status="ok", verdict="reject"),
                _proposal(status="ok", verdict="wait"),
                _proposal(status="ok", verdict="reject"),
            ])
            result = _diagnose(handle)
            assert _issue_count(result, "llm_risk_review_starvation") == 0
        finally:
            handle.close()

    def test_two_system_failures_below_threshold(self) -> None:
        handle = make_repo()
        try:
            self._seed(handle, proposals=[
                _proposal(status="failed"), _proposal(status="failed"),
            ])
            result = _diagnose(handle)
            assert _issue_count(result, "llm_risk_review_starvation") == 0
        finally:
            handle.close()


# ── issue envelope shape ─────────────────────────────────────────────────────


class TestIssueShape:
    """Every issue mirrors the ``report_diagnostics`` shape so the existing
    aggregator can consume the new module unchanged."""

    def test_issue_envelope_fields(self) -> None:
        handle = make_repo()
        try:
            _delete_all_markers(handle.conn)
            result = _diagnose(handle)
            assert result["issues"], "GREEN: marker-missing issues must fire"
            for issue in result["issues"]:
                assert set(issue) >= {"type", "severity", "layer",
                                      "details", "suggested_action"}, issue
                assert issue["type"].endswith("_contract_marker_missing")
                assert issue["severity"] in {"error", "warning"}, issue
                assert isinstance(issue["details"], dict), issue
                assert isinstance(issue["suggested_action"], str), issue
        finally:
            handle.close()


# ── P1-3: merged into the production diagnostics aggregation ────────────────


class TestMergedIntoStateConsistency:
    """08-10 Step 9 (P1-3): ``diagnose_state_consistency`` (the production
    entrypoint the hourly report calls) MUST surface the 08-10 governance
    diagnostics — the four fail-closed marker-missing checks and the seven
    detection checks — so a deleted/corrupt marker or a provenance-less
    carried confirmation is visible in the running report, not only in
    unit tests."""

    def test_diagnose_llm_risk_governance_exported(self) -> None:
        from plugins.crypto_guard.diagnostics import (
            diagnose_llm_risk_governance,  # noqa: F401
        )
        assert callable(diagnose_llm_risk_governance)

    def test_proposal_marker_missing_surfaces_via_state_consistency(self) -> None:
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )
        handle = make_repo()
        try:
            _delete_all_markers(handle.conn)
            handle.conn.execute(
                "DELETE FROM _migration_state WHERE key = %s",
                (MARKERS["proposal"],),
            )
            handle.conn.commit()
            result = diagnose_state_consistency(handle.repo)
            types = _issue_types(result)
            assert "proposal_contract_marker_missing" in types, (
                "GREEN: a deleted llm_risk_proposal_contract_v1 marker must "
                "surface via diagnose_state_consistency"
            )
            assert result["summary"]["proposal_contract_marker_missing"] == 1
            assert any(i["details"]["issue"] == "marker_absent"
                       for i in result["issues"]
                       if i["type"] == "proposal_contract_marker_missing")
        finally:
            handle.close()

    def test_carried_forward_without_provenance_via_state_consistency(self) -> None:
        from plugins.crypto_guard.diagnostics.state_consistency import (
            diagnose_state_consistency,
        )
        handle = make_repo()
        try:
            _seed_all_markers(
                handle.conn, at=datetime(2026, 8, 1, tzinfo=timezone.utc))
            _insert_decision(
                handle, created_at=_after_marker(handle),
                lifecycle=_lifecycle(status="valid", origin="carried_forward",
                                     source_decision_id=None))
            result = diagnose_state_consistency(handle.repo)
            types = _issue_types(result)
            assert "carried_confirmation_without_provenance" in types, (
                "GREEN: a carried-forward confirmation without source_decision_id "
                "must surface via the merged diagnose_state_consistency"
            )
            assert result["summary"]["carried_confirmation_without_provenance"] == 1
        finally:
            handle.close()
