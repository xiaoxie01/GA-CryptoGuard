# -*- coding: utf-8 -*-
"""Content-addressed evidence manifest (R4, 08-08 test feedback loop).

Append-only JSONL store under ``tests/.evidence/`` + fingerprint/reuse logic
(design §4):

* **Tier fingerprint = FULL dependency closure.** For unit/domain the
  fingerprint hashes every selected test file + every ``plugins.crypto_guard``
  module transitively reachable from them (import graph) + every shared base /
  runner / config file (``pytest.ini``, ``conftest.py``, mapping.json, the
  runner files used). For ``full`` it hashes the suite's ENTIRE input universe:
  all package Python, schema SQL, config, runner files and boundary files. A
  change to ANY dependency code — not just the changed paths — invalidates the
  fingerprint; green evidence is never reused on stale dependencies.
* **Semantic comment handling (P1 — not extension-based).** Python files
  fingerprint over the token stream with COMMENT tokens stripped (comments
  cannot change behavior). Docstrings are KEPT (``__doc__`` is runtime-visible).
  Every OTHER file type (yaml/json/ini/SQL/prompt) is hashed byte-for-byte — no
  "comment-only" exemption by extension.
* **Reuse rule.** A unit/domain/full run whose (gate, tier_fingerprint,
  test_manifest_hash, mapping_hash, pytest_ini_hash, python_version,
  postgres_version, workers) all match a prior GREEN record is a cache hit
  (``--no-reuse`` forces). A single run records ``GREEN``; ONLY the frozen
  final-seal double-run records ``BOTH_GREEN`` and it NEVER consults this
  cache (enforced by the runner, asserted in tests).
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import sys
import time
import tokenize
from pathlib import Path

from plugins.crypto_guard.tests import sync_mapping as sm

TESTS_DIR = sm.TESTS_DIR
REPO_ROOT = sm.REPO_ROOT
PKG_PATH = sm.PKG_PATH
DEFAULT_EVIDENCE_DIR = TESTS_DIR / ".evidence"
EVIDENCE_FILE = "evidence.jsonl"
SCHEMA_VERSION = 1

# Components that must ALL match a prior record for reuse (design §4).
REUSE_COMPONENTS = (
    "tier_fingerprint", "test_manifest_hash", "mapping_hash",
    "pytest_ini_hash", "python_version", "postgres_version", "workers",
)

# Latency goals (R1) used by --history (seconds). The full gate goal is the
# OPERATOR-APPROVED hard gate of 2400 s (40 min per run, R1-3); the 1500 s
# figure is the STRETCH goal that requires hardware with >= 13 effective CPU
# cores — it is surfaced SEPARATELY (STRETCH_GOALS) so --history never presents
# a non-binding stretch figure as THE full-suite gate (P2-2).
GATE_GOALS = {"unit": 180, "domain": 480, "full": 2400}
STRETCH_GOALS = {"full": 1500}

# Verdict semantics (rework): a single run (unit/domain/full) records GREEN;
# ONLY the frozen final-seal double-run records BOTH_GREEN. Reuse serves only
# the gate's own verdict; final-seal never consults the cache.
ACCEPTED_VERDICTS = {
    "unit": {"GREEN"},
    "domain": {"GREEN"},
    "full": {"GREEN"},
    "final-seal": set(),
}

# File suffixes swept into the full input universe beyond Python.
_EXTRA_SUFFIXES = {".sql", ".yaml", ".yml", ".json", ".ini", ".txt",
                   ".toml", ".prompt", ".md", ".jsonl"}
_SKIP_DIR_MARKS = (".evidence", "__pycache__", ".pytest_cache", ".git")


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------

def hash_python(source: str) -> str:
    """SHA-256 of the token stream with COMMENT tokens stripped.

    Docstrings (STRING tokens) are KEPT. A syntax/encoding failure falls back
    to the raw byte hash (fail-closed: any change invalidates).
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError,
            UnicodeDecodeError):
        return hashlib.sha256(
            source.encode("utf-8", errors="replace")).hexdigest()
    kept = [(t.type, t.string)
            for t in tokens if t.type not in (tokenize.COMMENT,
                                              tokenize.ENCODING)]
    return hashlib.sha256(repr(kept).encode("utf-8")).hexdigest()


