# -*- coding: utf-8 -*-
"""Change-aware gate runner (R2, 08-08 test feedback loop acceleration).

Resolves the paths changed since a git baseline to an explicit test set +
gate tier (unit/domain/full/final-seal), reusing ``run_complete_suite.py``
helpers (``_pytest``, ``_run_exact_stage``, ``verify_partition``) by import —
never duplicated.

Semantics (design §1):

* Default ``--changed-from`` = ``merge-base(origin/main, HEAD)``; the changed
  set merges committed + staged + unstaged + untracked paths. An unresolvable
  remote baseline FAILS-CLOSED to the full gate.
* Classification is fail-closed: unknown paths, runner/config files,
  storage/schema/migrations, and boundary files escalate to ``full``; an
  unmapped-but-imported source escalates to ``domain`` (never a silent empty
  set).
* ``--tier`` is ONLY-EVER-UPGRADE: forcing a tier below the natural
  classification is a hard error and NOTHING executes. A forced ``domain``
  gate over a natural ``unit`` change set WIDENS to the AFFECTED domains'
  full coverage (tests + serial); it falls back to the whole unit tier only
  when no changed path belongs to any domain (round-5 P2-4).
* ``full`` is a single cacheable complete-suite run recording ``GREEN``;
  ``final-seal`` is the frozen-tree CONSECUTIVE double-run with a REAL
  ``F1 -> RUN1 -> F2 -> RUN2 -> F3`` fingerprint proof (any inequality aborts
  with exit 3 and writes NO green evidence) and is the ONLY gate that records
  ``BOTH_GREEN``. It is always freshly executed, never from cache. Each
  final-seal run has an operator hard gate: elapsed > 2400 s (40 min) aborts
  immediately with exit 4 and NO green evidence (RUN1 over the gate never
  starts RUN2); the BOTH_GREEN record carries ``run_elapsed_seconds=[run1,
  run2]``.
* Before any full/final-seal gate the parent process safely bootstraps the
  dedicated test DSN and proves a REAL PostgreSQL version is reachable — an
  unprobeable server refuses to run (exit 2) so a record never carries
  ``postgres_version=null``.
* The runner's own evidence output dir is excluded from change classification
  (self-generated files never escalate to ``full``).
* ``--plan`` prints the resolved tier/test list + reasons and executes NOTHING.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from plugins.crypto_guard.tests import evidence_store as ev
from plugins.crypto_guard.tests import run_complete_suite as rcs
from plugins.crypto_guard.tests import sync_mapping as sm

TEST_ROOT = rcs.TEST_ROOT
REPO_ROOT = TEST_ROOT.parents[2]
DEFAULT_BASELINE = "origin/main"
# The runner's own append-only evidence output dir — NEVER allowed to enter
# change classification (self-generated files must not escalate to `full`).
EVIDENCE_DIR = TEST_ROOT / ".evidence"

# Module-level clock seam (R7-1 fake-clock pattern): tests inject a mock clock
# here; production uses the real monotonic perf counter. The final-seal hard
# gate is measured through this seam so it is testable WITHOUT a real suite run.
_clock = time.perf_counter

# Operator-approved same-machine full hard gate: each final-seal run must
# complete within 2400 s (40 min) INDEPENDENTLY. A run exceeding it fails the
# whole final-seal immediately and writes NO green evidence. (The 25-min target
# is a STRETCH goal reserved for hardware providing >= 13 effective CPU cores —
# increasing xdist worker processes cannot create effective cores.)
FINAL_SEAL_HARD_GATE_SECONDS = 2400


# --------------------------------------------------------------------------
# Git change resolution
# --------------------------------------------------------------------------

def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def default_baseline() -> str | None:
    """merge-base(origin/main, HEAD), or None when it cannot be resolved."""
    for probe in (
        ["rev-parse", "--verify", "refs/remotes/origin/main"],
        ["rev-parse", "--verify", "origin/main"],
    ):
        r = _git(probe)
        if r.returncode == 0:
            break
    else:
        return None
    mb = _git(["merge-base", "origin/main", "HEAD"])
    if mb.returncode != 0 or not mb.stdout.strip():
        return None
    return mb.stdout.strip().splitlines()[-1]


def _exclude_evidence_paths(paths: list[str],
                            evidence_dir: Path = EVIDENCE_DIR) -> list[str]:
    """Drop the runner's own evidence output from a change classification.

    The evidence store is append-only runtime log data; its files must NEVER
    enter the change set (which would escalate every future iteration run to
    the unmapped -> full gate). The path is matched against the resolved repo
    prefix so a renamed/cloned tree still excludes it.
    """
    try:
        rel_ev = evidence_dir.resolve().relative_to(
            REPO_ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        rel_ev = f"{TEST_ROOT.name}/.evidence"
    prefix = rel_ev.rstrip("/") + "/"
    return sorted(
        p for p in paths
        if p and p != rel_ev and not p.startswith(prefix))


def changed_paths_since(ref: str) -> list[str]:
    """Committed ∪ staged ∪ unstaged ∪ untracked paths since ``ref``.

    Evidence output generated by the runner itself is excluded so that a
    completed run never re-classifies its own artifacts as changed paths.
    """
    paths: set[str] = set()
    r = _git(["diff", "--name-only", f"{ref}..HEAD"])
    if r.returncode == 0:
        paths |= {ln for ln in r.stdout.splitlines() if ln.strip()}
    for args in (["diff", "--cached", "--name-only"],
                 ["diff", "--name-only"]):
        r = _git(args)
        if r.returncode == 0:
            paths |= {ln for ln in r.stdout.splitlines() if ln.strip()}
    r = _git(["ls-files", "--others", "--exclude-standard"])
    if r.returncode == 0:
        paths |= {ln for ln in r.stdout.splitlines() if ln.strip()}
    return _exclude_evidence_paths(paths)


def resolve_change_set(changed_from: str | None) -> list[str]:
    """Resolve the effective baseline; escalate to FULL when unresolvable.

    Returns the changed paths when the baseline resolves. When it cannot
    (no remote, unborn branch, or explicit ref missing), the caller must
    fail-closed: this returns ``None`` so ``main`` escalates to full.
    """
    if changed_from is not None:
        # Explicit ref: resolve directly.
        r = _git(["rev-parse", "--verify", f"{changed_from}^{{commit}}"])
        if r.returncode != 0:
            return None
        return changed_paths_since(changed_from)
    baseline = default_baseline()
    if baseline is None:
        return None
    return changed_paths_since(baseline)


# --------------------------------------------------------------------------
# Plan computation (pure; no subprocess — fully unit-testable)
# --------------------------------------------------------------------------

def compute_plan(mapping: dict | None = None,
                 changed_paths: list[str] | None = None) -> dict:
    """Resolve a change set to (natural_tier, selected_files, reasons)."""
    mapping = mapping if mapping is not None else sm.load_mapping()
    if changed_paths is None:
        changed_paths = []
    natural, tests, reasons = sm.aggregate_classification(
        mapping, changed_paths)
    return {
        "tier": natural,
        "tests": sorted(tests),
        "reasons": reasons,
        "changed_paths": sorted(changed_paths),
    }


# --------------------------------------------------------------------------
# Selected-node collection + partition (reuses rcs helpers)
# --------------------------------------------------------------------------

def _collect_selected(files: list[str], marker: str | None = None) -> set[str]:
    # Pass the selected file paths through rcs._pytest's node_ids argfile so
    # collection is scoped to exactly those files (a bare call would prepend
    # TEST_ROOT and collect the whole suite).
    selection = {str(TEST_ROOT / f) for f in sorted(files)}
    args = ["--collect-only", "-q"]
    if marker is not None:
        args += ["-m", marker]
    result = rcs._pytest(args, capture=True, node_ids=selection)
    if result.returncode not in (0, 5):
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"collect failed for {len(files)} selected files")
    return rcs._node_ids(result.stdout)


def selected_partition(files: list[str]) -> tuple[set[str], set[str]]:
    """Split selected files into (parallel, serial) node IDs; exact split."""
    parallel = _collect_selected(files, "not serial")
    serial = _collect_selected(files, "serial")
    overlap = parallel & serial
    all_selected = parallel | serial
    # Files with no collectable nodes (e.g. helper proxies) are fine; a
    # non-empty selected set must partition exactly like the full suite.
    if all_selected and (overlap or not parallel and not serial):
        raise RuntimeError(
            "invalid selected partition: "
            f"parallel={len(parallel)} serial={len(serial)} "
            f"overlap={len(overlap)}")
    return parallel, serial


# --------------------------------------------------------------------------
# Execution stages (reuses rcs._run_exact_stage / verify_partition)
# --------------------------------------------------------------------------

@dataclass
class GateReport:
    """Aggregated result of one gate execution (parallel + serial stages).

    ``node_verdicts`` is the compact per-test outcome parsed from the pytest
    ``-rA`` short summary, keyed by exact node id; ``durations`` is the top-N
    slowest list parsed from the ``--durations`` blocks (design §4/§5).
    """
    returncode: int = 0
    node_verdicts: dict = field(default_factory=dict)
    durations: list = field(default_factory=list)


_VERDICT_RE = re.compile(r"^(.*)\s+(PASSED|FAILED|SKIPPED|ERROR)$")


def _parse_node_verdicts(output: str, nodes: set[str]) -> dict[str, str]:
    """Compact per-node outcome from the ``-rA`` short summary."""
    verdicts: dict[str, str] = {}
    for line in output.splitlines():
        m = _VERDICT_RE.match(line.strip())
        if not m:
            continue
        node, verdict = m.group(1).strip(), m.group(2).lower()
        if node in nodes:
            verdicts[node] = verdict
    return verdicts


def _run_gate(tier: str, files: list[str], workers: int,
              durations: int) -> GateReport:
    """Execute one gate; aggregate returncode + node verdicts + durations."""
    if tier in ("full", "final-seal"):
        _all_nodes, parallel, serial = rcs.verify_partition()
        if not parallel and not serial:
            # Recommended-1 (round-5): a zero collected-node set is the SAME
            # vacuous-GREEN risk as files==[] on unit/domain — a whole-suite
            # gate that collected nothing would record GREEN with selected=0.
            # Fail closed: refuse, never fake-green.
            print(f"ERROR: gate tier={tier} collected 0 nodes — refusing "
                  f"vacuous GREEN", file=sys.stderr)
            return GateReport(returncode=2)
        return _run_stages("full", parallel, serial, workers, durations)
    if not files:
        if tier == "none":
            # Inert tier no-ops (main() already returns before a gate; this
            # keeps the gate layer itself honest if ever reached directly).
            print(f"gate_ok tier=none selected=0 passed=0 failed=0 (no-op)")
            return GateReport()
        # Round-4 Finding C backstop: a real unit/domain gate must NEVER run 0
        # tests — an empty selection would record a vacuous GREEN. Fail closed.
        print(f"ERROR: gate tier={tier} selected=0 — refusing vacuous GREEN",
              file=sys.stderr)
        return GateReport(returncode=2)
    parallel, serial = selected_partition(files)
    if not parallel and not serial:
        # Round-8 P2: `files` non-empty is not enough — a proxy/helper test
        # module with no collectable nodes makes selected_partition return
        # (set(), set()) (pytest exit 5 is permitted in _collect_selected), and
        # _run_stages with empty stages would return returncode 0 — a masked
        # vacuous GREEN (selected=file-count>0, node_verdicts={}). Same class
        # as the full-tier Recommended-1 backstop above: fail closed, never
        # fake-green.
        print(f"ERROR: gate tier={tier} collected 0 nodes — refusing "
              f"vacuous GREEN", file=sys.stderr)
        return GateReport(returncode=2)
    return _run_stages(tier, parallel, serial, workers, durations)


def _run_stages(name: str, parallel: set[str], serial: set[str],
                workers: int, durations: int) -> GateReport:
    report = GateReport()
    if parallel:
        stage = rcs._run_exact_stage(
            f"{name}:parallel",
            parallel,
            ["-q", "-rA", "-p", "xdist.plugin", "-n", str(workers),
             "--dist", "worksteal", f"--durations={durations}"],
        )
        combined = f"{stage.stdout}\n{stage.stderr}"
        report.durations += ev.durations_from_output(combined)
        report.node_verdicts.update(_parse_node_verdicts(combined, parallel))
        if stage.returncode != 0:
            report.returncode = stage.returncode
            return report
        # Exactness (passed == len(parallel), zero failed/skipped) proves every
        # selected node passed; fill anything the summary parse missed so the
        # machine record is complete for a green gate.
        for node in parallel:
            report.node_verdicts.setdefault(node, "passed")
    if serial:
        stage = rcs._run_exact_stage(
            f"{name}:serial",
            serial,
            ["-q", "-rA", f"--durations={durations}"],
        )
        combined = f"{stage.stdout}\n{stage.stderr}"
        report.durations += ev.durations_from_output(combined)
        report.node_verdicts.update(_parse_node_verdicts(combined, serial))
        report.returncode = stage.returncode
        for node in serial:
            report.node_verdicts.setdefault(node, "passed")
    return report


# --------------------------------------------------------------------------
# Evidence reuse / recording (Step 3: real .evidence/ JSONL store)
# --------------------------------------------------------------------------

def _maybe_reuse_evidence(gate: str, tier_fingerprint: str,
                          evidence_dir: Path, no_reuse: bool,
                          workers: int) -> dict | None:
    """Consult the .evidence/ store for the gate's own GREEN record."""
    if no_reuse:
        return None
    components = {
        "tier_fingerprint": tier_fingerprint,
        "test_manifest_hash": ev.test_manifest_hash(),
        "mapping_hash": ev.mapping_hash(),
        "pytest_ini_hash": ev.pytest_ini_hash(),
        "python_version": ev.python_version(),
        "postgres_version": ev.postgres_version(),
        "workers": workers,
    }
    return ev.find_reuse(gate, components, evidence_dir)


