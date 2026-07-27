"""Structural contracts for the split CryptoGuard regression suite."""

from __future__ import annotations

import importlib
import hashlib
import inspect
import unittest
from collections import Counter

from plugins.crypto_guard.tests import _smoke_suite

import pytest


pytestmark = pytest.mark.unit

# Captured from HEAD's pre-split test_smoke.py. The sole intentional class
# rename is normalized before hashing. This makes coverage loss detectable
# independently of the current implementation module and domain exports.
# 1211 + 9 Codex 终审返工 tests (07-23):
# - Test07_10FairBatchProductionChain::test_codex_p1_1_timeout_envelope_* (5)
# - Test07_10FairBatchProductionChain::test_p2_2_continuity_age_eq_* (2)
# - TestHourlyAnalysisSemanticAccuracy07_03::test_p2_1_htf_countertrend_* (2)
# Plus strengthened test_p1_1_fair_adapter_exhausted_deadline_skips_provider
# (no count change — same method name).
# Codex P2 exclude-only (07-23): renamed
# test_codex_p1_1_pre_envelope_marker_timeout_is_legacy_info
# -> test_codex_p1_1_pre_envelope_marker_timeout_is_excluded_not_current_error
# (count unchanged; SHA updated).
# 1220 -> 1219: 终审返工 R2 P2-2 (2026-07-26) deleted the dead-helper test
# ``CryptoGuardSmokeTest::test_distribution_source_label_sqlite_fallback_phrasing``
# from ``_smoke_suite``. That test only asserted the now-DELETED dead
# ``_distribution_source_label`` helper's phrasing; it had no production
# consumer (see ``test_pg_dead_duckdb_stats_helpers_removed_p2_2.py`` for the
# live coverage that replaces it). The removal drops the legacy test-definition
# manifest by exactly one object, so the count baseline moves 1220 -> 1219 and
# the manifest SHA is recomputed. This is a compliance baseline update - no
# production behavior, no test semantics, no coverage loss.
BASELINE_TEST_DEFINITION_COUNT = 1219
BASELINE_TEST_DEFINITION_SHA256 = (
    "e01364b21f82d1dc10f1dfa1375786e3acfbc271052ba3f4f24c071df7d50c15"
)
BASELINE_CLASS_RENAMES = {
    "Btc9RegressionChainTest": "TradeGateRegressionChainTest",
}


DOMAIN_MODULES = (
    "plugins.crypto_guard.tests.test_core_regressions",
    "plugins.crypto_guard.tests.test_paper_orders_regressions",
    "plugins.crypto_guard.tests.test_shadow_lifecycle_regressions",
    "plugins.crypto_guard.tests.test_hourly_report_regressions",
    "plugins.crypto_guard.tests.test_market_data_contracts",
    "plugins.crypto_guard.tests.test_llm_decision_regressions",
    "plugins.crypto_guard.tests.test_fair_scheduler_regressions",
)


def _legacy_test_objects() -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in vars(_smoke_suite).items():
        if inspect.isfunction(value) and name.startswith("test_"):
            result[name] = value
            continue
        if not inspect.isclass(value) or value.__module__ != _smoke_suite.__name__:
            continue
        if issubclass(value, unittest.TestCase):
            if unittest.defaultTestLoader.getTestCaseNames(value):
                result[name] = value
        elif name.startswith("Test"):
            result[name] = value
    return result


def _test_definition_manifest() -> list[str]:
    manifest: list[str] = []
    for name, value in vars(_smoke_suite).items():
        if inspect.isfunction(value) and name.startswith("test_"):
            manifest.append(f"function::{name}")
            continue
        if not inspect.isclass(value) or value.__module__ != _smoke_suite.__name__:
            continue
        for method_name, method in vars(value).items():
            if method_name.startswith("test_") and callable(method):
                manifest.append(f"{name}::{method_name}")
    return sorted(manifest)


def test_every_legacy_test_object_has_exactly_one_domain_owner() -> None:
    manifest = _test_definition_manifest()
    digest = hashlib.sha256("\n".join(manifest).encode("utf-8")).hexdigest()
    assert len(manifest) == BASELINE_TEST_DEFINITION_COUNT
    assert digest == BASELINE_TEST_DEFINITION_SHA256
    assert set(BASELINE_CLASS_RENAMES).isdisjoint(_legacy_test_objects())
    assert set(BASELINE_CLASS_RENAMES.values()) <= set(_legacy_test_objects())

    expected = _legacy_test_objects()
    owners: Counter[object] = Counter()
    for module_name in DOMAIN_MODULES:
        module = importlib.import_module(module_name)
        for exported_name in getattr(module, "__all__", ()):
            owners[getattr(module, exported_name)] += 1

    missing = sorted(name for name, value in expected.items() if owners[value] == 0)
    duplicated = sorted(name for name, value in expected.items() if owners[value] > 1)
    unexpected = sorted(
        repr(value) for value in owners if value not in set(expected.values())
    )
    assert missing == []
    assert duplicated == []
    assert unexpected == []


def test_compatibility_proxy_does_not_rebind_test_classes() -> None:
    proxy = importlib.import_module("plugins.crypto_guard.tests.test_smoke")
    rebound = [
        name
        for name, value in vars(proxy).items()
        if inspect.isclass(value) and name.startswith("Test")
    ]
    assert rebound == []

    marker_names: set[str] = set()
    for module_name in DOMAIN_MODULES:
        module = importlib.import_module(module_name)
        for mark in getattr(module, "pytestmark", ()):
            marker_names.add(mark.name)
    assert {"pg", "schema_mutation", "concurrency", "e2e", "slow"} <= marker_names

    fair = importlib.import_module(
        "plugins.crypto_guard.tests.test_fair_scheduler_regressions"
    )
    hard_timeout = (
        fair.Test07_10FairBatchProductionChain
        .test_s4_fair_path_hard_timeout_surfaces_symbol_timeout
    )
    hard_timeout_marks = {
        mark.name for mark in getattr(hard_timeout, "pytestmark", ())
    }
    assert {"serial", "subprocess", "slow"} <= hard_timeout_marks
    large_resp = (
        fair.Test07_10FairBatchProductionChain
        .test_p0_2_large_response_does_not_false_hard_timeout
    )
    large_resp_marks = {
        mark.name for mark in getattr(large_resp, "pytestmark", ())
    }
    assert {"serial", "subprocess", "slow"} <= large_resp_marks
    r4_lease = (
        fair.Test07_10FairBatchProductionChain
        .test_r4_p0_1_legitimate_long_lease_not_prematurely_exhausted
    )
    r4_lease_marks = {
        mark.name for mark in getattr(r4_lease, "pytestmark", ())
    }
    assert {"serial", "slow"} <= r4_lease_marks