def file_fingerprint(relpath: str, root: Path = REPO_ROOT) -> str:
    """Fingerprint one repo-relative file: comment-stripped for .py, else raw."""
    p = root / relpath
    if not p.exists():
        return "<missing>"
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    if relpath.endswith(".py"):
        return hash_python(text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mapping_hash() -> str:
    if not sm.MAPPING_PATH.exists():
        return "<missing>"
    return sm.mapping_digest(sm.load_mapping())


def pytest_ini_hash() -> str:
    p = REPO_ROOT / "pytest.ini"
    if not p.exists():
        return "<missing>"
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_manifest_hash() -> str:
    """SHA-256 over the whole collected test manifest (basename + content).

    Each ``test_*.py`` contributes its name and a comment-stripped content hash
    (consistent with ``hash_python`` — a comment cannot change behavior), so a
    new, renamed, or edited test file changes the hash. Green evidence is
    therefore never reused against a different test suite (rework P1).
    """
    h = hashlib.sha256()
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            content = path.read_bytes().hex()
        h.update(path.name.encode("utf-8"))
        h.update(b"\0")
        h.update(hash_python(content).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}." \
           f"{sys.version_info.micro}"


_SERVER_VERSION_CACHE: dict[str, str | None] = {}


def _server_version_num(dsn: str) -> str | None:
    """Probe ``server_version_num`` on the dedicated test DB (bounded).

    A short-lived ``psycopg`` connection with a 2s ``connect_timeout``; returns
    the server version string, or None when the server is unreachable.
    """
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW server_version_num")
                row = cur.fetchone()
        return str(row[0]) if row else None
    except Exception:  # noqa: BLE001 - a probe failure must never crash a gate
        return None


def postgres_version() -> str | None:
    """Dedicated-test-DB server version; None when unknown (fail-closed, P2-2).

    The env override ``CRYPTO_GUARD_POSTGRES_VERSION`` wins (operator
    injection, kept out of raw DSNs). Otherwise the real ``server_version_num``
    is probed once per database and cached. ``None`` - not "unknown" - is the
    fail-closed value: reuse refuses to serve green evidence on an unproven
    database.
    """
    override = os.environ.get("CRYPTO_GUARD_POSTGRES_VERSION")
    if override:
        return override
    return real_postgres_version()


def real_postgres_version() -> str | None:
    """REAL dedicated-test-DB server version, ignoring the env override (P2-5).

    ``CRYPTO_GUARD_POSTGRES_VERSION`` is an operator injection that may satisfy
    the evidence reuse/record query, but it must NEVER satisfy the
    full/final-seal real-probe refusal: a gate still has to prove a real server
    is reachable before it runs the complete suite. ``None`` means the server is
    genuinely unreachable on the published DSN (fail-closed). Shares the same
    per-DSN probe cache as ``postgres_version()`` so a live probe is done once.
    """
    dsn = os.environ.get("CRYPTO_GUARD_DATABASE_URL")
    if not dsn:
        return None
    key = dsn.split("@", 1)[-1]  # password-free cache key
    if key not in _SERVER_VERSION_CACHE:
        _SERVER_VERSION_CACHE[key] = _server_version_num(dsn)
    return _SERVER_VERSION_CACHE[key]


def _resolve_relative(path: Path, level: int, module: str | None) -> str | None:
    """Resolve a relative import to a dotted ``plugins.crypto_guard.*`` name.

    ``level`` is the leading-dot count (``.``=1, ``..``=2). The importing
    file's package directory is ``path.parent``; each extra dot ascends one
    level. A name that escapes ``plugins/crypto_guard`` resolves to None (no
    in-package dependency). Round-3 P2-2: without this, relative imports in
    package files (e.g. ``storage/__init__.py``'s ``from .pg_db import ...``)
    resolved to an empty edge set and the closure was not transitive.
    """
    pkg_dir = path.parent
    for _ in range(level - 1):
        pkg_dir = pkg_dir.parent
    try:
        rel = pkg_dir.relative_to(sm.PKG_DIR)
    except ValueError:
        return None
    dotted = "plugins.crypto_guard"
    if rel.parts:
        dotted += "." + ".".join(rel.parts)
    if module:
        dotted += "." + module
    return dotted


def _resolve_imports(path: Path) -> set[str]:
    """AST: plugins.crypto_guard.* imports of one file -> repo relpaths.

    Mirrors ``sync_mapping.ast_scan``'s edge resolution and adds the two
    closure-completeness rules (round-3 P2-2):

    * **Package-form leaf edge.** ``from plugins.crypto_guard.storage import
      pg_db`` records BOTH the package (``storage/__init__.py``) AND the real
      imported leaf (``storage/pg_db.py``) — the statement names only the
      package, but importing ``pg_db`` depends on ``storage/pg_db.py`` too.
      Without this, a change to that leaf did not invalidate the fingerprint.
    * **Relative imports.** ``from .pg_db import ...`` resolves against the
      importing file's package directory, so the closure is a TRUE transitive
      closure over the whole package.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("plugins.crypto_guard"):
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("plugins.crypto_guard"):
                modules.add(node.module)
                # Package-form leaf edge: `from pkg import sub` depends on the
                # leaf submodule file, not only the package __init__.
                for alias in node.names:
                    if alias.name != "*":
                        modules.add(f"{node.module}.{alias.name}")
            elif node.level >= 1:
                rel = _resolve_relative(path, node.level, node.module)
                if rel:
                    modules.add(rel)
    out: set[str] = set()
    for module in modules:
        path_f = sm.module_to_source_path(module)
        if path_f is not None:
            out.add(path_f.relative_to(REPO_ROOT).as_posix())
    return out


_IMPORT_CACHE: dict[str, set[str]] = {}


def _import_map() -> dict[str, set[str]]:
    """relpath -> direct plugins.crypto_guard imports, for the whole package."""
    if _IMPORT_CACHE:
        return _IMPORT_CACHE
    for py in sm.PKG_DIR.rglob("*.py"):
        rel = py.relative_to(REPO_ROOT).as_posix()
        if any(mark in rel for mark in _SKIP_DIR_MARKS):
            continue
        _IMPORT_CACHE[rel] = _resolve_imports(py)
    return _IMPORT_CACHE


def _transitive_closure(seeds: set[str], import_map: dict[str, set[str]]
                        ) -> set[str]:
    closure = set(seeds)
    frontier = list(seeds)
    while frontier:
        node = frontier.pop()
        for dep in import_map.get(node, ()) - closure:
            closure.add(dep)
            frontier.append(dep)
    return closure


def _hash_files(relpaths: set[str], root: Path = REPO_ROOT) -> str:
    h = hashlib.sha256()
    for rel in sorted(relpaths):
        if any(mark in "/" + rel.replace("\\", "/") + "/"
               for mark in (".evidence/", "/__pycache__/",
                            "/.pytest_cache/", "/.git/")):
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(file_fingerprint(rel, root).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def dependency_fingerprint(selected_tests: list[str],
                           import_map: dict[str, set[str]] | None = None,
                           root: Path = REPO_ROOT) -> str:
    """unit/domain fingerprint: full dependency closure of the selected tests.

    selected_tests are test-file basenames inside tests/. The closure walks
    the transitively-reachable plugins.crypto_guard modules and always folds in
    the runner/config/mapping inputs that define the gate itself.
    """
    import_map = import_map or _import_map()
    seeds = {f"{PKG_PATH}/tests/{t}" for t in selected_tests}
    closure = _transitive_closure(seeds, import_map)
    closure |= set(sm.FULL_GATE_PATHS)
    return _hash_files(closure, root)


def full_fingerprint(root: Path = REPO_ROOT,
                     import_map: dict[str, set[str]] | None = None) -> str:
    """full fingerprint: the suite's entire input universe."""
    import_map = import_map or _import_map()
    all_files: set[str] = set(import_map.keys())
    pkg_dir = root / "plugins" / "crypto_guard"
    if pkg_dir.exists():
        for p in pkg_dir.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if not p.is_file():
                continue
            if any(mark in "/" + rel.replace("\\", "/") + "/"
                   for mark in (".evidence/", "/__pycache__/",
                                "/.pytest_cache/")):
                continue
            if p.suffix in _EXTRA_SUFFIXES or rel.endswith(".py"):
                all_files.add(rel)
    all_files |= set(sm.FULL_GATE_PATHS) | set(sm.BOUNDARY_FILES)
    return _hash_files(all_files, root)


# --------------------------------------------------------------------------
# JSONL store (append-only)
# --------------------------------------------------------------------------

def load_records(evidence_dir: Path = DEFAULT_EVIDENCE_DIR) -> list[dict]:
    path = evidence_dir / EVIDENCE_FILE
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_record(record: dict,
                  evidence_dir: Path = DEFAULT_EVIDENCE_DIR) -> None:
    """Append one machine-readable JSONL record (never rewrites earlier rows)."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / EVIDENCE_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def find_reuse(gate: str, components: dict,
               evidence_dir: Path = DEFAULT_EVIDENCE_DIR) -> dict | None:
    """Latest prior record whose (gate, components) ALL match, gate's verdict.

    Verdict semantics (rework P2): a single unit/domain/full run reuses a
    prior ``GREEN`` record; ``BOTH_GREEN`` is only ever a final-seal verdict
    and final-seal by design never consults this cache.

    Fail-closed (P2-2): a cache hit is served ONLY when every environment
    component is KNOWN (not None/"unknown") on the query AND on the candidate
    record. An unknown Postgres version means the database identity is
    unproven -> never reuse green on it.
    """
    accepted = ACCEPTED_VERDICTS.get(gate, set())
    # Fail-closed: EVERY reuse component must be present AND known on the
    # query — a missing key (e.g. test_manifest_hash) can never silently pass.
    if any(k not in components for k in REUSE_COMPONENTS):
        return None
    for value in components.values():
        if value is None or value == "unknown":
            return None
    for rec in reversed(load_records(evidence_dir)):
        if rec.get("gate") != gate:
            continue
        if rec.get("result") not in accepted:
            continue
        if any(rec.get(k) is None or rec.get(k) == "unknown"
               for k in REUSE_COMPONENTS):
            continue
        if all(rec.get(k) == v for k, v in components.items()):
            return rec
    return None


# --------------------------------------------------------------------------
# Durations + history
# --------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^\s*(\d+\.\d+)s\s+(call|setup|teardown)\s+(.+)$")


def durations_from_output(text: str) -> list[dict]:
    """Parse pytest ``--durations`` blocks into [{seconds, phase, node}]."""
    out: list[dict] = []
    for line in text.splitlines():
        m = _DURATION_RE.match(line)
        if m:
            out.append({
                "seconds": float(m.group(1)),
                "phase": m.group(2),
                "node": m.group(3).strip(),
            })
    return out


def _median(values: list[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return values[n // 2]
    return (values[n // 2 - 1] + values[n // 2]) / 2.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    idx = min(len(values) - 1, int(p * len(values)))
    return values[idx]


def format_history(records: list[dict], goals: dict | None = None) -> str:
    """Per-gate latency table: goal, record count, min/median/p95, per-date."""
    goals = goals or GATE_GOALS
    lines: list[str] = []
    for gate in ("unit", "domain", "full"):
        accepted = ACCEPTED_VERDICTS.get(gate, {"GREEN"})
        recs = [
            r for r in records
            if r.get("gate") == gate and r.get("result") in accepted
            and r.get("elapsed_seconds") is not None
        ]
        # P2-2: the goal is the gate's operator-approved hard target; the
        # stretch target (when any) is rendered SEPARATELY on the same line, so
        # a reader never mistakes the non-binding stretch for THE gate.
        goal = goals.get(gate, "?")
        goal_text = f"goal_seconds={goal}"
        stretch = STRETCH_GOALS.get(gate)
        if stretch:
            goal_text += f" stretch_seconds={stretch}"
        if not recs:
            lines.append(f"history gate={gate} {goal_text} records=0")
            continue
        elapsed = sorted(r["elapsed_seconds"] for r in recs)
        by_date: dict[str, list[float]] = {}
        for r in recs:
            by_date.setdefault(r.get("date", "?"), []).append(
                r["elapsed_seconds"])
        lines.append(
            f"history gate={gate} {goal_text} "
            f"records={len(recs)} runs={sum(len(v) for v in by_date.values())}")
        lines.append(
            f"  overall min={min(elapsed):.1f} median={_median(elapsed):.1f} "
            f"p95={_percentile(elapsed, 0.95):.1f}")
        for date in sorted(by_date):
            vals = sorted(by_date[date])
            lines.append(
                f"  date={date} runs={len(vals)} min={min(vals):.1f} "
                f"median={_median(vals):.1f} p95={_percentile(vals, 0.95):.1f}")
    return "\n".join(lines)


def make_record(gate: str, plan: dict, workers: int, elapsed: float,
                verdict: str, node_verdicts: dict | None = None,
                durations: list | None = None) -> dict:
    """Build a complete evidence record from a gate run (design §4/§5).

    ``node_verdicts`` is the compact per-test outcome; ``durations`` is the
    top-N slowest list parsed from the ``--durations`` blocks. ``fingerprints``
    carries the verified F1/F2/F3 triple for a final-seal record only;
    ``run_elapsed_seconds`` carries the per-run [run1, run2] elapsed (final-seal
    only, operator per-run hard gate). The test manifest hash binds green
    evidence to the exact test suite.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "gate": gate,
        "tier_fingerprint": plan.get("fingerprint", ""),
        "fingerprints": plan.get("fingerprints"),
        "run_elapsed_seconds": plan.get("run_elapsed_seconds"),
        "changed_paths": plan.get("changed_paths", []),
        "test_manifest_hash": test_manifest_hash(),
        "mapping_hash": mapping_hash(),
        "pytest_ini_hash": pytest_ini_hash(),
        "python_version": python_version(),
        "postgres_version": postgres_version(),
        "workers": workers,
        "result": verdict,
        "elapsed_seconds": round(elapsed, 3),
        # P2-1: for full/final-seal gates ``plan["tests"]`` is empty BY DESIGN
        # (the whole suite runs via verify_partition, not a file list), so the
        # plan file count would record ``selected=0`` on a ~2000-test run. The
        # collected node count from ``node_verdicts`` is the REAL selection for
        # those gates; unit/domain keep the plan file count (their file list IS
        # the authoritative selection).
        "selected": (len(node_verdicts) if (node_verdicts
                                            and gate in ("full", "final-seal"))
                     else len(plan.get("tests", []))),
        "node_verdicts": node_verdicts or {},
        "durations": durations or [],
        "date": time.strftime("%Y-%m-%d"),
        "ts": time.time(),
    }
