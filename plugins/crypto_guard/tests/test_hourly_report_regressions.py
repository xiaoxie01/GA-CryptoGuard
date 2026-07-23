"""Hourly analysis semantics, diagnostics, and rendering regressions."""

import pytest

from plugins.crypto_guard.tests._smoke_suite import (
    HourlyReportAccuracyTest,
    TestHourlyAnalysisSemanticAccuracy07_03,
    TestPhaseE07_07HourlyReportAndBatchConsistency,
    TestPhaseH07_05DiagnosticsAndReportUX,
)

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

__all__ = [
    "HourlyReportAccuracyTest",
    "TestHourlyAnalysisSemanticAccuracy07_03",
    "TestPhaseH07_05DiagnosticsAndReportUX",
    "TestPhaseE07_07HourlyReportAndBatchConsistency",
]
