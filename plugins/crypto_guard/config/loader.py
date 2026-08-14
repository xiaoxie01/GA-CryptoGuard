from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from psycopg.conninfo import conninfo_to_dict


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
    database_url: str

    @property
    def database_path(self) -> Path:
        """DEPRECATED alias kept only while callers migrate to PostgreSQL.

        CryptoGuard now runs on PostgreSQL only (fail-closed; no SQLite
        fallback). The runtime DSN lives in :attr:`database_url`. A few legacy
        call sites still reference ``database_path`` (e.g. the Redis-path
        heuristic); they are migrated in the cutover and this property will be
        removed. It raises so any unmigrated caller fails loudly rather than
        silently using a stale SQLite path.
        """
        raise RuntimeError(
            "database_path is removed; CryptoGuard uses PostgreSQL. "
            "Use config.database_url (DSN) instead."
        )

    @property
    def risk_assistance(self):
        """08-10: LLM 风险委员会策略（design.md §4）。

        ``risk_assistance`` 是 ``trading_mode.yaml`` 的顶层段。惰性导入避免
        config.loader <-> risk 包初始化期循环。段缺失返回编译默认
        （mode=shadow）；段存在但含任何非法键/值/重叠都抛 ``ValueError``
        （fail closed —— 协助被禁用而非静默重分类）。
        """
        from plugins.crypto_guard.risk.risk_policy import load_risk_assistance_config

        return load_risk_assistance_config(self.trading_mode)

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
    """DEPRECATED. CryptoGuard is PostgreSQL-only; kept only to surface a clear
    error for unmigrated callers instead of silently building a SQLite path."""
    raise RuntimeError(
        "CryptoGuard no longer uses SQLite. Set CRYPTO_GUARD_DATABASE_URL to a "
        "PostgreSQL DSN (postgresql://crypto_guard_app:<pw>@host:5432/crypto_guard)."
    )


def resolve_database_url() -> str:
    """Resolve the PostgreSQL DSN from ``CRYPTO_GUARD_DATABASE_URL``.

    Fail-closed: if the env var is unset/empty, raise ``RuntimeError`` (caught
    by ``pg_db.resolve_dsn`` and re-raised as ``CryptoGuardDBUnavailable``).
    There is NO SQLite fallback. The DSN must be a ``postgresql://`` URL; the
    application password is supplied only via this env var (never committed).
    """
    raw = os.environ.get("CRYPTO_GUARD_DATABASE_URL", "").strip()
    if not raw:
        raise RuntimeError(
            "CRYPTO_GUARD_DATABASE_URL is not set; CryptoGuard requires a "
            "PostgreSQL DSN (postgresql://crypto_guard_app:<pw>@host:5432/"
            "crypto_guard). No SQLite fallback is available."
        )
    if not raw.startswith(("postgresql://", "postgres://")):
        raise RuntimeError(
            "CRYPTO_GUARD_DATABASE_URL must be a postgresql:// DSN; got a non-"
            "PostgreSQL value. CryptoGuard does not fall back to SQLite."
        )
    try:
        identity = conninfo_to_dict(raw)
    except Exception as exc:
        raise RuntimeError(
            "CRYPTO_GUARD_DATABASE_URL is not a valid PostgreSQL DSN"
        ) from exc
    user = str(identity.get("user") or "")
    dbname = str(identity.get("dbname") or "")
    allowed_identity = (
        (user == "crypto_guard_app" and dbname == "crypto_guard")
        or (user == "crypto_guard_test_app" and dbname == "crypto_guard_test")
    )
    if not allowed_identity:
        raise RuntimeError(
            "CRYPTO_GUARD_DATABASE_URL must use the dedicated "
            "crypto_guard_app/crypto_guard runtime identity (or the isolated "
            "crypto_guard_test_app/crypto_guard_test test identity); superuser "
            "and arbitrary-role DSNs are forbidden"
        )
    return raw


def _resolve_scoped_postgres_url(
    env_name: str,
    *,
    allowed_identities: set[tuple[str, str]],
) -> str:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        raise RuntimeError(f"{env_name} is not set")
    try:
        identity = conninfo_to_dict(raw)
    except Exception as exc:
        raise RuntimeError(f"{env_name} is not a valid PostgreSQL DSN") from exc
    pair = (str(identity.get("user") or ""), str(identity.get("dbname") or ""))
    if pair not in allowed_identities:
        raise RuntimeError(f"{env_name} violates its dedicated-role contract")
    return raw


