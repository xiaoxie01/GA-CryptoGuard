# -*- coding: utf-8 -*-
"""R3 affected-runner cross-module dependency proof (08-08 test feedback loop).

This is a PURE unit test: it exercises the source -> test/marker mapping
manifest (``mapping.json``) and the AST-import scanner (``sync_mapping.py``)
directly. No PostgreSQL, no service, no subprocess.

RED-first contract (implement.md Step 1): if ``mapping.json`` is missing or
empty, EVERY dependency-proof assertion below fails (the manifest does not
exist -> no source maps to any test -> no drift guard can pass).
"""

import json
from pathlib import Path

import pytest

from plugins.crypto_guard.tests import sync_mapping as sm

TESTS_DIR = Path(__file__).resolve().parent
MAPPING_PATH = TESTS_DIR / "mapping.json"

# Real, on-disk source modules (verified by the AST scan).
RUN_GA = "plugins/crypto_guard/run_ga_workers.py"
MIGRATIONS = "plugins/crypto_guard/storage/migrations.py"
PG_FIXTURES = "plugins/crypto_guard/tests/pg_fixtures.py"
WATCH_E2E = "test_pg_08_08_watch_trigger_order_e2e.py"

pytestmark = pytest.mark.unit


def _manifest_or_fail() -> dict:
    """RED guard: the manifest MUST exist and be non-empty."""
    assert MAPPING_PATH.exists(), "mapping.json is missing (RED)"
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    assert mapping.get("schema_version") == sm.SCHEMA_VERSION, \
        "mapping.json has no/unknown schema_version"
    assert mapping.get("domains"), "mapping.json has no domains (RED)"
    return mapping


def _domain_for(mapping: dict, source: str) -> str:
    for domain, entry in mapping["domains"].items():
        if source in entry["sources"]:
            return domain
    raise AssertionError(f"{source} is not mapped to any domain (RED)")


def test_manifest_exists_and_drift_guard_green():
    """Drift guard GREEN: mapping digest matches the AST scan exactly."""
    mapping = _manifest_or_fail()
    errors = sm.check_drift(mapping, sm.ast_scan())
    assert not errors, "drift guard RED:\n" + "\n".join(errors)


def test_changed_source_selects_every_mapped_domain_test():
    """A real changed source resolves to its full mapped domain test set."""
    mapping = _manifest_or_fail()
    domain = _domain_for(mapping, RUN_GA)
    tier, tests, reason = sm.classify_path(mapping, RUN_GA)
    assert tier == "domain", f"expected domain, got {tier}: {reason}"
    expected = sm.expand_domain_tests(mapping)[domain]
    expected |= set(mapping["domains"][domain].get("serial", []))
    assert tests == expected, (
        f"selected set != mapped domain set\nselected={sorted(tests)}\n"
        f"mapped={sorted(expected)}")
    # The full AST importer closure is covered (superset guarantee).
    scan = sm.ast_scan()
    assert set(scan.source_importers[RUN_GA]) <= tests


def test_shared_base_change_expands_to_all_importing_tests():
    """pg_fixtures.py change selects exactly every test that imports it."""
    mapping = _manifest_or_fail()
    tier, tests, reason = sm.classify_path(mapping, PG_FIXTURES)
    assert tier == "domain", f"expected domain, got {tier}: {reason}"
    scan = sm.ast_scan()
    assert tests == set(scan.helper_importers[PG_FIXTURES]), (
        f"shared base set mismatch\nselected={sorted(tests)}\n"
        f"AST importers={sorted(scan.helper_importers[PG_FIXTURES])}")


def test_dynamic_import_edge_includes_watch_e2e():
    """run_ga_workers::handle_opportunity_watch_recheck -> watch e2e selected."""
    mapping = _manifest_or_fail()
    key = RUN_GA + "::handle_opportunity_watch_recheck"
    assert key in mapping.get("dynamic_imports", {}), \
        f"dynamic_imports edge {key} missing (RED)"
    tier, tests, _ = sm.classify_path(mapping, RUN_GA)
    assert WATCH_E2E in tests, \
        "dynamic edge target not in the selected set (RED)"


