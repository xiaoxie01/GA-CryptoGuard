"""LLM 风险委员会：受约束的 LLM 风险协助策略配置（08-10, design.md §4）。

合约（test_pg_08_10_risk_policy_p2.py + confirmation_lifecycle_p1.py
TestRiskAssistancePolicyParsing）：

  - ``risk_assistance`` 是 ``config/trading_mode.yaml`` 的顶层版本化配置段，
    ``load_risk_assistance_config(config)`` 从 ``config["risk_assistance"]``
    读取（即 ``CryptoGuardConfig.trading_mode`` 所在的同一个 dict）。
    段缺失时返回编译默认 ``RiskAssistancePolicy()``（mode=shadow —— 迁移
    默认，绝不静默变成 paper_bounded）。
  - 段内缺省键回落编译默认；部分段如 ``{"mode": "paper_bounded"}`` 合法，
    仅覆盖 mode。
  - 硬闸门集合被编译进 ``HARD_GATE_CODES`` 常量，配置无法删除任何强制
    不变量：它只能选择该集合的**非空子集**，``policy.hard_gates`` 精确反映
    该子集（``hard_gates: []`` 被拒绝 —— 空闸门列表意味着"无硬闸门"，
    必须 fail closed，绝不静默豁免）。
  - 硬/自适应两集合不得重叠：一个被列为 hard 且（或默认会）属于 adaptive
    的值，反之亦然，都是硬错误。
  - Fail closed（ValueError，协助被禁用而非静默重分类）：未知 mode、未知
    策略键、类型错误、NaN/inf、TTL 超过其硬上限、硬上限低于 TTL、
    TTL/硬上限 timeframe 超出 {5m, 15m}、非正 TTL、未知闸门值、非 mapping
    段。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

# 编译进代码的强制硬闸门集合 —— 配置只能在它之上做子集，无法删除。
HARD_GATE_CODES: tuple[str, ...] = (
    "market_data_ready",
    "trusted_entry_confirmation",
    "account_enabled",
    "drawdown_limit",
    "exposure_limit",
    "valid_geometry",
    "idempotency",
    "extreme_regime",
)

# 自适应闸门（LLM 建议可经确定性验证器影响，但绝不豁免硬闸门）。
ADAPTIVE_GATE_CODES: tuple[str, ...] = (
    "minimum_stop_distance",
    "atr_stop_buffer",
    "minimum_rr",
    "news_like_event",
)

# LLM 风险委员会可引用的受限 reason-code 词表（Step 5 schema 合约）。委员会
# 只能在此集合内给 reason_codes；集合外引用在 context-aware 校验中 fail
# closed。与 ``test_pg_08_10_llm_risk_proposal_p1._KNOWN_REASON_CODES`` 一致。
KNOWN_REASON_CODES: frozenset[str] = frozenset({
    "entry_deviation",
    "minimum_stop_distance",
    "atr_stop_buffer",
    "minimum_rr",
    "news_like_event",
    "risk_allocation",
    "confirmation",
    "market_regime",
    "account_state",
    "no_edge",
})

VALID_MODES: tuple[str, ...] = ("off", "shadow", "paper_bounded")

_CONFIRMATION_TIMEFRAMES: frozenset[str] = frozenset({"5m", "15m"})

_DEFAULT_CONFIRMATION_TTL_BARS: dict[str, int] = {"5m": 3, "15m": 1}
_DEFAULT_CONFIRMATION_HARD_MAX_BARS: dict[str, int] = {"5m": 6, "15m": 2}

_POLICY_KEYS: frozenset[str] = frozenset(
    {
        "contract_version",
        "mode",
        "max_rounds",
        "max_tool_requests",
        "max_context_bytes",
        "max_uncertainty",
        "confirmation_ttl_bars",
        "confirmation_hard_max_bars",
        "max_entry_deviation_pct",
        "max_entry_deviation_atr",
        "max_stop_distance_pct",
        "max_stop_distance_atr",
        "hard_gates",
        "adaptive_gates",
    }
)


def _require_finite_number(value: Any, key: str) -> float:
    """数字校验：拒绝 bool/str/None，必须有限。

    ``bool`` 是 ``int`` 的子类，YAML 里的字面 ``true``/``false`` 会被当成
    数字 —— 必须显式拒绝，绝不静默把布尔当 0/1 参与风控计算。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"risk_assistance.{key} 必须是数字（不可为 bool/str）；got {value!r}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(
            f"risk_assistance.{key} 必须是有限数；got {value!r}"
        )
    return number


def _require_positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"risk_assistance.{key} 必须是整数（不可为 bool/str/float）；got {value!r}"
        )
    if value <= 0:
        raise ValueError(f"risk_assistance.{key} 必须为正整数；got {value}")
    return value