def resolve_migration_database_url() -> str:
    """Resolve the explicit DDL-only production migrator identity."""
    return _resolve_scoped_postgres_url(
        "CRYPTO_GUARD_MIGRATION_DATABASE_URL",
        allowed_identities={("crypto_guard_migrator", "crypto_guard")},
    )


def resolve_replay_database_url() -> str:
    """Resolve the isolated replay database; production runtime DB is forbidden."""
    raw = os.environ.get("CRYPTO_GUARD_REPLAY_DATABASE_URL", "").strip()
    if raw:
        return _resolve_scoped_postgres_url(
            "CRYPTO_GUARD_REPLAY_DATABASE_URL",
            allowed_identities={("crypto_guard_replay", "crypto_guard_replay")},
        )
    # Tests deliberately reuse their disposable role/database and isolate each
    # replay in a scratch schema. Production app identity never gets this path.
    runtime = resolve_database_url()
    identity = conninfo_to_dict(runtime)
    if (
        str(identity.get("user") or "") == "crypto_guard_test_app"
        and str(identity.get("dbname") or "") == "crypto_guard_test"
    ):
        return runtime
    raise RuntimeError("CRYPTO_GUARD_REPLAY_DATABASE_URL is not set")


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
    # Resolve the DSN through ``pg_db.resolve_dsn`` (NOT the raw
    # ``resolve_database_url``) so a missing/malformed DSN surfaces uniformly as
    # ``CryptoGuardDBUnavailable`` — the fail-closed contract callers gate on.
    # ``resolve_database_url`` raises a bare ``RuntimeError``; ``CryptoGuardDBUnavailable``
    # subclasses ``RuntimeError`` but the reverse is not true, so callers that
    # ``except CryptoGuardDBUnavailable`` would otherwise miss a missing-DSN at
    # config-load time. Lazy import avoids a config/loader <-> storage/pg_db cycle.
    from plugins.crypto_guard.storage import pg_db

    config = CryptoGuardConfig(
        trading_mode=_read_yaml(cfg_dir / "trading_mode.yaml"),
        symbols=_read_yaml(cfg_dir / "symbols.yaml"),
        scheduler=_read_yaml(cfg_dir / "scheduler.yaml"),
        strategies=_read_yaml(cfg_dir / "strategies.yaml"),
        database_url=pg_db.resolve_dsn(),
    )
    if config.live_trading_enabled:
        raise RuntimeError("CryptoGuard 禁止实盘：live_trading_enabled 必须为 false")
    mode = config.trading_mode.get("trading_mode", {})
    if mode.get("allow_trade_api") or mode.get("allow_withdraw_api") or mode.get("real_order_api_enabled"):
        raise RuntimeError("CryptoGuard 禁止交易/提现权限 API")
    # Phase B (07-03): validate market_semantics config segment.
    _validate_market_semantics(config.trading_mode)
    # Phase B (07-10): validate llm.scheduling + llm.generation config
    # segments. Per-symbol timeout must be a real integer in 180..1200s with
    # no silent clamping — out-of-range values fail fast at startup.
    _validate_llm_scheduling(config.trading_mode)
    # 08-10: validate the risk_assistance policy segment (design.md §4).
    # Unknown mode/key, overlapping hard/adaptive gates, empty hard_gates,
    # TTL above hard max, NaN/inf, and wrong types all fail fast at startup.
    # 08-10 P2-2 (fresh reviewer P2): validate the account-risk caps segment.
    # A PRESENT-but-invalid cap (bool, non-numeric, NaN/Inf, <=0) is a config
    # defect that must fail fast at startup — it can never silently become
    # "no cap" (which is what a fail-open ``_safe_positive``/``_cfg_pct``
    # produced for a 0/NaN cap before the fix). Absent caps are safe:
    # account_risk_guard DEFAULTS (2.0/10.0) fill the gap.
    _validate_account_risk(config.trading_mode)
    # 08-10 P2-1 (fresh reviewer P2): validate the ``risk`` thresholds segment.
    # A PRESENT-but-invalid threshold (bool, non-numeric, NaN/Inf, <=0) is a
    # config defect that fails fast at startup — as a fail-open
    # ``float(risk_cfg.get(...))`` it would silently disable the min_rr /
    # min_sl / min_tp gates (``rr < nan`` is always False). Absent keys are
    # safe: ``cfg_threshold`` fills the gap with the code defaults.
    _validate_risk(config.trading_mode)
    config.risk_assistance
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


