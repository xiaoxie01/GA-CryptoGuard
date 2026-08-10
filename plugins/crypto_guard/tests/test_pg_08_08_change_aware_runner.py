# -*- coding: utf-8 -*-
"""R3 change-aware runner tests (08-08 test feedback loop acceleration).

PURE unit test (no PostgreSQL, no service, no real gate execution): it drives
``run_change_aware.py`` directly — baseline resolution fail-closed, plan
computation, the ``--plan`` no-op contract, ``--tier`` ONLY-EVER-UPGRADE,
the distinct ``full`` (single, cacheable) vs ``final-seal`` (frozen double-run,
never served from cache) semantics, and fail-closed escalation of unknown /
storage / unmapped paths. Real gate execution is stubbed out everywhere; the
only subprocess used is a pytest ``--collect-only`` on a pure-unit file.
"""

import json
import os
from pathlib import Path

import pytest

from plugins.crypto_guard.tests import evidence_store as ev
from plugins.crypto_guard.tests import run_change_aware as rca
from plugins.crypto_guard.tests import run_complete_suite as rcs
from plugins.crypto_guard.tests import sync_mapping as sm

TESTS_DIR = Path(__file__).resolve().parent
MAPPING_PATH = TESTS_DIR / "mapping.json"

RUN_GA = "plugins/crypto_guard/run_ga_workers.py"
MIGRATIONS = "plugins/crypto_guard/storage/migrations.py"
PG_FIXTURES = "plugins/crypto_guard/tests/pg_fixtures.py"
WATCH_E2E = "test_pg_08_08_watch_trigger_order_e2e.py"
UNKNOWN = "plugins/crypto_guard/some_new_file.py"
INERT = ".trellis/tasks/x/planning.md"

pytestmark = pytest.mark.unit

_DUMMY_DB = "postgresql://dummy:dummy@127.0.0.1/dummy"


class _FakeResult:
    """Stub matching the GateReport contract main() reads after a gate."""
    returncode = 0
    node_verdicts = {}
    durations = []


def _mapping() -> dict:
    """RED guard: the manifest MUST exist and be non-empty."""
    assert MAPPING_PATH.exists(), "mapping.json is missing (RED)"
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    assert mapping.get("schema_version") == sm.SCHEMA_VERSION, \
        "mapping.json has no/unknown schema_version"
    return mapping


def _all_test_files_exist(mapping: dict, tests: set[str]) -> None:
    for rel in sorted(tests):
        path = TESTS_DIR / rel
        assert path.exists(), f"selected test file does not exist: {rel}"


# --------------------------------------------------------------------------
# Plan computation (pure, no subprocess)
# --------------------------------------------------------------------------

def test_plan_domain_source_selects_mapped_domain_plus_serial():
    """run_ga_workers.py -> watch_bridge domain incl. the 08-08 watch e2e."""
    mapping = _mapping()
    plan = rca.compute_plan(mapping, [RUN_GA])
    assert plan["tier"] == "domain", plan["reasons"]
    assert WATCH_E2E in plan["tests"], "dynamic edge target missing (RED)"
    _all_test_files_exist(mapping, set(plan["tests"]))
    assert plan["tests"], "domain gate must never select an empty set (RED)"


def test_plan_shared_base_expands_to_all_importers():
    """pg_fixtures.py change selects exactly its AST importer set."""
    mapping = _mapping()
    plan = rca.compute_plan(mapping, [PG_FIXTURES])
    assert plan["tier"] == "domain", plan["reasons"]
    scan = sm.ast_scan()
    assert set(plan["tests"]) == set(scan.helper_importers[PG_FIXTURES]), (
        f"shared base set mismatch\nselected={sorted(plan['tests'])}\n"
        f"AST importers={sorted(scan.helper_importers[PG_FIXTURES])}")


def test_plan_storage_migration_escalates_full():
    """storage/migrations.py gates the WHOLE suite (wide, empty subset)."""
    mapping = _mapping()
    plan = rca.compute_plan(mapping, [MIGRATIONS])
    assert plan["tier"] == "full", plan["reasons"]
    assert not plan["tests"], \
        "full gate runs the whole suite, not a subset (RED)"


def test_plan_unknown_path_escalates_full():
    """An unmapped new source is fail-closed to full, never a silent skip."""
    mapping = _mapping()
    plan = rca.compute_plan(mapping, [UNKNOWN])
    assert plan["tier"] == "full", plan["reasons"]
    assert not plan["tests"]


def test_plan_inert_artifact_is_a_noop():
    """Narrative .trellis artifacts classify to none (evidence no-op)."""
    mapping = _mapping()
    plan = rca.compute_plan(mapping, [INERT])
    assert plan["tier"] == "none" and not plan["tests"], plan["reasons"]


def test_plan_mixed_change_set_upgrades_to_max():
    """Inert never drags down; storage mixed with source upgrades to full."""
    mapping = _mapping()
    plan = rca.compute_plan(mapping, [RUN_GA, INERT, MIGRATIONS])
    assert plan["tier"] == "full", plan["reasons"]


# --------------------------------------------------------------------------
# Baseline resolution fail-closed
# --------------------------------------------------------------------------

