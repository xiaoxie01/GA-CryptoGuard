"""P2 (release-blocker rework 08-06): ``watch_order_bridge_contract_v1``
marker fail-closed diagnostic.

The P2 directive requires an independent marker ``watch_order_bridge_contract_v1``
written ONLY after the 08-04 bridge schema (4 columns + the partial unique
index ``idx_paper_orders_trigger_watch_once``) is complete AND the schema
health gate passes, inside the same advisory-lock-guarded transaction as the
schema change. Presence of the marker therefore proves the bridge actually
deployed; a mid-bridge rollback leaves no residue.

Mirroring ``_check_llm_failed_direction_fail_closed_marker_missing``,
``diagnose_state_consistency`` wires
``_check_watch_order_bridge_contract_marker_missing`` (state_consistency.py):
when the marker is ABSENT from ``_migration_state`` the suite must emit an
``error`` (type ``watch_order_bridge_contract_marker_missing``, severity
``error``) so callers detect the undeployed 08-04 bridge contract rather than
receiving a silently-healthy report — "marker 缺失必须 fail-closed".

These tests drive the REAL diagnostic chain (``diagnose_state_consistency``)
on an isolated PG scratch schema built by ``make_repo`` (which runs
``initialize_database`` into the TEST schema only). No production DB mutation,
no service restart, no commit/push/finish-work. The DELETE+restore of the
marker row happens only inside the scratch schema.
"""
from __future__ import annotations

import unittest

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e, pytest.mark.rollback_isolation]

from plugins.crypto_guard.diagnostics.state_consistency import (
    WATCH_ORDER_BRIDGE_CONTRACT_MARKER_KEY,
    diagnose_state_consistency,
)
from plugins.crypto_guard.tests.pg_fixtures import make_repo
from plugins.crypto_guard.tests.test_pg_migrations import EXPECTED_MARKERS


class TestWatchOrderBridgeMarkerP2(unittest.TestCase):
    def setUp(self) -> None:
        self._h = make_repo()
        self._conn = self._h.conn
        self._repo = self._h.repo

    def tearDown(self) -> None:
        self._h.close()

    def _marker_row(self) -> tuple | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT applied_at FROM _migration_state WHERE key = %s LIMIT 1",
                (WATCH_ORDER_BRIDGE_CONTRACT_MARKER_KEY,),
            )
            return cur.fetchone()

    def test_marker_seeded_by_greenfield_init(self) -> None:
        """Green path: a healthy fresh init writes the bridge marker, and the
        diagnostic reports clean (no marker-missing)."""
        self.assertIsNotNone(self._marker_row(), "bridge marker must be seeded by init")
        self.assertIn(WATCH_ORDER_BRIDGE_CONTRACT_MARKER_KEY, EXPECTED_MARKERS)
        result = diagnose_state_consistency(self._repo)
        self.assertTrue(result["ok"], f"healthy schema flagged: {result}")
        missing = [
            i for i in result["issues"]
            if i["type"] == "watch_order_bridge_contract_marker_missing"
        ]
        self.assertEqual(missing, [], "healthy schema must not report marker-missing")

    def test_marker_missing_is_fail_closed_error(self) -> None:
        """P2 fail-closed: deleting the marker row from the scratch schema makes
        ``diagnose_state_consistency`` emit a ``watch_order_bridge_contract_marker_missing``
        ``error`` and fail the gate (error_count > 0). The marker is restored so
        the scratch schema is left intact for teardown."""
        # GREEN precondition: the marker exists after a healthy init.
        deleted = self._marker_row()
        assert deleted is not None, "GREEN: marker row existed before deletion"

        # Simulate marker absence: DELETE the marker row (scratch schema only).
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM _migration_state WHERE key = %s",
                    (WATCH_ORDER_BRIDGE_CONTRACT_MARKER_KEY,),
                )
        try:
            result = diagnose_state_consistency(self._repo)
            missing = [
                i for i in result["issues"]
                if i["type"] == "watch_order_bridge_contract_marker_missing"
            ]
            self.assertGreaterEqual(
                len(missing), 1,
                "marker absence must emit watch_order_bridge_contract_marker_missing",
            )
            self.assertEqual(missing[0]["severity"], "error",
                             "marker-missing must be severity=error (fail-closed)")
            # Fail-closed: the gate goes False on the marker-missing error.
            self.assertFalse(result["ok"], "marker absence must fail the gate")
            self.assertGreaterEqual(result["error_count"], 1,
                                    "marker-missing error counted in error_count")
            self.assertGreaterEqual(
                result["summary"].get("watch_order_bridge_contract_marker_missing", 0),
                1,
                "summary must include watch_order_bridge_contract_marker_missing count",
            )
        finally:
            # RESTORE the marker so the scratch schema is left intact for teardown.
            with self._conn.transaction():
                with self._conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO _migration_state(key, applied_at) "
                        "VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                        (WATCH_ORDER_BRIDGE_CONTRACT_MARKER_KEY, deleted["applied_at"]),
                    )