def _validate_account_risk(trading_mode: dict[str, Any]) -> None:
    """Validate the ``account_risk`` caps segment of trading_mode.yaml (08-10
    P2-2, fresh-reviewer P2).

    Each cap — ``max_single_trade_risk_pct`` / ``max_total_risk_pct`` — must be,
    when present, a real finite float strictly greater than 0. ``bool`` is a
    subclass of ``int`` in Python, so a literal ``true``/``false`` in YAML must
    be rejected explicitly; NaN/Inf and non-numeric values are rejected too, and
    a ``0``/negative cap is meaningless (a 0 cap is a config defect — treated as
    "no cap" would be fail-open). Absent caps are safe: account_risk_guard
    DEFAULTS (2.0/10.0) fill the gap, so this validator only fails when a value
    IS present but invalid.
    """
    seg = trading_mode.get("account_risk")
    if seg is None:
        return  # absent segment -> account_risk_guard DEFAULTS apply
    if not isinstance(seg, dict):
        raise ValueError(
            "trading_mode.account_risk must be a mapping; "
            f"got {type(seg).__name__}"
        )
    import math as _math
    for key in ("max_single_trade_risk_pct", "max_total_risk_pct"):
        raw = seg.get(key)
        if raw is None:
            continue  # per-key optional; DEFAULTS fill the gap
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(
                f"trading_mode.account_risk.{key} 必须是有限正数；got {raw!r}"
            )
        value = float(raw)
        if not _math.isfinite(value) or value <= 0.0:
            raise ValueError(
                f"trading_mode.account_risk.{key} 必须是有限正数；got {raw!r}"
            )


