# -*- coding: utf-8 -*-
"""08-10 Step 1 RED contract: deterministic risk-adjustment verifier (P0).

Contract under test (design.md §7, prd.md P1-4 + §7 scenarios 8-10):

  - ``verify_risk_adjustment`` is the ONLY way an LLM risk proposal may affect
    a trade plan. It is deterministic and read-only: it returns an
    ``AdjustmentVerification`` and never writes.
  - The verifier applies the proposal through a CLONE ALLOWLIST (only
    entry_price / stop_loss / take_profits / risk_percent /
    news_like_event_policy are adjustable; symbol/side/fingerprint are
    immutable), re-runs the COMPLETE deterministic risk engine on the
    adjusted plan, and independently enforces the hard gates (market data,
    trusted confirmation, account, drawdown, exposure, geometry, extreme
    regime, idempotency) that the LLM can never waive.
  - Scenario 8: a compliant wider stop automatically reduces ``risk_percent``
    so monetary risk NEVER increases (``monetary_risk_delta <= 0``).
  - Scenario 9: an ``approve_as_is`` proposal whose deterministic hard gate /
    risk-engine re-run fails NEVER yields an order (``ok=False``).
  - Scenario 10: in ``shadow`` the verifier may pass yet
    ``effective_order_allowed`` MUST be False (hypothetical outcome only);
    ``paper_bounded`` allows exactly one order when every gate passes.

RED-first: ``risk/risk_adjustment_verifier.py`` does not exist yet; all
imports fail. The candidate plans below are engineered against the REAL
``risk_engine.validate_trade_plan`` thresholds (min_rr=1.5, min_confidence=0.72,
min_sl_distance_pct=0.8, min_tp_distance_pct=1.0, invalid_condition strictly
between entry and stop with >=0.1% stop buffer, ATR buffer 0.2*ATR, SHORT
skips the LONG quality gate).
"""
from __future__ import annotations

import math
import types
from typing import Any

import pytest

from plugins.crypto_guard.tests import pg_fixtures as fx

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

_ANALYSIS = 1_700_000_100_000
_BAR_MS = {"5m": 300_000, "15m": 900_000}


def _bearish_event(*, close_time: int = _ANALYSIS, price: float = 45.34,
                   tf: str = "5m") -> dict:
    return {
        "event": "bearish_bos",
        "timeframe": tf,
        "direction": "bearish",
        "candle_close_time": close_time,
        "price": price,
        "closed": True,
    }


def _snapshot(*, status: str = "complete", regime: str = "normal",
              at: int = _ANALYSIS) -> dict:
    """Engine-valid LTCUSDT SHORT snapshot (bearish structure/momentum,
    complete data, normal regime, ATR=0.5, no trend_stage/RSI/order_flow/
    chanlun interference)."""
    health = {tf: {"ready": True, "last_close_time": at}
              for tf in ("1d", "4h", "1h", "15m", "5m")}
    profiles = {tf: {"market_structure": "bearish", "momentum": "bearish"}
                for tf in ("1d", "4h", "1h", "15m")}
    modules = {
        "price_action": {
            "market_structure": "bearish",
            "structure_events": [_bearish_event(close_time=at)],
            "last_close": 45.34,
        },
        "momentum": {"direction": "bearish", "atr": {"current": 0.5}},
        "market_regime": {"regime": regime, "extreme": regime == "extreme"},
    }
    return {
        "symbol": "LTCUSDT",
        "analysis_time_utc": at,
        "mode": "shadow_test",
        "profiles": profiles,
        "modules": modules,
        "timeframe_modules": {"5m": {"price_action": modules["price_action"],
                                     "smc": {}}},
        "data_quality": {"status": status, "health_by_tf": health},
        "analysis_degraded": status != "complete",
        "partial_tf_mode": False,
    }


def _confirmation(*, close_time: int = _ANALYSIS, price: float = 45.34) -> dict:
    return {
        "type": "closed_candle_confirmation",
        "timeframe": "5m",
        "event_type": "BOS",
        "direction": "bearish",
        "candle_close_time": close_time,
        "price": price,
        "source": "price_action",
        "symbol": "LTCUSDT",
    }


