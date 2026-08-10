# -*- coding: utf-8 -*-
"""Source -> test/marker mapping manifest: AST scan, drift guard, resolution.

R3 (08-08 test feedback loop acceleration). ``mapping.json`` is the explicit
source -> test manifest asserted by ``test_pg_08_08_runner_dependency_proof.py``.
This module:

* **AST scan** — statically resolves every ``plugins.crypto_guard.*`` import
  in every ``test_*.py`` to the exact source file on disk (leaf module or
  package ``__init__.py``; namespace packages with no file are skipped).
* **Drift guard** — ``check_drift()`` proves the manifest is a SUPERSET of the
  real static import edges: any test importing a mapped source must be in that
  domain's test list, shared-base lists must equal the AST-derived importers,
  every listed source/test must exist. A missing real edge is a hard error.
* **Resolution + fail-closed classification** — ``classify_path()`` maps a
  changed path to a gate tier (unit/domain/full) plus an exact test set.
  Escalation: storage/schema/migrations, runner/config files, boundary files
  and unknown paths -> FULL; an unmapped-but-imported source -> DOMAIN via the
  AST importers (never a silent empty set). ``apply_tier`` enforces the
  ONLY-EVER-UPGRADE rule used by ``run_change_aware.py``.

Paths in ``mapping.json`` are repo-relative (``plugins/crypto_guard/...``);
``tests`` values are file basenames inside ``tests/`` and may be globs.

Invocation:
    python -m plugins.crypto_guard.tests.sync_mapping           # drift check
    python -m plugins.crypto_guard.tests.sync_mapping --sync    # rewrite derived lists
    python -m plugins.crypto_guard.tests.sync_mapping --dump    # JSON AST import map
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[2]
MAPPING_PATH = TESTS_DIR / "mapping.json"
PKG = "plugins.crypto_guard"
PKG_DIR = REPO_ROOT / "plugins" / "crypto_guard"
PKG_PATH = PKG.replace(".", "/")  # forward-slash repo path form
SCHEMA_VERSION = 1

# Files that gate the WHOLE suite when they change (configuration, runners).
# A conftest/pytest.ini/runner change can alter collection or isolation for
# every test -> fail-closed FULL.
FULL_GATE_PATHS = [
    "pytest.ini",
    "plugins/crypto_guard/__init__.py",
    "plugins/crypto_guard/tests/conftest.py",
    "plugins/crypto_guard/tests/run_complete_suite.py",
    "plugins/crypto_guard/tests/run_change_aware.py",
    "plugins/crypto_guard/tests/sync_mapping.py",
    "plugins/crypto_guard/tests/evidence_store.py",
    "plugins/crypto_guard/tests/fake_clock.py",
]

# Boundary files: never touched by this task; a change here is a red flag.
BOUNDARY_FILES = [
    "hub.pyw",
    "frontends/fsapp.py",
    "plugins/crypto_guard/data/binance_rest.py",
]

# Shared test scaffolding: any change expands to every test importing it.
# Lists are derived by the AST scan and stored in mapping.json.
SHARED_BASE_MODULES = [
    "plugins/crypto_guard/tests/pg_fixtures.py",
    "plugins/crypto_guard/tests/_pg_bootstrap.py",
    "plugins/crypto_guard/tests/_smoke_suite.py",
    "plugins/crypto_guard/tests/test_smoke.py",
]

# Gate tiers, weakest -> strongest. Natural tier for a change set is the max.
# ``final-seal`` is never a NATURAL tier (no path classifies to it); it exists
# only as the top of the --tier upgrade lattice: any natural tier + --tier
# final-seal is an upgrade, and only final-seal's own double-run is final-seal.
TIER_ORDER = ["none", "unit", "domain", "full", "final-seal"]

# A unit gate must resolve to at most this many test files, none of which is
# marked e2e/serial/schema_mutation/slow (heavy or non-parallelizable work).
UNIT_MAX_FILES = 10
UNIT_BANNED_MARKERS = {"e2e", "serial", "schema_mutation", "slow"}


def _norm(path: str) -> str:
    """Normalize a repo-relative or absolute path to forward-slash repo form."""
    p = path.replace("\\", "/")
    try:
        rel = Path(p).resolve().relative_to(REPO_ROOT.resolve())
        p = rel.as_posix()
    except (OSError, ValueError):
        pass
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def module_to_source_path(module: str) -> Path | None:
    """Resolve a dotted ``plugins.crypto_guard.*`` module to its file on disk.

    Leaf module -> ``.../m.py``; package -> ``.../__init__.py``; namespace
    packages with no file return None (nothing to hash or change).
    """
    if module == PKG:
        cand = PKG_DIR / "__init__.py"
        return cand if cand.exists() else None
    if not module.startswith(PKG + "."):
        return None
    parts = module[len(PKG) + 1:].split(".")
    base = PKG_DIR.joinpath(*parts)
    leaf = Path(str(base) + ".py")
    if leaf.exists():
        return leaf
    init = base / "__init__.py"
    if init.exists():
        return init
    return None


def _module_pytest_markers(path: Path) -> set[str]:
    """Extract module-level ``pytestmark`` markers from a test file via AST."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    return _markers_from_value(node.value)
    return set()


