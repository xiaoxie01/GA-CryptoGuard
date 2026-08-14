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
import re
import uuid
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import sql
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
        _ensure_entry_confirmation_lifecycle_contract_marker,
        _ensure_execution_funnel_report_contract_marker,
        _ensure_hourly_decision_context_continuity_contract_marker,
        _ensure_hourly_market_semantic_accuracy_contract_marker,
        _ensure_hourly_report_accuracy_r4_contract_marker,
        _ensure_llm_failed_direction_fail_closed_marker,
        _ensure_llm_fair_scheduling_context_contract_marker,
        _ensure_llm_provider_timeout_envelope_contract_marker,
        _ensure_llm_risk_context_isolation_contract_marker,
        _ensure_llm_risk_proposal_contract_marker,
        _ensure_llm_schema_breaker_preset_integrity_marker,
        _ensure_market_data_contract_marker,
        _ensure_profit_protection_cutoff_marker,
        _ensure_risk_adjustment_verifier_contract_marker,
        _ensure_stop_loss_adjustment_dedup_marker,
        _ensure_watch_order_bridge_contract_marker,
        _ensure_watch_recheck_risk_shape_contract_marker,
        _ensure_watch_review_payload_serialization_contract_marker,
        _ensure_watch_recheck_funnel_contract_marker,
    )

    _ensure_profit_protection_cutoff_marker(cur)
    _ensure_hourly_report_accuracy_r4_contract_marker(cur)
    _ensure_btc9_trade_gate_contract_marker(cur)
    _ensure_market_data_contract_marker(cur)
    _ensure_hourly_market_semantic_accuracy_contract_marker(cur)
    _ensure_hourly_decision_context_continuity_contract_marker(cur)
    _ensure_llm_fair_scheduling_context_contract_marker(cur)
    # 07-22 Codex P1-1: independent timeout-envelope marker must be present on
    # a fresh schema so diagnose_report_accuracy does not fail-closed on
    # llm_provider_timeout_envelope_contract_marker_missing.
    _ensure_llm_provider_timeout_envelope_contract_marker(cur)
    # 07-27 Phase-2 C: llm_failed_direction fail-closed marker. Without it
    # diagnose_state_consistency reports marker_missing on a fresh DB.
    _ensure_llm_failed_direction_fail_closed_marker(cur)
    # 07-31 P1-4: schema/breaker/preset-integrity marker. Without it a fresh
    # (release-initialized) schema reports marker_missing instead of clean.
    _ensure_llm_schema_breaker_preset_integrity_marker(cur)
    # 08-02 P1-3: execution-funnel report-contract marker. Without it a fresh
    # (release-initialized) schema reports marker_missing instead of clean.
    _ensure_execution_funnel_report_contract_marker(cur)
    # 08-06 P2 (release-blocker rework): watch -> order bridge contract marker.
    # Without it a fresh (release-initialized) schema reports
    # watch_order_bridge_contract_marker_missing instead of clean.
    _ensure_watch_order_bridge_contract_marker(cur)
    # 08-08 Step 7: three watch-recheck contract markers. Without them a fresh
    # (release-initialized) schema reports the corresponding marker_missing
    # instead of clean.
    _ensure_watch_recheck_risk_shape_contract_marker(cur)
    _ensure_watch_review_payload_serialization_contract_marker(cur)
    _ensure_watch_recheck_funnel_contract_marker(cur)
    # 08-10 Step 3+9: entry-confirmation lifecycle + LLM risk-governance markers.
    # Without them a fresh (release-initialized) schema reports the corresponding
    # marker_missing instead of clean.
    _ensure_entry_confirmation_lifecycle_contract_marker(cur)
    _ensure_llm_risk_proposal_contract_marker(cur)
    _ensure_risk_adjustment_verifier_contract_marker(cur)
    _ensure_llm_risk_context_isolation_contract_marker(cur)
    _ensure_stop_loss_adjustment_dedup_marker(cur)


_FRESH_SCHEMA_RE = re.compile(r"^phase_i_[0-9a-f]{32}$")