def _candidate_plan(**over: Any) -> dict:
    """A deterministic SHORT plan the real risk engine ACCEPTS (SL 1.235%,
    TP1 2.073%, RR1 1.678, invalid strictly between entry/stop with stop
    buffer 0.218%)."""
    plan = {
        "side": "SHORT",
        "entry_type": "limit",
        "entry_price": 45.34,
        "trigger_price": 45.34,
        "stop_loss": 45.90,
        "take_profits": [{"price": 44.40, "ratio": 0.5},
                         {"price": 43.90, "ratio": 0.5}],
        "risk_percent": 0.5,
        "invalid_condition": "5m 收盘站回 45.80",
        "reason": "结构偏空，等待反抽确认；仅用于模拟盘",
        "entry_trigger_confirmation": _confirmation(),
    }
    plan.update(over)
    return plan


def _account(**over: Any) -> dict:
    account = {
        "enabled": True,
        "paused": False,
        "equity": 10_000.0,
        "available_balance": 10_000.0,
        "leverage": 1.0,
        "open_orders": 0,
        "max_orders": 3,
        "open_position_risk_pct": 0.0,
        "max_single_trade_risk_pct": 2.0,
        "max_total_risk_pct": 10.0,
        "drawdown_pct": 0.0,
    }
    account.update(over)
    return account


def _lifecycle(**over: Any) -> Any:
    lc = types.SimpleNamespace(
        status="valid",
        origin="current_snapshot",
        confirmation=_confirmation(),
        source_decision_id=None,
        source_snapshot_id=None,
        source_analysis_time=_ANALYSIS,
        age_bars=0,
        ttl_bars=3,
        invalidation_reason=None,
        checks={},
    )
    for k, v in over.items():
        setattr(lc, k, v)
    return lc


def _policy(mode: str = "shadow"):
    from plugins.crypto_guard.risk.risk_policy import RiskAssistancePolicy
    return RiskAssistancePolicy(mode=mode)


def _verify(*, candidate_plan: dict | None = None, proposal: dict | None = None,
            lifecycle: Any | None = None, snapshot: dict | None = None,
            account: dict | None = None, mode: str = "shadow",
            confidence: float = 0.8, inject_fingerprint: bool = True):
    from plugins.crypto_guard.risk.risk_adjustment_verifier import (
        candidate_plan_fingerprint,
        verify_risk_adjustment,
    )
    plan = _candidate_plan() if candidate_plan is None else candidate_plan
    snap = _snapshot() if snapshot is None else snapshot
    pol = _policy(mode)
    if proposal is None:
        proposal = {"verdict": "approve_as_is", "reason_codes": [],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "ok"}
    if inject_fingerprint:
        # 08-10 P2-1 (reviewer finding): the verifier now fail-closes when a
        # proposal omits candidate_fingerprint. Default proposals quote the
        # candidate identity verbatim, exactly as a compliant LLM must.
        proposal = dict(proposal)
        proposal.setdefault(
            "candidate_fingerprint",
            candidate_plan_fingerprint(plan, snapshot=snap, policy=pol),
        )
    return verify_risk_adjustment(
        candidate_plan=plan,
        proposal=proposal,
        confirmation_lifecycle=_lifecycle() if lifecycle is None else lifecycle,
        snapshot=snap,
        account_state=_account() if account is None else account,
        policy=pol,
        decision_confidence=confidence,
    )


class TestBasicVerification:
    """approve_as_is over an engine-valid candidate."""

    def test_approve_passes_and_preserves_plan(self):
        result = _verify()
        assert result.ok is True
        assert result.adjusted_plan is not None
        assert result.adjusted_plan["side"] == "SHORT"
        assert math.isclose(result.adjusted_plan["risk_percent"], 0.5)
        assert math.isclose(result.monetary_risk_delta, 0.0, abs_tol=1e-9)
        assert result.final_risk_check.get("ok") is True
        assert result.errors == ()
        assert result.reason_codes == ()

    def test_shadow_never_allows_order(self):
        result = _verify(mode="shadow")
        assert result.ok is True
        assert result.effective_order_allowed is False

    def test_paper_bounded_allows_order(self):
        result = _verify(mode="paper_bounded")
        assert result.ok is True
        assert result.effective_order_allowed is True

    def test_off_never_allows_order(self):
        result = _verify(mode="off")
        assert result.ok is True
        assert result.effective_order_allowed is False


