from __future__ import annotations

import json

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.paper.paper_position_updater import update_paper_positions
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.storage.sqlite_db import connect_db


def main() -> None:
    cfg = load_config()
    initialize_database(cfg)
    conn = connect_db(cfg.database_path)
    try:
        print(json.dumps(update_paper_positions(CryptoGuardRepository(conn)), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