def test_revert_fail_deleted_mapping_entry_escalates_never_zero():
    """Delete a real mapping entry -> escalate to domain, never zero tests."""
    mapping = _manifest_or_fail()
    domain = _domain_for(mapping, RUN_GA)
    modified = json.loads(json.dumps(mapping))
    modified["domains"][domain]["sources"] = [
        src for src in modified["domains"][domain]["sources"]
        if src != RUN_GA
    ]
    tier, tests, reason = sm.classify_path(modified, RUN_GA)
    assert tier == "domain", \
        f"expected domain escalation after entry deletion, got {tier}: {reason}"
    assert tests, "escalation must never select an empty test set (RED)"
    scan = sm.ast_scan()
    assert set(scan.source_importers[RUN_GA]) <= tests


def test_repo_schema_storage_path_escalates_to_full():
    """migrations / repository / schema SQL always gate the WHOLE suite."""
    mapping = _manifest_or_fail()
    for path in (
        MIGRATIONS,
        "plugins/crypto_guard/storage/repository.py",
        "plugins/crypto_guard/storage/pg_db.py",
        "plugins/crypto_guard/schemas/schema.sql",
    ):
        tier, tests, reason = sm.classify_path(mapping, path)
        assert tier == "full", f"{path} -> {tier} (expected full): {reason}"
        assert not tests, \
            f"full gate must run the whole suite, not a subset: {path}"


def test_tier_force_is_only_ever_upgrade():
    """--tier may only upgrade; a downgrade of a natural tier hard-fails."""
    mapping = _manifest_or_fail()
    natural, _, _ = sm.aggregate_classification(mapping, [RUN_GA])
    assert natural == "domain"
    with pytest.raises(ValueError):
        sm.apply_tier(natural, "unit")          # downgrade -> hard error
    with pytest.raises(ValueError):
        sm.apply_tier(natural, "none")
    assert sm.apply_tier(natural, None) == "domain"
    assert sm.apply_tier(natural, "full") == "full"
    assert sm.apply_tier(natural, "final-seal") == "final-seal"


def test_inert_artifact_is_a_noop_not_a_gate():
    """Narrative .trellis artifacts classify to none (evidence-reuse no-op)."""
    mapping = _manifest_or_fail()
    tier, tests, reason = sm.classify_path(
        mapping, ".trellis/tasks/x/planning.md")
    assert tier == "none" and not tests, f"got {tier}: {reason}"
    # Mixed change set: inert path never drags the gate down.
    natural, tests, _ = sm.aggregate_classification(
        mapping, [RUN_GA, ".trellis/tasks/x/planning.md"])
    assert natural == "domain" and tests


def test_mapping_digest_is_stable_across_syncs():
    """P2-1 RED: ``--sync`` twice on an identical mapping must NOT change digest.

    The OLD digest was self-referential: it hashed the manifest INCLUDING the
    stored ``digest`` key, so every ``--sync`` rewrote the stored value and a
    second sync produced a DIFFERENT digest (evidence invalidated for no
    reason). Excluding the key makes the digest a stable fixed point.
    """
    mapping = _manifest_or_fail()
    scan = sm.ast_scan()
    synced1 = sm._sync(mapping, scan)
    synced2 = sm._sync(synced1, scan)
    assert synced1["digest"] == synced2["digest"], (
        "digest changed across identical --sync (self-referential); "
        f"1={synced1['digest']} 2={synced2['digest']}")


def test_check_drift_reports_corrupted_stored_digest():
    """P2-1 RED: a hand-corrupted stored digest must fail the drift guard.

    The OLD ``check_drift`` never compared the stored digest to the recomputed
    one, so a stale/corrupt digest passed. Now a mismatch is a hard error:
    evidence reuse must key on a reproducible fingerprint.
    """
    mapping = _manifest_or_fail()
    modified = json.loads(json.dumps(mapping))
    modified["digest"] = "0" * 64  # corrupt the stored value
    errors = sm.check_drift(modified, sm.ast_scan())
    assert any("digest mismatch" in e for e in errors), (
        f"check_drift did not report corrupted digest:\n" + "\n".join(errors))