class TestWiderStopScalesRiskPercent:
    """Scenario 8: compliant wider stop reduces risk_percent so monetary
    risk never increases."""

    def test_wider_stop_reduces_risk_percent(self):
        candidate = _candidate_plan(stop_loss=45.70)
        # candidate SL distance 0.36; adjusted SL distance 0.56
        proposal = {"verdict": "adjust", "reason_codes": ["minimum_stop_distance"],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "stop too tight", "adjustments": {"stop_loss": 45.90}}
        result = _verify(candidate_plan=candidate, proposal=proposal)
        assert result.ok is True
        assert result.adjusted_plan is not None
        scaled = 0.5 * 0.36 / 0.56
        assert math.isclose(result.adjusted_plan["risk_percent"], scaled,
                            rel_tol=1e-9)
        assert result.monetary_risk_delta < 0
        assert result.final_risk_check.get("ok") is True

    def test_monetary_risk_never_increases(self):
        account = _account(equity=10_000.0)
        candidate = _candidate_plan(stop_loss=45.70)
        proposal = {"verdict": "adjust", "reason_codes": ["minimum_stop_distance"],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "wider stop", "adjustments": {"stop_loss": 46.50}}
        result = _verify(candidate_plan=candidate, proposal=proposal,
                         account=account)
        original_risk = 0.005 * account["equity"]
        final_risk = result.adjusted_plan["risk_percent"] / 100 * account["equity"]
        assert final_risk <= original_risk
        assert result.monetary_risk_delta <= 0


class TestNewsLikeEventAdaptiveGate:
    """08-10 reviewer Recommended 1-3 closure + implement.md Step 8 note:
    ``news_like_event`` is ADAPTIVE at the verifier gate (an explicit
    ``adjustments.news_like_event_policy.allow == True`` is acknowledged with a
    ``news_like_event`` reason code instead of a verifier-regime error), but the
    FULL existing engine rerun (``validate_trade_plan``) is the LAST WORD and
    hard-blocks any ``EXTREME_REGIMES`` regime, so paper_bounded NEVER orders
    during a news regime (确认 != 下单). The schema now accepts the field so the
    surface is reachable and audited, and fail-closed is proven for
    allow=True / allow=False / absent."""

    def _proposal(self, adjustments: dict[str, Any]) -> dict:
        return {
            "verdict": "adjust",
            "reason_codes": [],
            "evidence_refs": [],
            "counter_evidence_refs": [],
            "summary": "news regime adjustment",
            "adjustments": adjustments,
        }

    def test_allow_true_acknowledged_but_engine_rerun_hard_blocks(self):
        result = _verify(
            snapshot=_snapshot(regime="news_like_event"),
            proposal=self._proposal({"news_like_event_policy": {"allow": True}}),
            mode="paper_bounded",
        )
        # The verifier's adaptive gate honored the proposal: the news_like_event
        # reason code is emitted and no verifier-regime error is appended.
        assert "news_like_event" in result.reason_codes
        # The engine rerun is the last word: it hard-blocks the extreme news
        # regime, so the outcome is always fail-closed, never an order.
        assert result.final_risk_check.get("ok") is False
        assert result.ok is False
        assert result.effective_order_allowed is False

    def test_news_like_event_policy_false_fails_closed(self):
        result = _verify(
            snapshot=_snapshot(regime="news_like_event"),
            proposal=self._proposal({"news_like_event_policy": {"allow": False}}),
            mode="paper_bounded",
        )
        assert result.ok is False
        assert result.effective_order_allowed is False
        assert any("news_like_event" in e for e in result.errors)

    def test_news_like_event_policy_absent_fails_closed(self):
        result = _verify(
            snapshot=_snapshot(regime="news_like_event"),
            proposal=self._proposal({"risk_percent": 0.4}),
            mode="paper_bounded",
        )
        assert result.ok is False
        assert result.effective_order_allowed is False
        assert any("news_like_event" in e for e in result.errors)


