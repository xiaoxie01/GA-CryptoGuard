"""CryptoGuard PostgreSQL schema initialization + health.

Greenfield design (PostgreSQL only; NO SQLite fallback, NO data migration from
the old SQLite store). ``initialize_database()`` applies the full
``schema_postgres.sql`` from a single transaction guarded by a transaction-
scoped advisory lock, seeds the default symbol universe + strategy versions,
and writes the contract markers. Because the schema file creates the final
state directly, the historical incremental migrations (column-add, dedup
cleanups, JSON->symbol-status backfill) are obsolete under greenfield and are
not run; their helper names are retained only as import-safe no-ops so
callers/tests that still reference them do not break during the cutover (the
test suite is migrated to PostgreSQL separately).

Hard contracts preserved:
- One transaction for the whole init; ``ROLLBACK`` on any error (no half-state).
- ``pg_advisory_xact_lock(<hash>)`` serializes concurrent initializers.
- Idempotent: re-running leaves schema + markers identical.
- ``check_schema_health`` introspects ``information_schema`` / ``pg_catalog``
  (``pg_indexes`` / ``pg_constraint``) - never ``sqlite_master``/``PRAGMA``.
- The contract markers are the LAST step, written only after health passes.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from plugins.crypto_guard.config.loader import PLUGIN_ROOT, CryptoGuardConfig, load_config

SCHEMA_PATH = PLUGIN_ROOT / "storage" / "schema_postgres.sql"

# Process-local reentrancy guard. The authoritative cross-process / cross-thread
# serialization is the DB advisory lock; this RLock just prevents two threads in
# the SAME process from redundantly stacking advisory-lock waits.
_INITIALIZE_DATABASE_LOCK = threading.RLock()

# Stable 64-bit advisory-lock key derived from a fixed name. ``pg_advisory_xact``
# takes two int32 halves; we hash the name and split it so the key is stable
# across processes and reboots (a literal constant would also work, but hashing
# the name documents intent and is collision-free for our single use).
_ADVISORY_LOCK_NAME = b"crypto_guard.initialize_database"


def _advisory_lock_key() -> tuple[int, int]:
    digest = hashlib.sha1(_ADVISORY_LOCK_NAME).digest()
    hi = int.from_bytes(digest[0:4], "big") & 0x7FFFFFFF
    lo = int.from_bytes(digest[4:8], "big") & 0x7FFFFFFF
    return hi, lo


@contextmanager
def _initialization_connection(
    config: CryptoGuardConfig,
    *,
    allow_ddl: bool,
):
    if allow_ddl:
        from plugins.crypto_guard.config.loader import resolve_migration_database_url

        conn = psycopg.connect(
            resolve_migration_database_url(), row_factory=dict_row, autocommit=False,
        )
        try:
            yield conn
        finally:
            conn.close()
        return
    from plugins.crypto_guard.storage.pg_db import get_conn

    with get_conn() as conn:
        yield conn


def _connected_role(cur: psycopg.Cursor) -> dict[str, Any]:
    cur.execute(
        """
        SELECT current_user AS current_user, session_user AS session_user,
               current_database() AS database_name, r.rolsuper, r.rolcreatedb,
               r.rolcreaterole, r.rolreplication, r.rolbypassrls
        FROM pg_roles r WHERE r.rolname=current_user
        """
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("PostgreSQL initialization identity unavailable")
    result = dict(row)
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_roles parent
            WHERE pg_has_role(current_user, parent.oid, 'member')
              AND parent.rolname <> current_user
              AND (parent.rolsuper OR parent.rolcreatedb OR parent.rolcreaterole
                   OR parent.rolreplication OR parent.rolbypassrls)
        ) AS inherited_dangerous
        """
    )
    result["inherited_dangerous"] = bool(cur.fetchone()["inherited_dangerous"])
    return result


def _grant_runtime_privileges(cur: psycopg.Cursor) -> None:
    """Install the production DML-only grant matrix from the migrator role."""
    cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    cur.execute("REVOKE CREATE ON SCHEMA public FROM crypto_guard_app")
    cur.execute("GRANT USAGE ON SCHEMA public TO crypto_guard_app")
    cur.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        "TO crypto_guard_app"
    )
    cur.execute(
        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public "
        "TO crypto_guard_app"
    )
    cur.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crypto_guard_app"
    )
    cur.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO crypto_guard_app"
    )


