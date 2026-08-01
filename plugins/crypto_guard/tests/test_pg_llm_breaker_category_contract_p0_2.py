# -*- coding: utf-8 -*-
"""07-31 production fix P0-2 (2026-07-31): breaker category isolation —
RED-first behavioral test + revert-fail.

Production evidence (batch 15m:1785487499999): 5 schema failures
(decision emitted as array + numeric take_profits items) pushed into
``CircuitBreaker._recent_results``; 5/10 = 50% >= rate_threshold 0.5 ->
breaker opened -> all 10 symbols persisted as breaker_skipped with
provider_call_count=0, destroying 8 coordinator successes.

Root cause: ``CircuitBreaker.record_attempt`` (llm_breaker.py:132)
UNCONDITIONALLY appends every non-repairable failure to the rate window,
so ``llm_json_parse_failed`` / ``llm_schema_validation_failed`` /
``llm_semantic_validation_failed`` / ``llm_output_truncated`` — which the
judge declares outside breaker jurisdiction (llm_agent_judge.py:608
``_BREAKER_INFRA_REASONS``) — pollute the rate window and drive a false
rate-open. The two definitions drifted apart.

Fix (single source of truth): a module-level
``BREAKER_DRIVING_CATEGORIES`` frozenset in llm_breaker.py consumed by
``record_attempt``; ``llm_agent_judge._BREAKER_INFRA_REASONS`` becomes an
import alias of it so drift is impossible.

Contract:
- ``llm_config_error``: immediate open (unchanged).
- driving categories (``llm_transport_error`` / ``llm_empty_response`` /
  ``llm_tool_call_no_text`` / ``llm_rate_limited``): append to rate window
  + count consecutive infra failures -> open at threshold or rate-open.
- non-driving failures (schema/json/semantic/truncated + anything else):
  count in total_attempts / failed / by_category (conservation), but do
  NOT enter the rate window and DO reset the consecutive-infra counter.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

from plugins.crypto_guard.reasoning.llm_breaker import (
    BREAKER_DRIVING_CATEGORIES,
    CircuitBreaker,
)
from plugins.crypto_guard.reasoning.llm_agent_judge import _BREAKER_INFRA_REASONS

_EXPECTED_DRIVING = frozenset({
    "llm_transport_error",
    "llm_empty_response",
    "llm_tool_call_no_text",
    "llm_rate_limited",
    "llm_config_error",
})


class TestBreakerCategoryContract:
    """P0-2: single importable driving-category contract."""

    def test_contract_exists_and_matches_spec(self) -> None:
        assert isinstance(BREAKER_DRIVING_CATEGORIES, frozenset), (
            "BREAKER_DRIVING_CATEGORIES must be a frozenset"
        )
        assert BREAKER_DRIVING_CATEGORIES == _EXPECTED_DRIVING, (
            f"driving set must be exactly {sorted(_EXPECTED_DRIVING)}; got "
            f"{sorted(BREAKER_DRIVING_CATEGORIES)}"
        )

    def test_judge_alias_is_same_object(self) -> None:
        # The judge's _BREAKER_INFRA_REASONS must be the SAME object so no
        # future drift between the two definitions is possible.
        assert _BREAKER_INFRA_REASONS is BREAKER_DRIVING_CATEGORIES, (
            "llm_agent_judge._BREAKER_INFRA_REASONS must alias "
            "llm_breaker.BREAKER_DRIVING_CATEGORIES (same object)"
        )


class TestNonDrivingDoesNotOpenBreaker:
    """P0-2 RED: schema/json/semantic/truncated failures must NOT open the
    breaker. Pre-fix each of these sequences drives a rate-open (6 >=
    min_rate_samples=5, failure rate 100%)."""

    def _breaker(self) -> CircuitBreaker:
        return CircuitBreaker(
            enabled=True,
            consecutive_threshold=3,
            rate_threshold=0.5,
            rate_window=10,
            min_rate_samples=5,
        )

    def test_schema_validation_failures_do_not_open(self) -> None:
        b = self._breaker()
        for _ in range(6):
            b.record_attempt(category="llm_schema_validation_failed", ok=False)
        snap = b.snapshot()
        assert b.state == "closed", (
            "6x llm_schema_validation_failed must NOT open the breaker "
            "(non-driving); got state=%s" % b.state
        )
        assert snap["recent_10_calls"] == 0, (
            "schema failures must not enter the rate window; "
            f"recent_10_calls={snap['recent_10_calls']}"
        )
        assert snap["failed"] == 6 and snap["total_attempts"] == 6, (
            "schema failures still count in total_attempts/failed "
            f"(conservation); got {snap['total_attempts']}/{snap['failed']}"
        )
        assert snap["by_category"].get("llm_schema_validation_failed") == 6, (
            "schema failures still count in by_category"
        )

    def test_json_parse_failures_do_not_open(self) -> None:
        b = self._breaker()
        for _ in range(6):
            b.record_attempt(category="llm_json_parse_failed", ok=False)
        assert b.state == "closed", (
            "6x llm_json_parse_failed must NOT open the breaker (non-driving)"
        )

    def test_semantic_validation_failures_do_not_open(self) -> None:
        b = self._breaker()
        for _ in range(5):
            b.record_attempt(category="llm_semantic_validation_failed", ok=False)
        assert b.state == "closed", (
            "5x llm_semantic_validation_failed must NOT open the breaker "
            "(non-driving)"
        )

    def test_output_truncated_does_not_open(self) -> None:
        b = self._breaker()
        for _ in range(5):
            b.record_attempt(category="llm_output_truncated", ok=False)
        assert b.state == "closed", (
            "5x llm_output_truncated must NOT open the breaker (non-driving)"
        )

    def test_mixed_non_driving_does_not_open(self) -> None:
        b = self._breaker()
        seq = ["llm_schema_validation_failed", "llm_json_parse_failed",
               "llm_semantic_validation_failed", "llm_output_truncated",
               "llm_schema_validation_failed", "llm_json_parse_failed"]
        for cat in seq:
            b.record_attempt(category=cat, ok=False)
        assert b.state == "closed", (
            "mixed non-driving failures must not open the breaker"
        )


class TestDrivingOpensBreaker:
    """P0-2 GREEN: transport / rate_limited / config still open."""

    def _breaker(self) -> CircuitBreaker:
        return CircuitBreaker(
            enabled=True,
            consecutive_threshold=3,
            rate_threshold=0.5,
            rate_window=10,
            min_rate_samples=5,
        )

    def test_transport_consecutive_opens(self) -> None:
        b = self._breaker()
        for _ in range(3):
            b.record_attempt(category="llm_transport_error", ok=False)
        assert b.state == "open", (
            "3x llm_transport_error must open the breaker (consecutive path)"
        )

    def test_rate_limited_now_drives_consecutive(self) -> None:
        # Pre-fix llm_rate_limited was NOT in the hardcoded consecutive tuple
        # (only in the drift-prone _BREAKER_INFRA_REASONS) so 3 consecutive
        # rate-limit failures stayed closed. Post-fix it drives the breaker.
        b = self._breaker()
        for _ in range(3):
            b.record_attempt(category="llm_rate_limited", ok=False)
        assert b.state == "open", (
            "3x llm_rate_limited must open the breaker (driving category)"
        )

    def test_config_error_opens_immediately(self) -> None:
        b = self._breaker()
        b.record_attempt(category="llm_config_error", ok=False)
        assert b.state == "open", (
            "llm_config_error must open immediately (unchanged contract)"
        )

    def test_rate_open_from_driving_only(self) -> None:
        # 5 schema + 5 transport: the rate window must contain ONLY the 5
        # transport failures -> 5/5 = 100% >= 50% -> open. Pre-fix the
        # window held all 10 and recent_10_calls was 10.
        b = self._breaker()
        for _ in range(5):
            b.record_attempt(category="llm_schema_validation_failed", ok=False)
        for _ in range(5):
            b.record_attempt(category="llm_transport_error", ok=False)
        snap = b.snapshot()
        assert snap["recent_10_calls"] == 5, (
            "rate window must contain only driving failures; got "
            f"recent_10_calls={snap['recent_10_calls']}"
        )
        assert snap["recent_10_failed"] == 5
        assert b.state == "open", (
            "5/5 driving failures in window must open the breaker"
        )

    def test_non_driving_resets_consecutive(self) -> None:
        # transport, transport, schema, transport, transport, transport:
        # the schema failure resets the consecutive counter, so the breaker
        # opens only when the 3rd transport AFTER the reset arrives.
        b = self._breaker()
        seq = ["llm_transport_error", "llm_transport_error",
               "llm_schema_validation_failed",
               "llm_transport_error", "llm_transport_error",
               "llm_transport_error"]
        for cat in seq:
            b.record_attempt(category=cat, ok=False)
        assert b.state == "open", (
            "consecutive infra must reset on non-driving failure, then "
            "re-open at 3 consecutive transport failures"
        )

    def test_non_driving_between_transports_blocks_consecutive_open(self) -> None:
        # transport, transport, schema, transport -> consecutive count is
        # 2 after the reset, breaker stays closed.
        b = self._breaker()
        for cat in ("llm_transport_error", "llm_transport_error",
                    "llm_schema_validation_failed", "llm_transport_error"):
            b.record_attempt(category=cat, ok=False)
        assert b.state == "closed", (
            "non-driving failure must reset the consecutive-infra counter"
        )


class TestConservationAndRepairable:
    """P0-2: counters stay conserved; repairable events never drive."""

    def test_snapshot_conservation_ten_schema_failures(self) -> None:
        b = CircuitBreaker(
            enabled=True, consecutive_threshold=3,
            rate_threshold=0.5, rate_window=10, min_rate_samples=5,
        )
        for _ in range(10):
            b.record_attempt(category="llm_schema_validation_failed", ok=False)
        snap = b.snapshot()
        assert snap["total_attempts"] == 10
        assert snap["successful"] == 0
        assert snap["failed"] == 10
        assert snap["by_category"]["llm_schema_validation_failed"] == 10
        assert snap["recent_10_calls"] == 0
        assert snap["recent_10_failed"] == 0
        assert b.state == "closed"

    def test_repairable_events_never_drive(self) -> None:
        b = CircuitBreaker(
            enabled=True, consecutive_threshold=3,
            rate_threshold=0.5, rate_window=10, min_rate_samples=5,
        )
        for _ in range(10):
            b.record_attempt(category="llm_schema_repairable", ok=True, repairable=True)
        snap = b.snapshot()
        assert b.state == "closed"
        assert snap["total_attempts"] == 0
        assert snap["repairable_count"] == 10
        assert snap["recent_10_calls"] == 0
