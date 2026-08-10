# -*- coding: utf-8 -*-
"""Evidence manifest contract (Step 3, 08-08 test feedback loop).

Executable spec for the content-addressed ``.evidence/`` JSONL store
(design §4): append-only records, full-dependency-closure fingerprints,
comment-stripped Python hashing (docstrings KEPT), byte-for-byte non-Python,
and the reuse rule. Pure unit tests — no PostgreSQL, no real gate execution.
"""
import json
from pathlib import Path

import pytest

from plugins.crypto_guard.tests import evidence_store as ev
from plugins.crypto_guard.tests import run_change_aware as rca

pytestmark = pytest.mark.unit

RUN_GA = "plugins/crypto_guard/run_ga_workers.py"
MIGRATIONS = "plugins/crypto_guard/storage/migrations.py"
TESTS_PREFIX = "plugins/crypto_guard/tests"


def _env(monkeypatch):
    monkeypatch.setenv(
        "CRYPTO_GUARD_DATABASE_URL",
        "postgresql://dummy:dummy@127.0.0.1/dummy")


class _FakeResult:
    """Stub matching the GateReport contract main() reads after a gate."""
    returncode = 0
    node_verdicts = {}
    durations = []


def _dummy_record(gate="unit", fp="fp-1", result=None, **extra) -> dict:
    """A self-contained record whose reuse components match ``_components``.

    Verdict semantics (rework): a single run defaults to ``GREEN``; only a
    final-seal record defaults to ``BOTH_GREEN``.
    """
    if result is None:
        result = "BOTH_GREEN" if gate == "final-seal" else "GREEN"
    rec = {
        "schema_version": ev.SCHEMA_VERSION,
        "gate": gate,
        "tier_fingerprint": fp,
        "changed_paths": [],
        "test_manifest_hash": "tm1",
        "mapping_hash": "m1",
        "pytest_ini_hash": "p1",
        "python_version": "3.11.9",
        "postgres_version": "160001",
        "workers": 8,
        "result": result,
        "elapsed_seconds": 42.0,
        "selected": 5,
        "date": "2026-08-08",
        "ts": 100.0,
    }
    rec.update(extra)
    return rec


def _components(fp: str) -> dict:
    return {
        "tier_fingerprint": fp,
        "test_manifest_hash": "tm1",
        "mapping_hash": "m1",
        "pytest_ini_hash": "p1",
        "python_version": "3.11.9",
        "postgres_version": "160001",
        "workers": 8,
    }


# --------------------------------------------------------------------------
# Append-only JSONL store + reuse rule
# --------------------------------------------------------------------------

def test_manifest_is_append_only_jsonl(tmp_path: Path):
    ev.append_record(_dummy_record(fp="fp-1"), tmp_path)
    ev.append_record(_dummy_record(fp="fp-2"), tmp_path)
    path = tmp_path / "evidence.jsonl"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    assert len(lines) == 2, "append-only: exactly two machine-readable rows"
    assert json.loads(lines[0]) == _dummy_record(fp="fp-1")
    assert json.loads(lines[1]) == _dummy_record(fp="fp-2")


def test_identical_fingerprint_reuses_evidence(tmp_path: Path):
    ev.append_record(_dummy_record(fp="fp-1"), tmp_path)
    hit = ev.find_reuse("unit", _components("fp-1"), tmp_path)
    assert hit is not None
    assert hit["result"] == "GREEN"
    assert hit["tier_fingerprint"] == "fp-1"


def test_different_fingerprint_does_not_reuse(tmp_path: Path):
    ev.append_record(_dummy_record(fp="fp-1"), tmp_path)
    assert ev.find_reuse("unit", _components("fp-2"), tmp_path) is None


def test_gate_mismatch_never_reuses(tmp_path: Path):
    # A full-gate record can never serve a unit-gate query.
    ev.append_record(_dummy_record(gate="full", fp="fp-1"), tmp_path)
    assert ev.find_reuse("unit", _components("fp-1"), tmp_path) is None


def test_red_result_never_reused(tmp_path: Path):
    ev.append_record(_dummy_record(fp="fp-1", result="RED"), tmp_path)
    assert ev.find_reuse("unit", _components("fp-1"), tmp_path) is None