def test_unresolvable_default_baseline_returns_none(monkeypatch):
    """No resolvable origin/main -> resolve_change_set returns None (-> full)."""
    monkeypatch.setattr(rca, "default_baseline", lambda: None)
    assert rca.resolve_change_set(None) is None


def test_explicit_baseline_missing_returns_none(monkeypatch):
    """An explicit --changed-from ref that does not exist fails closed."""
    monkeypatch.setattr(
        rca, "_git",
        lambda args: type("R", (), {"returncode": 128, "stdout": "",
                                   "stderr": "unknown revision"})())
    assert rca.resolve_change_set("does-not-exist") is None


def test_changed_paths_since_merges_all_trees(monkeypatch):
    """Committed ∪ staged ∪ unstaged ∪ untracked are all merged, deduped."""
    responses = {
        ("diff", "--name-only", "BASE..HEAD"): "a.py\nb.py\n",
        ("diff", "--cached", "--name-only"): "b.py\nc.py\n",
        ("diff", "--name-only"): "c.py\n",
        ("ls-files", "--others", "--exclude-standard"): "d.py\n",
    }
    monkeypatch.setattr(
        rca, "_git",
        lambda args: type("R", (), {
            "returncode": 0,
            "stdout": responses.get(tuple(args), ""),
            "stderr": "",
        })())
    assert rca.changed_paths_since("BASE") == ["a.py", "b.py", "c.py", "d.py"]


# --------------------------------------------------------------------------
# CLI contracts (gate execution always stubbed)
# --------------------------------------------------------------------------

def _env(monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_GUARD_DATABASE_URL", _DUMMY_DB)


def test_plan_never_executes(monkeypatch, capsys):
    """--plan prints the resolved plan and runs NOTHING (no pytest at all)."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [RUN_GA])
    ran = []

    def _boom(*a, **k):
        ran.append(a)
        raise AssertionError("--plan must not execute a gate")

    monkeypatch.setattr(rca, "_run_gate", _boom)
    monkeypatch.setattr(rcs, "_pytest", _boom)
    assert rca.main(["--plan", "--changed-from", "HEAD"]) == 0
    assert not ran, "--plan executed a gate"
    out = capsys.readouterr().out
    assert "plan_ok nothing_executed=1 tier=domain" in out
    assert f"path {RUN_GA} ->" in out


def test_plan_final_seal_is_dry_run(monkeypatch, capsys):
    """--plan --tier final-seal prints the plan, never runs the double-run."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [RUN_GA])
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    assert rca.main(["--plan", "--tier", "final-seal",
                     "--changed-from", "HEAD"]) == 0
    assert not ran, "--plan --tier final-seal executed a gate"
    assert "plan_ok nothing_executed=1 tier=final-seal" in capsys.readouterr().out


def test_tier_downgrade_of_full_hard_fails_and_executes_nothing(monkeypatch,
                                                                capsys):
    """natural=full + --tier domain|unit -> nonzero exit, NOTHING executes."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [MIGRATIONS])
    for downgrade in ("domain", "unit"):
        ran = []
        monkeypatch.setattr(rca, "_run_gate",
                            lambda *a, **k: ran.append(a) or _FakeResult())
        rc = rca.main(["--tier", downgrade, "--changed-from", "HEAD"])
        assert rc == 2, f"--tier {downgrade} on natural=full must exit nonzero"
        assert not ran, f"--tier {downgrade} executed a gate"
        assert "ERROR" in capsys.readouterr().err


def test_tier_force_is_only_ever_upgrade_ok(monkeypatch, capsys):
    """natural=domain + --tier full is a legal upgrade, plan reflects it."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [RUN_GA])
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    assert rca.main(["--plan", "--tier", "full",
                     "--changed-from", "HEAD"]) == 0
    assert not ran
    assert "plan_ok nothing_executed=1 tier=full" in capsys.readouterr().out


def test_tier_unit_to_domain_widens_to_whole_unit_tier(monkeypatch, capsys):
    """natural=unit + --tier domain over a test file in NO domain FALLS BACK to
    the whole unit tier — never just the changed file, and never an empty
    selection (P2-4: affected-domain widening applies only when a changed path
    actually belongs to a domain; see
    ``test_tier_domain_over_unit_widens_to_affected_domains``)."""
    _env(monkeypatch)
    monkeypatch.setattr(
        rca, "resolve_change_set",
        lambda ref: ["plugins/crypto_guard/tests/"
                     "test_pg_08_08_runner_dependency_proof.py"])
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    assert rca.main(["--plan", "--tier", "domain",
                     "--changed-from", "HEAD"]) == 0
    assert not ran
    out = capsys.readouterr().out
    unit_all = sm.unit_tier_tests()
    assert unit_all, "the unit tier must be non-empty (RED)"
    assert "plan_ok nothing_executed=1 tier=domain" in out
    for name in sorted(unit_all):
        assert name in out, f"--tier domain over unit must include {name}"


def test_tier_domain_over_unit_widens_to_affected_domains(monkeypatch, capsys):
    """P2-4 RED: --tier domain over a natural-unit change set must widen to the
    AFFECTED domains' full coverage (tests + serial), NOT the whole unit tier
    labeled "domain".

    ``test_suite_structure.py`` is unit-marked AND in every domain's tests
    list (round-4 shared-base fold), so it affects all 18 domains. The OLD code
    widened to ``unit_tier_tests()`` — the domains' serial/e2e coverage was
    never executed by a forced-domain gate.
    """
    _env(monkeypatch)
    changed = "plugins/crypto_guard/tests/test_suite_structure.py"
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [changed])
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    assert rca.main(["--plan", "--tier", "domain",
                     "--changed-from", "HEAD"]) == 0
    assert not ran
    out = capsys.readouterr().out
    assert "plan_ok nothing_executed=1 tier=domain" in out
    mapping = sm.load_mapping()
    expanded = sm.expand_domain_tests(mapping)
    affected = rca._affected_domains(mapping, [changed], expanded)
    assert affected, "test_suite_structure.py must affect at least one domain"
    unit_all = sm.unit_tier_tests()
    assert unit_all, "the unit tier must be non-empty (RED)"
    plan_tests = out.split("tests: ", 1)[1].splitlines()[0]
    widen = set(plan_tests.split(",")) if plan_tests else set()
    assert widen != unit_all, (
        "P2-4 RED: forced domain over unit with affected domains must NOT fall "
        "back to the whole unit tier (OLD behavior)")
    for domain in affected:
        expect = expanded[domain] | set(
            mapping["domains"][domain].get("serial", []))
        missing = expect - widen
        assert not missing, (
            f"domain {domain} coverage missing from the forced-domain widen: "
            f"{sorted(missing)}")


