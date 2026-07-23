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

__all__ = [
    "TestPhaseA07_10LLMFairSchedulingRepro",
    "TestPhaseE07_10LLMAccountingAndReport",
    "Test07_10FairBatchProductionChain",
    "TestR7P0_3ServiceOwnershipLease",
    "TestPhaseB07_10ConfigAndDeadlinePrimitives",
    "TestPhaseC07_10FairScheduler",
    "TestPhaseD07_10PromptContinuityMetadata",
]