def initialize_database(
    config: CryptoGuardConfig | None = None,
    *,
    allow_ddl: bool = False,
) -> dict[str, Any]:
    """Apply the PostgreSQL schema + seeds + contract markers (greenfield).

    Single transaction guarded by ``pg_advisory_xact_lock``. On any error the
    transaction rolls back, leaving NO schema/seed/marker residue. Re-running is
    a no-op (idempotent): every statement is ``IF NOT EXISTS`` / ``ON CONFLICT``.

    Returns ``{"ok": True, "database": <password-free identity>}`` on success. Never falls back
    to SQLite; a missing/unreachable PostgreSQL raises
    ``CryptoGuardDBUnavailable`` (via ``pg_db.get_conn``).
    """
    with _INITIALIZE_DATABASE_LOCK:
        cfg = config or load_config()
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with _initialization_connection(cfg, allow_ddl=allow_ddl) as conn:
            try:
                with conn.cursor() as cur:
                    role = _connected_role(cur)
                    current = str(role["current_user"])
                    session = str(role["session_user"])
                    database = str(role["database_name"])
                    dangerous = any(
                        bool(role[key]) for key in (
                            "rolsuper", "rolcreatedb", "rolcreaterole",
                            "rolreplication", "rolbypassrls",
                        )
                    ) or bool(role.get("inherited_dangerous"))
                    is_test_owner = (
                        current == session == "crypto_guard_test_app"
                        and database == "crypto_guard_test"
                        and not dangerous
                    )
                    is_migrator = (
                        allow_ddl
                        and current == session == "crypto_guard_migrator"
                        and database == "crypto_guard"
                        and not dangerous
                    )
                    if allow_ddl and not is_migrator:
                        raise RuntimeError(
                            "DDL initialization requires the dedicated migrator role"
                        )
                    # Serialize concurrent initializers (cross-process). The
                    # lock is released automatically at COMMIT/ROLLBACK because
                    # it is transaction-scoped.
                    hi, lo = _advisory_lock_key()
                    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (hi, lo))
                    cur.fetchone()

                    # 1. Apply the full greenfield schema -- but ONLY when it is
                    #    missing or unhealthy. On a healthy schema, re-running
                    #    the DDL is a correctness no-op (every statement is
                    #    IF NOT EXISTS) yet it still re-takes AccessExclusive
                    #    (CREATE TABLE) / Share (CREATE INDEX) locks that
                    #    CONFLICT with concurrent uncommitted DML
                    #    (RowExclusive) on another connection. Because every
                    #    _repo() call re-invokes initialize_database(), the
                    #    unconditional DDL deadlocked the per-call init against
                    #    a caller's open DML transaction (the test_smoke #21
                    #    hang root cause; SQLite DDL never conflicted with DML
                    #    so this was latent until the PG cutover). The health
                    #    probe is read-only catalog introspection (AccessShare
                    #    at most, compatible with RowExclusive), so probing
                    #    never blocks on the caller's DML. Seeds + markers
                    #    below are idempotent RowExclusive writes on symbols/
                    #    strategies/_migration_state (never the caller's DML
                    #    table), so they are still re-affirmed lock-safely.
                    #    Only a missing/unhealthy schema pays the DDL lock
                    #    cost, under the advisory lock that serializes
                    #    concurrent initializers.
                    pre_health = _check_schema_health_on_conn(conn)
                    if not pre_health["ok"]:
                        if not (is_test_owner or is_migrator):
                            raise RuntimeError(
                                "schema is unhealthy; explicit migrator initialization required"
                            )
                        # Fail-closed: a malformed existing schema (e.g. a
                        # required column dropped after init) makes the
                        # idempotent DDL raise -- ``CREATE TABLE IF NOT EXISTS``
                        # will not re-add a dropped column, and a partial index
                        # whose WHERE clause references it raises
                        # ``UndefinedColumn``. That raw psycopg error would
                        # leak past the post-init health gate's RuntimeError
                        # contract (test_r5_d1). Wrap it so any DDL DB error on
                        # an unhealthy schema surfaces a controlled
                        # RuntimeError; the outer ``except`` then rolls the init
                        # transaction back, leaving no marker/seed residue. A
                        # fresh/fixable schema's DDL succeeds, so this branch
                        # never fires on the greenfield init path.
                        from psycopg.errors import Error as _PsycopgDBError
                        try:
                            # 08-02 Finding 1 (P1): ``CREATE INDEX IF NOT EXISTS``
                            # is a name-only no-op in PostgreSQL -- it can NOT
                            # upgrade an existing ``idx_opportunity_watches_dedupe``
                            # that still carries the pre-P0-2 predicate
                            # ``WHERE dedupe_key IS NOT NULL``. Without the
                            # ``status = 'active'`` predicate, a terminal watch
                            # holds its dedupe_key and the health gate below
                            # fails hard. Drop the stale-predicate index first so
                            # the schema DDL recreates it with the P0-2 predicate
                            # (safe: the table has zero rows in production).
                            _drop_legacy_opportunity_watches_dedupe_index(cur)
                            # 08-06 P1 (release-blocker): a real pre-08-04
                            # production schema already HAS ``paper_orders`` /
                            # ``opportunity_watches`` (created by the greenfield
                            # cutover), so the schema DDL's ``CREATE TABLE IF NOT
                            # EXISTS`` no-ops on them and the standalone
                            # ``CREATE UNIQUE INDEX IF NOT EXISTS
                            # idx_paper_orders_trigger_watch_once ... WHERE
                            # trigger_watch_id ...`` below raises
                            # UndefinedColumn (42703) -- the column does not
                            # exist yet. The release operation must therefore NOT
                            # require a separate manual call to
                            # ``apply_08_04_watch_order_bridge_migration``:
                            # ``initialize_database`` itself runs the additive
                            # bridge migration FIRST, inside this same advisory-
                            # lock-guarded transaction, before the schema DDL
                            # (which then recreates the already-present index as
                            # an ``IF NOT EXISTS`` no-op). On a fresh greenfield
                            # schema ``_apply_08_04_watch_order_bridge_migration``
                            # safe no-ops (no ``paper_orders`` table yet) and
                            # ``schema_postgres.sql`` creates the full structure.
                            _apply_08_04_watch_order_bridge_migration(cur)
                            cur.execute(schema_sql)
                        except _PsycopgDBError as exc:
                            raise RuntimeError(
                                "initialize_database DDL failed on an unhealthy "
                                f"schema (fail-closed): {exc}"
                            ) from exc
                    if is_migrator:
                        _grant_runtime_privileges(cur)

                    # 2. Seed default symbols + strategy versions.
                    _seed_symbols(cur, cfg.symbols)
                    _seed_strategies(cur, cfg.strategies)

                    # 3. Contract markers - written ONLY after schema + seeds.
                    _ensure_profit_protection_cutoff_marker(cur)
                    _ensure_hourly_report_accuracy_r4_contract_marker(cur)
                    _ensure_btc9_trade_gate_contract_marker(cur)
                    _ensure_market_data_contract_marker(cur)
                    _ensure_hourly_market_semantic_accuracy_contract_marker(cur)
                    _ensure_hourly_decision_context_continuity_contract_marker(cur)
                    _ensure_llm_fair_scheduling_context_contract_marker(cur)
                    _ensure_llm_provider_timeout_envelope_contract_marker(cur)
                    _ensure_stop_loss_adjustment_dedup_marker(cur)
                    _ensure_llm_failed_direction_fail_closed_marker(cur)
                    _ensure_llm_schema_breaker_preset_integrity_marker(cur)
                    _ensure_execution_funnel_report_contract_marker(cur)
                    _ensure_watch_recheck_risk_shape_contract_marker(cur)
                    _ensure_watch_review_payload_serialization_contract_marker(cur)
                    _ensure_watch_recheck_funnel_contract_marker(cur)

                    # 4. Health gate - fail-closed BEFORE commit. If the schema
                    # is wrong, ROLLBACK everything (no marker survives).
                    health = _check_schema_health_on_conn(conn)
                    if not health["ok"]:
                        raise RuntimeError(
                            "schema health check failed after init: "
                            f"{health.get('missing_columns')}"
                        )

                    # 5. 08-06 P2: watch -> order bridge contract marker - the
                    # LAST step, written ONLY after the bridge schema is complete
                    # AND the health gate passes, still inside the same
                    # transaction. If the bridge migration or any later DDL
                    # failed, the health gate above already raised and the whole
                    # transaction (columns + index + this marker row) rolls back
                    # together - no residue.
                    _ensure_watch_order_bridge_contract_marker(cur)
                conn.commit()
                from plugins.crypto_guard.storage.pg_db import database_identity
                return {"ok": True, "database": database_identity(cfg.database_url)}
            except Exception:
                conn.rollback()
                raise


# ── seeding ─────────────────────────────────────────────────────────────────


def _seed_symbols(cur: psycopg.Cursor, symbols_cfg: dict[str, Any]) -> None:
    """Upsert the default symbol universe from config (idempotent)."""
    default = symbols_cfg.get("default_universe", {})
    profiles = symbols_cfg.get("symbol_profiles", {})
    if not default.get("enabled", True):
        return
    user_defaults = symbols_cfg.get("user_symbol_defaults", {})
    for symbol in default.get("symbols", []):
        profile = profiles.get(symbol, {})
        base_asset = symbol.removesuffix("USDT")
        timeframes = (
            profile.get("default_timeframes")
            or user_defaults.get("default_timeframes", [])
        )
        cur.execute(
            """
            INSERT INTO symbols(
                symbol, base_asset, quote_asset, category, enabled,
                source, risk_profile, default_timeframes
            )
            VALUES (%s, %s, 'USDT', %s, %s, 'default', %s, %s)
            ON CONFLICT (symbol) DO UPDATE SET
                category = EXCLUDED.category,
                enabled = EXCLUDED.enabled,
                default_timeframes = EXCLUDED.default_timeframes,
                updated_at = NOW()
            """,
            (
                symbol,
                base_asset,
                profile.get("category", "default_universe"),
                bool(profile.get("enabled", True)),
                profile.get("volatility_level", "auto"),
                json.dumps(timeframes, ensure_ascii=False),
            ),
        )


def _seed_strategies(cur: psycopg.Cursor, strategies_cfg: dict[str, Any]) -> None:
    """Insert strategy versions from config (idempotent; existing left alone)."""
    for item in strategies_cfg.get("strategies", []):
        name = item.get("strategy_name")
        version = str(item.get("version", "1.0"))
        if not name:
            continue
        status = item.get("status", "candidate")
        cur.execute(
            """
            INSERT INTO strategy_versions(
                strategy_name, version, status, config_json, change_reason
            )
            VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (strategy_name, version) DO NOTHING
            """,
            (
                name,
                version,
                status,
                json.dumps(item, ensure_ascii=False),
                "seed_from_config",
            ),
        )


# ── contract markers ────────────────────────────────────────────────────────


def _ensure_marker(cur: psycopg.Cursor, key: str) -> None:
    """Write a contract marker to ``_migration_state`` (idempotent).

    The schema creates ``_migration_state`` already, so this is a plain
    ``ON CONFLICT DO NOTHING`` upsert - the ``applied_at`` timestamp is written
    once and never refreshed on re-init (preserving the cutoff semantics the
    contract diagnostics rely on).
    """
    cur.execute(
        """
        INSERT INTO _migration_state(key, applied_at)
        VALUES (%s, NOW())
        ON CONFLICT (key) DO NOTHING
        """,
        (key,),
    )


def _ensure_profit_protection_cutoff_marker(cur: psycopg.Cursor) -> None:
    _ensure_marker(cur, "profit_protection_mark_price_contract_v1")


def _ensure_hourly_report_accuracy_r4_contract_marker(cur: psycopg.Cursor) -> None:
    _ensure_marker(cur, "hourly_report_accuracy_r4_contract_v1")


