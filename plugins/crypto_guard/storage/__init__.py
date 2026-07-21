"""CryptoGuard storage layer.

PostgreSQL is the only supported engine (fail-closed; NO SQLite fallback).
The pool-backed connection primitives live in :mod:`pg_db`.
"""

from .pg_db import (
    CryptoGuardDBUnavailable,
    check_connection,
    database_identity,
    get_conn,
    get_pool,
    reset_pool,
    transaction,
)
from .migrations import initialize_database
from .repository import CryptoGuardRepository

__all__ = [
    "CryptoGuardDBUnavailable",
    "check_connection",
    "database_identity",
    "get_conn",
    "get_pool",
    "initialize_database",
    "reset_pool",
    "transaction",
    "CryptoGuardRepository",
]