def _unfolded_direct_importers() -> dict:
    """Replicate the OLD scanner: walk ``test_*.py`` WITHOUT the shared-base fold.

    Round-4 Finding B RED-core. The fold (``_fold_shared_base_deps``) is the
    ONLY mechanism that makes a shared-base-only source visible, so this
    reconstruction must MISS such a source — the regression asserts exactly
    that, proving the fold is load-bearing.
    """
    direct: dict[str, set[str]] = {}
    for test in sorted(sm.TESTS_DIR.glob("test_*.py")):
        for module in sm._collect_modules(test):
            path = sm.module_to_source_path(module)
            if path is None:
                continue
            rel = path.relative_to(sm.REPO_ROOT).as_posix()
            if rel.startswith(sm.PKG_PATH + "/tests/"):
                continue  # helper file, not a package source
            direct.setdefault(rel, set()).add(test.name)
    return direct


def test_shared_base_only_source_folds_smoke_dependents():
    """Round-4 Finding B RED: a source imported by NO ``test_*.py`` directly
    but by the shared ``_smoke_suite.py`` must fold its transitive test
    dependents into ``source_importers``.

    The OLD scan walked only ``test_*.py``, so ``config/__init__.py`` —
    reached ONLY via ``_smoke_suite`` — had an EMPTY importer set, and a
    change to it selected ZERO tests (silent false-negative in the
    change-aware runner's core promise). The fold must make every smoke
    dependent visible, and the drift guard must stay green.
    """
    src = "plugins/crypto_guard/config/__init__.py"
    smoke = "plugins/crypto_guard/tests/_smoke_suite.py"
    scan = sm.ast_scan()

    # The folded set must be non-empty and cover every smoke dependent.
    importers = scan.source_importers.get(src, set())
    assert importers, \
        f"fold must populate source_importers[{src}] (RED: empty after fold)"
    smoke_dependents = set(scan.helper_importers.get(smoke, set()))
    assert smoke_dependents, "no tests import _smoke_suite (test is vacuous)"
    missing_without_fold = smoke_dependents - _unfolded_direct_importers().get(
        src, set())
    assert missing_without_fold, (
        "every smoke dependent imports src directly — the fold is no longer "
        "the source of visibility, regression is vacuous (RED)")
    assert smoke_dependents <= importers, (
        "fold did not carry the smoke dependents into "
        f"source_importers[{src}]\n"
        f"smoke_dependents={sorted(smoke_dependents)}\n"
        f"importers={sorted(importers)}")

    # Drift guard stays green with the folded edges.
    mapping = _manifest_or_fail()
    errors = sm.check_drift(mapping, scan)
    assert not errors, "drift guard RED after fold:\n" + "\n".join(errors)


def test_package_form_import_edge_captured():
    """P2-2 RED: package-form ``from plugins.crypto_guard.tests import
    pg_fixtures as fx`` must record the real leaf edge.

    The package root ``plugins.crypto_guard.tests`` has NO ``__init__.py`` (a
    namespace package), so the OLD scanner resolved the module path to nothing
    and silently dropped the edge — test_pg_08_08_rollback_isolation.py
    imported pg_fixtures yet never appeared in its importer sets, so a
    pg_fixtures.py change would NOT resolve to it. The leaf edge must be
    captured so shared-base changes expand to every real importer.
    """
    ROLLBACK_ISOLATION = "test_pg_08_08_rollback_isolation.py"
    mapping = _manifest_or_fail()
    scan = sm.ast_scan()
    importers = scan.helper_importers[PG_FIXTURES]
    assert ROLLBACK_ISOLATION in importers, (
        "package-form import edge dropped: rollback_isolation not in "
        f"pg_fixtures AST importers={sorted(importers)}")
    # Shared-base stored list must match the AST scan (drift guard).
    stored = mapping.get("shared_base_modules", {}).get(PG_FIXTURES, [])
    assert ROLLBACK_ISOLATION in stored, (
        "rollback_isolation missing from shared_base pg_fixtures list")
    # A synthetic pg_fixtures change resolves to include it (shared base ->
    # every importing test).
    tier, tests, reason = sm.classify_path(mapping, PG_FIXTURES)
    assert ROLLBACK_ISOLATION in tests, (
        f"pg_fixtures change did not select rollback_isolation: {reason}")