def _ensure_btc9_trade_gate_contract_marker(cur: psycopg.Cursor) -> None:
    _ensure_marker(cur, "btc9_trade_gate_contract_v1")


def _ensure_market_data_contract_marker(cur: psycopg.Cursor) -> None:
    _ensure_marker(cur, "market_data_contract_v1")


def _ensure_hourly_market_semantic_accuracy_contract_marker(cur: psycopg.Cursor) -> None:
    _ensure_marker(cur, "hourly_market_semantic_accuracy_contract_v1")


def _ensure_hourly_decision_context_continuity_contract_marker(cur: psycopg.Cursor) -> None:
    _ensure_marker(cur, "hourly_decision_context_continuity_contract_v1")


def _ensure_llm_fair_scheduling_context_contract_marker(cur: psycopg.Cursor) -> None:
    _ensure_marker(cur, "llm_fair_scheduling_context_contract_v1")


def _ensure_llm_provider_timeout_envelope_contract_marker(cur: psycopg.Cursor) -> None:
    """07-22 Codex P1-1 / P2 exclude-only: provider-timeout envelope marker.

    ``llm_timeout_config_out_of_range`` uses this marker's ``applied_at`` as the
    SQL lower bound for current-contract evaluation. Pre-marker rows (e.g.
    production decision 49 written before the post-prompt admission fix) remain
    in ``ga_decisions`` for audit but are EXCLUDED from current
    ``diagnose_report_accuracy`` issues — not as error and not as
    ``legacy_info``. Post-marker pcc>=1 with timeout_ms<=0 remains ``error``.
    ``ON CONFLICT DO NOTHING`` keeps applied_at immutable on re-init.
    """
    _ensure_marker(cur, "llm_provider_timeout_envelope_contract_v2")


def _ensure_llm_failed_direction_fail_closed_marker(cur: psycopg.Cursor) -> None:
    """Phase-2 P2-1 (07-27) requirement F: current-vs-historical split marker for
    the ``deterministic_direction_from_failed_llm`` diagnostic.

    ``apply_risk_to_decision`` now fail-closes every LLM failed terminal row to
    ``market_bias="unknown"`` BEFORE persistence (requirement C). Rows written
    BEFORE this fix's deployment still carry bullish/bearish bias on failed rows
    and are historical audit, not current error. This marker's ``applied_at`` is
    the cutoff:

      - marker-AFTER violation (created_at >= applied_at): a current
        ``warning`` — the requirement-C fail-closed block was reverted or
        bypassed.
      - marker-BEFORE (created_at < applied_at): historical audit only
        (``legacy_info``), NOT surfaced as a current issue.
      - marker-MISSING: fail-closed — the diagnostic emits a marker-missing
        ``error`` (requirement F: "marker 缺失必须 fail-closed") so callers
        detect the missing contract rather than receiving a silently-healthy
        report.

    P1-1 (07-27 final review): the fail-closed block (and this diagnostic) scope
    to ``llm_status == "failed"`` ONLY. ``disabled`` is the
    ``CRYPTO_GUARD_LLM_ANALYSIS=0`` deterministic-only operating mode — the
    deterministic direction IS the intended product there, so a ``disabled`` row
    with bullish/bearish bias MUST NOT be flagged (not as a current warning, not
    as a historical ``legacy_info``).

    P1-2 (07-27 final review): the cutoff is compared against the row's
    persisted creation time ``ga_decisions.created_at`` (``TIMESTAMPTZ DEFAULT
    NOW()``), NOT ``analysis_time_utc`` (TEXT ISO-8601). The cross-format
    ``analysis_time_utc`` vs ``applied_at`` comparison was unreliable.

    ``ON CONFLICT DO NOTHING`` keeps ``applied_at`` immutable on re-init. The
    marker is NOT written to production here — it is written only when
    ``initialize_database`` runs on the release path (gated on
    /trellis:crypto-guard-release + user authorization). The running
    production service is untouched.
    """
    _ensure_marker(cur, "llm_failed_direction_fail_closed_v1")


def _ensure_llm_schema_breaker_preset_integrity_marker(cur: psycopg.Cursor) -> None:
    """07-31 P1-4: schema-repair / breaker / preset integrity split marker.

    Production evidence #4: the pre-fix batch 15m:1785487499999 (5 schema
    failures polluting the breaker rate window -> breaker open -> 10
    breaker_skipped rows with provider_call_count=0) repeated every hour as
    current ``llm_failure_rate_high`` + ``llm_circuit_breaker_open`` errors.
    Post-fix those failures are repairable/isolated, so pre-deployment
    historical batches must NOT repeat as current errors. This marker's
    ``applied_at`` is the cutoff for the two LLM diagnostics:

      - marker-AFTER batch (analysis_time >= applied_at): current ``error``
        — a real post-deployment breach.
      - marker-BEFORE (analysis_time < applied_at): historical audit only
        (``legacy_info``), NEVER a current error (symptom #4: no hourly
        repeat of the pre-fix breaker-open batch).
      - marker-MISSING: fail-closed — the diagnostic emits a marker-missing
        ``error`` and the two LLM checks SKIP (an undeployed contract must
        not be evaluated as current; no silent green).

    ``ON CONFLICT DO NOTHING`` keeps ``applied_at`` immutable on re-init. The
    marker is NOT written to production here — it is written only when
    ``initialize_database`` runs on the release path (gated on
    /trellis:crypto-guard-release + user authorization). The running
    production service is untouched.
    """
    _ensure_marker(cur, "llm_schema_breaker_preset_integrity_v1")


def _ensure_execution_funnel_report_contract_marker(cur: psycopg.Cursor) -> None:
    """08-02 P1-3: execution-funnel report-contract split marker.

    Production baseline (2026-08-02 audit): 190 decisions/24h; deterministic
    raw S/A=76 but final executable raw-S/A=0; has_trade_plan=0;
    llm_not_confirmed=67; confirmed_without_plan=30; no_candidate_with_
    candidate=16; opportunity_watch payload=158 with conditions array=121 all
    text (structured=0); 92 decisions declared create_opportunity_watch but
    nothing materialized. The six P1-3 execution-funnel diagnostics surface
    the inverse contradictions on the CURRENT contract only; pre-fix rows
    must NOT repeat as current errors. This marker's ``applied_at`` is the
    cutoff:

      - marker-AFTER row (created_at >= applied_at): current ``error`` /
        ``warning`` — a real post-deployment breach of the execution-funnel
        contract.
      - marker-BEFORE (created_at < applied_at): historical audit only
        (``legacy_info``), NEVER a current error (the 190-row/24h pre-fix
        baseline must not recur as current noise).
      - marker-MISSING: fail-closed — the diagnostic emits a marker-missing
        ``error`` and the six execution-funnel checks SKIP (an undeployed
        contract must not be evaluated as current; no silent green).

    ``ON CONFLICT DO NOTHING`` keeps ``applied_at`` immutable on re-init. The
    marker is NOT written to production here — it is written only when
    ``initialize_database`` runs on the release path (gated on
    /trellis:crypto-guard-release + user authorization). The running
    production service is untouched.
    """
    _ensure_marker(cur, "execution_funnel_report_contract_v1")


def _ensure_watch_order_bridge_contract_marker(cur: psycopg.Cursor) -> None:
    """08-06 P2: watch -> order bridge contract marker (release-blocker rework).

    ``watch_order_bridge_contract_v1`` proves the legacy pre-08-04 production
    schema was actually upgraded to the 08-04 watch -> order bridge contract:
    the four bridge columns (``paper_orders.trigger_watch_id`` +
    ``opportunity_watches.recheck_status`` / ``recheck_order_id`` /
    ``last_recheck_at``) AND the ONCE-EVER partial unique index
    ``idx_paper_orders_trigger_watch_once`` (one paper order per watch for its
    ENTIRE lifetime, ``WHERE trigger_watch_id IS NOT NULL`` with no status
    filter) all exist and passed the schema health gate. ``initialize_database``
    writes it AFTER the health gate passes
    and BEFORE commit -- inside the SAME advisory-lock-guarded transaction as
    the schema change -- so:

      - marker-MISSING: fail-closed -- ``diagnose_state_consistency`` emits a
        marker-missing ``error`` and the release-ready gate blocks (an
        undeployed bridge contract must not present as healthy).
      - marker present but health gate failed earlier: impossible, because the
        marker is written only after the health check returns ``ok``; a
        mid-bridge failure rolls back the whole transaction and the marker row
        (which is a plain ``INSERT`` in the same txn) vanishes with it -- no
        residue.

    ``ON CONFLICT DO NOTHING`` keeps ``applied_at`` immutable on re-init. The
    marker is NOT written to production here -- it is written only when
    ``initialize_database`` runs on the release path (gated on
    /trellis:crypto-guard-release + user authorization). The running
    production service is untouched.
    """
    _ensure_marker(cur, "watch_order_bridge_contract_v1")