class TestLLMApproveHardGateFailCloses:
    """Scenario 9: LLM approves, deterministic gates still decide."""

    def test_paused_account_blocks(self):
        result = _verify(account=_account(paused=True))
        assert result.ok is False
        assert result.effective_order_allowed is False
        assert any("account" in e.lower() or "暂停" in e for e in result.errors)

    def test_disabled_account_blocks(self):
        result = _verify(account=_account(enabled=False))
        assert result.ok is False
        assert result.effective_order_allowed is False

    def test_drawdown_hard_break_blocks(self):
        result = _verify(account=_account(drawdown_pct=-3.5))
        assert result.ok is False

    def test_open_order_cap_blocks(self):
        result = _verify(account=_account(open_orders=3, max_orders=3))
        assert result.ok is False

    def test_degraded_market_data_blocks(self):
        result = _verify(snapshot=_snapshot(status="degraded"))
        assert result.ok is False
        assert result.effective_order_allowed is False

    def test_extreme_regime_blocks(self):
        result = _verify(snapshot=_snapshot(regime="extreme"))
        assert result.ok is False
        assert result.effective_order_allowed is False

    def test_approve_but_candidate_stop_too_tight_blocks(self):
        # LLM approves a plan whose stop is only 0.794% (below 0.8%): the
        # deterministic risk-engine re-run must reject it.
        candidate = _candidate_plan(stop_loss=45.70)
        result = _verify(candidate_plan=candidate)
        assert result.ok is False
        assert result.final_risk_check.get("ok") is False
        assert result.effective_order_allowed is False


class TestAdjustmentConstraints:
    """Proposal adjustments are constrained and validated."""

    def test_entry_deviation_outside_candidate_range_rejected(self):
        proposal = {"verdict": "adjust", "reason_codes": ["entry_deviation"],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "entry tweak",
                    "adjustments": {"entry_price": 46.50}}
        result = _verify(proposal=proposal)
        assert result.ok is False
        assert any("deviation" in e.lower() or "偏差" in e for e in result.errors)

    def test_small_entry_deviation_within_range_accepted(self):
        proposal = {"verdict": "adjust", "reason_codes": ["entry_deviation"],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "minor entry tweak",
                    "adjustments": {"entry_price": 45.30}}
        result = _verify(proposal=proposal)
        assert result.ok is True
        assert math.isclose(result.adjusted_plan["entry_price"], 45.30)

    def test_stop_tightening_rejected(self):
        proposal = {"verdict": "adjust", "reason_codes": ["minimum_stop_distance"],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "tighten", "adjustments": {"stop_loss": 45.60}}
        result = _verify(proposal=proposal)
        assert result.ok is False
        assert result.adjusted_plan is None

    def test_unknown_adjustment_key_rejected(self):
        proposal = {"verdict": "adjust", "reason_codes": [],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "qty", "adjustments": {"quantity": 2}}
        result = _verify(proposal=proposal)
        assert result.ok is False

    def test_recomputed_take_profits_accepted(self):
        proposal = {"verdict": "adjust", "reason_codes": ["take_profit"],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "recompute TPs",
                    "adjustments": {"take_profits": [
                        {"price": 44.20, "ratio": 0.5},
                        {"price": 43.70, "ratio": 0.5}]}}
        result = _verify(proposal=proposal)
        assert result.ok is True
        assert result.adjusted_plan["take_profits"][0]["price"] == 44.20


class TestConfirmationLifecycleGate:
    """The verifier independently re-validates the confirmation lifecycle."""

    def test_expired_confirmation_blocks(self):
        result = _verify(lifecycle=_lifecycle(status="expired", age_bars=4,
                                              ttl_bars=3))
        assert result.ok is False
        assert result.effective_order_allowed is False

    def test_invalidated_confirmation_blocks(self):
        result = _verify(lifecycle=_lifecycle(status="invalidated",
                                              invalidation_reason="opposite_structure"))
        assert result.ok is False

    def test_absent_confirmation_blocks(self):
        result = _verify(lifecycle=_lifecycle(status="absent"))
        assert result.ok is False

    def test_carried_forward_valid_confirmation_allowed(self):
        result = _verify(lifecycle=_lifecycle(status="valid",
                                              origin="carried_forward",
                                              age_bars=2, ttl_bars=3))
        assert result.ok is True