def _markers_from_value(value: ast.AST) -> set[str]:
    out: set[str] = set()
    if isinstance(value, ast.Attribute):
        name = value.attr
        if name not in ("mark",):
            out.add(name)
        elif isinstance(value.value, ast.Attribute) and value.value.attr != "mark":
            out.add(value.value.attr)
    elif isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
        # pytest.mark.pg
        f = value.func
        if isinstance(f.value, ast.Attribute) and f.value.attr == "mark":
            out.add(f.attr)
    elif isinstance(value, (ast.List, ast.Tuple)):
        for elt in value.elts:
            out |= _markers_from_value(elt)
    return out


def _expand_globs(patterns: list[str]) -> set[str]:
    """Expand ``tests`` patterns (basenames/globs) to existing test basenames."""
    result: set[str] = set()
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            result.update(p.name for p in TESTS_DIR.glob(pat))
        else:
            result.add(pat)
    return {r for r in result if (TESTS_DIR / r).exists()}


class Scan:
    """AST-derived ground truth: every static import edge in the test tree."""

    def __init__(self, source_importers: dict[str, set[str]],
                 helper_importers: dict[str, set[str]]):
        self.source_importers = source_importers  # relpath -> {test basenames}
        self.helper_importers = helper_importers  # tests/ helper relpath -> set
        self.test_files = sorted(
            p.name for p in TESTS_DIR.glob("test_*.py")
        )
        self.test_markers = {
            name: _module_pytest_markers(TESTS_DIR / name)
            for name in self.test_files
        }


def _is_import_module_call(node: ast.AST) -> bool:
    """Round-6 P2-1: True if ``node`` is an ``importlib.import_module`` /
    ``importlib.__import__`` / bare ``__import__`` call.

    Only constant-string first arguments are statically resolvable; a
    loop-variable argument (e.g. ``import_module(module_name)``) cannot be
    captured here and must be declared explicitly in ``dynamic_imports``.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "__import__"
    if isinstance(func, ast.Attribute):
        return (isinstance(func.value, ast.Name)
                and func.value.id == "importlib"
                and func.attr in ("import_module", "__import__"))
    return False


def _collect_modules(path: Path) -> set[str]:
    """AST-collect every ``plugins.crypto_guard.*`` dotted module one file imports."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PKG):
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(PKG):
                modules.add(node.module)
                # Package-form import from a NAMESPACE package root (no
                # __init__.py): `from plugins.crypto_guard.tests import
                # pg_fixtures as fx` — node.module resolves to nothing on
                # disk, yet the REAL edge is to the aliased leaf module.
                # Record the leaf edges so the AST importers include the
                # actual file (P2-2) instead of silently dropping them.
                if module_to_source_path(node.module) is None:
                    for alias in node.names:
                        if alias.name != "*":
                            modules.add(f"{node.module}.{alias.name}")
        elif _is_import_module_call(node):
            # Round-6 P2-1: importlib.import_module with a CONSTANT module name
            # is a real static edge the old scanner dropped (e.g.
            # test_suite_structure.py -> test_smoke.py). Loop-variable
            # arguments are unresolvable here -> declared in dynamic_imports.
            if (node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.startswith(PKG)):
                modules.add(node.args[0].value)
    return modules