def test_latest_matching_record_wins(tmp_path: Path):
    ev.append_record(_dummy_record(fp="fp-1", elapsed_seconds=10.0, ts=1.0),
                     tmp_path)
    ev.append_record(_dummy_record(fp="fp-1", elapsed_seconds=90.0, ts=2.0),
                     tmp_path)
    hit = ev.find_reuse("unit", _components("fp-1"), tmp_path)
    assert hit is not None
    assert hit["elapsed_seconds"] == 90.0


# --------------------------------------------------------------------------
# Fingerprint semantics: comments vs docstrings vs dependency closure
# --------------------------------------------------------------------------

def test_python_comment_only_edit_does_not_invalidate():
    src_a = "def f():\n    return 1\n# keep this comment\nX = f()\n"
    src_b = "def f():\n    return 1\n# a totally different comment\nX = f()\n"
    assert ev.hash_python(src_a) == ev.hash_python(src_b)


def test_python_docstring_change_invalidates():
    src_a = '"""docstring version A"""\nX = 1\n'
    src_b = '"""docstring version B"""\nX = 1\n'
    assert ev.hash_python(src_a) != ev.hash_python(src_b)


def test_python_code_change_invalidates():
    src_a = "X = 1\n"
    src_b = "X = 2\n"
    assert ev.hash_python(src_a) != ev.hash_python(src_b)


def test_dependency_change_invalidates_unit_fingerprint(tmp_path: Path):
    """A change to a transitive dependency (not the changed path) invalidates."""
    (tmp_path / TESTS_PREFIX).mkdir(parents=True)
    pkg = tmp_path / "plugins" / "crypto_guard"
    (pkg / "a.py").write_text("from plugins.crypto_guard.b import x\n",
                              encoding="utf-8")
    b = pkg / "b.py"
    import_map = {
        f"{TESTS_PREFIX}/test_a.py": {"plugins/crypto_guard/a.py"},
        "plugins/crypto_guard/a.py": {"plugins/crypto_guard/b.py"},
        "plugins/crypto_guard/b.py": set(),
    }

    b.write_text("x = 1\n", encoding="utf-8")
    fp_v1 = ev.dependency_fingerprint(["test_a.py"], import_map=import_map,
                                      root=tmp_path)
    b.write_text("x = 2\n", encoding="utf-8")
    fp_v2 = ev.dependency_fingerprint(["test_a.py"], import_map=import_map,
                                      root=tmp_path)
    assert fp_v1 != fp_v2, \
        "a change in a transitive dependency must invalidate the fingerprint"


def test_full_fingerprint_changes_when_package_code_changes(tmp_path: Path):
    pkg = tmp_path / "plugins" / "crypto_guard"
    pkg.mkdir(parents=True)
    x = pkg / "x.py"
    import_map = {"plugins/crypto_guard/x.py": set()}
    x.write_text("def a():\n    return 1\n", encoding="utf-8")
    fp_v1 = ev.full_fingerprint(root=tmp_path, import_map=import_map)
    x.write_text("def a():\n    return 2\n", encoding="utf-8")
    fp_v2 = ev.full_fingerprint(root=tmp_path, import_map=import_map)
    assert fp_v1 != fp_v2


def test_non_python_files_hashed_byte_for_byte(tmp_path: Path):
    """A 'comment-only' edit to YAML/JSON/SQL/prompt STILL invalidates."""
    cases = {
        "conf.yaml": "# comment\nkey: 1\n",
        "config.json": '{"k": 1}\n',
        "schema.sql": "-- comment\nCREATE TABLE t (id int);\n",
        "prompt.txt": "# meta comment\nYou are a helper.\n",
    }
    changed = {
        "conf.yaml": "# CHANGED comment\nkey: 1\n",
        "config.json": '{"k": 2}\n',
        "schema.sql": "-- CHANGED comment\nCREATE TABLE t (id int);\n",
        "prompt.txt": "# CHANGED meta comment\nYou are a helper.\n",
    }
    for name, body in cases.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    for name in cases:
        fp_v1 = ev.file_fingerprint(name, tmp_path)
        (tmp_path / name).write_text(changed[name], encoding="utf-8")
        fp_v2 = ev.file_fingerprint(name, tmp_path)
        assert fp_v1 != fp_v2, \
            f"{name}: non-Python files are hashed byte-for-byte"


def test_full_fingerprint_is_stable_within_a_tree():
    fp_a = ev.full_fingerprint()
    fp_b = ev.full_fingerprint()
    assert fp_a == fp_b
    assert len(fp_a) == 64
    one = ev.dependency_fingerprint(["test_pg_08_08_evidence_manifest.py"])
    assert fp_a != one, "full fingerprint != a single unit test's closure"