def _record_evidence(gate: str, plan: dict, workers: int,
                     elapsed: float, verdict: str, evidence_dir: Path,
                     node_verdicts: dict | None = None,
                     durations: list | None = None) -> None:
    """Append one machine-readable JSONL evidence record (design §4/§5)."""
    ev.append_record(
        ev.make_record(gate, plan, workers, elapsed, verdict,
                       node_verdicts=node_verdicts, durations=durations),
        evidence_dir)


def _publish_test_dsn() -> bool:
    """Publish the dedicated test DSN into the environment (idempotent).

    Bootstraps the role/DB through ``_pg_bootstrap.app_dsn`` (admin password
    only, never echoed) when ``CRYPTO_GUARD_DATABASE_URL`` is not already set.
    Returns False only when a bootstrap ATTEMPT itself fails (e.g. no admin
    password). ``None`` from the later version probe is NOT a failure here:
    unit/domain gates may still run with evidence reuse fail-closed (P2-4).
    """
    if os.environ.get("CRYPTO_GUARD_DATABASE_URL"):
        return True
    try:
        from plugins.crypto_guard.tests._pg_bootstrap import app_dsn
        os.environ["CRYPTO_GUARD_DATABASE_URL"] = app_dsn()
        return True
    except Exception as exc:  # noqa: BLE001 - fail BEFORE the suite runs
        # Never echo str(exc) to stderr (P2, redaction): an underlying
        # connection error body may carry DSN / connection text. Only the
        # exception TYPE is safe to surface.
        print(f"ERROR: test DB bootstrap failed: "
              f"{type(exc).__name__}", file=sys.stderr)
        return False