class TestImmutableAndFingerprint:
    """Candidate identity can never be changed by a proposal."""

    def test_side_symbol_preserved(self):
        result = _verify()
        assert result.adjusted_plan["side"] == "SHORT"
        assert result.adjusted_plan["entry_trigger_confirmation"]["symbol"] == "LTCUSDT"

    def test_fingerprint_never_in_adjustments(self):
        # the verifier rejects proposals that try to rewrite identity even if
        # a schema bypass smuggled the key through.
        proposal = {"verdict": "adjust", "reason_codes": [],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "forged identity",
                    "adjustments": {"candidate_fingerprint": "fp_evil"}}
        result = _verify(proposal=proposal)
        assert result.ok is False

    def test_fingerprint_omitted_fails_closed(self):
        # 08-10 P2-1 (reviewer finding): a proposal that omits
        # candidate_fingerprint is rejected even when every other gate passes.
        proposal = {"verdict": "approve_as_is", "reason_codes": [],
                    "evidence_refs": [], "counter_evidence_refs": [],
                    "summary": "ok"}
        result = _verify(proposal=proposal, inject_fingerprint=False)
        assert result.ok is False
        assert result.effective_order_allowed is False
        assert result.adjusted_plan is None
        assert any("fingerprint" in e.lower() for e in result.errors)

    def test_fingerprint_absent_confirmation_never_collides(self):
        """08-12 fresh-reviewer Recommended-1: ``candidate_plan_fingerprint``
        with ``entry_trigger_confirmation=None``. A carried-only recheck plan
        carries NO structured confirmation (fail-closed
        ``_bind_trusted_entry_confirmation`` leaves it None), and the candidate
        fingerprint must not forward the empty dict to the strict identity
        check ``canonical_confirmation_fingerprint`` (raises / KeyError). The
        absent-confirmation component is the deterministic empty marker ``""``,
        which must NEVER equal a real 64-hex digest: a candidate WITHOUT a
        confirmation fingerprints distinctly from any candidate WITH one, and
        is deterministic across calls."""
        from plugins.crypto_guard.risk.risk_adjustment_verifier import (
            candidate_plan_fingerprint,
        )
        snap = _snapshot()
        pol = _policy("shadow")
        no_conf = _candidate_plan(entry_trigger_confirmation=None)
        with_conf = _candidate_plan()  # _confirmation() default
        fp_none_1 = candidate_plan_fingerprint(no_conf, snapshot=snap, policy=pol)
        fp_none_2 = candidate_plan_fingerprint(no_conf, snapshot=snap, policy=pol)
        fp_conf = candidate_plan_fingerprint(with_conf, snapshot=snap, policy=pol)
        # determinism
        assert fp_none_1 == fp_none_2
        # a real 64-hex sha256 digest, never the empty marker leak
        assert len(fp_none_1) == 64
        assert all(c in "0123456789abcdef" for c in fp_none_1)
        # distinctness: absent-confirmation candidate NEVER collides with a
        # candidate carrying a real confirmation fingerprint
        assert fp_none_1 != fp_conf
        # same geometry WITHOUT the key == explicit None (both reach the same
        # deterministic empty marker)
        fp_no_key = candidate_plan_fingerprint(
            _candidate_plan(entry_trigger_confirmation=None),
            snapshot=snap, policy=pol,
        )
        # drop the key entirely -- the dict path must be identical
        plan_no_key = dict(_candidate_plan())
        plan_no_key.pop("entry_trigger_confirmation", None)
        fp_dropped = candidate_plan_fingerprint(plan_no_key, snapshot=snap,
                                                policy=pol)
        assert fp_no_key == fp_dropped == fp_none_1


