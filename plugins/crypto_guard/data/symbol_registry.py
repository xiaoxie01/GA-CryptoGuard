from __future__ import annotations

from typing import Any

from plugins.crypto_guard.data.binance_rest import normalize_symbol, validate_um_futures_symbol
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def add_symbol(repo: CryptoGuardRepository, symbol: str, *, category: str = "custom", timeframes: list[str] | None = None, validate: bool = True) -> dict[str, Any]:
    norm = normalize_symbol(symbol)
    if validate and not validate_um_futures_symbol(norm):
        return {"ok": False, "symbol": norm, "error": "Binance USDⓈ-M Futures 未找到该合约"}
    row = repo.upsert_symbol(norm, category=category, enabled=True, source="user", timeframes=timeframes or ["4h", "1h", "15m", "5m"])
    return {"ok": True, "symbol": norm, "row": row}


def remove_symbol(repo: CryptoGuardRepository, symbol: str) -> dict[str, Any]:
    norm = normalize_symbol(symbol)
    return {"ok": repo.remove_symbol(norm), "symbol": norm}


def pause_symbol(repo: CryptoGuardRepository, symbol: str) -> dict[str, Any]:
    norm = normalize_symbol(symbol)
    return {"ok": repo.set_symbol_enabled(norm, False), "symbol": norm}


def resume_symbol(repo: CryptoGuardRepository, symbol: str) -> dict[str, Any]:
    norm = normalize_symbol(symbol)
    return {"ok": repo.set_symbol_enabled(norm, True), "symbol": norm}


def list_symbols(repo: CryptoGuardRepository) -> dict[str, Any]:
    rows = repo.list_symbols(include_disabled=True)
    return {"ok": True, "symbols": rows, "count": len(rows)}