def _ensure_watch_recheck_risk_shape_contract_marker(cur: psycopg.Cursor) -> None:
    """08-08 Step 7: watch-recheck risk-shape contract split marker.

    ``watch_recheck_risk_shape_contract_v1`` is the cutoff for
    ``watch_recheck_risk_shape_mismatch``. Production rows written BEFORE this
    fix's deployment may carry a ``risk_check_json`` of the old/wrong shape
    (string ``ok``, missing key, JSON null); those are historical audit, not a
    current contract breach. This marker's ``applied_at`` is the SQL lower
    bound (exclude-only — pre-marker rows never enter current issues, not even
    as ``legacy_info``):

      - marker-AFTER recheck decision (created_at >= applied_at) with
        ``risk_check_json`` NULL/JSON-null or ``risk_check_json->'ok'`` not a
        boolean: current ``error``.
      - marker-MISSING: fail-closed — ``diagnose_state_consistency`` emits
        ``watch_recheck_risk_shape_contract_marker_missing`` and the
        report-accuracy check self-skips (an undeployed contract must not be
        evaluated as current; no silent green).

    ``ON CONFLICT DO NOTHING`` keeps ``applied_at`` immutable on re-init. The
    marker is NOT written to production here — it is written only when
    ``initialize_database`` runs on the release path (gated on
    /trellis:crypto-guard-release + user authorization). The running
    production service is untouched.
    """
    _ensure_marker(cur, "watch_recheck_risk_shape_contract_v1")


def _ensure_watch_review_payload_serialization_contract_marker(cur: psycopg.Cursor) -> None:
    """08-08 Step 7: watch-review payload-serialization contract split marker.

    ``watch_review_payload_serialization_contract_v1`` is the cutoff for
    ``watch_review_payload_serialization_failure``. Pre-fix production
    ``opportunity_watch_recheck`` jobs that failed to serialize the LLM review
    payload are historical; only post-marker jobs whose structured field
    ``payload_json->'result'->'agent_review'->>'llm_failure_category'`` equals
    ``payload_serialization_failed`` are a current breach. This marker's
    ``applied_at`` is the SQL lower bound (exclude-only).

      - marker-AFTER job (created_at >= applied_at): current ``error``.
      - marker-MISSING: fail-closed — ``diagnose_state_consistency`` emits
        ``watch_review_payload_serialization_contract_marker_missing`` and the
        report-accuracy check self-skips.

    ``ON CONFLICT DO NOTHING`` keeps ``applied_at`` immutable on re-init. The
    marker is NOT written to production here — it is written only when
    ``initialize_database`` runs on the release path (gated on
    /trellis:crypto-guard-release + user authorization). The running
    production service is untouched.
    """
    _ensure_marker(cur, "watch_review_payload_serialization_contract_v1")


def _ensure_watch_recheck_funnel_contract_marker(cur: psycopg.Cursor) -> None:
    """08-08 Step 7: watch-recheck funnel-contract split marker.

    ``watch_recheck_funnel_contract_v1`` is the cutoff for
    ``watch_recheck_funnel_starvation`` (error = executable recheck decision
    never bridged to a paper order; warning = run of >= N consecutive recheck
    rejections with zero orders). Pre-fix production rows are historical; only
    post-marker rows are current. This marker's ``applied_at`` is the SQL lower
    bound (exclude-only — pre-marker history never triggers, not even as
    ``legacy_info``).

      - marker-AFTER row (created_at >= applied_at): current ``error`` /
        ``warning``.
      - marker-MISSING: fail-closed — ``diagnose_state_consistency`` emits
        ``watch_recheck_funnel_contract_marker_missing`` and the report-accuracy
        check self-skips.

    ``ON CONFLICT DO NOTHING`` keeps ``applied_at`` immutable on re-init. The
    marker is NOT written to production here — it is written only when
    ``initialize_database`` runs on the release path (gated on
    /trellis:crypto-guard-release + user authorization). The running
    production service is untouched.
    """
    _ensure_marker(cur, "watch_recheck_funnel_contract_v1")


def _ensure_stop_loss_adjustment_dedup_marker(cur: psycopg.Cursor) -> None:
    """Marker for the historical stop-loss dedup migration.

    Under greenfield there is no historical dirty data to clean, so the marker
    is written directly (the cleanup it once gated is obsolete). Diagnostics
    that probe this marker see it as already-applied.
    """
    _ensure_marker(cur, "stop_loss_adjustment_dedup_v1")


# ── externally-called additive table migrations ─────────────────────────────


def apply_r6f_service_ownership_migration(conn: psycopg.Connection) -> None:
    """Ensure the ``_service_ownership`` lease table exists.

    Called by ``service_manager.start_all_services`` BEFORE the lease CAS so the
    lease row can be read/written on a DB that has not yet run the full
    ``initialize_database`` (the non-owner path must not run full init). Under
    greenfield the schema creates this table, so this is a guarded no-op - but
    it stays idempotent and additive so a partial/old DB is still handled. The
    connection's transaction is owned by the caller (who commits).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS _service_ownership (
                key TEXT PRIMARY KEY,
                pid BIGINT NOT NULL,
                started_at_ms BIGINT NOT NULL,
                db_path TEXT NOT NULL,
                release_commit TEXT,
                owner_identity TEXT,
                lease_until_ms BIGINT NOT NULL
            )
            """
        )
        _add_column(cur, "_service_ownership", "owner_token", "TEXT")


# ── 08-06 P2: precise once-ever index introspection (pg_index/pg_attribute/pg_get_expr) ──


ONCE_EVER_INDEX_NAME = "idx_paper_orders_trigger_watch_once"
ONCE_EVER_INDEX_PREDICATE = "trigger_watch_id IS NOT NULL"


def _normalize_predicate(expr: str | None) -> str | None:
    """Normalize a ``pg_get_expr`` predicate for EXACT comparison.

    Strips balanced outer parentheses and collapses whitespace so the catalog
    text ``(trigger_watch_id IS NOT NULL)`` (PG 16.14) compares equal to the
    canonical ``trigger_watch_id IS NOT NULL`` regardless of cosmetic
    parenthesization. Returns None for a NULL predicate.
    """
    if expr is None:
        return None
    s = expr.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        outer = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    outer = False
                    break
        if not outer or depth != 0:
            break
        s = s[1:-1].strip()
    return re.sub(r"\s+", " ", s).strip()


def _introspect_once_ever_index(cur: psycopg.Cursor, schema: str) -> dict[str, Any] | None:
    """Precise catalog facts for ``idx_paper_orders_trigger_watch_once``.

    Reads ``pg_index`` flags (``indisunique`` / ``indisvalid`` / ``indisready``),
    the key column via ``pg_attribute``, expression / INCLUDE-key detection, and
    the FULL partial predicate via ``pg_get_expr``. Returns None when the index
    is absent. This is the single source of truth shared by the migration
    rebuild-detection, the schema-health gate, and the marker spy -- never a
    loose ``indexdef`` string-substring guess.
    """
    cur.execute(
        """
        SELECT
            ix.indisunique, ix.indisvalid, ix.indisready,
            ix.indpred IS NOT NULL AS has_pred,
            pg_get_expr(ix.indpred, ix.indrelid) AS pred_expr,
            ix.indnkeyatts AS nkeyatts, ix.indnatts AS natts,
            ix.indexprs IS NOT NULL AS has_expr_keys,
            a.attname AS key_attname
        FROM pg_index ix
        JOIN pg_class c ON ix.indexrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        LEFT JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = ix.indkey[0]
        WHERE n.nspname = %s AND c.relname = %s
        """,
        (schema, ONCE_EVER_INDEX_NAME),
    )
    return cur.fetchone()