def _fold_shared_base_deps(source_importers: dict[str, set[str]],
                           helper_importers: dict[str, set[str]]) -> None:
    """Round-4 P2 (Finding B): fold shared-base transitive source edges.

    A test that imports a SHARED_BASE_MODULE (e.g. ``_smoke_suite.py``)
    transitively depends on every source that base imports — and on every
    source imported by a base the base imports (``_smoke_suite`` imports
    ``pg_fixtures``). The OLD scanner walked only ``test_*.py``, so
    ``source_importers[utils.py]`` held only DIRECT importers and a change to
    ``utils.py`` MISSED every test reaching it through ``_smoke_suite`` — a
    silent false-negative in the change-aware runner's core promise. Folding
    makes ``source_importers[S]`` the TRUE set of tests depending on S, and
    ``check_drift`` then enforces that every such test is in S's domain test
    list, closing the hole.
    """
    shared = [r for r in SHARED_BASE_MODULES if (REPO_ROOT / r).exists()]
    helper_sources: dict[str, set[str]] = {}
    helper_edges: dict[str, set[str]] = {}
    for rel in shared:
        for module in _collect_modules(REPO_ROOT / rel):
            path = module_to_source_path(module)
            if path is None:
                continue
            r = path.relative_to(REPO_ROOT).as_posix()
            if r in shared:
                helper_edges.setdefault(rel, set()).add(r)
            elif not r.startswith(PKG_PATH + "/tests/"):
                helper_sources.setdefault(rel, set()).add(r)

    def _dependents(rel: str) -> set[str]:
        # Every test importing rel directly, or reaching it through another
        # shared base (transitively). Sound because module-level imports run.
        out = set(helper_importers.get(rel, set()))
        for other in shared:
            if other == rel:
                continue
            reach = {other}
            frontier = [other]
            while frontier:
                cur = frontier.pop()
                for nxt in helper_edges.get(cur, set()):
                    if nxt not in reach:
                        reach.add(nxt)
                        frontier.append(nxt)
            if rel in reach:
                out |= helper_importers.get(other, set())
        return out

    for rel in shared:
        tests = _dependents(rel)
        for src in helper_sources.get(rel, set()):
            source_importers.setdefault(src, set()).update(tests)


def ast_scan() -> Scan:
    """Walk every ``test_*.py`` + fold shared-base transitive source edges."""
    source_importers: dict[str, set[str]] = {}
    helper_importers: dict[str, set[str]] = {}
    for test_path in sorted(TESTS_DIR.glob("test_*.py")):
        name = test_path.name
        for module in _collect_modules(test_path):
            path = module_to_source_path(module)
            if path is None:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith(PKG_PATH + "/tests/"):
                helper_importers.setdefault(rel, set()).add(name)
            else:
                source_importers.setdefault(rel, set()).add(name)
    _fold_shared_base_deps(source_importers, helper_importers)
    return Scan(source_importers, helper_importers)


def load_mapping() -> dict[str, Any]:
    if not MAPPING_PATH.exists():
        return {}
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


