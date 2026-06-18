from __future__ import annotations

from typing import Any

from plugins.crypto_guard.config.loader import load_config


def load_active_strategies() -> list[dict[str, Any]]:
    cfg = load_config()
    return [s for s in cfg.strategies.get("strategies", []) if s.get("status") == "active"]
