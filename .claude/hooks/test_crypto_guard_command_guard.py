from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("crypto-guard-command-guard.py")
SPEC = importlib.util.spec_from_file_location("crypto_guard_command_guard", MODULE_PATH)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class CryptoGuardCommandGuardTest(unittest.TestCase):
    def test_read_only_commands_are_allowed(self) -> None:
        self.assertEqual(guard.classify_command("git status --short"), set())
        self.assertEqual(
            guard.classify_command("rg -n initialize_database plugins"),
            set(),
        )
        self.assertEqual(
            guard.classify_command(
                'psql -d crypto_guard -c "SELECT current_user"'
            ),
            set(),
        )
        self.assertEqual(
            guard.classify_command("pg_dump -d crypto_guard -f backup.dump"),
            set(),
        )
        self.assertEqual(
            guard.classify_command("pg_restore --list backup.dump"),
            set(),
        )

    def test_read_only_prefix_cannot_hide_compound_sensitive_command(self) -> None:
        self.assertEqual(
            guard.classify_command(
                'git status --short; psql -d postgres -c "CREATE DATABASE crypto_guard"'
            ),
            {"database-mutation"},
        )
        self.assertEqual(
            guard.classify_command(
                "python -m pytest -q; Stop-Process -Id 1234"
            ),
            {"service-control"},
        )

    def test_postgresql_admin_and_restore_commands_are_sensitive(self) -> None:
        commands = (
            "createdb crypto_guard",
            "createuser crypto_guard_app",
            'psql -d postgres -c "CREATE ROLE crypto_guard_app LOGIN"',
            'psql -d crypto_guard -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO crypto_guard_app"',
            "psql -d crypto_guard -f plugins/crypto_guard/storage/schema_postgres.sql",
            'psql -d crypto_guard -c "\\i plugins/crypto_guard/storage/schema_postgres.sql"',
            "psql -d crypto_guard < plugins/crypto_guard/storage/schema_postgres.sql",
            "pg_restore -d crypto_guard backup.dump",
            'python -c "import psycopg; conn.execute(\'CREATE DATABASE crypto_guard\')"',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    guard.classify_command(command),
                    {"database-mutation"},
                )

    def test_inline_postgres_admin_is_not_service_control(self) -> None:
        """``user='postgres'`` inside ``python -c`` is a psycopg user, not a service."""
        self.assertEqual(
            guard.classify_command(
                "python -c \"import psycopg; psycopg.connect(user='postgres'); "
                "cur.execute('CREATE DATABASE crypto_guard')\""
            ),
            {"database-mutation"},
        )

    def test_direct_fsapp_launch_is_service_control(self) -> None:
        self.assertEqual(
            guard.classify_command("python frontends/fsapp.py"),
            {"service-control"},
        )
        self.assertEqual(
            guard.classify_command("pythonw hub.pyw"),
            {"service-control"},
        )

    def test_subprocess_fsapp_launch_is_service_control(self) -> None:
        self.assertEqual(
            guard.classify_command(
                "python -c \"import subprocess; "
                "subprocess.Popen(['python','frontends/fsapp.py'])\""
            ),
            {"service-control"},
        )

    def test_postgresql_service_management_remains_service_control(self) -> None:
        self.assertEqual(
            guard.classify_command("Start-Service postgresql-x64-17"),
            {"service-control"},
        )
        self.assertEqual(
            guard.classify_command("Restart-Service postgresql-x64-17"),
            {"service-control"},
        )
        self.assertEqual(
            guard.classify_command("Stop-Service postgresql"),
            {"service-control"},
        )
        # Bare postgres + explicit service-manager verb still matches.
        self.assertEqual(
            guard.classify_command("Start-Process postgres"),
            {"service-control"},
        )

    def test_persistent_database_dsn_change_requires_service_control(self) -> None:
        self.assertEqual(
            guard.classify_command(
                "setx CRYPTO_GUARD_DATABASE_URL postgresql://redacted"
            ),
            {"service-control"},
        )
        self.assertEqual(
            guard.classify_command(
                "[Environment]::SetEnvironmentVariable("
                "'CRYPTO_GUARD_MIGRATION_DATABASE_URL','redacted','User')"
            ),
            {"service-control"},
        )
        self.assertEqual(
            guard.classify_command(
                "reg add HKCU\\Environment /v CRYPTO_GUARD_DATABASE_URL "
                "/d postgresql://redacted /f"
            ),
            {"service-control"},
        )

    def test_process_level_dsn_injection_is_allowed(self) -> None:
        """Process-scoped $env: assignment is the approved secret channel.

        The URI scheme ``postgresql://`` must not be confused with a
        PostgreSQL Windows service name (``postgresql-x64-17``).
        """
        self.assertEqual(
            guard.classify_command(
                "$env:CRYPTO_GUARD_DATABASE_URL='postgresql://redacted'; "
                "python -c 'print(1)'"
            ),
            set(),
        )
        self.assertEqual(
            guard.classify_command(
                "$env:CRYPTO_GUARD_MIGRATION_DATABASE_URL="
                "'postgresql://migrator-redacted'; "
                "$env:CRYPTO_GUARD_DATABASE_URL="
                "'postgresql://app-redacted'; "
                "python -c 'print(\"ready\")'"
            ),
            set(),
        )
        # Bare service control must still match after the negative look-ahead.
        self.assertEqual(
            guard.classify_command("Restart-Service postgresql-x64-17"),
            {"service-control"},
        )
        self.assertEqual(
            guard.classify_command("Stop-Service postgresql"),
            {"service-control"},
        )
        # Production initialize_database still requires approval even when the
        # process-scoped DSN uses the postgresql:// URI scheme.
        self.assertEqual(
            guard.classify_command(
                "$env:CRYPTO_GUARD_MIGRATION_DATABASE_URL="
                "'postgresql://redacted'; "
                "python -c \"from plugins.crypto_guard.storage.migrations "
                "import initialize_database; initialize_database()\""
            ),
            {"database-mutation"},
        )

    def test_postgresql_service_control_is_sensitive(self) -> None:
        self.assertEqual(
            guard.classify_command("Restart-Service postgresql-x64-17"),
            {"service-control"},
        )

    def test_destructive_git_is_always_blocked(self) -> None:
        allowed, reason = guard.evaluate_hook(
            {"tool_input": {"command": "git reset --hard HEAD"}}
        )
        self.assertFalse(allowed)
        self.assertIn("destructive Git", reason)

    def test_process_stop_and_database_delete_are_sensitive(self) -> None:
        self.assertEqual(
            guard.classify_command("Stop-Process -Id 1234"),
            {"service-control"},
        )
        self.assertEqual(
            guard.classify_command("python -m frontends.fsapp"),
            {"service-control"},
        )
        self.assertEqual(
            guard.classify_command(
                "Remove-Item E:\\data\\crypto_guard.sqlite3"
            ),
            {"database-mutation"},
        )

    def test_production_migration_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            approval_path = Path(directory) / "approval.json"
            with patch.object(guard, "APPROVAL_PATH", approval_path):
                allowed, reason = guard.evaluate_hook(
                    {
                        "tool_input": {
                            "command": (
                                "python -c \"from plugins.crypto_guard.storage."
                                "migrations import initialize_database; "
                                "initialize_database()\""
                            )
                        }
                    }
                )
        self.assertFalse(allowed)
        self.assertIn("approval token", reason)

    def test_explicit_external_temp_database_repro_is_allowed(self) -> None:
        command = (
            "$env:CRYPTO_GUARD_DB='C:\\Temp\\cg-repro.sqlite3'; "
            "python -c \"from plugins.crypto_guard.storage.migrations import "
            "initialize_database; initialize_database()\" "
            "# crypto-guard-non-production-db:C:\\Temp\\cg-repro.sqlite3"
        )
        self.assertEqual(guard.classify_command(command), set())

    def test_non_production_marker_cannot_exempt_project_data(self) -> None:
        command = (
            "$env:CRYPTO_GUARD_DB='data\\crypto_guard\\scratch.sqlite3'; "
            "python -c \"from plugins.crypto_guard.storage.migrations import "
            "initialize_database; initialize_database()\" "
            "# crypto-guard-non-production-db:data\\crypto_guard\\scratch.sqlite3"
        )
        self.assertEqual(
            guard.classify_command(command),
            {"database-mutation"},
        )

    def test_authorized_operation_consumes_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            approval_path = Path(directory) / "approval.json"
            token = guard.authorize(
                ["database-mutation"],
                ".trellis/tasks/example",
                15,
                2,
                approval_path,
            )
            command = (
                "python -c \"from plugins.crypto_guard.storage.migrations "
                "import initialize_database; initialize_database()\" "
                f"# crypto-guard-approval:{token}"
            )
            allowed, reason = guard._approval_allows(
                command,
                {"database-mutation"},
                expected_task=".trellis/tasks/example",
                path=approval_path,
            )
            self.assertTrue(allowed, reason)
            payload = json.loads(approval_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["remaining_uses"], 1)

    def test_expired_token_is_rejected_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            approval_path = Path(directory) / "approval.json"
            approval_path.write_text(
                json.dumps(
                    {
                        "token": "expired",
                        "task": ".trellis/tasks/example",
                        "operations": ["service-control"],
                        "created_at": "2000-01-01T00:00:00+00:00",
                        "expires_at": (
                            datetime.now(timezone.utc) - timedelta(minutes=1)
                        ).isoformat(),
                        "remaining_uses": 1,
                    }
                ),
                encoding="utf-8",
            )
            allowed, reason = guard._approval_allows(
                "python fsapp.py # crypto-guard-approval:expired",
                {"service-control"},
                expected_task=".trellis/tasks/example",
                path=approval_path,
            )
            self.assertFalse(allowed)
            self.assertIn("expired", reason)
            self.assertFalse(approval_path.exists())

    def test_token_is_bound_to_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            approval_path = Path(directory) / "approval.json"
            token = guard.authorize(
                ["service-control"],
                ".trellis/tasks/expected",
                15,
                1,
                approval_path,
            )
            allowed, reason = guard._approval_allows(
                f"python fsapp.py # crypto-guard-approval:{token}",
                {"service-control"},
                expected_task=".trellis/tasks/other",
                path=approval_path,
            )
            self.assertFalse(allowed)
            self.assertIn("different Trellis task", reason)


if __name__ == "__main__":
    unittest.main()