class TestAccountRiskCapsFailClosed:
    """08-10 P2-3 (reviewer finding): account risk caps are FAIL-CLOSED.

    prd.md P1-3: effective risk may never exceed the configured per-trade cap
    or the remaining total-account budget (``max_total_risk_pct`` minus the
    already-committed ``open_position_risk_pct``). A proposal whose intended
    risk (min over candidate / stop-scaling / explicit adjustment) exceeds a
    cap is REJECTED with the cap cited — the LLM's unsafe proposal is never
    silently clamped into an order.
    """

    def test_single_trade_cap_exceeded_fails_closed(self):
        account = _account(max_single_trade_risk_pct=1.0,
                           max_total_risk_pct=10.0, open_position_risk_pct=0.0)
        candidate = _candidate_plan(risk_percent=1.5)  # above the 1.0 cap
        result = _verify(candidate_plan=candidate, account=account)
        assert result.ok is False
        assert result.effective_order_allowed is False
        assert any("max_single_trade_risk_pct" in e or "单笔" in e
                   for e in result.errors), result.errors

    def test_total_cap_remaining_budget_exceeded_fails_closed(self):
        # remaining budget = max_total(2.0) - open_position_risk(1.5) = 0.5
        account = _account(max_single_trade_risk_pct=2.0,
                           max_total_risk_pct=2.0, open_position_risk_pct=1.5)
        candidate = _candidate_plan(risk_percent=1.0)  # above the 0.5 budget
        result = _verify(candidate_plan=candidate, account=account)
        assert result.ok is False
        assert result.effective_order_allowed is False
        assert any("max_total_risk_pct" in e or "预算" in e or "总风险" in e
                   for e in result.errors), result.errors

    def test_within_caps_approve_still_passes(self):
        # positive control: the new checks must not break the happy path.
        account = _account(max_single_trade_risk_pct=1.0,
                           max_total_risk_pct=10.0, open_position_risk_pct=0.0)
        result = _verify(account=account, mode="paper_bounded")
        assert result.ok is True
        assert result.effective_order_allowed is True

    def test_cap_exact_equality_allowed(self):
        # boundary: risk exactly equal to the cap is allowed (only > fails).
        account = _account(max_single_trade_risk_pct=0.5,
                           max_total_risk_pct=10.0, open_position_risk_pct=0.0)
        result = _verify(account=account, mode="paper_bounded")
        assert result.ok is True

    def test_non_positive_cap_fails_closed(self):
        """08-10 P2-2 (reviewer finding): a PRESENT-but-invalid cap (<=0,
        non-finite, bool) is a REJECTION, never a silent drop to 'no cap'.
        Pre-fix ``_safe_positive`` returned None for <=0 -> the cap gate was
        skipped -> '0 cap' silently became 'no cap' (fail-open)."""
        account = _account(max_single_trade_risk_pct=0.0,
                           max_total_risk_pct=10.0, open_position_risk_pct=0.0)
        result = _verify(account=account, mode="paper_bounded")
        assert result.ok is False, result
        assert result.effective_order_allowed is False, result
        assert any("max_single_trade_risk_pct" in e or "单笔" in e
                   for e in result.errors), result.errors

    def test_nan_cap_fails_closed(self):
        """08-10 P2-2: a NaN cap must reject (never float('nan') comparisons
        that are always False and thereby bypass the cap gate)."""
        account = _account(max_single_trade_risk_pct=float("nan"),
                           max_total_risk_pct=10.0, open_position_risk_pct=0.0)
        result = _verify(account=account, mode="paper_bounded")
        assert result.ok is False, result
        assert result.effective_order_allowed is False, result
        assert any("max_single_trade_risk_pct" in e or "单笔" in e
                   for e in result.errors), result.errors

    def test_negative_cap_fails_closed(self):
        """08-10 P2-2: a negative cap is a config defect, rejected fail-closed."""
        account = _account(max_single_trade_risk_pct=-1.0,
                           max_total_risk_pct=10.0, open_position_risk_pct=0.0)
        result = _verify(account=account, mode="paper_bounded")
        assert result.ok is False, result
        assert result.effective_order_allowed is False, result
        assert any("max_single_trade_risk_pct" in e or "单笔" in e
                   for e in result.errors), result.errors