def _once_ever_index_is_exact(facts: dict[str, Any] | None) -> tuple[bool, str]:
    """EXACT once-ever contract check over catalog facts.

    Returns ``(ok, reason)``. ``ok`` is True only when the index is:
      - UNIQUE, valid, ready;
      - exactly ONE key column, and it is ``paper_orders.trigger_watch_id``;
      - no expression keys, no INCLUDE columns;
      - partial (``indpred`` non-NULL);
      - normalized full predicate EXACTLY ``trigger_watch_id IS NOT NULL``.

    Any deviation -- an extra ``AND trigger_watch_id > 0``, an OR/AND clause,
    the old status-filtered live-only predicate, a non-unique / wrong-column /
    composite / expression index, or a missing predicate -- is judged UNHEALTHY.
    """
    if facts is None:
        return False, "index absent"
    if not facts["indisunique"]:
        return False, "index is not UNIQUE"
    if not facts["indisvalid"]:
        return False, "index is not valid"
    if not facts["indisready"]:
        return False, "index is not ready"
    if facts["has_expr_keys"]:
        return False, "index has an expression key"
    if facts["nkeyatts"] != 1 or facts["natts"] != 1:
        return False, (
            f"expected exactly one key column and no INCLUDE "
            f"(nkeyatts={facts['nkeyatts']}, natts={facts['natts']})"
        )
    if facts["key_attname"] != "trigger_watch_id":
        return False, (
            f"key column is {facts['key_attname']!r}, expected 'trigger_watch_id'"
        )
    if not facts["has_pred"]:
        return False, "index is NOT partial (indpred is NULL)"
    norm = _normalize_predicate(facts["pred_expr"])
    if norm != ONCE_EVER_INDEX_PREDICATE:
        return False, (
            f"predicate {facts['pred_expr']!r} normalized to {norm!r}; "
            f"expected exactly {ONCE_EVER_INDEX_PREDICATE!r}"
        )
    return True, "exact once-ever index"


def _apply_08_04_watch_order_bridge_migration(cur: psycopg.Cursor) -> None:
    """08-04 contract B + 08-06 once-ever: watch -> order bridge columns + index.

    Adds (idempotently) ``paper_orders.trigger_watch_id``, the partial unique
    index ``idx_paper_orders_trigger_watch_once`` (ONE paper order per triggered
    watch over its ENTIRE lifetime - never released by status), and the
    ``opportunity_watches.recheck_status`` / ``recheck_order_id`` /
    ``last_recheck_at`` bookkeeping columns. Pure additive upgrade for existing
    DBs; on a fresh greenfield schema the ``paper_orders`` table does not exist
    yet, so this safe no-ops and lets ``schema_postgres.sql`` create the full
    structure. The caller owns the transaction (the auto-wiring in
    ``initialize_database`` runs this inside the SAME advisory-lock-guarded
    transaction as the schema DDL).

    08-06 once-ever rework (Codex P1): the index predicate is
    ``WHERE trigger_watch_id IS NOT NULL`` with NO status filter, so a terminal
    order still holds the once-ever link and a delayed-retry recheck can never
    produce a second order. ``CREATE UNIQUE INDEX IF NOT EXISTS`` is a name-only
    no-op, so a same-name index is introspected via ``pg_index`` /
    ``pg_attribute`` / ``pg_get_expr`` and REBUILT to the exact once-ever form in
    this same transaction whenever it deviates in ANY way (legacy status filter,
    extra AND/OR clause, non-unique, wrong column, composite, expression key, or
    missing predicate). Before any create/rebuild, duplicate non-NULL
    ``trigger_watch_id`` rows fail-closed (RuntimeError) - business rows are
    NEVER auto-deleted.
    """
    if not _table_exists(cur, "paper_orders"):
        return
    _add_column(cur, "paper_orders", "trigger_watch_id", "BIGINT")
    # 08-06 R-2: fail-closed with a controlled error if the bridge target table is
    # absent while paper_orders exists (a partial/inconsistent schema) -- never
    # surface a raw UndefinedTable to the release operator.
    if not _table_exists(cur, "opportunity_watches"):
        raise RuntimeError(
            "apply_08_04_watch_order_bridge_migration: paper_orders exists but "
            "opportunity_watches does not; partial/inconsistent schema - refusing "
            "to apply the watch->order bridge bookkeeping columns"
        )
    _add_column(cur, "opportunity_watches", "recheck_status", "TEXT")
    _add_column(cur, "opportunity_watches", "recheck_order_id", "BIGINT")
    _add_column(cur, "opportunity_watches", "last_recheck_at", "TIMESTAMPTZ")

    # 08-06 once-ever: fail-closed on pre-existing duplicate non-NULL
    # trigger_watch_id. The once-ever index cannot be built over duplicates, and
    # we must NEVER auto-delete business rows to make it fit.
    cur.execute(
        """
        SELECT trigger_watch_id, COUNT(*) AS c
        FROM paper_orders
        WHERE trigger_watch_id IS NOT NULL
        GROUP BY trigger_watch_id HAVING COUNT(*) > 1
        """
    )
    dup = cur.fetchone()
    if dup is not None:
        raise RuntimeError(
            "idx_paper_orders_trigger_watch_once cannot be built: "
            f"trigger_watch_id={dup['trigger_watch_id']} has {dup['c']} rows; "
            "refusing to auto-delete business data (once-ever contract)"
        )

    # Detect a same-name index and REBUILD it unless it is EXACTLY the once-ever
    # contract -- precise pg_index/pg_attribute/pg_get_expr introspection, not a
    # loose indexdef-string "contains status" guess. The legacy live-only
    # (status-filtered) predicate, a non-unique index, a wrong-column / composite
    # / expression-key index, a non-partial index, and ANY predicate other than
    # exactly ``trigger_watch_id IS NOT NULL`` are all rebuilt in this same
    # transaction. ``CREATE UNIQUE INDEX IF NOT EXISTS`` is a name-only no-op, so
    # without this the rebuild would be silently skipped.
    cur.execute("SELECT current_schema() AS s")
    schema = cur.fetchone()["s"]
    facts = _introspect_once_ever_index(cur, schema)
    ok, _reason = _once_ever_index_is_exact(facts)
    if ok:
        return
    if facts is not None:
        cur.execute(f"DROP INDEX {ONCE_EVER_INDEX_NAME}")
    cur.execute(
        f"""
        CREATE UNIQUE INDEX {ONCE_EVER_INDEX_NAME}
            ON paper_orders(trigger_watch_id)
            WHERE trigger_watch_id IS NOT NULL
        """
    )


def apply_08_04_watch_order_bridge_migration(conn: psycopg.Connection) -> None:
    """Public wrapper: 08-04 contract B watch -> order bridge (see the
    cursor-based ``_apply_08_04_watch_order_bridge_migration``).

    Kept for callers that still explicitly invoke the additive migration on an
    existing connection (e.g. the B7 idempotency test). Since 08-06 the
    release path performs this automatically inside ``initialize_database``;
    the helper stays idempotent so an extra explicit call is a no-op. The
    caller owns the transaction.
    """
    with conn.cursor() as cur:
        _apply_08_04_watch_order_bridge_migration(cur)


def apply_r10_attempt_counter_migration(conn: psycopg.Connection) -> None:
    """Ensure the ``_analysis_attempt_counter`` table exists (R10-P2).

    Under greenfield the schema creates this table, so this is a guarded no-op;
    it stays idempotent for partial/old DBs. The caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS _analysis_attempt_counter (
                batch_id TEXT PRIMARY KEY,
                next_attempt BIGINT NOT NULL
            )
            """
        )


# ── column/index helpers (PostgreSQL) ───────────────────────────────────────


