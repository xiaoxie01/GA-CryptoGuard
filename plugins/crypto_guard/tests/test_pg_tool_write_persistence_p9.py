"""P9 RED test: tool writes via ``_repo()`` must persist (commit on clean exit).

Production-shape regression guard for the SQLite -> PostgreSQL greenfield
cutover.

``ga_crypto_tools._repo()`` borrows a pooled connection and opens one explicit
top-level transaction. Repository write methods self-wrap
``conn.transaction()``, which become savepoints inside that unit. The wrapper
must commit all tool effects together on success and roll them all back on
exception:

  - ``crypto_request_config_update`` returns a ``change_id`` for a
    ``config_hot_reload`` row that no longer exists;
  - ``crypto_symbol_add`` / ``pause`` / ``resume`` / ``remove`` mutate a row
    that snaps back;
  - ``crypto_confirm_config_update`` leaves ``runtime_config`` unchanged.

Under SQLite the connection was autocommit per statement, so this atomicity was
implicit and partial tool effects could survive. PostgreSQL makes the complete
tool unit explicit.

The read below uses a FRESH pooled connection (independent MVCC snapshot) so a
row is visible ONLY if ``_repo()`` actually committed -- this isolates the
"does the tool commit?" question from any single-connection snapshot staleness.

Revert-fail: without the commit, the fresh-connection read returns no row ->
``assertIsNotNone`` fails (RED).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.pg, pytest.mark.e2e]

import unittest

from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.tests.pg_fixtures import make_repo


class TestToolWritePersistenceP9(unittest.TestCase):
    def setUp(self) -> None:
        self._h = make_repo()
        self.conn = self._h.conn
        self.repo = self._h.repo

    def tearDown(self) -> None:
        self._h.close()

    def test_crypto_request_config_update_persists_across_connections(self) -> None:
        """INSERT-path tool write (config_hot_reload) must survive _repo() exit."""
        from plugins.crypto_guard.tools.ga_crypto_tools import (
            crypto_request_config_update,
        )

        result = crypto_request_config_update(
            "risk.min_confidence_for_paper_order",
            0.73,
            requested_by="u1",
            request_text="把置信度阈值改成 0.73",
        )
        self.assertTrue(result["ok"], f"request returned not-ok: {result}")
        change_id = int(result["change_id"])

        # Read on a FRESH pooled connection (independent MVCC snapshot). If
        # _repo() committed, the row is visible here. If _repo() rolled back,
        # the row is gone -> fetchone() is None -> RED.
        with pg_db.get_conn() as fresh:
            with fresh.cursor() as cur:
                cur.execute(
                    "SELECT status, config_key FROM config_hot_reload WHERE id=%s",
                    (change_id,),
                )
                row = cur.fetchone()

        self.assertIsNotNone(
            row,
            f"config_hot_reload row id={change_id} not persisted -- _repo() "
            "rolled back the tool write on exit instead of committing it "
            "(revert-fail trigger)",
        )
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["config_key"], "risk.min_confidence_for_paper_order")

    def test_crypto_symbol_pause_persists_across_connections(self) -> None:
        """UPDATE-path tool write (symbols.enabled) must survive _repo() exit.

        BTCUSDT is seeded by initialize_database(), so pause targets a real
        row. No network: pause_symbol only UPDATEs symbols.
        """
        from plugins.crypto_guard.tools.ga_crypto_tools import crypto_symbol_pause

        # Sanity: BTCUSDT is seeded and currently enabled.
        with pg_db.get_conn() as fresh:
            with fresh.cursor() as cur:
                cur.execute(
                    "SELECT enabled FROM symbols WHERE symbol=%s",
                    ("BTCUSDT",),
                )
                before = cur.fetchone()
        self.assertIsNotNone(before, "precondition: BTCUSDT not seeded")

        result = crypto_symbol_pause("BTCUSDT")
        self.assertTrue(result.get("ok"), f"symbol pause returned not-ok: {result}")

        # Fresh connection: if _repo() committed, enabled=FALSE persists.
        with pg_db.get_conn() as fresh:
            with fresh.cursor() as cur:
                cur.execute(
                    "SELECT enabled FROM symbols WHERE symbol=%s",
                    ("BTCUSDT",),
                )
                after = cur.fetchone()

        self.assertIsNotNone(after, "symbols row for BTCUSDT vanished (unexpected)")
        self.assertFalse(
            bool(after["enabled"]),
            "BTCUSDT still enabled after crypto_symbol_pause -- _repo() rolled "
            "back the UPDATE on exit instead of committing it (revert-fail "
            "trigger)",
        )


if __name__ == "__main__":
    unittest.main()
