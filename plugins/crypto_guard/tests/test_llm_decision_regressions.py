"""LLM retry, decision continuity, schema, and breaker regressions."""

import pytest

from plugins.crypto_guard.tests._smoke_suite import (
    TestPhaseA07_05BaselineFailures,
    TestPhaseA07_09OvertriggerFollowup,
    TestPhaseA07_09SchemaRepairBreaker,
    TestPhaseB07_07LLMRetryAndBreaker,
    TestPhaseC07_07PlanStateLabel,
    TestPhaseD07_07RawGradeCaps,
    TestPhaseG07_05LLMFallbackContract,
    TestPhaseH07_05RealControllerContentContracts,
    TestPhaseH07_05RealControllerDiagnosticPath,
)

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

__all__ = [
    "TestPhaseA07_05BaselineFailures",
    "TestPhaseG07_05LLMFallbackContract",
    "TestPhaseH07_05RealControllerDiagnosticPath",
    "TestPhaseH07_05RealControllerContentContracts",
    "TestPhaseB07_07LLMRetryAndBreaker",
    "TestPhaseC07_07PlanStateLabel",
    "TestPhaseD07_07RawGradeCaps",
    "TestPhaseA07_09SchemaRepairBreaker",
    "TestPhaseA07_09OvertriggerFollowup",
]
