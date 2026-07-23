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
BASELINE_TEST_DEFINITION_COUNT = 1205
BASELINE_TEST_DEFINITION_SHA256 = (
    "e178779e6c1d60f5415c0474540118cac0982374dede8258ef3e236543f6bfe1"
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