def _ensure_parent_db_ready() -> bool:
    """Parent-process test-DB bootstrap + REAL Postgres version proof.

    Runs BEFORE any full/final-seal gate. Publishes the dedicated test DSN,
    then proves a REAL PostgreSQL version is reachable; when the server cannot
    be reached the gate REFUSES to run — a record must never carry
    ``postgres_version=null``. The env override ``CRYPTO_GUARD_POSTGRES_VERSION``
    NEVER satisfies this refusal (P2-5): the decision is made by
    ``real_postgres_version()``, a live probe. (The override may still satisfy
    the reuse/record query once the gate is running.)
    """
    if not _publish_test_dsn():
        return False
    if ev.real_postgres_version() is None:
        print("ERROR: cannot probe a REAL PostgreSQL version on the test DSN; "
              "refusing to run the suite (never record postgres_version=null)",
              file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _affected_domains(mapping: dict, changed_paths: list[str],
                      expanded: dict[str, set[str]]) -> set[str]:
    """Domains whose test coverage owns any changed path (round-5 P2-4).

    A forced ``domain`` gate over a natural ``unit`` change set must run the
    AFFECTED domains' full coverage (tests + serial), not the whole unit tier
    under a ``domain`` label — the old widen never executed the domains'
    serial/e2e tests. A changed path belongs to a domain when its file name is
    in that domain's expanded test list or its serial list.
    """
    changed_bases = {p.rsplit("/", 1)[-1] for p in (changed_paths or [])}
    affected: set[str] = set()
    for domain, entry in mapping.get("domains", {}).items():
        coverage = expanded.get(domain, set()) | set(entry.get("serial", []))
        if changed_bases & coverage:
            affected.add(domain)
    return affected


def _print_plan(plan: dict) -> None:
    print(f"plan tier={plan['tier']} selected={len(plan['tests'])}")
    for path in plan["changed_paths"]:
        reason = plan["reasons"].get(path, "?")
        print(f"  path {path} -> {reason}")
    print(f"  tests: {','.join(plan['tests']) or '(none)'}")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="run_change_aware")
    parser.add_argument("--changed-from", default=None,
                        help="git ref; default merge-base(origin/main, HEAD)")
    parser.add_argument("--plan", action="store_true",
                        help="print tier/test list + reasons; run NOTHING")
    parser.add_argument("--tier", choices=["unit", "domain", "full",
                                           "final-seal"],
                        help="force a gate tier (ONLY-EVER-UPGRADE)")
    parser.add_argument("--workers", type=int,
                        default=min(8, os.cpu_count() or 4))
    parser.add_argument("--durations", type=int, default=50)
    parser.add_argument("--evidence-dir", type=Path,
                        default=TEST_ROOT / ".evidence")
    parser.add_argument("--no-reuse", action="store_true",
                        help="ignore evidence cache, always execute")
    parser.add_argument("--history", action="store_true",
                        help="print the latency history table; execute NOTHING")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be >= 1")

    if args.history:
        # Read-only query: works without DB credentials, executes nothing.
        print(ev.format_history(ev.load_records(args.evidence_dir)))
        return 0

    mapping = sm.load_mapping()
    if not mapping:
        print("mapping.json missing — run sync_mapping --sync first",
              file=sys.stderr)
        return 2

    changed_paths = resolve_change_set(args.changed_from)
    if changed_paths is not None:
        # Self-generated evidence output must never enter change classification
        # (rework): the runner excludes its configured evidence dir.
        changed_paths = _exclude_evidence_paths(changed_paths,
                                                args.evidence_dir)
    if changed_paths is None:
        # Fail-closed: an unresolvable baseline escalates to the full gate.
        plan = compute_plan(mapping, [])
        natural = "full"
        plan["tier"] = natural
        plan["reasons"] = {"<unresolvable-baseline>":
                           "origin/main or --changed-from unresolvable -> full"}
        print("fail_closed unresolvable_baseline -> full", file=sys.stderr)
    else:
        plan = compute_plan(mapping, changed_paths)
        natural = plan["tier"]

    forced = None
    if args.tier is not None:
        try:
            forced = sm.apply_tier(natural, args.tier)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        plan["tier"] = forced
        if forced == "domain" and natural == "unit":
            # P2-4 (round-5): a forced domain gate over a natural-unit change
            # set runs the AFFECTED domains' full coverage (tests + serial) —
            # NOT the whole unit tier labeled "domain", which never executed
            # the domains' serial/e2e tests. The natural selection is always
            # kept (a widen never drops coverage). Fall back to the whole unit
            # tier ONLY when no changed path belongs to any domain.
            expanded = sm.expand_domain_tests(mapping)
            affected = _affected_domains(mapping, changed_paths, expanded)
            if affected:
                widened = set(plan["tests"])
                for domain in affected:
                    widened |= expanded[domain]
                    widened |= set(
                        mapping["domains"][domain].get("serial", []))
            else:
                widened = set(plan["tests"]) | sm.unit_tier_tests()
            plan["tests"] = sorted(widened)
        elif forced in ("unit", "domain") and natural == "none":
            # Round-4 Finding C: a forced unit/domain gate over an INERT change
            # set (natural none) must run the WHOLE unit tier — the OLD code
            # kept the selection empty, executed 0 tests and recorded a vacuous
            # GREEN with selected=0. The widen keeps the gate label honest;
            # an EMPTY unit tier refuses (fail-closed) rather than fake-green.
            widened = sm.unit_tier_tests()
            if not widened:
                print("ERROR: forced --tier unit/domain over an inert change "
                      "set selects an EMPTY unit tier — refusing vacuous GREEN",
                      file=sys.stderr)
                return 2
            plan["tests"] = sorted(widened)

    tier = plan["tier"]

    if args.plan:
        # Dry-run contract: --plan prints the resolved tier/test list and
        # executes NOTHING — including for final-seal and none.
        _print_plan(plan)
        print(f"plan_ok nothing_executed=1 tier={tier}")
        return 0

    if tier == "none":
        # Inert-only change set: evidence-reuse no-op, nothing executes.
        print(f"gate_ok tier=none selected=0 passed=0 failed=0 (no-op)")
        return 0

    if not (os.environ.get("CRYPTO_GUARD_DB_ADMIN_PASSWORD")
            or os.environ.get("CRYPTO_GUARD_DATABASE_URL")):
        # P2-3: --plan is a read-only git/change query that executes NOTHING and
        # returns above without demanding DB credentials. Round-4 Finding A: the
        # tier==none no-op ALSO returns above, so a credential-less machine can
        # acknowledge an inert change set. Every tier that reaches this point
        # executes REAL tests against the dedicated test DB — credentials are
        # required.
        parser.error(
            "set CRYPTO_GUARD_DB_ADMIN_PASSWORD or CRYPTO_GUARD_DATABASE_URL "
            "for the dedicated crypto_guard_test database")

    if tier in ("full", "final-seal"):
        # Parent-process test-DB bootstrap + REAL Postgres version proof BEFORE
        # any complete-suite gate: refuse to run (never record a null/unknown
        # postgres_version) when the dedicated test DB is unreachable.
        if not _ensure_parent_db_ready():
            print("ERROR: full/final-seal gate refused to run: test DB not "
                  "ready (cannot prove a real PostgreSQL version)",
                  file=sys.stderr)
            return 2
    elif tier in ("unit", "domain"):
        # P2-4: unit/domain evidence reuse must be viable in a password-only
        # setup. Publish the test DSN and probe the REAL version so the reuse
        # query below can match (a probe failure stays fail-closed: no reuse,
        # record carries null — the gate itself still runs). The probe is a
        # bounded 2s connect_timeout once per DSN, cached per process.
        _publish_test_dsn()
        ev.postgres_version()

    if tier == "final-seal":
        print("final_seal double_run NEVER served from cache")
        # REAL freeze proof (rework): F1 -> RUN1 -> F2 -> RUN2 -> F3. The tree
        # fingerprint is recomputed before AND after each pass on the SAME
        # committed byte tree; any inequality proves the double-run was not
        # frozen, aborts immediately, and writes NO green evidence.
        plan["fingerprint"] = ev.full_fingerprint()
        f1 = plan["fingerprint"]
        fingerprints = {"f1": f1, "f2": None, "f3": None}
        merged_verdicts: dict = {}
        merged_durations: list = []
        started = _clock()
        run_elapsed: list[float] = []
        for run in (1, 2):
            run_started = _clock()
            report = _run_gate("full", [], args.workers, args.durations)
            run_elapsed.append(_clock() - run_started)
            merged_verdicts.update(report.node_verdicts)
            merged_durations += report.durations
            if report.returncode != 0:
                print(f"final_seal RUN{run} FAILED", file=sys.stderr)
                return report.returncode
            # Operator hard gate: EACH final-seal run must finish independently
            # within FINAL_SEAL_HARD_GATE_SECONDS (40 min). RUN1 over the gate
            # fails immediately — RUN2 must NOT start; a hard-gate fail writes
            # NO green evidence (exit 4, distinct from drift exit 3).
            if run_elapsed[-1] > FINAL_SEAL_HARD_GATE_SECONDS:
                print(
                    f"final_seal RUN{run} EXCEEDED hard gate "
                    f"{run_elapsed[-1]:.1f}s > "
                    f"{FINAL_SEAL_HARD_GATE_SECONDS}s (40 min per run); "
                    f"aborting with NO green evidence",
                    file=sys.stderr)
                return 4
            f_next = ev.full_fingerprint()
            fingerprints[f"f{run + 1}"] = f_next
            if f_next != f1:
                print(
                    f"FINGERPRINT DRIFT final_seal f1 != f{run + 1}: "
                    f"double-run was NOT frozen; aborting with no evidence",
                    file=sys.stderr)
                return 3
        plan["fingerprints"] = fingerprints
        plan["run_elapsed_seconds"] = [round(t, 3) for t in run_elapsed]
        _record_evidence("final-seal", plan, args.workers,
                         _clock() - started, "BOTH_GREEN",
                         args.evidence_dir,
                         node_verdicts=merged_verdicts,
                         durations=merged_durations)
        print(f"final_seal_ok BOTH_GREEN F1==F2==F3={f1[:12]} "
              f"run_elapsed_seconds={plan['run_elapsed_seconds']} "
              f"elapsed_seconds={_clock() - started:.2f}")
        return 0

    # Tier fingerprint: unit/domain = full dependency closure of the selected
    # tests; full = the suite's entire input universe.
    if tier in ("unit", "domain"):
        plan["fingerprint"] = ev.dependency_fingerprint(plan["tests"])
    else:
        plan["fingerprint"] = ev.full_fingerprint()

    # Evidence reuse gate (Step 3 real store; --no-reuse forces execution).
    if not args.no_reuse:
        reused = _maybe_reuse_evidence(tier, plan["fingerprint"],
                                       args.evidence_dir, False,
                                       args.workers)
        if reused is not None:
            print(f"evidence_reused tier={tier} "
                  f"fingerprint={reused.get('tier_fingerprint', '')}")
            return 0

    started = time.perf_counter()
    report = _run_gate(tier, plan["tests"], args.workers, args.durations)
    if report.returncode != 0:
        return report.returncode
    # Single runs record GREEN; ONLY the frozen final-seal double-run records
    # BOTH_GREEN (rework P2 — verdicts are per-gate).
    _record_evidence(tier, plan, args.workers,
                     time.perf_counter() - started, "GREEN",
                     args.evidence_dir,
                     node_verdicts=report.node_verdicts,
                     durations=report.durations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
