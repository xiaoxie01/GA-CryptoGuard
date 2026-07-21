"""Phase I — fresh DB verification of all diagnostic suites.

PG cutover: this verifier no longer spins up a temp SQLite file. It builds a
fresh isolated PostgreSQL scratch schema (``phase_i_<uuid>``) on the dedicated
``crypto_guard_test`` DB (app role ``crypto_guard_test_app``, never the ``postgres``
superuser), applies the full schema + seeds + ALL contract markers (so no
finding is wrongly demoted to ``legacy_info``), and runs all three release
gates (Schema Health, ``diagnose_state_consistency``, and
``diagnose_report_accuracy``) against a repo
bound to that schema. A freshly-initialized schema must report clean (``ok``
with zero errors/warnings). The scratch schema is dropped on exit. Fail-closed:
if the scratch connection cannot be opened or seeded the error propagates (NO
SQLite fallback).

Usage:
    python -m plugins.crypto_guard.tools._phase_i_fresh_verify
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

# Bootstrap the dedicated test DB + app role BEFORE importing config/migrations
# (which resolve ``CRYPTO_GUARD_DATABASE_URL`` at import/call time). The admin
# password is read from ``CRYPTO_GUARD_DB_ADMIN_PASSWORD`` (test env only) and
# is NEVER written to source/YAML/logs/git. The role is least-privilege
# (NOSUPERUSER); the runtime never connects as the ``postgres`` superuser.
from plugins.crypto_guard.tests._pg_bootstrap import ensure_bootstrap

_APP_DSN = ensure_bootstrap()
os.environ["CRYPTO_GUARD_DATABASE_URL"] = _APP_DSN

from plugins.crypto_guard.config import load_config
from plugins.crypto_guard.storage.migrations import (
    SCHEMA_PATH,
    _seed_strategies,
    _seed_symbols,
    check_schema_health,
)
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.diagnostics.state_consistency import diagnose_state_consistency
from plugins.crypto_guard.diagnostics.report_diagnostics import diagnose_report_accuracy


# ---------------------------------------------------------------------------
# Scratch-schema isolation (mirrors _phase_h_fault_inject._scratch_fault_repo)
# ---------------------------------------------------------------------------


def _ensure_all_contract_markers(cur: psycopg.Cursor) -> None:
    """Write every contract marker into the scratch schema.

    Production's ``initialize_database`` writes all markers after schema+seeds;
    we replicate the full set here so the diagnostic suites never demote a real
    finding to ``legacy_info`` (wrong severity) just because a marker is absent
    on the fresh schema. Marker rows land in ``_migration_state`` (created by
    the schema SQL) inside the scratch schema via ``search_path``.
    """
    from plugins.crypto_guard.storage.migrations import (
        _ensure_btc9_trade_gate_contract_marker,
        _ensure_hourly_decision_context_continuity_contract_marker,
        _ensure_hourly_market_semantic_accuracy_contract_marker,
        _ensure_hourly_report_accuracy_r4_contract_marker,
        _ensure_llm_fair_scheduling_context_contract_marker,
        _ensure_market_data_contract_marker,
        _ensure_profit_protection_cutoff_marker,
        _ensure_stop_loss_adjustment_dedup_marker,
    )

    _ensure_profit_protection_cutoff_marker(cur)
    _ensure_hourly_report_accuracy_r4_contract_marker(cur)
    _ensure_btc9_trade_gate_contract_marker(cur)
    _ensure_market_data_contract_marker(cur)
    _ensure_hourly_market_semantic_accuracy_contract_marker(cur)
    _ensure_hourly_decision_context_continuity_contract_marker(cur)
    _ensure_llm_fair_scheduling_context_contract_marker(cur)
    _ensure_stop_loss_adjustment_dedup_marker(cur)


@contextmanager
def _scratch_fresh_repo() -> Iterator[CryptoGuardRepository]:
    """Yield a repo bound to a fresh isolated scratch schema; drop it on exit.

    A dedicated (non-pooled) ``psycopg.connect`` to the test DSN with
    ``search_path`` routed to a unique scratch schema so writes never touch the
    ``public`` schema (and the production pool is never opened). The schema SQL
    is schema-agnostic (no ``public.`` qualification), so ``CREATE TABLE``
    lands in ``search_path``. We deliberately do NOT call
    ``initialize_database`` (which targets the production pool / advisory
    lock / contract markers) - we apply schema + seeds + markers directly.
    """
    schema_name = f"phase_i_{uuid.uuid4().hex}".lower()
    conn = psycopg.connect(_APP_DSN, row_factory=dict_row, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema_name}"')
        # Commit CREATE SCHEMA + SET search_path as its own transaction so the
        # schema exists before the DDL is applied into it.
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(f'SET search_path = "{schema_name}", public')
        # Apply schema + seeds + contract markers as one transaction so a
        # seed/marker failure drops nothing half-built (we DROP on exit).
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        cfg = load_config()
        with conn.cursor() as cur:
            cur.execute(schema_sql)
            _seed_symbols(cur, cfg.symbols)
            _seed_strategies(cur, cfg.strategies)
            _ensure_all_contract_markers(cur)
        conn.commit()
        yield CryptoGuardRepository(conn)
    finally:
        # Drop the scratch schema (autocommit so DROP works outside a txn) and
        # close the dedicated connection. Swallow only DROP errors so a
        # diagnostic exception still propagates from the ``yield``.
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        except Exception:
            pass
        conn.close()


def main() -> int:
    print("=" * 70)
    print("Phase I - fresh PostgreSQL DB verification of diagnostic suites")
    print("=" * 70)
    print(f"Test DB DSN: {_APP_DSN.split('@')[-1] if '@' in _APP_DSN else _APP_DSN}")
    print()

    with _scratch_fresh_repo() as repo:
        schema = check_schema_health(conn=repo.conn)
        print("Schema Health:")
        print(f"  ok={schema.get('ok')}")
        print(f"  missing_columns={len(schema.get('missing_columns') or [])}")
        if schema.get("missing_columns"):
            for item in schema["missing_columns"][:5]:
                print(f"    - {item}")

        # State consistency
        state = diagnose_state_consistency(repo)
        print("\nState Consistency:")
        print(f"  ok={state.get('ok')}")
        print(f"  total_issues={state.get('total_issues', 'n/a')}")
        print(f"  error_count={state.get('error_count', 'n/a')}")
        print(f"  warning_count={state.get('warning_count', 'n/a')}")
        if state.get("issues"):
            for i in state["issues"][:5]:
                print(f"    - {i}")

        # Report accuracy
        report = diagnose_report_accuracy(repo)
        print(f"\nReport Accuracy:")
        print(f"  ok={report.get('ok')}")
        print(f"  total_issues={report.get('total_issues', 'n/a')}")
        print(f"  error_count={report.get('error_count', 'n/a')}")
        print(f"  warning_count={report.get('warning_count', 'n/a')}")
        print(f"  legacy_info_count={report.get('legacy_info_count', 'n/a')}")
        if report.get("issues"):
            for i in report["issues"][:5]:
                print(f"    - {i}")

    # A freshly-initialized scratch schema must report clean on all three gates.
    schema_ok = bool(schema.get("ok"))
    state_ok = bool(state.get("ok")) and not state.get("error_count")
    report_ok = bool(report.get("ok")) and not report.get("error_count")
    passed = schema_ok and state_ok and report_ok
    print()
    print("=" * 70)
    print(
        "Result: "
        + ("SUCCESS - fresh DB reports clean" if passed else "FAILURE - diagnostics flagged the fresh DB")
    )
    print("=" * 70)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