_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")


def _safe_error_summary(exc: BaseException) -> str:
    """Short failure summary safe for external surfaces.

    Mirrors ``backtest/historical_replay._safe_error_summary``. Never
    renders ``str(exc)``/``repr(exc)``: exception messages can embed the
    DSN, credentials, or arbitrary database text. Only the exception type
    name plus an optional strictly-validated SQLSTATE
    (``^[0-9A-Z]{5}$``) is allowed. The ``sqlstate`` read is fail-safe: a
    raising getter is ignored and never replaces the underlying error.
    """
    summary = type(exc).__name__
    try:
        sqlstate = getattr(exc, "sqlstate", None)
    except Exception:
        sqlstate = None
    if isinstance(sqlstate, str) and _SQLSTATE_RE.match(sqlstate):
        summary += f" (SQLSTATE {sqlstate})"
    return summary


def _drop_fresh_schema(schema_name: str) -> str | None:
    """Drop the scratch schema via a dedicated connection; never silent.

    Mirrors ``backtest/historical_replay._drop_scratch_schema``: runs on a
    FRESH connection (autocommit) so it never depends on the fresh-repo
    connection's transaction state. The schema name must match the
    ``phase_i_<32hex>`` scratch contract before any DROP. Returns ``None`` on
    success or a short human-readable failure description (never the DSN or
    any credential) when cleanup itself failed.
    """
    if not _FRESH_SCHEMA_RE.match(schema_name):
        raise RuntimeError(f"refusing to drop non-scratch schema {schema_name!r}")
    conn: psycopg.Connection | None = None
    try:
        conn = psycopg.connect(_APP_DSN, row_factory=dict_row, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_namespace WHERE nspname = %s",
                (schema_name,),
            )
            row = cur.fetchone()
            if row is None or row["count"] != 0:
                return "schema still exists after DROP"
        return None
    except Exception as exc:
        return f"cleanup failure: {_safe_error_summary(exc)}"
    finally:
        # ``conn.close`` may itself fail (e.g. the server died mid-cleanup);
        # it must never replace the returned failure string or the in-flight
        # body exception, so guard it the same way the body guard does.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _attach_cleanup_failure(body_exc: BaseException, failure: str) -> None:
    """Attach a cleanup-failure detail to the in-flight body exception.

    Mirrors ``backtest/historical_replay._attach_cleanup_failure``.
    ``BaseException.add_note`` requires Python 3.11+ (PEP 678); on older
    runtimes fall back to chaining a ``RuntimeError`` via ``__cause__`` so the
    cleanup failure is never lost.
    """
    message = f"scratch schema cleanup failed: {failure}"
    add_note = getattr(body_exc, "add_note", None)
    if callable(add_note):
        add_note(message)
    else:
        body_exc.__cause__ = RuntimeError(message)


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
    body_exc: BaseException | None = None
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
    except BaseException as exc:
        body_exc = exc
        raise
    finally:
        # NEVER flip ``autocommit`` on a connection that may still be INTRANS
        # (psycopg raises ProgrammingError). Roll back if the connection is
        # still usable, close it, then drop the schema through a dedicated
        # cleanup connection that owns its own transaction state (mirrors
        # backtest/historical_replay._scratch_replay_repo).
        try:
            conn.rollback()
        except Exception:
            pass
        # ``conn.close`` may itself fail (e.g. the server died mid-run); it
        # must never skip the schema DROP, so guard it the same way.
        try:
            conn.close()
        except Exception:
            pass
        failure = _drop_fresh_schema(schema_name)
        if failure is not None:
            if body_exc is None:
                raise RuntimeError(
                    f"scratch schema cleanup failed: {failure}"
                ) from None
            _attach_cleanup_failure(body_exc, failure)


def main() -> int:
    print("=" * 70)
    print("Phase I - fresh PostgreSQL DB verification of diagnostic suites")
    print("=" * 70)
    print("Test DB: isolated crypto_guard_test scratch schemas (app role)")
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
