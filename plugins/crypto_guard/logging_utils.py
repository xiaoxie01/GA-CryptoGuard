from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from plugins.crypto_guard.config.loader import PROJECT_ROOT


_CONFIGURED = False


def get_logger(name: str = "crypto_guard") -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger(name)
    if not _CONFIGURED:
        _configure_root()
        _CONFIGURED = True
    return logger


def log_path() -> Path:
    raw = os.environ.get("CRYPTO_GUARD_LOG_DIR")
    root = Path(raw).expanduser().resolve() if raw else PROJECT_ROOT / "logs" / "crypto_guard"
    root.mkdir(parents=True, exist_ok=True)
    return root / "crypto_guard.log"


def _configure_root() -> None:
    logger = logging.getLogger("crypto_guard")
    logger.setLevel(os.environ.get("CRYPTO_GUARD_LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    if logger.handlers:
        return

    fmt = logging.Formatter(
        fmt="%(asctime)sZ %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    file_handler = RotatingFileHandler(log_path(), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if os.environ.get("CRYPTO_GUARD_LOG_CONSOLE", "1").lower() not in {"0", "false", "no"}:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        logger.addHandler(console)