def _escalate_full(mapping: dict[str, Any], relpath: str) -> bool:
    if relpath.startswith(PKG_PATH + "/storage/") or relpath.startswith(
            PKG_PATH + "/schemas/"):
        return True
    if relpath.startswith("plugins/"):
        # Any non-crypto_guard plugin file is outside the mapped universe.
        pass
    full_paths = set(FULL_GATE_PATHS + BOUNDARY_FILES)
    full_paths.update(mapping.get("escalate_to_full", []))
    for prefix in sorted(full_paths, key=len, reverse=True):
        if relpath == prefix or relpath.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _is_inert(mapping: dict[str, Any], relpath: str) -> bool:
    for pat in mapping.get("inert_artifacts", []) or [".trellis/"]:
        pat = pat.rstrip("/")
        if relpath == pat or relpath.startswith(pat + "/") or (
                relpath.startswith("journal-")):
            return True
    return False


def expand_domain_tests(mapping: dict[str, Any]) -> dict[str, set[str]]:
    return {
        domain: _expand_globs(entry.get("tests", []))
        for domain, entry in mapping.get("domains", {}).items()
    }


def shared_base_tests(mapping: dict[str, Any], relpath: str) -> set[str]:
    entry = mapping.get("shared_base_modules", {}).get(relpath)
    if entry == {"all": True}:
        return {p.name for p in TESTS_DIR.glob("test_*.py")}
    if isinstance(entry, list):
        return _expand_globs(entry)
    return set()


def resolve_source(mapping: dict[str, Any], relpath: str,
                   scan: Scan | None = None,
                   ) -> tuple[list[str], set[str], bool]:
    """Map a source file to (domains, tests, escalated).

    escalated=True means the mapping entry was MISSING and we recovered the
    importer set from the AST scan (fail-closed: never a silent empty set).
    """
    for domain, entry in mapping.get("domains", {}).items():
        if relpath in entry.get("sources", []):
            tests = expand_domain_tests(mapping)[domain]
            if entry.get("serial"):
                tests |= _expand_globs(entry["serial"])
            return [domain], tests, False
    if relpath in mapping.get("shared_base_modules", {}):
        return ["__shared_base__"], shared_base_tests(mapping, relpath), False
    # Dynamic import edges: source file -> tests keyed by ``file::symbol``.
    # A SUPPLEMENT to the static closure, never a replacement: for an unmapped
    # source the escalated set is static importers UNION dynamic targets.
    dynamic_targets: set[str] = set()
    for key, targets in mapping.get("dynamic_imports", {}).items():
        if key.split("::", 1)[0] == relpath:
            dynamic_targets |= _expand_globs(targets)
    if relpath in mapping.get("test_helpers", {}):
        return ["__test_helper__"], _expand_globs(
            mapping["test_helpers"][relpath]) | dynamic_targets, False
    scan = scan or ast_scan()
    importers = scan.source_importers.get(relpath, set())
    if importers:
        # Unmapped but really imported: escalate to domain, never zero.
        return [], importers | dynamic_targets, True
    if dynamic_targets:
        return ["__dynamic__"], dynamic_targets, False
    # No mapping entry, no AST importers, no dynamic edge: a genuinely unknown
    # source. NOT "escalated to domain" (that would be a silent empty set) —
    # the caller fails closed to FULL.
    return [], set(), False


