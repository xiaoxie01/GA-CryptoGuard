"""Phase I — fresh DB verification of all 3 diagnostic suites."""
import os
import tempfile
from pathlib import Path

TMP_DIR = Path(tempfile.mkdtemp(prefix="cg_phase_i_fresh_"))
DB_PATH = TMP_DIR / "fresh.db"
os.environ["CRYPTO_GUARD_DB"] = str(DB_PATH)

from plugins.crypto_guard.config import load_config
from plugins.crypto_guard.storage.sqlite_db import connect_db
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.diagnostics.state_consistency import diagnose_state_consistency
from plugins.crypto_guard.diagnostics.report_diagnostics import diagnose_report_accuracy

cfg = load_config()
print(f"Fresh DB: {cfg.database_path}")

# Initialize
result = initialize_database(cfg)
print(f"Schema Health: {result}")

conn = connect_db(cfg.database_path)
try:
    repo = CryptoGuardRepository(conn)

    # State consistency
    state = diagnose_state_consistency(repo)
    print(f"\nState Consistency:")
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
finally:
    conn.close()