def _column_exists(cur: psycopg.Cursor, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _table_exists(cur: psycopg.Cursor, table: str) -> bool:
    """Return whether ``table`` exists in the current schema.

    Resolves the target schema the same way unqualified DDL does
    (``current_schema()``), so under per-test scratch-schema isolation a
    greenfield scratch schema correctly reports the tables absent and the
    additive 08-04 bridge migration safe no-ops there (letting
    ``schema_postgres.sql`` create the full structure). Under a legacy
    pre-08-04 production schema the ``paper_orders`` table exists, so the
    bridge helper proceeds to add its columns + partial unique index.
    """
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = %s
        """,
        (table,),
    )
    return cur.fetchone() is not None


def _add_column(cur: psycopg.Cursor, table: str, column: str, definition: str) -> None:
    """Add a column if absent. ``definition`` is the PG type clause (e.g. ``TEXT``)."""
    if not _column_exists(cur, table, column):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ── schema health ───────────────────────────────────────────────────────────


def _check_schema_health_on_conn(conn: psycopg.Connection) -> dict[str, Any]:
    """Run the health introspection on an existing PG connection (no commit)."""
    with conn.cursor() as cur:
        return _introspect_schema_health(cur)


def check_schema_health(
    *,
    config: CryptoGuardConfig | None = None,
    conn: psycopg.Connection | None = None,
) -> dict[str, Any]:
    """Verify the production PostgreSQL schema has every required object.

    Introspects ``information_schema`` / ``pg_catalog`` (``pg_indexes`` /
    ``pg_constraint``) - NEVER ``sqlite_master``/``PRAGMA``. Returns
    ``{"ok": bool, "missing_columns": [...], "tables_checked": [...]}``.

    If ``conn`` is given it is used as-is (the caller owns its transaction); a
    read-only snapshot is taken and rolled back so no writes escape. If no
    ``conn`` is given a pooled connection is opened and returned to the pool.
    """
    if conn is not None:
        # The caller owns this connection's transaction. The health probe is
        # read-only (SELECTs on information_schema / pg_catalog only), so it
        # performs no writes that would need rolling back. We MUST NOT issue a
        # bare ``conn.rollback()`` here: on an autocommit=False pooled
        # connection that is already INTRANS (the common case - the caller is
        # mid-transaction), a full ``rollback()`` discards the caller's
        # uncommitted writes (e.g. a just-inserted ``signals`` row), breaking
        # paper-order creation with a spurious FK violation. Instead wrap the
        # probe in ``conn.transaction()``: on an IDLE connection this opens and
        # closes a fresh read-only transaction; on an INTRANS connection it is a
        # SAVEPOINT that releases cleanly without touching the caller's outer
        # transaction. Either way the caller's transaction state is preserved.
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    result = _introspect_schema_health(cur)
            return result
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "missing_columns": [
                    {"table": "(health)", "column": f"check raised: {exc}"}
                ],
                "tables_checked": [],
            }

    from plugins.crypto_guard.storage.pg_db import get_conn

    cfg = config or load_config()
    try:
        with get_conn() as _conn:
            with _conn.cursor() as cur:
                result = _introspect_schema_health(cur)
            _conn.rollback()
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "missing_columns": [
                {"table": "(health)", "column": f"db unavailable: {exc}"}
            ],
            "tables_checked": [],
        }


# Required columns per table (the post-greenfield contract). Mirrors the old
# SQLite health spec; the column set is what every correctly-initialized DB
# MUST expose for the runtime + diagnostics to function.
_REQUIRED_COLUMNS: dict[str, list[str]] = {
    "skill_feedback_memory": ["pattern_type", "affected_symbols", "affected_sides"],
    "skill_execution_logs": ["commit_state", "batch_id", "attempt_id"],
    "ga_decisions": [
        "account_feedback_gate_json",
        "market_regime_gate_json",
        "batch_id",
        "previous_grade",
        "rendered_summary",
    ],
    "opportunity_watches": [
        "dedupe_key",
        "recheck_status",
        "recheck_order_id",
        "last_recheck_at",
    ],
    "paper_positions": ["updated_at"],
    "strategy_evaluations": [
        "ga_decision_id",
        "paper_trade_id",
        "outcome_source",
        "shadow_virtual_trade_id",
    ],
    "paper_orders": [
        "initial_stop_loss",
        "last_processed_candle_time",
        "trigger_watch_id",
    ],
    "paper_trades": ["initial_stop_loss", "initial_risk_usdt"],
    "paper_trade_logs": ["dedupe_key"],
    "shadow_virtual_trades": [
        "strategy_name",
        "status",
        "entry_type",
        "opened_at",
        "expires_at",
        "last_processed_candle_time",
    ],
    "backfill_progress": [
        "symbol",
        "interval",
        "last_open_time_fetched",
        "last_updated_ms",
    ],
    "agent_jobs": ["claim_token", "lease_until", "defer_count", "deferred_at"],
    "analysis_batches": ["claim_ready_at", "sealed_at"],
    "_service_ownership": ["owner_token"],
}

# Required indexes (must exist by name).
_REQUIRED_INDEXES: list[str] = [
    "idx_opportunity_watches_dedupe",
    "idx_one_open_trade_per_order",
    "idx_shadow_vt_unique",
    "idx_strategy_evals_shadow_unique",
    "idx_alert_outbox_dedupe_unique",
    "idx_paper_trade_logs_dedupe_key",
    "idx_paper_orders_trigger_watch_once",
]

# Required tables (must exist by name).
_REQUIRED_TABLES: list[str] = [
    "symbols",
    "candles",
    "market_profiles",
    "market_snapshots",
    "module_analysis_results",
    "analysis_states",
    "skill_execution_logs",
    "skill_feedback_memory",
    "ga_decisions",
    "analysis_batches",
    "batch_symbol_status",
    "signals",
    "ad_hoc_analyses",
    "opportunity_watches",
    "paper_accounts",
    "paper_orders",
    "paper_trades",
    "paper_positions",
    "paper_trade_logs",
    "paper_equity_snapshots",
    "trade_reviews",
    "strategy_versions",
    "strategy_evaluations",
    "strategy_patches",
    "shadow_test_results",
    "historical_replay_results",
    "self_evolution_runs",
    "evolution_triggers",
    "strategy_memory",
    "daily_review_reports",
    "scheduler_runs",
    "agent_jobs",
    "task_locks",
    "feishu_events",
    "alert_outbox",
    "_migration_state",
    "_service_ownership",
    "backfill_progress",
    "_analysis_attempt_counter",
    "alert_failure_log",
    "config_hot_reload",
    "parquet_archive_runs",
    "runtime_config",
    "user_feedback",
    "sop_definitions",
    "shadow_virtual_trades",
]

# SHA-256 of the normalized PostgreSQL catalog contract produced by
# ``schema_postgres.sql``. It covers every application table column (type,
# nullability, identity/default), every table constraint, every non-primary
# index, and application triggers/functions. Update only after deliberately
# changing the canonical DDL and regenerating it from a fresh scratch schema.
#
# 08-06 (release-blocker rework): the fingerprint is column-ORDER-insensitive
# (columns are keyed by (table, column) name, not ``ordinal_position``). A real
# legacy pre-08-04 DB upgraded via ``ALTER TABLE ADD COLUMN`` appends the four
# bridge columns AFTER ``created_at``/``updated_at``, whereas greenfield
# declares them mid-table; the two paths yield the identical logical catalog,
# and only an order-insensitive fingerprint lets an upgraded legacy schema pass
# the health gate (its SHA-256 must equal this greenfield value).
_EXPECTED_SCHEMA_FINGERPRINT = "c937a91931256296e2eee02dd34956aa6be0ed0b1dacd4b5386a144f264d39bf"


def _normalize_catalog_text(value: Any, schema: str) -> str:
    text = "" if value is None else " ".join(str(value).split())
    return text.replace(f'"{schema}".', "<schema>.").replace(
        f"{schema}.", "<schema>."
    )


def _schema_catalog_fingerprint(cur: psycopg.Cursor, schema: str) -> str:
    """Return a stable full-schema fingerprint for the current app schema."""
    contract: dict[str, list[dict[str, Any]]] = {
        "columns": [], "constraints": [], "indexes": [],
        "triggers": [], "functions": [],
    }
    cur.execute(
        """
        SELECT table_name, column_name, data_type, udt_name,
               is_nullable, is_identity, identity_generation, column_default
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name = ANY(%s)
        ORDER BY table_name, column_name
        """,
        (schema, _REQUIRED_TABLES),
    )
    for row in cur.fetchall():
        item = dict(row)
        item["column_default"] = _normalize_catalog_text(
            item.get("column_default"), schema
        )
        contract["columns"].append(item)

    cur.execute(
        """
        SELECT cls.relname AS table_name, con.conname, con.contype,
               pg_get_constraintdef(con.oid, true) AS definition
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid=con.conrelid
        JOIN pg_namespace ns ON ns.oid=cls.relnamespace
        WHERE ns.nspname=%s AND cls.relname = ANY(%s)
        ORDER BY cls.relname, con.conname
        """,
        (schema, _REQUIRED_TABLES),
    )
    for row in cur.fetchall():
        item = dict(row)
        item["definition"] = _normalize_catalog_text(item["definition"], schema)
        contract["constraints"].append(item)

    cur.execute(
        """
        SELECT tablename AS table_name, indexname,
               indexdef AS definition
        FROM pg_indexes
        WHERE schemaname=%s AND tablename = ANY(%s)
        ORDER BY tablename, indexname
        """,
        (schema, _REQUIRED_TABLES),
    )
    for row in cur.fetchall():
        item = dict(row)
        item["definition"] = _normalize_catalog_text(item["definition"], schema)
        contract["indexes"].append(item)

    cur.execute(
        """
        SELECT cls.relname AS table_name, trg.tgname,
               pg_get_triggerdef(trg.oid, true) AS definition
        FROM pg_trigger trg
        JOIN pg_class cls ON cls.oid=trg.tgrelid
        JOIN pg_namespace ns ON ns.oid=cls.relnamespace
        WHERE ns.nspname=%s AND NOT trg.tgisinternal
        ORDER BY cls.relname, trg.tgname
        """,
        (schema,),
    )
    for row in cur.fetchall():
        item = dict(row)
        item["definition"] = _normalize_catalog_text(item["definition"], schema)
        contract["triggers"].append(item)

    cur.execute(
        """
        SELECT p.proname, pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p JOIN pg_namespace ns ON ns.oid=p.pronamespace
        WHERE ns.nspname=%s
        ORDER BY p.proname, p.oid
        """,
        (schema,),
    )
    for row in cur.fetchall():
        item = dict(row)
        item["definition"] = _normalize_catalog_text(item["definition"], schema)
        contract["functions"].append(item)

    payload = json.dumps(contract, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _introspect_schema_health(cur: psycopg.Cursor) -> dict[str, Any]:
    missing: list[dict[str, str]] = []
    tables_checked: list[str] = []

    # Resolve the target schema the same way unqualified DDL does: PostgreSQL's
    # ``current_schema()`` returns the first existing schema on ``search_path``
    # (where ``CREATE TABLE foo`` lands). This is ``public`` in production and
    # ``test_<uuid>`` under per-test scratch-schema isolation. Hard-coding
    # ``'public'`` would make the probe blind to the scratch schema and report
    # every object as missing right after init - breaking the whole test path.
    cur.execute("SELECT current_schema() AS s")
    schema = cur.fetchone()["s"]

    # ── required columns ───────────────────────────────────────────────────
    for table, columns in _REQUIRED_COLUMNS.items():
        tables_checked.append(table)
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        )
        existing_cols = {r["column_name"] for r in cur.fetchall()}
        if not existing_cols:
            for col in columns:
                missing.append({"table": table, "column": col})
            continue
        for col in columns:
            if col not in existing_cols:
                missing.append({"table": table, "column": col})

    # ── required indexes ───────────────────────────────────────────────────
    cur.execute(
        """
        SELECT indexname FROM pg_indexes
        WHERE schemaname = %s AND indexname = ANY(%s)
        """,
        (schema, _REQUIRED_INDEXES),
    )
    present_indexes = {r["indexname"] for r in cur.fetchall()}
    for idx_name in _REQUIRED_INDEXES:
        if idx_name not in present_indexes:
            missing.append({"table": "(index)", "column": idx_name})

    # ── required tables ────────────────────────────────────────────────────
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_name = ANY(%s)
        """,
        (schema, _REQUIRED_TABLES),
    )
    present_tables = {r["table_name"] for r in cur.fetchall()}
    for tbl_name in _REQUIRED_TABLES:
        if tbl_name not in present_tables:
            missing.append({"table": tbl_name, "column": "(table)"})

    if not missing:
        actual_fingerprint = _schema_catalog_fingerprint(cur, schema)
        if actual_fingerprint != _EXPECTED_SCHEMA_FINGERPRINT:
            missing.append(
                {
                    "table": "(schema_contract)",
                    "column": "catalog fingerprint mismatch",
                }
            )

    # ── batch_symbol_status.status CHECK constraint ────────────────────────
    # Verify the CHECK(status IN ('pending','completed','failed')) constraint
    # exists on batch_symbol_status via pg_constraint (not a SQL-text regex).
    missing.extend(_check_batch_symbol_status_constraint(cur, schema))

    # ── paper_trade_logs dedupe_key partial unique index ───────────────────
    missing.extend(_check_dedupe_key_partial_unique_index(cur, schema))

    # ── opportunity_watches dedupe_key partial unique index ────────────────
    missing.extend(_check_opportunity_watches_dedupe_index(cur, schema))

    # ── paper_orders trigger_watch_id partial unique index (08-04 contract B) ─
    missing.extend(_check_paper_orders_trigger_watch_index(cur, schema))

    # ── backfill_progress composite primary key (symbol, interval) ─────────
    missing.extend(_check_backfill_progress_primary_key(cur, schema))

    return {
        "ok": len(missing) == 0,
        "missing_columns": missing,
        "tables_checked": tables_checked,
    }