@dataclass(frozen=True)
class RiskAssistancePolicy:
    """编译默认 / 解析后的风险协助策略。

    构造器自带完整不变量校验：任何非法值都会在构造时抛 ``ValueError``
    （fail closed），不会得到一个语义可疑的策略对象。
    """

    contract_version: int = 1
    mode: str = "shadow"
    max_rounds: int = 2
    max_tool_requests: int = 5
    max_context_bytes: int = 49152
    max_uncertainty: float = 0.35
    confirmation_ttl_bars: dict[str, int] = field(
        default_factory=lambda: dict(_DEFAULT_CONFIRMATION_TTL_BARS)
    )
    confirmation_hard_max_bars: dict[str, int] = field(
        default_factory=lambda: dict(_DEFAULT_CONFIRMATION_HARD_MAX_BARS)
    )
    max_entry_deviation_pct: float = 0.50
    max_entry_deviation_atr: float = 0.25
    max_stop_distance_pct: float = 2.50
    max_stop_distance_atr: float = 2.00
    hard_gates: list[str] = field(default_factory=lambda: list(HARD_GATE_CODES))
    adaptive_gates: list[str] = field(
        default_factory=lambda: list(ADAPTIVE_GATE_CODES)
    )

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in VALID_MODES:
            raise ValueError(
                f"risk_assistance.mode 必须是 {VALID_MODES} 之一；got {self.mode!r}"
            )
        if isinstance(self.contract_version, bool) or not isinstance(
            self.contract_version, int
        ):
            raise ValueError(
                f"risk_assistance.contract_version 必须是整数；got {self.contract_version!r}"
            )
        if self.contract_version != 1:
            raise ValueError(
                f"risk_assistance.contract_version 必须为 1；got {self.contract_version}"
            )
        for key in ("max_rounds", "max_tool_requests", "max_context_bytes"):
            _require_positive_int(getattr(self, key), key)

        uncertainty = _require_finite_number(self.max_uncertainty, "max_uncertainty")
        if uncertainty < 0.0 or uncertainty > 1.0:
            raise ValueError(
                f"risk_assistance.max_uncertainty 必须 ∈ [0, 1]；got {uncertainty}"
            )
        for key in (
            "max_entry_deviation_pct",
            "max_entry_deviation_atr",
            "max_stop_distance_pct",
            "max_stop_distance_atr",
        ):
            value = _require_finite_number(getattr(self, key), key)
            if value < 0.0:
                raise ValueError(f"risk_assistance.{key} 不能为负；got {value}")

        self._validate_confirmation_bounds()
        self._validate_gate_sets()

    def _validate_confirmation_bounds(self) -> None:
        ttl = self.confirmation_ttl_bars
        hard_max = self.confirmation_hard_max_bars
        if not isinstance(ttl, Mapping) or not isinstance(hard_max, Mapping):
            raise ValueError(
                "risk_assistance.confirmation_ttl_bars / confirmation_hard_max_bars "
                "必须是 mapping"
            )
        tfs = set(ttl) | set(hard_max)
        if not tfs:
            raise ValueError("risk_assistance confirmation TTL mapping 不能为空")
        bad_tf = [tf for tf in tfs if tf not in _CONFIRMATION_TIMEFRAMES]
        if bad_tf:
            raise ValueError(
                f"risk_assistance confirmation timeframe 必须 ∈ "
                f"{sorted(_CONFIRMATION_TIMEFRAMES)}；got {sorted(bad_tf)}"
            )
        for tf in tfs:
            ttl_val = _require_positive_int(ttl.get(tf), f"confirmation_ttl_bars.{tf}")
            max_val = _require_positive_int(
                hard_max.get(tf), f"confirmation_hard_max_bars.{tf}"
            )
            if ttl_val > max_val:
                raise ValueError(
                    f"risk_assistance.confirmation_ttl_bars.{tf} ({ttl_val}) 必须 "
                    f"<= confirmation_hard_max_bars.{tf} ({max_val})"
                )

    def _validate_gate_sets(self) -> None:
        hard = self.hard_gates
        adaptive = self.adaptive_gates
        if not isinstance(hard, list) or not hard:
            raise ValueError(
                "risk_assistance.hard_gates 必须是非空 list；空列表意味着无硬闸门，禁止"
            )
        bad_hard = [g for g in hard if g not in HARD_GATE_CODES]
        if bad_hard:
            raise ValueError(
                f"risk_assistance.hard_gates 含未知闸门 {sorted(bad_hard)}；"
                f"合法集合 {sorted(HARD_GATE_CODES)}"
            )
        if not isinstance(adaptive, list):
            raise ValueError("risk_assistance.adaptive_gates 必须是 list")
        bad_adaptive = [g for g in adaptive if g not in ADAPTIVE_GATE_CODES]
        if bad_adaptive:
            raise ValueError(
                f"risk_assistance.adaptive_gates 含未知闸门 {sorted(bad_adaptive)}；"
                f"合法集合 {sorted(ADAPTIVE_GATE_CODES)}"
            )
        overlap = set(hard) & set(adaptive)
        if overlap:
            raise ValueError(
                "risk_assistance 硬/自适应闸门集合不得重叠；"
                f"重叠值 {sorted(overlap)}"
            )


