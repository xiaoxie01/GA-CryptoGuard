# -*- coding: utf-8 -*-
"""08-10 Step 2 RED contract: risk-assistance policy configuration/parsing.

Contract under test (design.md §4, prd.md P0-2 + P1-6; same semantics as the
Step-1 policy-parsing contract in ``test_pg_08_10_confirmation_lifecycle_p1.py``
``TestRiskAssistancePolicyParsing``):

  - ``risk_assistance`` is a versioned configuration section with an EXACT key
    set and types. ``load_risk_assistance_config(config)`` reads it from the
    TOP-LEVEL ``trading_mode.yaml`` mapping (the same dict that
    ``CryptoGuardConfig.trading_mode`` holds): ``config["risk_assistance"]``.
    A MISSING section returns the default ``RiskAssistancePolicy()``
    (mode=shadow -- the migration default, never ``paper_bounded``).
  - Omitted keys inside the section fall back to compiled defaults; a partial
    section like ``{"mode": "paper_bounded"}`` is valid and only overrides mode.
  - The hard-gate set is COMPILED into ``HARD_GATE_CODES`` and can never be
    edited away: the constant always contains all eight mandatory gates. Config
    may only select a NON-EMPTY SUBSET of those codes; ``policy.hard_gates``
    then reflects that subset exactly (it can never ADD a non-mandatory gate).
    ``hard_gates: []`` is rejected (an empty gate list would mean "no hard
    gates" -- fail closed, never silently waive).
  - No overlap between the hard and adaptive classes: a value listed as hard
    that is (or would default to) adaptive, or vice versa, is a hard error.
  - Fail closed (ValueError -- assistance disabled, never silently
    reclassified): unknown mode, unknown policy key, wrong type, NaN/inf,
    a TTL above its hard maximum, a hard max below its TTL, a TTL/hard-max
    timeframe outside {5m,15m}, a non-positive TTL, and an unknown gate value.

RED-first: ``risk/risk_policy.py`` does not exist yet; every import fails with
ModuleNotFoundError. That is the intended baseline.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]

MANDATORY_HARD_GATES = {
    "market_data_ready", "trusted_entry_confirmation", "account_enabled",
    "drawdown_limit", "exposure_limit", "valid_geometry", "idempotency",
    "extreme_regime",
}
KNOWN_ADAPTIVE_GATES = {
    "minimum_stop_distance", "atr_stop_buffer", "minimum_rr",
    "news_like_event",
}
VALID_MODES = ("off", "shadow", "paper_bounded")


def _section(**over: object) -> dict:
    sec = {
        "contract_version": 1,
        "mode": "shadow",
        "max_rounds": 2,
        "max_tool_requests": 5,
        "max_context_bytes": 49152,
        "max_uncertainty": 0.35,
        "confirmation_ttl_bars": {"5m": 3, "15m": 1},
        "confirmation_hard_max_bars": {"5m": 6, "15m": 2},
        "max_entry_deviation_pct": 0.30,
        "max_entry_deviation_atr": 0.25,
        "max_stop_distance_pct": 2.50,
        "max_stop_distance_atr": 2.00,
        "hard_gates": sorted(MANDATORY_HARD_GATES),
        "adaptive_gates": sorted(KNOWN_ADAPTIVE_GATES),
    }
    sec.update(over)
    return sec


def _load(**over: object):
    from plugins.crypto_guard.risk.risk_policy import (
        load_risk_assistance_config,
    )
    return load_risk_assistance_config(
        {"risk_assistance": _section(**over)})


class TestDefaultPolicy:
    """``RiskAssistancePolicy()`` is the compiled default: shadow, exact TTLs."""

    def test_default_mode_is_shadow(self):
        from plugins.crypto_guard.risk.risk_policy import (
            RiskAssistancePolicy,
        )
        p = RiskAssistancePolicy()
        assert p.mode == "shadow"
        assert p.contract_version == 1

    def test_default_ttl_and_hard_max(self):
        from plugins.crypto_guard.risk.risk_policy import (
            RiskAssistancePolicy,
        )
        p = RiskAssistancePolicy()
        assert p.confirmation_ttl_bars == {"5m": 3, "15m": 1}
        assert p.confirmation_hard_max_bars == {"5m": 6, "15m": 2}

    def test_default_hard_gates_are_the_full_mandatory_set(self):
        from plugins.crypto_guard.risk.risk_policy import (
            RiskAssistancePolicy,
        )
        p = RiskAssistancePolicy()
        assert set(p.hard_gates) == MANDATORY_HARD_GATES

    def test_default_adaptive_gates(self):
        from plugins.crypto_guard.risk.risk_policy import (
            RiskAssistancePolicy,
        )
        p = RiskAssistancePolicy()
        assert set(p.adaptive_gates) == KNOWN_ADAPTIVE_GATES

    def test_default_bounded_numbers(self):
        from plugins.crypto_guard.risk.risk_policy import (
            RiskAssistancePolicy,
        )
        p = RiskAssistancePolicy()
        assert p.max_context_bytes == 49152
        assert p.max_uncertainty == 0.35
        assert p.max_rounds == 2
        assert p.max_tool_requests == 5
        assert p.max_entry_deviation_pct == 0.50
        assert p.max_entry_deviation_atr == 0.25
        assert p.max_stop_distance_pct == 2.50
        assert p.max_stop_distance_atr == 2.00


class TestExactSchemaParsing:
    """``load_risk_assistance_config`` parses a complete section exactly."""

    def test_full_valid_section_parses(self):
        p = _load()
        assert p.mode == "shadow"
        assert p.confirmation_ttl_bars == {"5m": 3, "15m": 1}
        assert set(p.hard_gates) == MANDATORY_HARD_GATES
        assert set(p.adaptive_gates) == KNOWN_ADAPTIVE_GATES

    def test_all_three_modes_parse(self):
        for mode in VALID_MODES:
            p = _load(mode=mode)
            assert p.mode == mode

    def test_missing_section_returns_default_shadow(self):
        from plugins.crypto_guard.risk.risk_policy import (
            RiskAssistancePolicy,
            load_risk_assistance_config,
        )
        p = load_risk_assistance_config({})
        assert p == RiskAssistancePolicy()
        assert p.mode == "shadow"

    def test_mode_only_partial_section_fills_defaults(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        # A partial section overriding ONLY mode is valid; every other key
        # falls back to the compiled default (full mandatory hard gates).
        p = load_risk_assistance_config(
            {"risk_assistance": {"mode": "paper_bounded"}})
        assert p.mode == "paper_bounded"
        assert set(p.hard_gates) == MANDATORY_HARD_GATES
        assert p.confirmation_ttl_bars == {"5m": 3, "15m": 1}
        assert p.max_context_bytes == 49152

    def test_explicit_hard_gate_subset_is_exact_but_codes_compiled(self):
        from plugins.crypto_guard.risk.risk_policy import HARD_GATE_CODES
        # Config may select a SUBSET of the compiled codes; policy.hard_gates
        # reflects that subset EXACTLY. It can never add a non-mandatory gate,
        # and the compiled floor (HARD_GATE_CODES) is never edited away.
        subset = sorted(MANDATORY_HARD_GATES - {"extreme_regime"})
        p = _load(hard_gates=subset)
        assert set(p.hard_gates) == set(subset)
        assert "extreme_regime" not in set(p.hard_gates)
        assert set(HARD_GATE_CODES) == MANDATORY_HARD_GATES

    def test_config_may_subset_adaptive_gates(self):
        # Reducing adaptive gates is conservative (those blockers become
        # hard-only) and therefore allowed.
        p = _load(adaptive_gates=["minimum_stop_distance"])
        assert set(p.adaptive_gates) == {"minimum_stop_distance"}

    def test_contract_version_mismatch_rejected(self):
        with pytest.raises(ValueError):
            _load(contract_version=2)


class TestFailClosed:
    """Garbage/missing/unknown config disables assistance (ValueError)."""

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            _load(mode="live_money")

    def test_unknown_policy_key_rejected(self):
        with pytest.raises(ValueError):
            _load(allow_llm_order=True)

    def test_unknown_adaptive_gate_rejected(self):
        with pytest.raises(ValueError):
            _load(adaptive_gates=["slippery_slope"])

    def test_unknown_hard_gate_rejected(self):
        with pytest.raises(ValueError):
            _load(hard_gates=["nuclear_option"])

    def test_hard_value_that_is_really_adaptive_rejected(self):
        # ``minimum_stop_distance`` is adaptive; naming it as a hard gate
        # overlaps the adaptive class (including the default set) -> hard error.
        with pytest.raises(ValueError):
            _load(hard_gates=["market_data_ready", "minimum_stop_distance"])

    def test_adaptive_value_that_is_really_hard_rejected(self):
        # ``extreme_regime`` is mandatory-hard; naming it adaptive overlaps the
        # hard class -> hard error.
        with pytest.raises(ValueError):
            _load(adaptive_gates=["extreme_regime"])

    def test_empty_hard_gates_rejected(self):
        # An explicit empty hard-gate list would mean "no hard gates" and is
        # never acceptable -- fail closed.
        with pytest.raises(ValueError):
            _load(hard_gates=[])

    def test_ttl_above_hard_max_rejected(self):
        with pytest.raises(ValueError):
            _load(confirmation_ttl_bars={"5m": 7, "15m": 1})

    def test_hard_max_below_ttl_rejected(self):
        with pytest.raises(ValueError):
            _load(confirmation_ttl_bars={"5m": 3, "15m": 1},
                  confirmation_hard_max_bars={"5m": 2, "15m": 1})

    def test_ttl_timeframe_outside_5m_15m_rejected(self):
        with pytest.raises(ValueError):
            _load(confirmation_ttl_bars={"1m": 5, "5m": 3, "15m": 1})

    def test_non_positive_ttl_rejected(self):
        with pytest.raises(ValueError):
            _load(confirmation_ttl_bars={"5m": 0, "15m": 1})

    def test_nan_rejected(self):
        with pytest.raises(ValueError):
            _load(max_uncertainty=float("nan"))

    def test_inf_rejected(self):
        with pytest.raises(ValueError):
            _load(max_stop_distance_pct=float("inf"))

    def test_negative_tolerance_rejected(self):
        with pytest.raises(ValueError):
            _load(max_entry_deviation_pct=-0.1)

    def test_wrong_types_rejected(self):
        with pytest.raises(ValueError):
            _load(mode=1)
        with pytest.raises(ValueError):
            _load(max_rounds="2")
        with pytest.raises(ValueError):
            _load(max_context_bytes="49152")
        with pytest.raises(ValueError):
            _load(confirmation_ttl_bars="5m=3")
        with pytest.raises(ValueError):
            _load(max_uncertainty=True)  # bool is not a number

    def test_non_mapping_section_rejected(self):
        from plugins.crypto_guard.risk.risk_policy import (
            load_risk_assistance_config,
        )
        with pytest.raises(ValueError):
            load_risk_assistance_config({"risk_assistance": "shadow"})