def test_full_gate_refuses_zero_collected_nodes(monkeypatch, capsys):
    """Recommended-1 RED: the whole-suite gate must refuse a ZERO collected-node
    set (vacuous GREEN), same as files==[] on unit/domain.

    The OLD ``_run_gate`` ran verify_partition and _run_stages unconditionally:
    if the partition came back empty it would record a GREEN full gate with
    selected=0. Now the gate fails closed (exit 2) and writes nothing.
    """
    _env(monkeypatch)
    monkeypatch.setattr(rca.rcs, "verify_partition",
                        lambda: (set(), set(), set()))
    report = rca._run_gate("full", [], 8, 50)
    assert report.returncode == 2, (
        f"full gate with 0 collected nodes must fail closed (exit 2), got "
        f"returncode={report.returncode}")
    assert "ERROR" in capsys.readouterr().err


def test_unit_gate_refuses_zero_collected_nodes(monkeypatch, capsys):
    """Round-8 P2 RED: the unit tier must ALSO refuse a zero collected-node set.

    ``if not files:`` alone is not enough — a NON-empty file list whose files
    collect no test nodes (a proxy/helper test module) makes
    ``selected_partition`` return ``(set(), set())`` (pytest exit 5 is permitted
    in ``_collect_selected``), so the OLD ``_run_gate`` ran ``_run_stages`` with
    empty stages and returned returncode 0 — a masked vacuous GREEN
    (selected=file-count>0, node_verdicts={}). The gate must fail closed (exit
    2) and write nothing, exactly like the full-tier backstop.
    """
    _env(monkeypatch)
    monkeypatch.setattr(rca, "selected_partition",
                        lambda files: (set(), set()))
    report = rca._run_gate("unit", ["test_proxy_only.py"], 8, 50)
    assert report.returncode == 2, (
        f"unit gate whose selected files collect 0 nodes must fail closed "
        f"(exit 2), got returncode={report.returncode}")
    assert "ERROR" in capsys.readouterr().err


def test_domain_gate_refuses_zero_collected_nodes(monkeypatch, capsys):
    """Round-8 P2 RED: the domain tier must refuse a zero collected-node set."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "selected_partition",
                        lambda files: (set(), set()))
    report = rca._run_gate("domain", ["test_proxy_only.py"], 8, 50)
    assert report.returncode == 2, (
        f"domain gate whose selected files collect 0 nodes must fail closed "
        f"(exit 2), got returncode={report.returncode}")
    assert "ERROR" in capsys.readouterr().err


def test_unresolvable_baseline_fail_closes_to_full(monkeypatch, capsys):
    """An unresolvable baseline escalates the natural tier to full."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: None)
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    assert rca.main(["--plan", "--changed-from", "HEAD"]) == 0
    assert not ran
    result = capsys.readouterr()
    assert "fail_closed unresolvable_baseline -> full" in result.err
    assert "plan_ok nothing_executed=1 tier=full" in result.out