def test_real_import_map_is_a_true_transitive_closure():
    """Round-3 P2-2 RED: the REAL import extraction is a full closure.

    The OLD ``_resolve_imports`` dropped (a) package-form leaf edges — ``from
    plugins.crypto_guard.storage import pg_db`` recorded only
    ``storage/__init__.py``, never the real leaf ``storage/pg_db.py`` — and
    (b) ALL relative imports (``from .pg_db import ...`` inside
    ``storage/__init__.py``), so ``dependency_fingerprint`` was NOT the "FULL
    dependency closure" the design (§4) claims. These assertions use the REAL
    on-disk extraction (no explicit ``import_map``) — the same path a live
    gate's fingerprint takes — and so a change to ``storage/pg_db.py``
    invalidates every test that imports it through either form.
    """
    import_map = ev._import_map()
    pg_db_rel = "plugins/crypto_guard/storage/pg_db.py"
    # test_pg_connection.py does `from plugins.crypto_guard.storage import pg_db`.
    assert pg_db_rel in import_map.get(
        f"{TESTS_PREFIX}/test_pg_connection.py", set()), (
        "package-form leaf edge dropped: the closure misses storage/pg_db.py")
    # storage/__init__.py does `from .pg_db import ...` (relative import).
    assert pg_db_rel in import_map.get(
        "plugins/crypto_guard/storage/__init__.py", set()), (
        "relative import dropped: the closure misses storage/pg_db.py")


# --------------------------------------------------------------------------
# Postgres version: real probe + fail-closed reuse (P2-2)
# --------------------------------------------------------------------------

def test_postgres_version_env_override_wins(monkeypatch):
    monkeypatch.setenv("CRYPTO_GUARD_POSTGRES_VERSION", "160001")
    assert ev.postgres_version() == "160001"


def test_postgres_version_probes_real_server(monkeypatch):
    """The env override absent, the real dedicated-test-DB version is probed."""
    monkeypatch.setenv("CRYPTO_GUARD_DATABASE_URL",
                       "postgresql://u:p@localhost:5432/db")
    monkeypatch.delenv("CRYPTO_GUARD_POSTGRES_VERSION", raising=False)
    monkeypatch.setattr(ev, "_server_version_num", lambda dsn: "160001")
    assert ev.postgres_version() == "160001"


def test_postgres_version_fail_closed_when_unprobeable(monkeypatch):
    """An unprobeable DB yields None — never a blind 'unknown' reuse hit."""
    monkeypatch.setenv("CRYPTO_GUARD_DATABASE_URL",
                       "postgresql://u:p@127.0.0.1:1/db")  # dead port
    monkeypatch.delenv("CRYPTO_GUARD_POSTGRES_VERSION", raising=False)
    assert ev.postgres_version() is None


def test_reuse_refused_when_postgres_version_unknown(tmp_path):
    """An unknown Postgres version can never serve a cache hit."""
    ev.append_record(_dummy_record(fp="fp-1"), tmp_path)
    comps = _components("fp-1")
    comps["postgres_version"] = "unknown"
    assert ev.find_reuse("unit", comps, tmp_path) is None


def test_reuse_refused_when_candidate_version_unknown(tmp_path):
    """A candidate record with an unknown version is never reusable."""
    ev.append_record(_dummy_record(fp="fp-1", postgres_version="unknown"),
                     tmp_path)
    assert ev.find_reuse("unit", _components("fp-1"), tmp_path) is None


def test_reuse_allowed_when_all_components_known(tmp_path):
    """Known-everywhere query + record still reuses (no over-blocking)."""
    ev.append_record(_dummy_record(fp="fp-1"), tmp_path)
    assert ev.find_reuse("unit", _components("fp-1"), tmp_path) is not None


def test_reuse_accepts_green_for_single_gate(tmp_path):
    """A single run's GREEN record is the cacheable verdict (rework P2)."""
    ev.append_record(_dummy_record(gate="unit", fp="fp-1"), tmp_path)
    assert ev.find_reuse("unit", _components("fp-1"), tmp_path) is not None


def test_reuse_refused_for_both_green_full_record(tmp_path):
    """A legacy BOTH_GREEN full record is NOT the full gate's verdict."""
    ev.append_record(_dummy_record(gate="full", fp="fp-1",
                                   result="BOTH_GREEN"), tmp_path)
    assert ev.find_reuse("full", _components("fp-1"), tmp_path) is None