def classify_path(mapping: dict[str, Any], relpath: str,
                  scan: Scan | None = None) -> tuple[str, set[str], str]:
    """Classify one changed path -> (tier, test_files, reason).

    tiers: none (inert) / unit / domain / full (fail-closed).
    """
    relpath = _norm(relpath)
    scan = scan or ast_scan()
    if _is_inert(mapping, relpath):
        return "none", set(), f"inert artifact: {relpath}"
    if _escalate_full(mapping, relpath):
        return "full", set(), f"fail-closed full gate: {relpath}"
    if relpath.startswith(PKG_PATH + "/tests/"):
        base = relpath.rsplit("/", 1)[-1]
        if base == "mapping.json":
            return "full", set(), "mapping manifest change -> full"
        if relpath in SHARED_BASE_MODULES:
            tests = shared_base_tests(mapping, relpath)
            # Round-6 P2-1: the AST scan is ground truth — union the scan's
            # importers so a shared base whose consumers include a proxy test
            # (e.g. test_smoke <- test_suite_structure via importlib) is never
            # under-run just because the stored list predates the edge.
            tests |= scan.helper_importers.get(relpath, set())
            if not tests:
                return "full", set(), f"unmapped shared base {relpath} -> full"
            return "domain", tests, f"shared base module: {relpath}"
        if base.startswith("test_") and base.endswith(".py"):
            helpers = mapping.get("test_helpers", {}).get(relpath, [])
            tests = {base} | _expand_globs(helpers)
            # Round-6 P2-1: a test imported by OTHER test files must pull those
            # importers into the gate — the AST scan (static + importlib
            # constant edges) AND any explicit dynamic_imports edge (loop-var
            # import_module calls the AST cannot resolve). Without this,
            # changing a regression module under-runs the structure test that
            # imports it via importlib.
            tests |= scan.helper_importers.get(relpath, set())
            for key, targets in mapping.get("dynamic_imports", {}).items():
                if key.split("::", 1)[0] == relpath:
                    tests |= _expand_globs(targets)
            tier = "unit"
            text = (TESTS_DIR / base).read_text(encoding="utf-8",
                                                errors="replace")
            if (scan.test_markers.get(base, set()) & UNIT_BANNED_MARKERS
                    or any(f"mark.{m}" in text for m in
                           ("serial", "e2e", "slow", "schema_mutation"))):
                tier = "domain"
            elif not scan.test_markers.get(base, set()) & {"unit", "pg"}:
                tier = "domain"  # untagged -> conservative
            if tier == "unit" and len(tests) > UNIT_MAX_FILES:
                tier = "domain"
            return tier, tests, f"test file: {base}"
        return "full", set(), f"unmapped tests/ file {base} -> full"
    if relpath.startswith(PKG_PATH + "/"):
        domains, tests, escalated = resolve_source(mapping, relpath, scan)
        if domains:
            reason = f"source {relpath} -> {','.join(domains)}"
            if escalated:
                reason += " [AST fallback]"
            return "domain", tests, reason
        if escalated:
            return "domain", tests, f"unmapped source {relpath} -> domain (AST importers)"
        return "full", set(), f"unknown crypto_guard path {relpath} -> full"
    if relpath.startswith("plugins/"):
        return "full", set(), f"unknown plugin path {relpath} -> full"
    # Everything else (scripts, docs outside .trellis, etc.) is unknown.
    return "full", set(), f"unknown path {relpath} -> full"


def aggregate_classification(mapping: dict[str, Any], relpaths: list[str],
                             scan: Scan | None = None,
                             ) -> tuple[str, set[str], dict[str, str]]:
    """Classify a change set; return (natural_tier, union_tests, reasons)."""
    scan = scan or ast_scan()
    natural_idx = 0
    union: set[str] = set()
    reasons: dict[str, str] = {}
    for relpath in relpaths:
        tier, tests, reason = classify_path(mapping, relpath, scan)
        reasons[relpath] = reason
        natural_idx = max(natural_idx, TIER_ORDER.index(tier))
        union |= tests
    return TIER_ORDER[natural_idx], union, reasons


def unit_tier_tests(scan: Scan | None = None) -> set[str]:
    """Every test file whose natural classification is ``unit`` (design §1.4).

    The explicit-widen set: a forced ``domain`` gate over a natural ``unit``
    gate must run the WHOLE unit tier (not just the changed file) so the gate
    label matches the executed coverage.
    """
    scan = scan or ast_scan()
    mapping = load_mapping()
    unit: set[str] = set()
    for name in scan.test_files:
        tier, tests, _ = classify_path(
            mapping, f"{PKG_PATH}/tests/{name}", scan)
        if tier == "unit":
            unit |= tests
    return unit