class TestAccountRiskConfigValidation:
    """08-10 P2-2 (reviewer finding): startup validation for the account risk
    caps. A PRESENT-but-invalid cap (bool, non-numeric, NaN/Inf, <=0) must fail
    fast at ``load_config`` time — misconfiguration is a config defect that can
    never silently become 'no cap'. Absent caps are safe (account_risk_guard
    DEFAULTS 2.0/10.0 fill the gap)."""

    @staticmethod
    def _validate(account_risk: Any) -> None:
        from plugins.crypto_guard.config.loader import _validate_account_risk
        return _validate_account_risk({"account_risk": account_risk})

    def test_valid_caps_pass(self):
        self._validate({"max_single_trade_risk_pct": 2.0,
                        "max_total_risk_pct": 10.0})

    def test_absent_account_risk_passes(self):
        # DEFAULTS apply when the segment is missing entirely.
        from plugins.crypto_guard.config.loader import _validate_account_risk
        _validate_account_risk({})

    def test_zero_cap_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"max_single_trade_risk_pct": 0.0,
                            "max_total_risk_pct": 10.0})

    def test_negative_cap_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"max_single_trade_risk_pct": 2.0,
                            "max_total_risk_pct": -1.0})

    def test_bool_cap_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"max_single_trade_risk_pct": True,
                            "max_total_risk_pct": 10.0})

    def test_nan_cap_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"max_single_trade_risk_pct": float("nan"),
                            "max_total_risk_pct": 10.0})

    def test_inf_cap_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"max_single_trade_risk_pct": 2.0,
                            "max_total_risk_pct": float("inf")})

    def test_non_numeric_cap_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"max_single_trade_risk_pct": "2.0",
                            "max_total_risk_pct": 10.0})

    def test_non_mapping_account_risk_rejected(self):
        with pytest.raises(ValueError):
            self._validate("2.0")


class TestRiskThresholdThreading:
    """08-10 fresh-reviewer Recommended-1: ``min_sl_distance_pct`` /
    ``min_rr`` are read ONCE and threaded, never re-read ``load_config()``
    per gate. A config failure is a recorded REJECTION (fail-closed), never
    a propagated exception; explicit thresholds skip the config read entirely."""

    def test_load_config_raise_fails_closed(self, monkeypatch):
        """A config read failure inside ``verify_risk_adjustment`` yields
        ``ok=False`` with the config error recorded — no exception escapes."""
        import plugins.crypto_guard.risk.risk_adjustment_verifier as raf

        def _boom(*_args, **_kw):
            raise RuntimeError("trading_mode.yaml corrupted")

        monkeypatch.setattr(raf, "load_config", _boom)
        result = _verify(mode="paper_bounded")
        assert result.ok is False, result
        assert result.effective_order_allowed is False, result
        assert any("配置读取失败" in e or "阈值不可用" in e
                   for e in result.errors), result.errors

    def test_candidate_adaptive_blockers_threads_explicit_thresholds(
        self, monkeypatch,
    ):
        """Explicit ``min_sl_pct`` / ``min_rr`` are honored WITHOUT re-reading
        config (the producer threads values from its own single load)."""
        import plugins.crypto_guard.risk.risk_adjustment_verifier as raf

        def _boom(*_args, **_kw):
            raise AssertionError("load_config must not be re-read")

        monkeypatch.setattr(raf, "load_config", _boom)
        plan = _candidate_plan()  # SL 1.235%, RR = max(TP)/risk = 1.44/0.56
        snap = _snapshot()
        # Loose thresholds: SL 1.235 >= 0.8, RR 2.571 >= 1.5 -> nothing fails.
        assert raf.candidate_adaptive_blockers(
            plan, snapshot=snap, min_sl_pct=0.8, min_rr=1.5) == ()
        # Tight min_sl: 1.235 < 2.0 -> minimum_stop_distance joins the set.
        assert "minimum_stop_distance" in raf.candidate_adaptive_blockers(
            plan, snapshot=snap, min_sl_pct=2.0, min_rr=1.5)
        # Tight min_rr: 2.571 < 3.0 -> minimum_rr joins the set.
        assert "minimum_rr" in raf.candidate_adaptive_blockers(
            plan, snapshot=snap, min_sl_pct=0.8, min_rr=3.0)