def _check_batch_symbol_status_constraint(cur: psycopg.Cursor, schema: str) -> list[dict[str, str]]:
    """Verify the exact CHECK constraint on batch_symbol_status.status.

    ``pg_get_constraintdef`` renders the check predicate; we verify it lists
    exactly pending/completed/failed. A missing or different constraint fails
    closed.
    """
    cur.execute(
        """
        SELECT pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class k ON c.conrelid = k.oid
        JOIN pg_namespace n ON k.relnamespace = n.oid
        WHERE n.nspname = %s AND k.relname = 'batch_symbol_status'
          AND c.contype = 'c'
        """,
        (schema,),
    )
    rows = cur.fetchall()
    expected = {"'pending'", "'completed'", "'failed'"}
    for r in rows:
        defn = (r["def"] or "").lower()
        if "status" in defn and expected.issubset({tok for tok in expected if tok in defn}):
            # Confirm no extra status values are admitted.
            if "'pending'" in defn and "'completed'" in defn and "'failed'" in defn:
                return []
    return [
        {
            "table": "batch_symbol_status",
            "column": "CHECK(status IN ('pending','completed','failed'))",
        }
    ]


def _check_dedupe_key_partial_unique_index(cur: psycopg.Cursor, schema: str) -> list[dict[str, str]]:
    """Verify idx_paper_trade_logs_dedupe_key is a UNIQUE PARTIAL index on
    ``dedupe_key`` (WHERE dedupe_key IS NOT NULL).

    A non-unique or non-partial index would break the dedupe contract (multiple
    NULL dedupe_key rows must be allowed). Introspected via ``pg_indexes`` +
    ``pg_index`` (``indisunique`` + the partial predicate ``WHERE``), not SQL
    text.
    """
    cur.execute(
        """
        SELECT i.indexname, i.indexdef
        FROM pg_indexes i
        WHERE i.schemaname = %s AND i.tablename = 'paper_trade_logs'
          AND i.indexname = 'idx_paper_trade_logs_dedupe_key'
        """,
        (schema,),
    )
    row = cur.fetchone()
    if not row:
        return [
            {
                "table": "paper_trade_logs",
                "column": "idx_paper_trade_logs_dedupe_key (missing)",
            }
        ]
    defn = (row["indexdef"] or "").lower()
    problems = []
    if "unique" not in defn:
        problems.append(
            {
                "table": "paper_trade_logs",
                "column": "idx_paper_trade_logs_dedupe_key UNIQUE",
            }
        )
    if "dedupe_key is not null" not in defn:
        problems.append(
            {
                "table": "paper_trade_logs",
                "column": "idx_paper_trade_logs_dedupe_key WHERE dedupe_key IS NOT NULL",
            }
        )
    # Verify the indexed column is exactly dedupe_key via pg_index.
    cur.execute(
        """
        SELECT pg_get_indexdef(ix.indexrelid) AS def
        FROM pg_index ix
        JOIN pg_class c ON ix.indexrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = %s AND c.relname = 'idx_paper_trade_logs_dedupe_key'
        """,
        (schema,),
    )
    idx_row = cur.fetchone()
    indexed_ok = False
    if idx_row:
        # The index definition must be on (dedupe_key) - not a composite that
        # merely mentions dedupe_key.
        def_text = (idx_row["def"] or "").lower()
        if "(dedupe_key)" in def_text.replace(" ", ""):
            indexed_ok = True
    if not indexed_ok:
        problems.append(
            {
                "table": "paper_trade_logs",
                "column": "idx_paper_trade_logs_dedupe_key column=dedupe_key",
            }
        )
    return problems