def apply_tier(natural: str, forced: str | None) -> str:
    """ONLY-EVER-UPGRADE: a forced tier below natural is a hard error."""
    if forced is None:
        return natural
    natural_idx = TIER_ORDER.index(natural)
    forced_idx = TIER_ORDER.index(forced)
    if forced_idx < natural_idx:
        raise ValueError(
            f"--tier {forced} is a DOWNGRADE of natural tier {natural}; "
            f"--tier may only upgrade (fail-closed)."
        )
    return forced


def mapping_digest(mapping: dict[str, Any]) -> str:
    """Content hash of the canonical manifest (sorted keys, stable).

    The ``digest`` key itself is EXCLUDED from the hash (P2-1). With the old
    self-referential scheme the digest was a fixed point only by chance: every
    ``--sync`` rewrote the stored digest, changing the hashed blob, so two
    ``--sync`` runs on an identical mapping produced DIFFERENT digests and
    invalidated evidence. Worse, a hand-corrupted stored digest could never be
    detected because recomputation simply re-included it. Excluding the key
    yields a stable fixed point: ``--sync`` twice on an unchanged mapping gives
    the same digest, and a corrupted/missing stored digest is detectable by
    ``check_drift``.
    """
    def _sort(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _sort(obj[k]) for k in sorted(obj) if k != "digest"}
        if isinstance(obj, list):
            return sorted(_sort(v) for v in obj)
        return obj
    blob = json.dumps(_sort(mapping), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def check_drift(mapping: dict[str, Any] | None = None,
                scan: Scan | None = None) -> list[str]:
    """Drift guard. Empty list = GREEN.

    * Every static source import edge is covered by the manifest (a test
      importing a mapped source must be in that source's domain test list).
    * Shared-base / test-helper importer lists equal the AST-derived importers.
    * Every listed source and test file exists.
    * Domain sources are unambiguous (one domain per source).
    """
    mapping = mapping or load_mapping()
    if not mapping:
        return ["mapping.json is missing or empty"]
    if mapping.get("schema_version") != SCHEMA_VERSION:
        return [f"mapping schema_version != {SCHEMA_VERSION}"]
    # P2-1: the STORED digest must equal the recomputed one. A mismatch means
    # the manifest was hand-edited, the digest is stale/self-referential, or
    # the scanner changed — evidence reuse would key on a fingerprint nobody
    # can reproduce, so a mismatch is a hard drift error (never auto-fixed).
    stored_digest = mapping.get("digest")
    recomputed = mapping_digest(mapping)
    if stored_digest != recomputed:
        errors: list[str] = [
            f"mapping digest mismatch: stored={stored_digest or '(missing)'} "
            f"recomputed={recomputed} (run sync_mapping --sync)"]
    else:
        errors = []
    scan = scan or ast_scan()

    domain_tests = expand_domain_tests(mapping)
    sources_by_domain: dict[str, set[str]] = {}
    for domain, entry in mapping.get("domains", {}).items():
        srcs = set(entry.get("sources", []))
        sources_by_domain[domain] = srcs
        for src in srcs:
            if not (REPO_ROOT / src).exists():
                errors.append(f"domain {domain}: source not on disk: {src}")
    # Domain test lists exist.
    for domain, tests in domain_tests.items():
        for name in tests:
            if not (TESTS_DIR / name).exists():
                errors.append(f"domain {domain}: test not on disk: {name}")
    # Serial lists exist too.
    for domain, entry in mapping.get("domains", {}).items():
        for name in entry.get("serial", []):
            if not (TESTS_DIR / name).exists():
                errors.append(f"domain {domain}: serial test not on disk: {name}")

    # Each source in exactly one domain (except escalate-to-full sources,
    # whose tier is full regardless of domain membership).
    seen: dict[str, str] = {}
    for domain, srcs in sources_by_domain.items():
        for src in srcs:
            if _escalate_full(mapping, src):
                continue
            if src in seen:
                errors.append(
                    f"source {src} in BOTH {seen[src]} and {domain}")
            seen[src] = domain

    # Every static source import edge covered: importer(T,M) => T in domain(M).
    for relpath, importers in scan.source_importers.items():
        if _escalate_full(mapping, relpath):
            continue
        domains = [d for d, srcs in sources_by_domain.items()
                   if relpath in srcs]
        if not domains:
            errors.append(
                f"unmapped source imported by tests: {relpath} "
                f"<- {sorted(importers)}")
            continue
        for importer in importers:
            if not any(importer in domain_tests[d] for d in domains):
                errors.append(
                    f"missing mapping edge: {importer} imports {relpath} "
                    f"but is not in domain test list(s) {domains}")

    # Shared-base lists must exactly match AST importers (only for the
    # non-test-file scaffolding helpers; test-file helpers are checked below;
    # runner/config helpers that escalate to full need no stored list).
    for helper, importers in scan.helper_importers.items():
        if helper.startswith(PKG_PATH + "/tests/test_"):
            continue
        if _escalate_full(mapping, helper):
            continue
        stored = mapping.get("shared_base_modules", {}).get(helper)
        if stored == {"all": True}:
            continue
        if set(stored or []) != importers:
            errors.append(
                f"shared base {helper}: stored={sorted(stored or [])} "
                f"AST={sorted(importers)}")
    # Test-helper lists must equal the AST-derived importers of those helpers.
    for helper, importers in scan.helper_importers.items():
        if helper.startswith(PKG_PATH + "/tests/test_"):
            stored = mapping.get("test_helpers", {}).get(helper, [])
            if set(stored) != importers:
                errors.append(
                    f"test helper {helper}: stored={sorted(stored)} "
                    f"AST={sorted(importers)}")

    # Dynamic import targets must exist on disk.
    for key, targets in mapping.get("dynamic_imports", {}).items():
        for name in _expand_globs(targets):
            if not (TESTS_DIR / name).exists():
                errors.append(f"dynamic import {key}: test not on disk: {name}")

    return errors


def _write_mapping(mapping: dict[str, Any]) -> None:
    MAPPING_PATH.write_text(
        json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sync(mapping: dict[str, Any], scan: Scan) -> dict[str, Any]:
    """Regenerate the derived importer lists in a copy of the manifest."""
    mapping = json.loads(json.dumps(mapping))
    shared = mapping.setdefault("shared_base_modules", {})
    for helper in SHARED_BASE_MODULES:
        if helper == "plugins/crypto_guard/tests/conftest.py":
            continue
        importers = sorted(scan.helper_importers.get(helper, set()))
        shared[helper] = importers
    helpers = mapping.setdefault("test_helpers", {})
    for helper, importers in scan.helper_importers.items():
        if helper.startswith(PKG_PATH + "/tests/test_"):
            helpers[helper] = sorted(importers)
    mapping["digest"] = mapping_digest(mapping)
    return mapping


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    scan = ast_scan()
    mapping = load_mapping()
    if "--dump" in argv:
        blob = {
            "source_importers": {k: sorted(v) for k, v in
                                 sorted(scan.source_importers.items())},
            "helper_importers": {k: sorted(v) for k, v in
                                 sorted(scan.helper_importers.items())},
            "test_files": scan.test_files,
            "test_markers": {k: sorted(v) for k, v in
                             sorted(scan.test_markers.items())},
        }
        print(json.dumps(blob, indent=2, sort_keys=True))
        return 0
    if "--sync" in argv:
        synced = _sync(mapping, scan)
        _write_mapping(synced)
        print(f"synced {MAPPING_PATH.name}: "
              f"shared_base/test_helpers regenerated, "
              f"digest={mapping_digest(synced)}")
        return 0
    if not mapping:
        print("mapping.json missing — run with --sync after creating it",
              file=sys.stderr)
        return 2
    errors = check_drift(mapping, scan)
    if errors:
        print("DRIFT:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"drift_ok sources={len(scan.source_importers)} "
          f"test_files={len(scan.test_files)} "
          f"digest={mapping_digest(mapping)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
