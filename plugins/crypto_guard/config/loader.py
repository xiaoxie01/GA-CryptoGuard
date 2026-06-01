from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PLUGIN_ROOT.parents[1]
CONFIG_DIR = PLUGIN_ROOT / "config"


@dataclass(frozen=True)
class CryptoGuardConfig:
    """集中保存插件配置，禁止实盘开关在这里做最终兜底。"""

    trading_mode: dict[str, Any]
    symbols: dict[str, Any]
    scheduler: dict[str, Any]
    strategies: dict[str, Any]
    database_path: Path

    @property
    def live_trading_enabled(self) -> bool:
        mode = self.trading_mode.get("trading_mode", {})
        return bool(mode.get("live_trading_enabled", False))

    @property
    def paper_trading_enabled(self) -> bool:
        mode = self.trading_mode.get("trading_mode", {})
        return bool(mode.get("paper_trading_enabled", True))


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 YAML object: {path}")
    return data


def _default_db_path() -> Path:
    raw = os.environ.get("CRYPTO_GUARD_DB")
    if raw:
        return Path(raw).expanduser().resolve()
    return (PROJECT_ROOT / "data" / "crypto_guard" / "crypto_guard.sqlite3").resolve()


def load_config(config_dir: Path | None = None) -> CryptoGuardConfig:
    cfg_dir = config_dir or CONFIG_DIR
    config = CryptoGuardConfig(
        trading_mode=_read_yaml(cfg_dir / "trading_mode.yaml"),
        symbols=_read_yaml(cfg_dir / "symbols.yaml"),
        scheduler=_read_yaml(cfg_dir / "scheduler.yaml"),
        strategies=_read_yaml(cfg_dir / "strategies.yaml"),
        database_path=_default_db_path(),
    )
    if config.live_trading_enabled:
        raise RuntimeError("CryptoGuard 禁止实盘：live_trading_enabled 必须为 false")
    mode = config.trading_mode.get("trading_mode", {})
    if mode.get("allow_trade_api") or mode.get("allow_withdraw_api") or mode.get("real_order_api_enabled"):
        raise RuntimeError("CryptoGuard 禁止交易/提现权限 API")
    return config