def test_full_gate_is_single_cacheable_run(monkeypatch):
    """natural=full consults the evidence store; reuse short-circuits."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "_ensure_parent_db_ready", lambda: True,
                        raising=False)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [MIGRATIONS])
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    monkeypatch.setattr(rca, "_maybe_reuse_evidence",
                        lambda *a, **k: {"fingerprint": "abc123"})
    assert rca.main(["--changed-from", "HEAD"]) == 0
    assert not ran, "evidence reuse must short-circuit the full run"


def test_final_seal_always_double_runs_never_reuses(monkeypatch):
    """final-seal runs BOTH passes fresh, verifies F1==F2==F3, no cache."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "_ensure_parent_db_ready", lambda: True,
                        raising=False)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [MIGRATIONS])
    calls = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: calls.append(a) or _FakeResult())
    reused = []
    monkeypatch.setattr(rca, "_maybe_reuse_evidence",
                        lambda *a, **k: reused.append(a) or None)
    recorded = []
    monkeypatch.setattr(rca, "_record_evidence",
                        lambda *a, **k: recorded.append(a))
    assert rca.main(["--tier", "final-seal", "--changed-from", "HEAD"]) == 0
    assert len(calls) == 2, \
        f"final-seal must execute the frozen double-run, got {len(calls)}"
    assert not reused, "final-seal must NEVER be served from cache"
    assert len(recorded) == 1, "one BOTH_GREEN record after the double-run"
    fps = recorded[0][1]["fingerprints"]
    assert fps["f1"] == fps["f2"] == fps["f3"], \
        "the frozen double-run must record a verified F1==F2==F3 triple"


def test_final_seal_fingerprint_drift_after_run1_aborts_no_evidence(
        monkeypatch, capsys, tmp_path):
    """F1 != F2 (tree changed mid-run) -> immediate abort, NO green record."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "_ensure_parent_db_ready", lambda: True,
                        raising=False)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [MIGRATIONS])
    calls = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: calls.append(a) or _FakeResult())
    fingerprints = iter(["f1", "f2-drifted"])
    monkeypatch.setattr(rca.ev, "full_fingerprint",
                        lambda *a, **k: next(fingerprints))
    recorded = []
    monkeypatch.setattr(rca, "_record_evidence",
                        lambda *a, **k: recorded.append(a))
    code = rca.main(["--tier", "final-seal", "--changed-from", "HEAD",
                     "--evidence-dir", str(tmp_path)])
    assert code == 3, f"drift after RUN1 must exit nonzero, got {code}"
    assert len(calls) == 1, "RUN2 must NOT start after F1 != F2 drift"
    assert not recorded, "drift must write NO green evidence"
    assert "FINGERPRINT DRIFT" in capsys.readouterr().err


def test_final_seal_fingerprint_drift_after_run2_aborts_no_evidence(
        monkeypatch, capsys, tmp_path):
    """F1 == F2 but F3 != F1 -> abort after RUN2, NO green record."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "_ensure_parent_db_ready", lambda: True,
                        raising=False)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [MIGRATIONS])
    calls = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: calls.append(a) or _FakeResult())
    fingerprints = iter(["f1", "f1", "f3-drifted"])
    monkeypatch.setattr(rca.ev, "full_fingerprint",
                        lambda *a, **k: next(fingerprints))
    recorded = []
    monkeypatch.setattr(rca, "_record_evidence",
                        lambda *a, **k: recorded.append(a))
    code = rca.main(["--tier", "final-seal", "--changed-from", "HEAD",
                     "--evidence-dir", str(tmp_path)])
    assert code == 3, f"drift after RUN2 must exit nonzero, got {code}"
    assert len(calls) == 2, "both passes ran before the F3 drift was caught"
    assert not recorded, "drift must write NO green evidence"


# --------------------------------------------------------------------------
# final-seal per-run hard gate (mock-clock RED/GREEN, no real suite needed)
# --------------------------------------------------------------------------

class _MockClock:
    """Deterministic fake for the runner's ``_clock`` seam (R7-1 pattern)."""

    def __init__(self) -> None:
        self._now = 0.0

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now