def test_reuse_refused_when_test_manifest_hash_missing(tmp_path):
    """A candidate record without the test manifest hash is never reusable."""
    rec = _dummy_record(gate="unit", fp="fp-1")
    del rec["test_manifest_hash"]
    ev.append_record(rec, tmp_path)
    assert ev.find_reuse("unit", _components("fp-1"), tmp_path) is None


def test_reuse_refused_when_query_manifest_hash_missing(tmp_path):
    """A query without the test manifest hash can never hit (fail-closed)."""
    ev.append_record(_dummy_record(gate="unit", fp="fp-1"), tmp_path)
    comps = _components("fp-1")
    del comps["test_manifest_hash"]
    assert ev.find_reuse("unit", comps, tmp_path) is None


def test_make_record_includes_manifest_node_verdicts_durations():
    """make_record carries the design §4/§5 machine fields (rework P1)."""
    plan = {"tier": "full", "tests": ["test_a.py"],
            "changed_paths": ["x.py"], "fingerprint": "fp-1"}
    rec = ev.make_record(
        "full", plan, 8, 42.0, "GREEN",
        node_verdicts={"test_a.py::test_x": "passed"},
        durations=[{"seconds": 1.0, "phase": "call",
                    "node": "test_a.py::test_x"}])
    assert rec["result"] == "GREEN"
    assert rec["test_manifest_hash"]
    assert rec["node_verdicts"] == {"test_a.py::test_x": "passed"}
    assert rec["durations"] == [{"seconds": 1.0, "phase": "call",
                                 "node": "test_a.py::test_x"}]


def test_make_record_carries_final_seal_fingerprints():
    """A final-seal record carries the verified F1/F2/F3 triple."""
    plan = {"tier": "final-seal", "tests": [], "changed_paths": [],
            "fingerprint": "f1",
            "fingerprints": {"f1": "a", "f2": "a", "f3": "a"}}
    rec = ev.make_record("final-seal", plan, 8, 42.0, "BOTH_GREEN")
    assert rec["result"] == "BOTH_GREEN"
    assert rec["fingerprints"] == {"f1": "a", "f2": "a", "f3": "a"}


def test_test_manifest_hash_changes_when_test_file_changes(tmp_path,
                                                           monkeypatch):
    """A test-file edit or a new test file changes the manifest hash."""
    monkeypatch.setattr(ev, "TESTS_DIR", tmp_path)
    (tmp_path / "test_a.py").write_text("X = 1\n", encoding="utf-8")
    h1 = ev.test_manifest_hash()
    (tmp_path / "test_a.py").write_text("X = 2\n", encoding="utf-8")
    h2 = ev.test_manifest_hash()
    assert h1 != h2, "a test-file content change must change the manifest hash"
    (tmp_path / "test_b.py").write_text("X = 1\n", encoding="utf-8")
    h3 = ev.test_manifest_hash()
    assert h3 != h2, "a new test file must change the manifest hash"


# --------------------------------------------------------------------------
# final-seal never consults the cache (design §4 hard rule)
# --------------------------------------------------------------------------

def test_final_seal_never_consults_cache(tmp_path: Path, monkeypatch):
    """A matching BOTH_GREEN final-seal record exists, yet the runner MUST
    still freshly execute both passes and never consult the store."""
    ev.append_record(_dummy_record(gate="final-seal", fp="fp-X"), tmp_path)
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
    code = rca.main(["--tier", "final-seal", "--changed-from", "HEAD",
                     "--evidence-dir", str(tmp_path)])
    assert code == 0
    assert len(calls) == 2, \
        f"final-seal must freshly execute both passes, got {len(calls)}"
    assert not reused, "final-seal must NEVER consult the evidence store"
    assert len(recorded) == 1


# --------------------------------------------------------------------------
# Durations + history table (Step 4)
# --------------------------------------------------------------------------

def test_durations_from_output_parses_pytest_blocks():
    text = (
        "--- slowest 5 durations ---\n"
        "12.34s call     test_a.py::test_x\n"
        "1.20s setup     test_b.py::test_y\n"
        "0.50s teardown  test_b.py::test_y\n"
    )
    parsed = ev.durations_from_output(text)
    assert parsed == [
        {"seconds": 12.34, "phase": "call", "node": "test_a.py::test_x"},
        {"seconds": 1.20, "phase": "setup", "node": "test_b.py::test_y"},
        {"seconds": 0.50, "phase": "teardown", "node": "test_b.py::test_y"},
    ]