def cfg_threshold(risk_cfg: dict[str, Any] | None, key: str, default: float) -> float:
    """Read one ``risk`` threshold FAIL-CLOSED (08-10 P2-1, fresh reviewer P2).

    Every gate that feeds an open/close decision MUST fail closed on a
    misconfigured threshold, never silently disable itself. A present value
    that is ``bool`` / non-(int,float) / non-finite / <=0 is a config defect
    and raises ``ValueError``; the caller is expected to catch it and record a
    fail-closed rejection (verifier ``_risk_thresholds`` raise -> recorded
    ``风控阈值配置读取失败``; producer threading raise -> ``failed`` envelope).
    Absent key -> ``default`` (the code default; e.g. ``min_rr`` 2.0 applies
    ONLY when the key is absent — the YAML-effective value is 1.5).
    """
    raw = (risk_cfg or {}).get(key)
    if raw is None:
        return float(default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(
            f"trading_mode.risk.{key} 必须是有限正数；got {raw!r}"
        )
    value = float(raw)
    import math as _math
    if not _math.isfinite(value) or value <= 0.0:
        raise ValueError(
            f"trading_mode.risk.{key} 必须是有限正数；got {raw!r}"
        )
    return value


_RISK_THRESHOLD_DEFAULTS: dict[str, float] = {
    "min_rr": 2.0,
    "min_sl_distance_pct": 0.8,
    "min_tp_distance_pct": 1.0,
    "min_confidence": 0.72,
    "min_confidence_for_paper_order": 0.72,
    "rsi_overbought_threshold": 75,
    "rsi_oversold_threshold": 25,
}


def _validate_risk(trading_mode: dict[str, Any]) -> None:
    """Validate the ``risk`` thresholds segment of trading_mode.yaml (08-10 P2-1).

    Present-but-invalid thresholds (bool / non-(int,float) / NaN / Inf / <=0)
    fail fast at startup — a silently-fail-open gate (``rr < nan`` is always
    False) is exactly the defect class the verifier fail-closed fixes. Absent
    keys are safe: the code defaults (``cfg_threshold``) fill the gap.
    """
    seg = trading_mode.get("risk")
    if seg is None:
        return  # absent segment -> code defaults apply
    if not isinstance(seg, dict):
        raise ValueError(
            "trading_mode.risk must be a mapping; "
            f"got {type(seg).__name__}"
        )
    for key, default in _RISK_THRESHOLD_DEFAULTS.items():
        cfg_threshold(seg, key, default)


# 07-10 Phase B: per-symbol deadline range. Out-of-range values fail fast at
# startup - NO silent clamping. The contract is 180..1200s (3..20 min). Below
# 180 a single slow provider call (P95 35s, max 104s) cannot complete even one
# attempt plus jitter+parse; above 1200 (20 min) the symbol can overlap the
# 15-minute scheduler tick and must rely on single-flight instead.
LLM_PER_SYMBOL_TIMEOUT_MIN_SECONDS = 180
LLM_PER_SYMBOL_TIMEOUT_MAX_SECONDS = 1200


def _validate_llm_scheduling(trading_mode: dict[str, Any]) -> None:
    """Validate the ``llm.scheduling`` + ``llm.generation`` segments.

    The per-symbol timeout MUST be a real integer in 180..1200. ``bool`` is a
    subclass of ``int`` in Python, so a literal ``true``/``false`` in YAML
    would silently pass an ``isinstance(x, int)`` check - reject it. Floats
    and strings are also rejected (no silent coercion). Out-of-range values
    raise, never clamp - a misconfigured 30s timeout that silently became
    180s would mask the starvation we are repairing.

    ``per_attempt_timeout_seconds`` must be a positive integer no greater
    than the per-symbol timeout. ``max_concurrency`` must be an integer in
    1..4. ``batch_completion_guard_seconds`` must be positive.
    """
    llm = trading_mode.get("llm")
    if not isinstance(llm, dict):
        # ``llm`` is optional in legacy configs; only validate when present.
        return

    sched = llm.get("scheduling")
    if not isinstance(sched, dict):
        raise ValueError(
            "llm.scheduling must be a mapping (07-10 fair scheduling); "
            f"got {type(sched).__name__}"
        )

    # per_symbol_timeout_seconds: reject bool, float, str; range 180..1200.
    pst = sched.get("per_symbol_timeout_seconds")
    if isinstance(pst, bool) or not isinstance(pst, int):
        raise ValueError(
            "llm.scheduling.per_symbol_timeout_seconds 必须是整数（不可为 "
            f"bool/float/str）；got {pst!r}"
        )
    if pst < LLM_PER_SYMBOL_TIMEOUT_MIN_SECONDS or pst > LLM_PER_SYMBOL_TIMEOUT_MAX_SECONDS:
        raise ValueError(
            "llm.scheduling.per_symbol_timeout_seconds 必须 ∈ ["
            f"{LLM_PER_SYMBOL_TIMEOUT_MIN_SECONDS}, "
            f"{LLM_PER_SYMBOL_TIMEOUT_MAX_SECONDS}]（不允许静默截断）；got {pst}"
        )

    # per_attempt_timeout_seconds: positive int, <= per_symbol_timeout.
    pat = sched.get("per_attempt_timeout_seconds")
    if isinstance(pat, bool) or not isinstance(pat, int):
        raise ValueError(
            "llm.scheduling.per_attempt_timeout_seconds 必须是整数；"
            f"got {pat!r}"
        )
    if pat <= 0:
        raise ValueError(
            f"llm.scheduling.per_attempt_timeout_seconds 必须为正整数；got {pat}"
        )
    if pat > pst:
        raise ValueError(
            "llm.scheduling.per_attempt_timeout_seconds 必须 <= "
            f"per_symbol_timeout_seconds ({pat} > {pst})"
        )

    # max_concurrency: integer 1..4.
    mc = sched.get("max_concurrency")
    if isinstance(mc, bool) or not isinstance(mc, int):
        raise ValueError(
            f"llm.scheduling.max_concurrency 必须是整数；got {mc!r}"
        )
    if mc < 1 or mc > 4:
        raise ValueError(
            f"llm.scheduling.max_concurrency 必须 ∈ [1, 4]；got {mc}"
        )

    # batch_completion_guard_seconds: positive.
    guard = sched.get("batch_completion_guard_seconds")
    if isinstance(guard, bool) or not isinstance(guard, int):
        raise ValueError(
            "llm.scheduling.batch_completion_guard_seconds 必须是整数；"
            f"got {guard!r}"
        )
    if guard <= 0:
        raise ValueError(
            "llm.scheduling.batch_completion_guard_seconds 必须为正整数；"
            f"got {guard}"
        )

    # mode: must be a known value.
    mode = sched.get("mode")
    if mode not in ("fair_pool", "legacy_serial"):
        raise ValueError(
            "llm.scheduling.mode 必须是 'fair_pool' 或 'legacy_serial'；"
            f"got {mode!r}"
        )

    # rotate_start_symbol: bool.
    if not isinstance(sched.get("rotate_start_symbol"), bool):
        raise ValueError(
            "llm.scheduling.rotate_start_symbol 必须是 bool；"
            f"got {sched.get('rotate_start_symbol')!r}"
        )

    # generation: optional but, when present, must have valid bounds.
    gen = llm.get("generation")
    if gen is None:
        return
    if not isinstance(gen, dict):
        raise ValueError(
            f"llm.generation must be a mapping；got {type(gen).__name__}"
        )
    for key, minimum in (
        ("max_prompt_bytes", 1024),
        ("target_prompt_bytes", 1024),
        ("max_output_tokens", 1),
        ("thinking_budget_tokens", 0),
    ):
        val = gen.get(key)
        if isinstance(val, bool) or not isinstance(val, int):
            raise ValueError(f"llm.generation.{key} 必须是整数；got {val!r}")
        if val < minimum:
            raise ValueError(
                f"llm.generation.{key} 必须 >= {minimum}；got {val}"
            )
    # 07-13 R7 (P1-1): ``min_structured_answer_tokens`` is OPTIONAL with a
    # default of 4096 - legacy configs that predate the reserve-minimum
    # contract still load and get the floor applied. When present it must be
    # an integer (bool/float/str rejected) >= 0. Plan ref:
    # production-incident-repair-plan-07-13.md §4 P0-3 item 1 + AC6.
    _msa_raw = gen.get("min_structured_answer_tokens", 4096)
    if isinstance(_msa_raw, bool) or not isinstance(_msa_raw, int):
        raise ValueError(
            f"llm.generation.min_structured_answer_tokens 必须是整数；got {_msa_raw!r}"
        )
    if _msa_raw < 0:
        raise ValueError(
            f"llm.generation.min_structured_answer_tokens 必须 >= 0；got {_msa_raw}"
        )
    if gen.get("target_prompt_bytes") > gen.get("max_prompt_bytes"):
        raise ValueError(
            "llm.generation.target_prompt_bytes 必须 <= max_prompt_bytes"
        )
    # 07-13 R6-D (P0-3.1) + R7 (P1-1): when extended thinking is enabled
    # (thinking_budget_tokens > 0), it MUST be strictly less than
    # max_output_tokens so structured JSON has a non-empty answer reserve, AND
    # the remaining structured-answer reserve
    # (``max_output_tokens - thinking_budget_tokens``) MUST be at least
    # ``min_structured_answer_tokens``. The pre-fix production config
    # (thinking=6000, max_output=4096) let thinking consume the entire output
    # budget, truncating the structured answer at exactly 4096 tokens
    # (stop_reason=max_tokens). thinking=0 (disabled) is always allowed
    # regardless of max_output (the whole max_output is the answer reserve).
    # Plan ref: production-incident-repair-plan-07-13.md §4 P0-3 item 1 + AC6.
    _think = int(gen.get("thinking_budget_tokens", 0) or 0)
    _max_out = int(gen.get("max_output_tokens", 0) or 0)
    _min_reserve = int(_msa_raw)
    if _think > 0 and _think >= _max_out:
        raise ValueError(
            f"llm.generation.thinking_budget_tokens ({_think}) 必须 < "
            f"max_output_tokens ({_max_out}) when thinking is enabled (>0); "
            f"otherwise structured JSON has no answer reserve and output is "
            f"truncated at max_output_tokens (stop_reason=max_tokens). "
            f"Set thinking_budget_tokens=0 to disable extended thinking, or "
            f"raise max_output_tokens above the thinking budget."
        )
    if _think > 0 and (_max_out - _think) < _min_reserve:
        raise ValueError(
            f"llm.generation structured-JSON reserve "
            f"(max_output_tokens ({_max_out}) - thinking_budget_tokens "
            f"({_think}) = {_max_out - _think}) must be >= "
            f"min_structured_answer_tokens ({_min_reserve}) when thinking is "
            f"enabled (>0); otherwise the model can spend the reserve on "
            f"thinking and truncate the structured answer "
            f"(stop_reason=max_tokens). Raise max_output_tokens, lower "
            f"thinking_budget_tokens, or lower min_structured_answer_tokens."
        )
    temp = gen.get("temperature")
    if temp is not None:
        if isinstance(temp, bool) or not isinstance(temp, (int, float)):
            raise ValueError(
                f"llm.generation.temperature 必须是数字；got {temp!r}"
            )
        temp_f = float(temp)
        if temp_f < 0.0 or temp_f > 2.0:
            raise ValueError(
                f"llm.generation.temperature 必须 ∈ [0.0, 2.0]；got {temp_f}"
            )