def _final_seal_harness(monkeypatch, run_behavior) -> tuple[list, list]:
    """Stub a final-seal main() invocation; return (gate_calls, recorded)."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "_ensure_parent_db_ready", lambda: True,
                        raising=False)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [MIGRATIONS])
    clock = _MockClock()
    monkeypatch.setattr(rca, "_clock", clock)
    calls = []
    fingerprints = iter(["f1", "f1", "f1"])
    monkeypatch.setattr(rca.ev, "full_fingerprint",
                        lambda *a, **k: next(fingerprints))
    recorded = []
    monkeypatch.setattr(rca, "_record_evidence",
                        lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: run_behavior(calls, clock)
                        or _FakeResult())
    return calls, recorded


def test_final_seal_run1_over_hard_gate_aborts_no_run2_no_evidence(
        monkeypatch, capsys, tmp_path):
    """RUN1 > 2400 s (mock clock) -> immediate fail, RUN2 never starts,
    NO green evidence (operator hard gate: each run <= 40 min)."""
    def _slow_on_run1(calls, clock):
        calls.append(1)
        clock.advance(rca.FINAL_SEAL_HARD_GATE_SECONDS + 1)
    calls, recorded = _final_seal_harness(monkeypatch, _slow_on_run1)
    code = rca.main(["--tier", "final-seal", "--changed-from", "HEAD",
                     "--evidence-dir", str(tmp_path)])
    assert code == 4, \
        f"RUN1 over hard gate must exit nonzero, got {code}"
    assert len(calls) == 1, \
        "RUN2 must NOT start after RUN1 exceeds the 40-min hard gate"
    assert not recorded, "hard-gate exceed must write NO green evidence"
    assert "EXCEEDED hard gate" in capsys.readouterr().err


def test_final_seal_run2_over_hard_gate_aborts_no_evidence(
        monkeypatch, capsys, tmp_path):
    """RUN2 > 2400 s (mock clock) -> fail, NO green evidence recorded."""
    def _slow_on_run2(calls, clock):
        calls.append(1)
        if len(calls) == 2:
            clock.advance(rca.FINAL_SEAL_HARD_GATE_SECONDS + 1)
    calls, recorded = _final_seal_harness(monkeypatch, _slow_on_run2)
    code = rca.main(["--tier", "final-seal", "--changed-from", "HEAD",
                     "--evidence-dir", str(tmp_path)])
    assert code == 4, \
        f"RUN2 over hard gate must exit nonzero, got {code}"
    assert len(calls) == 2, \
        "both passes ran but RUN2 exceeded the 40-min hard gate"
    assert not recorded, "hard-gate exceed must write NO green evidence"


def test_final_seal_success_records_per_run_elapsed(monkeypatch, capsys,
                                                    tmp_path):
    """A BOTH_GREEN final-seal record carries run_elapsed_seconds=[run1, run2]."""
    def _run(calls, clock):
        calls.append(1)
        clock.advance(100.0)  # each run ~100 s, well under 2400 s
    calls, recorded = _final_seal_harness(monkeypatch, _run)
    code = rca.main(["--tier", "final-seal", "--changed-from", "HEAD",
                     "--evidence-dir", str(tmp_path)])
    assert code == 0
    assert len(calls) == 2, "a green final-seal runs both passes"
    assert len(recorded) == 1, "one BOTH_GREEN record after the double-run"
    plan = recorded[0][1]
    assert plan["run_elapsed_seconds"] == [100.0, 100.0], plan
    assert plan["fingerprints"]["f1"] == plan["fingerprints"]["f2"] == \
        plan["fingerprints"]["f3"]
    out = capsys.readouterr().out
    assert "run_elapsed_seconds=[100.0, 100.0]" in out
    assert "BOTH_GREEN" in out


@pytest.mark.parametrize("tier", ["full", "final-seal"])
def test_full_final_seal_refuse_run_when_db_unprobeable(tier, monkeypatch,
                                                        capsys, tmp_path):
    """An unreachable test DB fails the gate BEFORE the suite runs."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "_ensure_parent_db_ready", lambda: False,
                        raising=False)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [MIGRATIONS])
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    recorded = []
    monkeypatch.setattr(rca, "_record_evidence",
                        lambda *a, **k: recorded.append(a))
    code = rca.main(["--tier", tier, "--changed-from", "HEAD",
                     "--evidence-dir", str(tmp_path)])
    assert code == 2, f"{tier} must refuse to run when the DB is unprobeable"
    assert not ran, "gate must fail BEFORE running the suite"
    assert not recorded, "a refused gate must write NO evidence record"
    assert "ERROR" in capsys.readouterr().err


def test_bootstrap_failure_never_echoes_secret(monkeypatch, capsys):
    """A bootstrap exception body may carry DSN/connection text — stderr must
    surface only the exception TYPE, never str(exc) (P2, secret redaction)."""
    monkeypatch.delenv("CRYPTO_GUARD_DATABASE_URL", raising=False)
    import plugins.crypto_guard.tests._pg_bootstrap as boot

    class _LeakyConnectionError(RuntimeError):
        pass

    def _boom():
        raise _LeakyConnectionError(
            "connection to server at postgresql://app:SUPERSECRET@db:5432/leak "
            "failed: password authentication failed")

    monkeypatch.setattr(boot, "app_dsn", _boom)
    assert rca._ensure_parent_db_ready() is False
    err = capsys.readouterr().err
    assert "SUPERSECRET" not in err, "the DSN password must never reach stderr"
    assert "postgresql://" not in err, "raw DSN text must never reach stderr"
    assert "_LeakyConnectionError" in err, "the exception type is still surfaced"


def test_evidence_output_path_excluded_from_change_set(monkeypatch):
    """Self-generated evidence output never enters change classification."""
    responses = {
        ("diff", "--name-only", "BASE..HEAD"): "a.py\n",
        ("diff", "--cached", "--name-only"): "",
        ("diff", "--name-only"): "",
        ("ls-files", "--others", "--exclude-standard"):
            "plugins/crypto_guard/tests/.evidence/evidence.jsonl\n",
    }
    monkeypatch.setattr(
        rca, "_git",
        lambda args: type("R", (), {
            "returncode": 0,
            "stdout": responses.get(tuple(args), ""),
            "stderr": "",
        })())
    assert rca.changed_paths_since("BASE") == ["a.py"]


