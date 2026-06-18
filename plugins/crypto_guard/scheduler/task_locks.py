from __future__ import annotations

import os

from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def acquire_lock(repo: CryptoGuardRepository, lock_name: str, ttl_seconds: int = 600) -> tuple[bool, str]:
    owner = f"{os.getpid()}:{lock_name}"
    return repo.acquire_lock(lock_name, owner, ttl_seconds), owner


def release_lock(repo: CryptoGuardRepository, lock_name: str, owner: str) -> None:
    repo.release_lock(lock_name, owner)