def load_risk_assistance_config(config: Mapping[str, Any]) -> RiskAssistancePolicy:
    """从顶层 ``config["risk_assistance"]`` 段解析策略；段缺失返回编译默认。

    每个键都精确校验；任何未知键 / 类型错误 / 越界 / 重叠都抛
    ``ValueError``（fail closed —— 协助被禁用而非静默重分类）。
    """
    section = config.get("risk_assistance")
    if section is None:
        return RiskAssistancePolicy()
    if not isinstance(section, Mapping):
        raise ValueError(
            "risk_assistance 必须是 YAML mapping 段；"
            f"got {type(section).__name__}"
        )

    unknown = [k for k in section if k not in _POLICY_KEYS]
    if unknown:
        raise ValueError(
            f"risk_assistance 含未知键 {sorted(unknown)}；合法键 {sorted(_POLICY_KEYS)}"
        )

    kwargs: dict[str, Any] = {}

    contract_version = section.get("contract_version", 1)
    if isinstance(contract_version, bool) or not isinstance(contract_version, int):
        raise ValueError(
            f"risk_assistance.contract_version 必须是整数；got {contract_version!r}"
        )
    if contract_version != 1:
        raise ValueError(
            f"risk_assistance.contract_version 必须为 1；got {contract_version}"
        )
    kwargs["contract_version"] = contract_version

    mode = section.get("mode", "shadow")
    if not isinstance(mode, str) or mode not in VALID_MODES:
        raise ValueError(
            f"risk_assistance.mode 必须是 {VALID_MODES} 之一；got {mode!r}"
        )
    kwargs["mode"] = mode

    for key in ("max_rounds", "max_tool_requests", "max_context_bytes"):
        if key in section:
            kwargs[key] = _require_positive_int(section[key], key)

    for key in (
        "max_uncertainty",
        "max_entry_deviation_pct",
        "max_entry_deviation_atr",
        "max_stop_distance_pct",
        "max_stop_distance_atr",
    ):
        if key in section:
            value = _require_finite_number(section[key], key)
            if key == "max_uncertainty" and (value < 0.0 or value > 1.0):
                raise ValueError(
                    f"risk_assistance.max_uncertainty 必须 ∈ [0, 1]；got {value}"
                )
            if key != "max_uncertainty" and value < 0.0:
                raise ValueError(f"risk_assistance.{key} 不能为负；got {value}")
            kwargs[key] = value

    kwargs["confirmation_ttl_bars"] = _parse_confirmation_map(
        section, "confirmation_ttl_bars", _DEFAULT_CONFIRMATION_TTL_BARS
    )
    kwargs["confirmation_hard_max_bars"] = _parse_confirmation_map(
        section, "confirmation_hard_max_bars", _DEFAULT_CONFIRMATION_HARD_MAX_BARS
    )

    if "hard_gates" in section:
        raw = section["hard_gates"]
        if not isinstance(raw, list) or not raw:
            raise ValueError(
                "risk_assistance.hard_gates 必须是非空 list；空列表意味着无硬闸门，禁止"
            )
        kwargs["hard_gates"] = [str(g) for g in raw]
    if "adaptive_gates" in section:
        raw = section["adaptive_gates"]
        if not isinstance(raw, list):
            raise ValueError("risk_assistance.adaptive_gates 必须是 list")
        kwargs["adaptive_gates"] = [str(g) for g in raw]

    # 构造器执行完整不变量校验（含跨字段：TTL<=hard_max、硬/自适应不重叠）。
    return RiskAssistancePolicy(**kwargs)


def _parse_confirmation_map(
    section: Mapping[str, Any], key: str, defaults: dict[str, int]
) -> dict[str, int]:
    raw = section.get(key, defaults)
    if not isinstance(raw, Mapping):
        raise ValueError(f"risk_assistance.{key} 必须是 mapping；got {raw!r}")
    if not raw:
        raise ValueError(f"risk_assistance.{key} 不能为空")
    for tf, val in raw.items():
        if tf not in _CONFIRMATION_TIMEFRAMES:
            raise ValueError(
                f"risk_assistance.{key} timeframe 必须 ∈ "
                f"{sorted(_CONFIRMATION_TIMEFRAMES)}；got {tf!r}"
            )
        _require_positive_int(val, f"{key}.{tf}")
    return {**defaults, **{str(tf): int(v) for tf, v in raw.items()}}