def test_single_gate_record_green_with_manifest_durations_verdicts(
        tmp_path, monkeypatch):
    """A single unit/domain/full gate records GREEN and the record carries the
    test manifest hash, per-node verdicts and durations (rework P1/P2)."""
    _env(monkeypatch)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [RUN_GA])

    class _FakeGate:
        returncode = 0
        node_verdicts = {"test_pg_a.py::test_x": "passed"}
        durations = [{"seconds": 1.5, "phase": "call",
                      "node": "test_pg_a.py::test_x"}]

    monkeypatch.setattr(rca, "_run_gate", lambda *a, **k: _FakeGate())
    monkeypatch.setattr(rca, "_maybe_reuse_evidence", lambda *a, **k: None)
    code = rca.main(["--changed-from", "HEAD", "--evidence-dir",
                     str(tmp_path)])
    assert code == 0
    recs = ev.load_records(tmp_path)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["gate"] == "domain", rec
    assert rec["result"] == "GREEN", \
        "single run records GREEN, not BOTH_GREEN (P2)"
    assert rec["test_manifest_hash"]
    assert rec["node_verdicts"] == _FakeGate.node_verdicts
    assert rec["durations"] == _FakeGate.durations


# --------------------------------------------------------------------------
# Selected-node partition (only a pytest --collect-only on a pure-unit file)
# --------------------------------------------------------------------------

def test_selected_partition_is_exact_for_unit_file():
    """A pure-unit file partitions all-parallel with zero overlap."""
    parallel, serial = rca.selected_partition(
        ["test_pg_08_08_runner_dependency_proof.py"])
    assert parallel, "expected collectable parallel nodes"
    assert not serial, "pure-unit file must have no serial nodes"
    assert not (parallel & serial)


# --------------------------------------------------------------------------
# Fresh-reviewer P2 closures (must all pass before the single valid final-seal)
# --------------------------------------------------------------------------