def test_history_table_aggregates_per_gate_and_date():
    records = [
        _dummy_record(gate="unit", elapsed_seconds=100.0, ts=1.0,
                      date="2026-08-08"),
        _dummy_record(gate="unit", elapsed_seconds=120.0, ts=2.0,
                      date="2026-08-08"),
        _dummy_record(gate="unit", elapsed_seconds=200.0, ts=3.0,
                      date="2026-08-09"),
        _dummy_record(gate="domain", elapsed_seconds=400.0, ts=4.0,
                      date="2026-08-08"),
        _dummy_record(gate="unit", result="RED", elapsed_seconds=999.0,
                      ts=5.0),
    ]
    table = ev.format_history(records)
    assert "history gate=unit goal_seconds=180" in table
    assert "records=3" in table
    assert "date=2026-08-08 runs=2" in table
    assert "date=2026-08-09 runs=1" in table
    assert "history gate=domain goal_seconds=480" in table
    assert ("history gate=full goal_seconds=2400 stretch_seconds=1500 "
            "records=0") in table
    # RED runs are excluded from latency history.
    assert "999" not in table


def test_history_full_goal_is_hard_gate_not_stretch():
    """P2-2 RED: --history must present the OPERATOR-APPROVED full HARD gate
    (2400 s / 40 min per run) as the goal; the 1500 s R1-3 stretch goal
    (requires >= 13 effective CPU cores) is surfaced SEPARATELY.

    The OLD code showed ``goal_seconds=1500`` — the non-binding stretch — as if
    it were THE full-suite gate, contradicting the operator-approved 40-min hard
    gate on this 6-core/12-thread machine.
    """
    table = ev.format_history([])
    assert "history gate=full goal_seconds=2400" in table, (
        f"full gate must show the 2400 s hard gate:\n{table}")
    assert "stretch_seconds=1500" in table, (
        f"the 1500 s stretch goal must be surfaced separately:\n{table}")
    # The stretch figure must never appear AS the goal.
    assert "goal_seconds=1500" not in table, (
        f"1500 must never be presented as THE full goal:\n{table}")
    # unit/domain goals are unchanged and carry no stretch.
    unit = [ln for ln in table.splitlines()
            if ln.startswith("history gate=unit")]
    assert unit and "goal_seconds=180" in unit[0] and "stretch" not in unit[0]


def test_make_record_full_seal_uses_collected_node_count():
    """P2-1 RED: full/final-seal record ``selected`` = collected node count,
    not 0.

    ``plan["tests"]`` is empty BY DESIGN for whole-suite gates (the node set
    comes from ``verify_partition``), so the OLD code recorded ``selected=0``
    on a ~2000-test full run — an evidence record that hid the true selection.
    The ``node_verdicts`` ARE the real selection for these gates.
    """
    full = ev.make_record(
        "full", {"tier": "full", "tests": [], "changed_paths": [],
                 "fingerprint": "fp-f"}, 8, 42.0, "GREEN",
        node_verdicts={f"test_{i}.py::t": "passed" for i in range(2000)})
    assert full["selected"] == 2000, (
        f"full gate selected must be the collected node count, got "
        f"{full['selected']}")
    # final-seal behaves identically (BOTH_GREEN record carries the node count).
    seal = ev.make_record(
        "final-seal", {"tier": "final-seal", "tests": [], "changed_paths": [],
                       "fingerprint": "f1",
                       "fingerprints": {"f1": "a", "f2": "a", "f3": "a"}},
        8, 84.0, "BOTH_GREEN",
        node_verdicts={"a::t": "passed", "b::t": "passed"})
    assert seal["selected"] == 2, (
        f"final-seal selected must be the collected node count, got "
        f"{seal['selected']}")
    # unit/domain keep the plan FILE count (their file list is authoritative),
    # even though node_verdicts may hold more nodes than files.
    unit = ev.make_record(
        "unit", {"tier": "unit", "tests": ["test_x.py"],
                 "changed_paths": ["x.py"], "fingerprint": "fp-u"},
        8, 1.0, "GREEN",
        node_verdicts={"test_x.py::t": "passed", "test_x.py::u": "passed"})
    assert unit["selected"] == 1, (
        f"unit selected must be the plan file count, got {unit['selected']}")