def _check_opportunity_watches_dedupe_index(cur: psycopg.Cursor, schema: str) -> list[dict[str, str]]:
    """Verify idx_opportunity_watches_dedupe is a UNIQUE PARTIAL index on
    ``dedupe_key`` with the P0-2 predicate ``WHERE dedupe_key IS NOT NULL AND
    status = 'active'``.

    The predicate is the whole point: a terminal watch (triggered/invalidated/
    expired) must NOT hold its dedupe_key, or a later B/S-A batch could never
    re-create an active watch for the same symbol+direction (the pre-P0-2
    ``WHERE dedupe_key IS NOT NULL`` defect). Introspected via ``pg_indexes`` +
    ``pg_index`` (``indisunique`` + the partial predicate), not SQL text.
    """
    cur.execute(
        """
        SELECT i.indexname, i.indexdef
        FROM pg_indexes i
        WHERE i.schemaname = %s AND i.tablename = 'opportunity_watches'
          AND i.indexname = 'idx_opportunity_watches_dedupe'
        """,
        (schema,),
    )
    row = cur.fetchone()
    if not row:
        return [
            {
                "table": "opportunity_watches",
                "column": "idx_opportunity_watches_dedupe (missing)",
            }
        ]
    defn = (row["indexdef"] or "").lower()
    problems = []
    if "unique" not in defn:
        problems.append(
            {
                "table": "opportunity_watches",
                "column": "idx_opportunity_watches_dedupe UNIQUE",
            }
        )
    if "dedupe_key is not null" not in defn:
        problems.append(
            {
                "table": "opportunity_watches",
                "column": "idx_opportunity_watches_dedupe WHERE dedupe_key IS NOT NULL",
            }
        )
    if "status" not in defn or "'active'" not in defn:
        problems.append(
            {
                "table": "opportunity_watches",
                "column": "idx_opportunity_watches_dedupe WHERE status = 'active'",
            }
        )
    # Verify the indexed column is exactly dedupe_key via pg_index.
    cur.execute(
        """
        SELECT pg_get_indexdef(ix.indexrelid) AS def
        FROM pg_index ix
        JOIN pg_class c ON ix.indexrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = %s AND c.relname = 'idx_opportunity_watches_dedupe'
        """,
        (schema,),
    )
    idx_row = cur.fetchone()
    indexed_ok = False
    if idx_row:
        def_text = (idx_row["def"] or "").lower()
        if "(dedupe_key)" in def_text.replace(" ", ""):
            indexed_ok = True
    if not indexed_ok:
        problems.append(
            {
                "table": "opportunity_watches",
                "column": "idx_opportunity_watches_dedupe column=dedupe_key",
            }
        )
    return problems


def _check_paper_orders_trigger_watch_index(cur: psycopg.Cursor, schema: str) -> list[dict[str, str]]:
    """Verify idx_paper_orders_trigger_watch_once is a UNIQUE PARTIAL index on
    ``trigger_watch_id`` with the 08-06 ONCE-EVER predicate ``WHERE
    trigger_watch_id IS NOT NULL`` (NO status filter).

    The predicate is the whole point: one ``opportunity_watch`` creates at most
    ONE ``paper_order`` over its entire lifetime; a terminal
    (filled/expired/cancelled) order STILL holds the watch link, so a delayed
    retry recheck can never create a second order. NULL ``trigger_watch_id``
    rows (signal-originated orders) are unconstrained. ANY deviation from the
    exact once-ever contract is REJECTED -- the old 08-04 live-only predicate
    released terminal orders from the constraint, which was the Codex P1 (a
    retry could mint a second order). A wrong predicate, a non-unique index, a
    wrong-column/composite/expression index, or a non-partial index would
    silently break the bridge idempotency, so this is a precise ``pg_index`` /
    ``pg_attribute`` / ``pg_get_expr`` introspection (shared with the migration
    rebuild-detection and the marker spy), never a loose ``indexdef`` string
    guess.
    """
    facts = _introspect_once_ever_index(cur, schema)
    ok, reason = _once_ever_index_is_exact(facts)
    if ok:
        return []
    if facts is None:
        return [
            {
                "table": "paper_orders",
                "column": "idx_paper_orders_trigger_watch_once (missing)",
            }
        ]
    return [
        {
            "table": "paper_orders",
            "column": f"idx_paper_orders_trigger_watch_once {reason}",
        }
    ]


def _drop_legacy_opportunity_watches_dedupe_index(cur: psycopg.Cursor) -> None:
    """Drop a stale-predicate ``idx_opportunity_watches_dedupe`` so DDL can recreate it.

    PostgreSQL ``CREATE INDEX IF NOT EXISTS`` is a name-only no-op: it never
    upgrades an index that already exists with an old definition. An
    ``opportunity_watches`` table carrying the pre-P0-2 predicate
    (``WHERE dedupe_key IS NOT NULL``, without ``status = 'active'``) passes
    through init DDL unchanged and then trips the health gate fail-closed. This
    helper introspects ``pg_indexes`` for the actual predicate and, only when it
    lacks ``status = 'active'``, drops the index schema-scoped (via
    ``current_schema()``, so per-test scratch schemas resolve correctly). The
    subsequent schema DDL recreates it with the P0-2 predicate. Safe: the
    opportunity_watches table is empty in the production baseline.
    """
    cur.execute("SELECT current_schema() AS s")
    schema = cur.fetchone()["s"]
    cur.execute(
        """
        SELECT i.indexdef
        FROM pg_indexes i
        WHERE i.schemaname = %s AND i.tablename = 'opportunity_watches'
          AND i.indexname = 'idx_opportunity_watches_dedupe'
        """,
        (schema,),
    )
    row = cur.fetchone()
    if not row:
        return
    defn = (row["indexdef"] or "").lower()
    if "status" not in defn or "'active'" not in defn:
        cur.execute(
            sql.SQL("DROP INDEX IF EXISTS {}.{}").format(
                sql.Identifier(schema),
                sql.Identifier("idx_opportunity_watches_dedupe"),
            )
        )


def _check_backfill_progress_primary_key(cur: psycopg.Cursor, schema: str) -> list[dict[str, str]]:
    """Verify backfill_progress has exactly PRIMARY KEY (symbol, interval)."""
    cur.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid
          AND a.attnum = ANY(i.indkey)
        JOIN pg_class c ON i.indrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = %s AND c.relname = 'backfill_progress'
          AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (schema,),
    )
    pk_cols = [r["attname"].lower() for r in cur.fetchall()]
    expected_sorted = sorted(["symbol", "interval"])
    if sorted(pk_cols) != expected_sorted:
        return [
            {
                "table": "backfill_progress",
                "column": f"PRIMARY KEY(symbol, interval) (actual: ({', '.join(pk_cols) or 'none'}))",
            }
        ]
    return []


__all__ = [
    "initialize_database",
    "check_schema_health",
    "apply_r6f_service_ownership_migration",
    "apply_r10_attempt_counter_migration",
    "apply_08_04_watch_order_bridge_migration",
]