def test_plan_requires_no_db_credentials(monkeypatch, capsys):
    """P2-3 RED: ``--plan`` executes NOTHING, so it must not demand DB creds.

    The OLD credentials gate (admin password or test-DB URL) ran BEFORE the
    ``--plan`` branch, so a read-only change query failed on machines with no
    configured test DB. ``--plan`` is exempted: it resolves the change set (git
    only) and prints the plan.
    """
    monkeypatch.delenv("CRYPTO_GUARD_DB_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("CRYPTO_GUARD_DATABASE_URL", raising=False)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [RUN_GA])
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    monkeypatch.setattr(rcs, "_pytest", lambda *a, **k: ran.append(a))
    assert rca.main(["--plan", "--changed-from", "HEAD"]) == 0
    assert not ran, "--plan without DB credentials must not execute a gate"
    assert "plan_ok nothing_executed=1" in capsys.readouterr().out


def test_parent_ready_refused_by_dead_db_despite_version_override(
        monkeypatch, capsys):
    """P2-5 RED: CRYPTO_GUARD_POSTGRES_VERSION must NOT satisfy the refusal.

    The OLD ``_ensure_parent_db_ready`` checked ``postgres_version()``, which
    returns the env override verbatim — so setting the override made a gate run
    even when the real server was unreachable (a record then carried a version
    nobody proved). The refusal must be decided by a REAL live probe.

    Round-3 P2-1: the probe result is read through the module-level
    ``_SERVER_VERSION_CACHE``, so that cache is ISOLATED here — a real probe
    cached by an earlier test in the same process (e.g. a ``make_record`` test)
    would otherwise make this assertion order-dependent. A DSN is present and
    the real ``_publish_test_dsn`` succeeds via the already-set branch, so the
    probe path is genuinely reached (non-vacuous): the refusal must fail on a
    None probe, not on bootstrap failing to start.
    """
    monkeypatch.setenv("CRYPTO_GUARD_DATABASE_URL", _DUMMY_DB)
    monkeypatch.setenv("CRYPTO_GUARD_POSTGRES_VERSION", "16.99")
    # Isolate the module cache so an earlier real probe cannot leak in.
    monkeypatch.setattr(rca.ev, "_SERVER_VERSION_CACHE", {})
    # The REAL probe of the (dummy) DSN -> None (fail-closed).
    monkeypatch.setattr(rca.ev, "_server_version_num", lambda dsn: None,
                        raising=False)
    assert rca._ensure_parent_db_ready() is False


def test_unit_gate_publishes_test_dsn_and_reuses(monkeypatch, capsys,
                                                 tmp_path):
    """P2-4 RED: unit/domain gates must publish the test DSN + probe PG version.

    In a password-only setup the OLD runner never published
    ``CRYPTO_GUARD_DATABASE_URL`` for unit/domain gates, so ``postgres_version()``
    stayed None and the reuse query refused every hit — unit/domain evidence
    reuse was dead. The fix publishes the DSN (bootstrap) and probes the real
    version before the reuse query, so an identical second gate reuses evidence.
    """
    monkeypatch.delenv("CRYPTO_GUARD_DATABASE_URL", raising=False)
    monkeypatch.setenv("CRYPTO_GUARD_DB_ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [RUN_GA])

    published = []

    def _fake_publish():
        # Simulate a successful bootstrap: publish the test DSN.
        os.environ["CRYPTO_GUARD_DATABASE_URL"] = _DUMMY_DB
        published.append(1)
        return True

    monkeypatch.setattr(rca, "_publish_test_dsn", _fake_publish, raising=False)
    monkeypatch.setattr(rca.ev, "postgres_version", lambda: "16.99",
                        raising=False)
    monkeypatch.setattr(rca.ev, "_server_version_num", lambda dsn: "16.99",
                        raising=False)

    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    code1 = rca.main(["--changed-from", "HEAD", "--evidence-dir",
                      str(tmp_path)])
    assert code1 == 0
    assert published, "unit gate must publish the test DSN before reuse (P2-4)"
    assert len(ran) == 1, "first unit gate must execute"
    code2 = rca.main(["--changed-from", "HEAD", "--evidence-dir",
                      str(tmp_path)])
    assert code2 == 0
    assert len(ran) == 1, "identical unit gate must reuse evidence, not run"
    assert "evidence_reused tier=domain" in capsys.readouterr().out


def test_inert_change_noops_without_db_credentials(monkeypatch, capsys):
    """Round-4 Finding A RED: an inert change set no-ops WITHOUT credentials.

    The OLD credentials gate ran BEFORE tier resolution, so a credential-less
    machine hit ``parser.error`` on a purely inert change set instead of the
    evidence no-op. A ``none``-tier gate executes NOTHING, so it must not
    demand DB credentials it never uses.
    """
    monkeypatch.delenv("CRYPTO_GUARD_DB_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("CRYPTO_GUARD_DATABASE_URL", raising=False)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [INERT])
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    assert rca.main(["--changed-from", "HEAD"]) == 0
    assert not ran, "an inert change must no-op without executing a gate"
    out = capsys.readouterr().out
    assert "gate_ok tier=none selected=0" in out


def _forced_over_inert(tier: str, monkeypatch, capsys, tmp_path):
    """Drive ``--tier <tier>`` over an inert change set; return (rc, gate_calls).

    Uses an isolated evidence dir (like every other gate-executing test) so a
    prior run's GREEN record can never be reused and short-circuit the gate.
    """
    _env(monkeypatch)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [INERT])
    monkeypatch.setattr(rca.ev, "postgres_version", lambda: "16.99",
                        raising=False)
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    rc = rca.main(["--tier", tier, "--changed-from", "HEAD",
                   "--evidence-dir", str(tmp_path)])
    return rc, ran


def test_forced_unit_over_inert_widens_to_unit_tier(monkeypatch, capsys,
                                                    tmp_path):
    """Round-4 Finding C RED: ``--tier unit`` over natural ``none`` must run the
    WHOLE unit tier — the OLD code kept the selection empty, ran 0 tests and
    recorded a vacuous GREEN with selected=0."""
    unit_all = sm.unit_tier_tests()
    assert unit_all, "the unit tier must be non-empty (RED)"
    rc, ran = _forced_over_inert("unit", monkeypatch, capsys, tmp_path)
    assert rc == 0
    assert len(ran) == 1, "forced unit over none must execute exactly one gate"
    called_tier, files = ran[0][0], ran[0][1]
    assert called_tier == "unit"
    assert set(files) == unit_all, (
        f"forced unit over none must run the whole unit tier, "
        f"got {len(files)} files (vacuous GREEN if 0)")


def test_forced_domain_over_inert_widens_to_unit_tier(monkeypatch, capsys,
                                                      tmp_path):
    """Round-4 Finding C RED: ``--tier domain`` over natural ``none`` likewise
    widens to a NON-EMPTY set — never selected=0 / vacuous GREEN."""
    unit_all = sm.unit_tier_tests()
    assert unit_all, "the unit tier must be non-empty (RED)"
    rc, ran = _forced_over_inert("domain", monkeypatch, capsys, tmp_path)
    assert rc == 0
    assert len(ran) == 1, "forced domain over none must execute exactly one gate"
    called_tier, files = ran[0][0], ran[0][1]
    assert called_tier == "domain"
    assert set(files) == unit_all, (
        f"forced domain over none must run a non-empty set, "
        f"got {len(files)} files (vacuous GREEN if 0)")


def test_forced_unit_over_inert_plan_shows_widened_set(monkeypatch, capsys):
    """Round-4 Finding C: ``--plan --tier unit`` over natural none must PRINT
    the widened unit tier (plan reflects the executed coverage)."""
    unit_all = sm.unit_tier_tests()
    assert unit_all
    _env(monkeypatch)
    monkeypatch.setattr(rca, "resolve_change_set", lambda ref: [INERT])
    ran = []
    monkeypatch.setattr(rca, "_run_gate",
                        lambda *a, **k: ran.append(a) or _FakeResult())
    rc = rca.main(["--plan", "--tier", "unit", "--changed-from", "HEAD"])
    assert rc == 0
    assert not ran, "--plan must execute nothing"
    out = capsys.readouterr().out
    assert "plan_ok nothing_executed=1 tier=unit" in out
    for name in sorted(unit_all):
        assert name in out, f"--tier unit over none must plan {name}"


def test_stage_counts_ignores_application_log_failed_skipped_phrases():
    r"""RED (final-seal -rA false-positive): app-log ``<digits> failed`` /
    ``<digits> skipped`` phrases must NOT count as test outcomes.

    ``run_change_aware._run_stages`` runs the full suite with ``-rA``, which
    appends EVERY test's captured output — including application log records —
    to the stage report. Fail-closed batch tests emit ``..._1783641599999
    failed identity contract`` and ``enabled=10 queued=10 skipped=0``; the
    UNANCHORED ``(\d+) failed|skipped`` scan summed those into a false
    ``full:parallel stage was not exact`` (``failed=3567283199998``) and
    aborted RUN1 of the valid final-seal with no evidence. The ``passed`` scan
    was always anchored; ``failed``/``skipped``/``deselected`` now are too.
    """
    combined = (
        "2026-08-10T06:41:41Z ERROR [MainThread] crypto_guard.worker: "
        "process_fair_batch: malformed scheduled_market_analysis job id=1 "
        "batch=15m:r8a_worker_swapped_1783641599999 failed identity contract\n"
        "2026-08-10T06:45:21Z WARNING [MainThread] crypto_guard.scheduler: "
        "enqueue_market_analysis: batch 15m:1699999199999 seal failed "
        "(exact-set validation) - rolled back, ok=False. "
        "enabled=10 queued=10 skipped=0\n"
        # 终审返工 P2 (08-10): plain app-log prose that must NEVER count as test
        # outcomes — "<digits> failed jobs", "<digits> skipped records".
        "2026-08-10T06:50:00Z INFO [MainThread] crypto_guard.worker: "
        "10 failed jobs and 3 skipped records in the 5m drain window\n"
        "2026-08-10T06:51:00Z INFO [MainThread] crypto_guard.worker: "
        "bootstrap reconciliation left 3 skipped records pending\n"
        "============================ "
        "2120 passed, 10 subtests passed in 1433.60s "
        "============================\n"
    )
    passed, nonzero = rcs._stage_counts(combined)
    assert passed == 2120
    assert nonzero == {"failed": 0, "skipped": 0, "deselected": 0}, (
        "app-log '<digits> failed'/'<digits> skipped' phrases must not "
        "false-flag a green stage as inexact")


def test_stage_counts_still_flags_real_summary_failures():
    r"""The anchored count scan must still catch a GENUINE summary banner
    failure (pytest exit 0 never happens with one, but the exactness guard is
    belt-and-suspenders against a plugin quirk / misparse)."""
    combined = (
        "============================ "
        "2 failed, 2146 passed, 3 skipped, 1 deselected in 1420.00s "
        "============================\n"
    )
    passed, nonzero = rcs._stage_counts(combined)
    assert passed == 2146
    assert nonzero == {"failed": 2, "skipped": 3, "deselected": 1}


def test_stage_counts_parses_real_pytest_summary_formats():
    r"""终审返工 P2 (08-10): pin the REAL pytest banner shapes the final-seal
    double-run produces — NO ``====`` wrapper, ``(H:MM:SS)`` clock suffix,
    optional ``N subtests passed`` token, and ``N failed`` BEFORE the ``passed``
    token — plus the last-complete-banner-wins rule. A parser regression here
    would fail-closed the re-run (``(-1, zeros)``) even on a fully green tree.
    """
    # Real RUN1/RUN2 parallel + serial banners from the valid double-run
    # (bowz2i4ab), and the real failed-run banner from the RUN2 abort.
    cases = [
        ("2122 passed, 10 subtests passed in 1393.62s (0:23:13)",
         2122, {"failed": 0, "skipped": 0, "deselected": 0}),
        ("28 passed in 187.31s (0:03:07)",
         28, {"failed": 0, "skipped": 0, "deselected": 0}),
        ("2122 passed, 10 subtests passed in 1463.60s (0:24:23)",
         2122, {"failed": 0, "skipped": 0, "deselected": 0}),
        ("1 failed, 2121 passed, 10 subtests passed in 1366.03s (0:22:46)",
         2121, {"failed": 1, "skipped": 0, "deselected": 0}),
    ]
    for banner, exp_passed, exp_nonzero in cases:
        passed, nonzero = rcs._stage_counts(banner + "\n")
        assert passed == exp_passed, banner
        assert nonzero == exp_nonzero, banner
    # Last complete banner wins: the serial banner appended after the parallel
    # one must be the parsed summary (28, not 2122) - and vice-versa.
    combined = (
        "2122 passed, 10 subtests passed in 1393.62s (0:23:13)\n"
        "28 passed in 187.31s (0:03:07)\n"
    )
    passed, nonzero = rcs._stage_counts(combined)
    assert passed == 28
    assert nonzero == {"failed": 0, "skipped": 0, "deselected": 0}
    combined = (
        "28 passed in 187.31s (0:03:07)\n"
        "2122 passed, 10 subtests passed in 1393.62s (0:23:13)\n"
    )
    passed, nonzero = rcs._stage_counts(combined)
    assert passed == 2122
    assert nonzero == {"failed": 0, "skipped": 0, "deselected": 0}
    # A lone app-log line is NOT a complete banner -> fail-closed (-1, zeros).
    passed, nonzero = rcs._stage_counts(
        "INFO 10 failed jobs and 3 skipped records in the 5m drain window\n")
    assert passed == -1
    assert nonzero == {"failed": 0, "skipped": 0, "deselected": 0}
