from __future__ import annotations

import json

from plugins.crypto_guard.config.loader import load_config
from plugins.crypto_guard.paper.paper_position_updater import update_paper_positions
from plugins.crypto_guard.storage import pg_db
from plugins.crypto_guard.storage.migrations import initialize_database
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def main() -> None:
    cfg = load_config()
    initialize_database(cfg)
    # PG cutover: pooled connection (auto-returned; every repo write self-wraps
    # ``conn.transaction()``).
    with pg_db.get_conn() as conn:
        print(json.dumps(update_paper_positions(CryptoGuardRepository(conn)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
