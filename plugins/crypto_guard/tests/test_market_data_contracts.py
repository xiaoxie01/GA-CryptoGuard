"""Market-data completeness and snapshot/schema contract regressions."""

import pytest

from plugins.crypto_guard.tests._smoke_suite import (
    TestMarketDataCompletenessP0,
    TestMarketDataCompletenessR2Fixes,
    TestR10SnapshotAuthoritativeAnalysisTime,
    TestR11SchemaTypeContract,
    TestR11StrictPositiveInt,
    TestR11ValidateTradePlanTypeContract,
    TestR8SnapshotPathContract,
    TestR9EndToEndContract,
    test_r9_build_trade_plan_confirmation_contains_symbol,
    test_r9_build_trade_plan_returns_none_when_snapshot_missing_symbol,
)

pytestmark = [pytest.mark.pg, pytest.mark.concurrency]

__all__ = [
    "TestR8SnapshotPathContract",
    "TestR9EndToEndContract",
    "test_r9_build_trade_plan_confirmation_contains_symbol",
    "test_r9_build_trade_plan_returns_none_when_snapshot_missing_symbol",
    "TestR10SnapshotAuthoritativeAnalysisTime",
    "TestR11StrictPositiveInt",
    "TestR11ValidateTradePlanTypeContract",
    "TestR11SchemaTypeContract",
    "TestMarketDataCompletenessP0",
    "TestMarketDataCompletenessR2Fixes",
]