class TestRiskThresholdFailClosed:
    """08-10 fresh-reviewer P2-1: a PRESENT-but-invalid ``risk`` threshold
    (NaN/Inf/bool/0/negative) must FAIL CLOSED inside the verifier. Before the
    fix a fail-open ``float(risk_cfg.get(...))`` accepted NaN, and ``rr < nan``
    is ALWAYS False -> the min_rr / min_sl gates silently DISABLED themselves
    (fail-open: every plan passed the gate)."""

    @staticmethod
    def _bad_cfg(**risk: Any) -> types.SimpleNamespace:
        return types.SimpleNamespace(trading_mode={"risk": risk})

    def test_nan_min_rr_fails_closed(self, monkeypatch):
        import plugins.crypto_guard.risk.risk_adjustment_verifier as raf

        monkeypatch.setattr(
            raf, "load_config",
            lambda: self._bad_cfg(min_rr=float("nan"),
                                  min_sl_distance_pct=0.8),
        )
        result = _verify(mode="paper_bounded")
        assert result.ok is False, result
        assert result.effective_order_allowed is False, result
        assert any("阈值配置读取失败" in e or "阈值不可用" in e
                   for e in result.errors), result.errors

    def test_zero_min_rr_fails_closed(self, monkeypatch):
        import plugins.crypto_guard.risk.risk_adjustment_verifier as raf

        monkeypatch.setattr(
            raf, "load_config",
            lambda: self._bad_cfg(min_rr=0.0, min_sl_distance_pct=0.8),
        )
        result = _verify(mode="paper_bounded")
        assert result.ok is False, result
        assert result.effective_order_allowed is False, result

    def test_bool_min_rr_fails_closed(self, monkeypatch):
        import plugins.crypto_guard.risk.risk_adjustment_verifier as raf

        monkeypatch.setattr(
            raf, "load_config",
            lambda: self._bad_cfg(min_rr=True, min_sl_distance_pct=0.8),
        )
        result = _verify(mode="paper_bounded")
        assert result.ok is False, result
        assert result.effective_order_allowed is False, result

    def test_nan_min_sl_fails_closed(self, monkeypatch):
        import plugins.crypto_guard.risk.risk_adjustment_verifier as raf

        monkeypatch.setattr(
            raf, "load_config",
            lambda: self._bad_cfg(min_rr=1.5, min_sl_distance_pct=float("nan")),
        )
        result = _verify(mode="paper_bounded")
        assert result.ok is False, result
        assert result.effective_order_allowed is False, result


class TestRiskThresholdConfigValidation:
    """08-10 fresh-reviewer P2-1: startup validation for the ``risk``
    thresholds segment. Present-but-invalid (bool / non-numeric / NaN/Inf /
    <=0) fails fast at ``load_config`` time — misconfiguration is a config
    defect that can never silently become 'no gate'. Absent keys are safe
    (``cfg_threshold`` code defaults fill the gap)."""

    @staticmethod
    def _validate(risk_seg: Any) -> None:
        from plugins.crypto_guard.config.loader import _validate_risk
        return _validate_risk({"risk": risk_seg})

    def test_valid_thresholds_pass(self):
        self._validate({"min_rr": 1.5, "min_sl_distance_pct": 0.8,
                        "min_tp_distance_pct": 1.0, "min_confidence": 0.72})

    def test_absent_risk_segment_passes(self):
        from plugins.crypto_guard.config.loader import _validate_risk
        _validate_risk({})

    def test_zero_threshold_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"min_rr": 0.0, "min_sl_distance_pct": 0.8})

    def test_negative_threshold_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"min_rr": 2.0, "min_sl_distance_pct": -0.1})

    def test_bool_threshold_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"min_rr": True, "min_sl_distance_pct": 0.8})

    def test_nan_threshold_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"min_rr": float("nan"), "min_sl_distance_pct": 0.8})

    def test_inf_threshold_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"min_rr": float("inf"), "min_sl_distance_pct": 0.8})

    def test_non_numeric_threshold_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"min_rr": "1.5", "min_sl_distance_pct": 0.8})

    def test_non_mapping_risk_rejected(self):
        with pytest.raises(ValueError):
            self._validate("1.5")

    def test_cfg_threshold_absent_uses_default(self):
        from plugins.crypto_guard.config.loader import cfg_threshold
        assert cfg_threshold({"min_sl_distance_pct": 0.8}, "min_rr", 2.0) == 2.0

    def test_cfg_threshold_valid_present(self):
        from plugins.crypto_guard.config.loader import cfg_threshold
        assert cfg_threshold({"min_rr": 1.5}, "min_rr", 2.0) == 1.5

    def test_cfg_threshold_present_none_uses_default(self):
        from plugins.crypto_guard.config.loader import cfg_threshold
        assert cfg_threshold({"min_rr": None}, "min_rr", 2.0) == 2.0
