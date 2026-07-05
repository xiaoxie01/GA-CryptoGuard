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

    @property
    def market_data(self) -> dict[str, Any]:
        """R1: configurable sample contract — no scattered hardcodes.

        Loaded from ``scheduler.yaml``'s ``market_data:`` section. Exposes
        ``required_samples``, ``analysis_window``, ``fetch_lookback``,
        ``backfill``, and ``freshness`` sub-keys. Falls back to defaults
        (1d/4h/1h=250, 15m=200, 5m=150) when the section is absent.
        """
        md = self.scheduler.get("market_data") or {}
        if not isinstance(md, dict):
            return _default_market_data()
        # Ensure required sub-keys exist with defaults.
        defaults = _default_market_data()
        for key, default_val in defaults.items():
            if key not in md:
                md[key] = default_val
            elif isinstance(default_val, dict) and isinstance(md[key], dict):
                for sub_key, sub_val in default_val.items():
                    if sub_key not in md[key]:
                        md[key][sub_key] = sub_val
        return md


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


def _default_market_data() -> dict[str, Any]:
    """R1 default sample contract (follow-up: 1D/4H/1H=250, 15M=200, 5M=150)."""
    return {
        "required_samples": {"1d": 250, "4h": 250, "1h": 250, "15m": 200, "5m": 150},
        "analysis_window": {"1d": 250, "4h": 250, "1h": 250, "15m": 200, "5m": 150},
        "fetch_lookback": {"1d": 3, "4h": 6, "1h": 12, "15m": 12, "5m": 24},
        "backfill": {
            "enabled": True,
            "page_limit": 1500,
            "max_pages_per_run": 50,
            "require_healthy_kline_for_limit": True,
            "unhealthy_kline_wick_ratio": 2.0,
        },
        "freshness": {"require_latest_closed": True},
    }


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
    # Phase B (07-03): validate market_semantics config segment.
    _validate_market_semantics(config.trading_mode)
    return config


def _validate_market_semantics(trading_mode: dict[str, Any]) -> None:
    """Validate the ``market_semantics`` segment of trading_mode.yaml.

    The cap must be a finite float in [0, 1] and strictly below
    ``MIN_CONFIDENCE_FOR_PAPER_ORDER`` so countertrend rebounds cannot reach
    the execution gate. Allowed-stage lists must be subsets of the legal
    stage enum. Misconfiguration fails fast at startup, not at decision time.
    """
    from plugins.crypto_guard.strategy.grade_config import MIN_CONFIDENCE_FOR_PAPER_ORDER

    seg = trading_mode.get("market_semantics")
    if not isinstance(seg, dict):
        raise ValueError(
            "trading_mode.market_semantics must be a mapping; "
            f"got {type(seg).__name__}"
        )
    raw_cap = seg.get("htf_conflict_confidence_cap")
    try:
        cap = float(raw_cap)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "market_semantics.htf_conflict_confidence_cap 必须是数字；"
            f"got {raw_cap!r}"
        ) from exc
    import math as _math
    if not _math.isfinite(cap) or cap < 0.0 or cap > 1.0:
        raise ValueError(
            f"market_semantics.htf_conflict_confidence_cap 必须 ∈ [0, 1]；got {cap}"
        )
    if cap >= MIN_CONFIDENCE_FOR_PAPER_ORDER:
        raise ValueError(
            f"market_semantics.htf_conflict_confidence_cap={cap} 必须 < "
            f"MIN_CONFIDENCE_FOR_PAPER_ORDER={MIN_CONFIDENCE_FOR_PAPER_ORDER}"
        )
    legal_stages = {"early", "middle", "late", "range", "transition", "unknown"}
    for key in (
        "allowed_stages_for_neutral_bias",
        "allowed_stages_for_mixed_bias",
        "allowed_stages_for_unknown_bias",
    ):
        raw = seg.get(key)
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"market_semantics.{key} 必须是非空 list；got {raw!r}")
        invalid = [s for s in raw if s not in legal_stages]
        if invalid:
            raise ValueError(
                f"market_semantics.{key} 含非法 stage {invalid}；合法集合 {sorted(legal_stages)}"
            )
