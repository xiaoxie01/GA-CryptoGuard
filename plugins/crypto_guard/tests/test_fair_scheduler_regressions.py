"""Fair scheduling, deadline, subprocess, and ownership regressions."""

import pytest

from plugins.crypto_guard.tests._smoke_suite import (
    Test07_10FairBatchProductionChain,
    TestPhaseA07_10LLMFairSchedulingRepro,
    TestPhaseB07_10ConfigAndDeadlinePrimitives,
    TestPhaseC07_10FairScheduler,
    TestPhaseD07_10PromptContinuityMetadata,
    TestPhaseE07_10LLMAccountingAndReport,
    TestR7P0_3ServiceOwnershipLease,
)

pytestmark = [pytest.mark.pg, pytest.mark.concurrency]

# These methods intentionally arbitrate one database-wide service lease. They
# remain complete, but must not overlap one another across xdist workers.
TestR7P0_3ServiceOwnershipLease.pytestmark = pytest.mark.serial
Test07_10FairBatchProductionChain.test_s4_fair_path_hard_timeout_surfaces_symbol_timeout = (
    pytest.mark.serial(
        pytest.mark.subprocess(
            pytest.mark.slow(
                Test07_10FairBatchProductionChain.test_s4_fair_path_hard_timeout_surfaces_symbol_timeout
            )
        )
    )
)
# P0-2 large-response Pipe drain also spawns a real child; under xdist it
# contends for CPU/IO and can falsely trip the wall-clock gate. Keep it on
# the serial stage with the other hard subprocess contracts.
Test07_10FairBatchProductionChain.test_p0_2_large_response_does_not_false_hard_timeout = (
    pytest.mark.serial(
        pytest.mark.subprocess(
            pytest.mark.slow(
                Test07_10FairBatchProductionChain.test_p0_2_large_response_does_not_false_hard_timeout
            )
        )
    )
)
# R4-P0-1 seeds deferred_at near the absolute window bound with real wall-clock
# arithmetic. Under xdist the 30s margin was eaten by run_once latency; keep
# it serial (and the seed margin is 90s) so host load cannot false-exhaust.
Test07_10FairBatchProductionChain.test_r4_p0_1_legitimate_long_lease_not_prematurely_exhausted = (
    pytest.mark.serial(
        pytest.mark.slow(
            Test07_10FairBatchProductionChain.test_r4_p0_1_legitimate_long_lease_not_prematurely_exhausted
        )
    )
)

__all__ = [
    "TestPhaseA07_10LLMFairSchedulingRepro",
    "TestPhaseE07_10LLMAccountingAndReport",
    "Test07_10FairBatchProductionChain",
    "TestR7P0_3ServiceOwnershipLease",
    "TestPhaseB07_10ConfigAndDeadlinePrimitives",
    "TestPhaseC07_10FairScheduler",
    "TestPhaseD07_10PromptContinuityMetadata",
]