# ---------------------------------------------------------------------------
# Round-6 P2-1 (fresh reviewer, 08-10): importlib.import_module dynamic edges
# are invisible to the AST scan AND undeclared. test_suite_structure.py imports
# test_smoke and test_fair_scheduler_regressions via importlib.import_module
# with CONSTANT module names, and 5 more DOMAIN_MODULES via a loop variable, so
# a change to those modules UNDER-RAN the structure test. The scanner must
# capture constant-string import_module edges, classify_path must consult the
# scan + declared dynamic edges in the test-file AND shared-base branches, and
# check_drift must fail when the stored helper list omits a real importer.
# RED-first per round.
# ---------------------------------------------------------------------------

TEST_SMOKE = "plugins/crypto_guard/tests/test_smoke.py"
STRUCTURE = "test_suite_structure.py"


def test_importlib_constant_edge_captured_by_scan_and_drift_closed():
    """Round-6 P2-1 RED: the importlib.import_module constant-string edge
    (test_suite_structure -> test_smoke) must be captured by the AST scan, and
    check_drift must FAIL when the stored test_helpers list omits the consumer.

    RED against the old scanner: the edge was invisible, so the scan reported
    no importer and the drift guard saw no discrepancy on the omission.
    """
    scan = sm.ast_scan()
    assert STRUCTURE in scan.helper_importers.get(TEST_SMOKE, set()), (
        "Round-6 P2-1 RED: importlib.import_module constant edge not captured "
        f"by scan (helper_importers[{TEST_SMOKE}]="
        f"{sorted(scan.helper_importers.get(TEST_SMOKE, set()))})")
    mapping = _manifest_or_fail()
    # Omit the consumer from the stored helper list -> drift MUST report it.
    mapping["test_helpers"][TEST_SMOKE] = [
        t for t in mapping["test_helpers"].get(TEST_SMOKE, [])
        if t != STRUCTURE]
    errors = sm.check_drift(mapping, scan)
    assert any("test helper" in e and "test_smoke.py" in e for e in errors), (
        "Round-6 P2-1 RED: drift guard is blind to the omitted importlib "
        "consumer (check_drift errors: " + ("; ".join(errors) or "none") + ")")


def test_classify_test_smoke_change_selects_importlib_proxy_consumer():
    """Round-6 P2-1 RED: a change to test_smoke.py must select
    test_suite_structure.py (its importlib.import_module proxy consumer).

    test_smoke is a SHARED_BASE_MODULE, so classify_path must consult the AST
    scan in the shared-base branch too — the stored list alone under-runs.
    """
    scan = sm.ast_scan()
    tier, tests, reason = sm.classify_path(
        _manifest_or_fail(), TEST_SMOKE, scan)
    assert tier == "domain", f"expected domain, got {tier}: {reason}"
    assert STRUCTURE in tests, (
        f"Round-6 P2-1 RED: test_smoke change under-runs the proxy consumer "
        f"(selected={sorted(tests)}): {reason}")


def test_classify_domain_module_change_selects_structure_owner():
    """Round-6 P2-1 RED: a change to a DOMAIN_MODULES regression module
    (imported by test_suite_structure via a loop-variable import_module call
    the AST cannot resolve) must select test_suite_structure.py through the
    declared dynamic_imports edge."""
    scan = sm.ast_scan()
    for module in (
            "plugins/crypto_guard/tests/test_core_regressions.py",
            "plugins/crypto_guard/tests/test_paper_orders_regressions.py",
            "plugins/crypto_guard/tests/test_shadow_lifecycle_regressions.py",
            "plugins/crypto_guard/tests/test_hourly_report_regressions.py",
            "plugins/crypto_guard/tests/test_market_data_contracts.py",
            "plugins/crypto_guard/tests/test_llm_decision_regressions.py",
            "plugins/crypto_guard/tests/test_fair_scheduler_regressions.py"):
        tier, tests, reason = sm.classify_path(
            _manifest_or_fail(), module, scan)
        assert STRUCTURE in tests, (
            f"Round-6 P2-1 RED: {module} change under-runs the structure test "
            f"(selected={sorted(tests)}): {reason}")
