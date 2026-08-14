# -*- coding: utf-8 -*-
"""08-10 Step 7: deterministic bounded adjustment verifier.

design.md §7 + prd.md P1-3 + implement.md Step 7. ``verify_risk_adjustment``
is the ONLY deterministic path through which an LLM risk proposal may affect a
trade plan. The verifier is pure and immutable: it never mutates the candidate
plan, the proposal or the snapshot, and it never writes to any store.

The proposal is advisory. Every branch fail-closes:

  - ``wait`` / ``reject`` -> no plan is constructed, ``ok=False``;
  - ``approve_as_is`` -> the candidate is deep-copied verbatim;
  - ``adjust`` -> only the allowlisted numeric fields
    (``entry_price`` / ``stop_loss`` / ``take_profits`` / ``risk_percent`` /
    ``news_like_event_policy``) may change; any other key — including a forged
    ``candidate_fingerprint`` or ``symbol``/``side`` — is a structural
    rejection and discards the plan entirely;
  - a top-level ``candidate_fingerprint`` in the proposal must match
    ``candidate_plan_fingerprint(...)`` EXACTLY or the whole verification fails.

Deterministic constraints (mirroring the risk engine, applied to the ADJUSTED
plan, plus policy-bounded deviations):

  - stop may never tighten: adjusted stop distance >= candidate stop distance;
  - a wider stop/entry scales ``risk_percent`` down so monetary risk never
    increases (design.md §7 step 8 risk-budget ceiling);
  - account risk caps are FAIL-CLOSED (08-10 P2-3): the intended risk (min
    over candidate / stop-scaling / explicit adjustment) must not exceed the
    configured ``max_single_trade_risk_pct`` nor the remaining total budget
    ``max_total_risk_pct - open_position_risk_pct``; exceeding either REJECTS
    the proposal with the cap cited — never a silent clamp into an order;
  - entry must stay inside the candidate zone: deviation <=
    ``max_entry_deviation_pct`` AND <= ``max_entry_deviation_atr`` * ATR;
  - stop distance must satisfy min percentage (``min_sl_distance_pct``) and
    the ATR buffer ``max(0.2*ATR, entry*min_sl_pct)``, and never exceed
    ``max_stop_distance_pct`` / ``max_stop_distance_atr``*ATR;
  - take profits stay on the profitable side, finite/positive, ratios in
    (0, 1], summing to ~1.0;
  - the adjusted plan is then re-run through the FULL existing risk engine
    (``validate_trade_plan``) — the engine is the last word on the plan;
  - hard gates are independently enforced: valid confirmation lifecycle,
    market-data ready, no extreme regime (news_like_event is adaptive: it
    blocks unless the plan carries an explicit ``news_like_event_policy``
    dict with truthy ``allow``), account enabled/not paused/no hard drawdown/
    no order-cap exhaustion.

Only when ``ok=True`` AND ``policy.mode == "paper_bounded"`` is
``effective_order_allowed`` True. In ``shadow`` / ``off`` the verifier reports
the outcome but never authorises an order.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from plugins.crypto_guard.analysis.market_regime_engine import EXTREME_REGIMES
from plugins.crypto_guard.risk.account_risk_guard import DEFAULTS as ACCOUNT_DEFAULTS
from plugins.crypto_guard.risk.risk_engine import validate_trade_plan
from plugins.crypto_guard.risk.risk_policy import (
    ADAPTIVE_GATE_CODES,
    RiskAssistancePolicy,
)
from plugins.crypto_guard.reasoning.entry_confirmation_lifecycle import (
    canonical_confirmation_fingerprint,
)
from plugins.crypto_guard.config.loader import cfg_threshold, load_config

# Allowlisted keys an ``adjust`` proposal may change. Everything else is a
# structural rejection (identity, symbol, side, execution fields, DB/notify
# actions and ``candidate_fingerprint`` can never appear here).
ADJUSTABLE_FIELDS = frozenset(
    {
        "entry_price",
        "stop_loss",
        "take_profits",
        "risk_percent",
        "news_like_event_policy",
    }
)

VALID_VERDICTS = frozenset({"approve_as_is", "adjust", "wait", "reject"})

# Hard drawdown threshold mirrors account_risk_guard.DEFAULTS
# ["drawdown_hard_risk_off_threshold"] so the verifier and the account gate
# cannot drift apart.
_DRAWDOWN_HARD_RISK_OFF_THRESHOLD = float(
    ACCOUNT_DEFAULTS.get("drawdown_hard_risk_off_threshold", -3.0)
)

_FALSE_OK = {"ok": False, "reasons": ["无可用计划"], "metrics": {}}


@dataclass(frozen=True)
class AdjustmentVerification:
    """Immutable result of a bounded adjustment verification."""

    ok: bool
    adjusted_plan: dict[str, Any] | None
    monetary_risk_delta: float
    final_risk_check: dict[str, Any]
    errors: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    effective_order_allowed: bool = False


# ---------------------------------------------------------------------------
# tiny numeric helpers (fail-closed on bool/NaN/Inf/non-number)
# ---------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        f = float(value)
        if math.isfinite(f):
            return f
    return None


def _safe_positive(value: Any) -> float | None:
    f = _num(value)
    if f is not None and f > 0:
        return f
    return None


def _plan_entry(plan: dict[str, Any]) -> float | None:
    """entry_price, falling back to trigger_price (mirrors risk engine)."""
    entry = _safe_positive(plan.get("entry_price"))
    if entry is None:
        entry = _safe_positive(plan.get("trigger_price"))
    return entry


def _lifecycle_get(lifecycle: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-key access so both the real ``LifecycleResolution`` and
    the RED test's ``SimpleNamespace`` work."""
    if lifecycle is None:
        return default
    if isinstance(lifecycle, dict):
        return lifecycle.get(key, default)
    return getattr(lifecycle, key, default)


def _risk_reward(plan: dict[str, Any]) -> float | None:
    entry = _plan_entry(plan)
    stop = _safe_positive(plan.get("stop_loss"))
    if entry is None or stop is None or entry == stop:
        return None
    tps = plan.get("take_profits")
    if not isinstance(tps, list) or not tps:
        return None
    rewards: list[float] = []
    for tp in tps:
        if not isinstance(tp, dict):
            continue
        price = _safe_positive(tp.get("price"))
        if price is not None:
            rewards.append(abs(price - entry))
    if not rewards:
        return None
    return round(max(rewards) / abs(entry - stop), 4)


def _valid_tp_list(tps: Any) -> bool:
    if not isinstance(tps, list) or not tps:
        return False
    for tp in tps:
        if not isinstance(tp, dict):
            return False
        price = _safe_positive(tp.get("price"))
        ratio = _num(tp.get("ratio"))
        if price is None or ratio is None or not (0 < ratio <= 1):
            return False
    return True


def _normalize_tps(tps: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(tps, list):
        for tp in tps:
            if isinstance(tp, dict):
                out.append(
                    {
                        "price": _safe_positive(tp.get("price")),
                        "ratio": _num(tp.get("ratio")),
                    }
                )
    return out


# ---------------------------------------------------------------------------
# candidate fingerprint
# ---------------------------------------------------------------------------
def candidate_plan_fingerprint(
    candidate_plan: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
    policy: RiskAssistancePolicy | None = None,
) -> str:
    """Deterministic fingerprint of a candidate plan's immutable identity.

    Covers: symbol, side, entry, trigger, stop, take profits (price+ratio),
    risk_percent, the confirmation's canonical fingerprint, analysis time and
    the policy contract version. Used to (a) let the LLM quote the candidate
    verbatim and (b) detect any identity drift in the merged proposal.
    """
    snapshot = snapshot or {}
    policy = policy or RiskAssistancePolicy()
    confirmation = candidate_plan.get("entry_trigger_confirmation")
    if not isinstance(confirmation, dict):
        confirmation = {}
    # 08-10 (fresh-reviewer P2 rework): a carried-only recheck plan carries NO
    # structured confirmation -- the honest ``_bind_trusted_entry_confirmation``
    # fail-closed semantics leave ``entry_trigger_confirmation`` None on a
    # round whose current snapshot yields no event. ``canonical_confirmation_
    # fingerprint`` is a strict identity check (it raises on an incomplete
    # confirmation, and an EMPTY dict misses every ``_FINGERPRINT_FIELDS`` key
    # -> KeyError), so the candidate fingerprint must NOT forward the empty
    # dict to it. The absent-confirmation component is the deterministic empty
    # marker (never a real 64-hex fingerprint): a candidate WITHOUT a
    # confirmation fingerprints distinctly from any candidate WITH one, and the
    # carried-only round reaches the LLM instead of crashing.
    confirmation_fingerprint = (
        canonical_confirmation_fingerprint(confirmation) if confirmation else ""
    )
    symbol = (
        candidate_plan.get("symbol")
        or confirmation.get("symbol")
        or snapshot.get("symbol")
        or ""
    )
    payload = {
        "symbol": str(symbol or ""),
        "side": str(candidate_plan.get("side") or "").upper(),
        "entry": _safe_positive(candidate_plan.get("entry_price")),
        "trigger": _safe_positive(candidate_plan.get("trigger_price")),
        "stop_loss": _safe_positive(candidate_plan.get("stop_loss")),
        "take_profits": _normalize_tps(candidate_plan.get("take_profits")),
        "risk_percent": _num(candidate_plan.get("risk_percent")),
        "confirmation_fingerprint": confirmation_fingerprint,
        "analysis_time_utc": _num(snapshot.get("analysis_time_utc")),
        "policy_version": int(policy.contract_version),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _news_like_event_policy_allows(plan: dict[str, Any]) -> bool:
    """An explicit ``news_like_event_policy`` may adaptively document how the
    news-like regime is handled. Only a dict with truthy ``allow`` neutralises
    the adaptive news gate — anything else keeps the gate closed."""
    policy = plan.get("news_like_event_policy")
    if not isinstance(policy, dict):
        return False
    return bool(policy.get("allow"))


def _risk_thresholds() -> tuple[float, float]:
    """Read ``trading_mode.risk`` thresholds (min_sl_distance_pct, min_rr) ONCE.

    08-10 fresh-reviewer Recommended-1: every gate previously re-read
    ``load_config()`` (4 YAML files + DSN resolution + validation) at call
    time. Callers that already hold a config object thread the values through
    explicitly; a standalone fallback performs the single shared read here.
    """
    # 08-10 P2-1 (fresh reviewer P2): each threshold read is FAIL-CLOSED via
    # ``cfg_threshold`` — a present-but-invalid value (NaN/bool/0/negative)
    # raises, and ``verify_risk_adjustment``'s caller converts that into a
    # recorded ``风控阈值配置读取失败`` rejection. A fail-open
    # ``float(risk_cfg.get(...))`` would silently DISABLE the gate
    # (``rr < nan`` is always False).
    risk_cfg = (load_config().trading_mode).get("risk", {})
    return (
        cfg_threshold(risk_cfg, "min_sl_distance_pct", 0.8),
        cfg_threshold(risk_cfg, "min_rr", 2.0),
    )


def candidate_adaptive_blockers(
    candidate_plan: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
    min_sl_pct: float | None = None,
    min_rr: float | None = None,
) -> tuple[str, ...]:
    """Deterministic mirror of the ADAPTIVE gates applied to the CANDIDATE plan.

    08-10 fresh-reviewer P2-1: ``round_ctx.blocker_ids`` was the static
    ``policy.hard_gates`` vocabulary (8 codes) instead of the round's ACTUAL
    failing adaptive gates, and the committee acknowledgment check was subset-
    only — so ``acknowledged_blockers: []`` passed even when the candidate
    failed ``minimum_stop_distance`` / ``atr_stop_buffer``. This function
    recomputes the failing adaptive gate set from the same gate math the
    verifier uses (``_gate_stop_distance`` min side, ``_gate_min_rr``,
    ``_gate_regime`` news side) so the producer can populate ``blocker_ids``
    with the real failing set. It only ever emits codes from
    ``ADAPTIVE_GATE_CODES``; a candidate with no failing adaptive gate yields
    an empty tuple. Read-only and deterministic (fixed gate order).

    08-10 fresh-reviewer Recommended-1: ``min_sl_pct`` / ``min_rr`` may be
    threaded explicitly by a caller that already holds a config object (the
    production producer); when either is omitted the single shared
    ``_risk_thresholds()`` read supplies both. No per-gate re-read.
    """
    snapshot = snapshot or {}
    failing: list[str] = []

    if min_sl_pct is None or min_rr is None:
        _cfg_min_sl, _cfg_min_rr = _risk_thresholds()
        if min_sl_pct is None:
            min_sl_pct = _cfg_min_sl
        if min_rr is None:
            min_rr = _cfg_min_rr
    entry = _plan_entry(candidate_plan)
    stop = _safe_positive(candidate_plan.get("stop_loss"))
    if entry is not None and stop is not None:
        dist = abs(entry - stop)
        if dist / entry * 100 < min_sl_pct:
            failing.append("minimum_stop_distance")
        atr = _safe_positive(
            ((snapshot.get("modules") or {}).get("momentum") or {}).get("atr", {}).get("current")
        )
        if atr is not None:
            min_buffer = max(atr * 0.2, entry * min_sl_pct / 100)
            if dist < min_buffer:
                failing.append("atr_stop_buffer")

    rr = _risk_reward(candidate_plan)
    if rr is None or rr < min_rr:
        failing.append("minimum_rr")

    regime = ((snapshot.get("modules") or {}).get("market_regime") or {})
    regime_name = str(regime.get("regime") or "normal")
    if (bool(regime.get("extreme")) or regime_name in EXTREME_REGIMES) and (
        "news" in regime_name.lower()
        and not _news_like_event_policy_allows(candidate_plan)
    ):
        failing.append("news_like_event")

    return tuple(code for code in ADAPTIVE_GATE_CODES if code in failing)


# ---------------------------------------------------------------------------
# the verifier
# ---------------------------------------------------------------------------
def verify_risk_adjustment(
    *,
    candidate_plan: dict[str, Any],
    proposal: dict[str, Any],
    confirmation_lifecycle: Any,
    snapshot: dict[str, Any],
    account_state: dict[str, Any],
    policy: RiskAssistancePolicy,
    decision_confidence: float = 0.0,
) -> AdjustmentVerification:
    """Deterministically verify a bounded LLM adjustment (Step 7). Read-only.

    Returns an immutable ``AdjustmentVerification``. ``adjusted_plan`` is
    ``None`` only on structural rejections (bad verdict, wait/reject, stop
    tightening, non-allowlisted adjustment key); on gate failures the adjusted
    plan is retained so callers can inspect what was proposed, but ``ok`` is
    False and no order is ever authorised.
    """
    errors: list[str] = []
    reason_codes: list[str] = []

    # --- config thresholds: read ONCE, fail closed (Recommended-1) ----------
    # ``min_sl_distance_pct`` / ``min_rr`` are consumed by the stop-distance and
    # min-RR gates below. Reading them a single time here (instead of one
    # ``load_config()`` per gate) keeps the verifier deterministic and makes a
    # config failure a recorded rejection, never a propagated exception.
    try:
        cfg_min_sl_pct, cfg_min_rr = _risk_thresholds()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"风控阈值配置读取失败: {exc}")
        cfg_min_sl_pct, cfg_min_rr = None, None

    # --- structural checks -------------------------------------------------
    if not isinstance(candidate_plan, dict):
        errors.append("candidate_plan 不是对象")
    if not isinstance(proposal, dict):
        errors.append("proposal 不是对象")
    if not isinstance(snapshot, dict):
        errors.append("snapshot 不是对象")
    if errors:
        return AdjustmentVerification(
            ok=False,
            adjusted_plan=None,
            monetary_risk_delta=0.0,
            final_risk_check=_FALSE_OK,
            errors=tuple(errors),
            reason_codes=(),
            effective_order_allowed=False,
        )
    account = account_state if isinstance(account_state, dict) else {}
    if policy is None:
        policy = RiskAssistancePolicy()

    verdict = proposal.get("verdict")
    if verdict not in VALID_VERDICTS:
        errors.append(f"未知 verdict: {verdict!r}")
        return _reject(errors, reason_codes, policy)

    # 08-10 P2-1 (reviewer): candidate identity fingerprint is FAIL-CLOSED.
    # The proposal MUST quote the candidate fingerprint verbatim; omission or
    # mismatch both reject. The verifier recomputes the fingerprint itself, so
    # it never trusts the proposal's identity claim.
    claimed_fp = proposal.get("candidate_fingerprint")
    if not isinstance(claimed_fp, str):
        errors.append("proposal 缺少 candidate_fingerprint（必须逐字引用候选指纹）")
        return _reject(errors, reason_codes, policy)
    expected_fp = candidate_plan_fingerprint(
        candidate_plan, snapshot=snapshot, policy=policy
    )
    if claimed_fp != expected_fp:
        errors.append(
            f"candidate_fingerprint 不匹配: {claimed_fp!r}"
        )
        return _reject(errors, reason_codes, policy)

    # wait / reject -> no-order without plan construction (design §7 step 3)
    if verdict in ("wait", "reject"):
        errors.append(f"verdict={verdict} 不构建计划，禁止开仓")
        return _reject(errors, reason_codes, policy)

    adjustments: dict[str, Any] = {}
    if verdict == "adjust":
        adjustments = proposal.get("adjustments")
        if not isinstance(adjustments, dict) or not adjustments:
            errors.append("adjust 必须携带非空 adjustments 对象")
            return _reject(errors, reason_codes, policy)
        for key in adjustments:
            if key not in ADJUSTABLE_FIELDS:
                errors.append(f"adjustments 含非允许键: {key}")
        if errors:
            return _reject(errors, reason_codes, policy)
    elif proposal.get("adjustments") is not None:
        errors.append(f"{verdict} 禁止携带 adjustments")
        return _reject(errors, reason_codes, policy)

    # --- build the adjusted plan (deep copy, never touch the candidate) ----
    adjusted_plan = copy.deepcopy(candidate_plan)
    for key, value in adjustments.items():
        if key in ("entry_price", "stop_loss", "risk_percent"):
            if _safe_positive(value) is None:
                errors.append(f"adjustments.{key} 必须为有限正数")
        elif key == "take_profits":
            if not _valid_tp_list(value):
                errors.append("adjustments.take_profits 必须为非空 [{price, ratio}] 列表")
        adjusted_plan[key] = copy.deepcopy(value)
    if errors:
        return _reject(errors, reason_codes, policy)

    cand_entry = _plan_entry(candidate_plan)
    cand_stop = _safe_positive(candidate_plan.get("stop_loss"))
    cand_dist = abs(cand_entry - cand_stop) if cand_entry is not None and cand_stop is not None else None
    adj_entry = _plan_entry(adjusted_plan)
    adj_stop = _safe_positive(adjusted_plan.get("stop_loss"))
    adj_dist = abs(adj_entry - adj_stop) if adj_entry is not None and adj_stop is not None else None

    # stop may never tighten (structural rejection -> discard the plan)
    if cand_dist is not None and adj_dist is not None and adj_dist < cand_dist:
        errors.append(
            f"止损收紧不允许（调整后距离 {adj_dist:.4f} < 候选距离 {cand_dist:.4f}）"
        )
        return _reject(errors, reason_codes, policy)

    # --- monetary risk never increases (design §7 step 8) ------------------
    cand_risk = _safe_positive(candidate_plan.get("risk_percent"))
    scaled_risk = cand_risk
    if (
        cand_risk is not None
        and cand_dist is not None
        and adj_dist is not None
        and adj_dist > cand_dist
    ):
        scaled_risk = cand_risk * cand_dist / adj_dist
        reason_codes.append("minimum_stop_distance")

    caps: list[float] = [cand_risk] if cand_risk is not None else []
    if scaled_risk is not None:
        caps.append(scaled_risk)
    if "risk_percent" in adjustments:
        proposed = _safe_positive(adjustments["risk_percent"])
        if proposed is not None:
            caps.append(proposed)

    # 08-10 P2-3 (reviewer finding): account risk caps are FAIL-CLOSED.
    # prd.md P1-3: effective risk may never exceed the configured per-trade
    # cap nor the remaining total-account budget (max_total_risk_pct minus the
    # already-committed open_position_risk_pct). planned_risk is the intended
    # risk BEFORE account caps (min over candidate / stop-scaling / explicit
    # adjustment); if it exceeds a cap the proposal is REJECTED with the cap
    # cited — an unsafe LLM proposal is never silently clamped into an order.
    # The min-clamp below stays as a safety net for the monetary-risk-never-
    # increases invariant.
    planned_risk = min(caps) if caps else cand_risk
    cap_single = _safe_positive(account.get("max_single_trade_risk_pct"))
    cap_total = _safe_positive(account.get("max_total_risk_pct"))
    # 08-10 P2-2 (fresh reviewer P2): a PRESENT-but-invalid cap (bool, NaN/Inf,
    # non-number, <=0) is a REJECTION, never a silent drop to "no cap". Pre-fix
    # ``_safe_positive`` returned None for such values, which skipped the gates
    # below and let a "0 cap" silently become "no cap" (fail-open). Absent keys
    # stay safe: upstream account_risk_guard DEFAULTS (2.0/10.0) fill the gap.
    for _cap_key in ("max_single_trade_risk_pct", "max_total_risk_pct"):
        if _cap_key in account and _safe_positive(account.get(_cap_key)) is None:
            errors.append(
                f"账户风险上限 {_cap_key} 配置无效 "
                f"({account.get(_cap_key)!r})，必须为有限正数，禁止开仓"
            )
    open_risk = _num(account.get("open_position_risk_pct"))
    if (
        planned_risk is not None
        and cap_single is not None
        and planned_risk > cap_single
    ):
        errors.append(
            f"有效风险 {planned_risk:.4f}% 超过单笔上限 max_single_trade_risk_pct "
            f"({cap_single:.4f}%)，禁止开仓"
        )
    remaining_budget: float | None = None
    if cap_total is not None and open_risk is not None:
        remaining_budget = cap_total - open_risk
        if planned_risk is not None and planned_risk > remaining_budget:
            errors.append(
                f"有效风险 {planned_risk:.4f}% 超过账户总风险剩余预算 "
                f"(max_total_risk_pct {cap_total:.4f}% - 已占用 "
                f"open_position_risk_pct {open_risk:.4f}% = "
                f"{remaining_budget:.4f}%)，禁止开仓"
            )
    if errors:
        return _reject(errors, reason_codes, policy)
    if cap_single is not None:
        caps.append(cap_single)
    if remaining_budget is not None:
        caps.append(remaining_budget)
    final_risk = min(caps) if caps else cand_risk
    if final_risk is not None:
        adjusted_plan["risk_percent"] = final_risk

    equity = _safe_positive(account.get("equity"))
    if cand_risk is not None and final_risk is not None and equity is not None:
        orig_monetary = cand_risk / 100 * equity
        final_monetary = final_risk / 100 * equity
        monetary_risk_delta = final_monetary - orig_monetary
    else:
        monetary_risk_delta = 0.0

    # --- hard gates (independently enforced, each appends an error) --------
    _gate_confirmation_lifecycle(confirmation_lifecycle, errors)
    _gate_market_data(snapshot, errors)
    _gate_regime(snapshot, adjusted_plan, errors, reason_codes)
    _gate_account(account, errors)

    # --- geometry / bounded deviations on the ADJUSTED plan ----------------
    _gate_geometry(adjusted_plan, errors)
    _gate_entry_deviation(cand_entry, adj_entry, snapshot, policy, errors)
    _gate_stop_distance(adj_entry, adj_stop, snapshot, policy, errors,
                        min_sl_pct=cfg_min_sl_pct)
    _gate_take_profits(adjusted_plan, adj_entry, errors)
    _gate_min_rr(adjusted_plan, errors, min_rr=cfg_min_rr)

    # --- full existing risk engine re-run (design §7 step 11) --------------
    final_risk_check = _rerun_engine(
        adjusted_plan, snapshot, decision_confidence, errors
    )

    return AdjustmentVerification(
        ok=not errors,
        adjusted_plan=adjusted_plan,
        monetary_risk_delta=monetary_risk_delta,
        final_risk_check=final_risk_check,
        errors=tuple(errors),
        reason_codes=tuple(reason_codes),
        effective_order_allowed=(policy.mode == "paper_bounded") and not errors,
    )


def _reject(
    errors: list[str], reason_codes: list[str], policy: RiskAssistancePolicy
) -> AdjustmentVerification:
    return AdjustmentVerification(
        ok=False,
        adjusted_plan=None,
        monetary_risk_delta=0.0,
        final_risk_check={"ok": False, "reasons": list(errors), "metrics": {}},
        errors=tuple(errors),
        reason_codes=tuple(reason_codes),
        effective_order_allowed=False,
    )


def _gate_confirmation_lifecycle(lifecycle: Any, errors: list[str]) -> None:
    status = _lifecycle_get(lifecycle, "status", None)
    if status != "valid":
        errors.append(f"入场确认生命周期状态为 {status!r}，禁止开仓")
        return
    age = _lifecycle_get(lifecycle, "age_bars", 0)
    ttl = _lifecycle_get(lifecycle, "ttl_bars", 0)
    if isinstance(age, int) and isinstance(ttl, int) and ttl > 0 and age > ttl:
        errors.append(
            f"入场确认已超 TTL（age_bars={age} > ttl_bars={ttl}），禁止开仓"
        )


def _gate_market_data(snapshot: dict[str, Any], errors: list[str]) -> None:
    data_quality = snapshot.get("data_quality") or {}
    dq_status = str(data_quality.get("status") or "")
    if dq_status and dq_status != "complete":
        errors.append(
            f"market_data_not_ready: data_quality.status={dq_status}，禁止开仓"
        )


def _gate_regime(
    snapshot: dict[str, Any],
    adjusted_plan: dict[str, Any],
    errors: list[str],
    reason_codes: list[str],
) -> None:
    regime = ((snapshot.get("modules") or {}).get("market_regime") or {})
    regime_name = str(regime.get("regime") or "normal")
    is_extreme = bool(regime.get("extreme")) or regime_name in EXTREME_REGIMES
    if not is_extreme:
        return
    if "news" in regime_name.lower() and _news_like_event_policy_allows(adjusted_plan):
        reason_codes.append("news_like_event")
        return
    errors.append(f"当前市场状态为 {regime_name}，禁止开仓")


def _gate_account(account: dict[str, Any], errors: list[str]) -> None:
    if account.get("enabled") is not True:
        errors.append("账户未启用（enabled != True），禁止开仓")
    if account.get("paused") is True:
        errors.append("账户已暂停（paused），禁止开仓")
    dd = _num(account.get("drawdown_pct"))
    if dd is not None and dd <= _DRAWDOWN_HARD_RISK_OFF_THRESHOLD:
        errors.append(
            f"账户回撤 {dd}% 触发硬性风控上限（{_DRAWDOWN_HARD_RISK_OFF_THRESHOLD}%），禁止开仓"
        )
    open_orders = _num(account.get("open_orders"))
    max_orders = _num(account.get("max_orders"))
    if open_orders is not None and max_orders is not None and max_orders > 0:
        if open_orders >= max_orders:
            errors.append(
                f"账户挂单/持仓数达到上限（{open_orders}/{max_orders}），禁止开仓"
            )


def _gate_geometry(adjusted_plan: dict[str, Any], errors: list[str]) -> None:
    side = str(adjusted_plan.get("side") or "").upper()
    entry = _plan_entry(adjusted_plan)
    stop = _safe_positive(adjusted_plan.get("stop_loss"))
    if side not in ("LONG", "SHORT"):
        errors.append("trade_plan 缺少 LONG/SHORT 方向")
        return
    if entry is None or stop is None:
        errors.append("计划 entry/stop 缺失或非法")
        return
    if side == "SHORT" and not (stop > entry):
        errors.append("SHORT 止损必须高于入场价")
    if side == "LONG" and not (stop < entry):
        errors.append("LONG 止损必须低于入场价")


def _gate_entry_deviation(
    cand_entry: float | None,
    adj_entry: float | None,
    snapshot: dict[str, Any],
    policy: RiskAssistancePolicy,
    errors: list[str],
) -> None:
    if cand_entry is None or adj_entry is None:
        return
    dev_abs = abs(adj_entry - cand_entry)
    dev_pct = dev_abs / cand_entry * 100
    max_pct = float(policy.max_entry_deviation_pct)
    atr = _safe_positive(
        ((snapshot.get("modules") or {}).get("momentum") or {}).get("atr", {}).get("current")
    )
    max_atr = float(policy.max_entry_deviation_atr) * atr if atr is not None else None
    pct_ok = dev_pct <= max_pct
    atr_ok = max_atr is None or dev_abs <= max_atr
    if not (pct_ok and atr_ok):
        atr_part = (
            f"，ATR 偏差 {dev_abs:.4f}（上限 {max_atr:.4f}）"
            if max_atr is not None
            else ""
        )
        errors.append(
            f"entry 偏离候选范围：偏差 {dev_pct:.2f}%（上限 {max_pct}%）{atr_part}"
        )


def _gate_stop_distance(
    entry: float | None,
    stop: float | None,
    snapshot: dict[str, Any],
    policy: RiskAssistancePolicy,
    errors: list[str],
    *,
    min_sl_pct: float | None = None,
) -> None:
    if entry is None or stop is None:
        return
    if min_sl_pct is None:
        # The verifier threads this value from its single config read; a None
        # here means that read failed (already recorded) -> fail closed.
        errors.append("止损距离最小阈值不可用（配置读取失败），禁止开仓")
        return
    dist = abs(entry - stop)
    dist_pct = dist / entry * 100
    if dist_pct < min_sl_pct:
        errors.append(f"止损距离 {dist_pct:.3f}% 低于最小要求 {min_sl_pct}%，交易空间不足")
    atr = _safe_positive(
        ((snapshot.get("modules") or {}).get("momentum") or {}).get("atr", {}).get("current")
    )
    if atr is not None:
        min_buffer = max(atr * 0.2, entry * min_sl_pct / 100)
        if dist < min_buffer:
            errors.append(
                f"止损距离 {dist:.4f} 不足 ATR 缓冲 {min_buffer:.4f}（0.2×ATR={atr * 0.2:.4f}），易被噪音打掉"
            )
    max_pct = float(policy.max_stop_distance_pct)
    if dist_pct > max_pct:
        errors.append(f"止损距离 {dist_pct:.3f}% 超过最大允许 {max_pct}%")
    if atr is not None and dist > float(policy.max_stop_distance_atr) * atr:
        errors.append(
            f"止损距离 {dist:.4f} 超过 ATR 上限 {float(policy.max_stop_distance_atr) * atr:.4f}"
            f"（{policy.max_stop_distance_atr}×ATR）"
        )


def _gate_take_profits(
    adjusted_plan: dict[str, Any], entry: float | None, errors: list[str]
) -> None:
    tps = adjusted_plan.get("take_profits")
    if not isinstance(tps, list) or not tps:
        errors.append("trade_plan 缺少 take_profits")
        return
    side = str(adjusted_plan.get("side") or "").upper()
    total_ratio = 0.0
    shape_ok = True
    for tp in tps:
        if not isinstance(tp, dict):
            shape_ok = False
            break
        price = _safe_positive(tp.get("price"))
        ratio = _num(tp.get("ratio"))
        if price is None or ratio is None or not (0 < ratio <= 1):
            shape_ok = False
            break
        if entry is not None:
            if side == "SHORT" and not (price < entry):
                shape_ok = False
                break
            if side == "LONG" and not (price > entry):
                shape_ok = False
                break
        total_ratio += ratio
    if not shape_ok:
        errors.append("take_profits 几何/数值非法（价格须有限正数、ratio 须 ∈ (0,1]）")
        return
    if not math.isclose(total_ratio, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        errors.append(f"take_profits ratio 总和 {total_ratio:.4f} 必须 ≈ 1.0")


def _gate_min_rr(
    adjusted_plan: dict[str, Any],
    errors: list[str],
    *,
    min_rr: float | None = None,
) -> None:
    if min_rr is None:
        errors.append("最小 RR 阈值不可用（配置读取失败），禁止开仓")
        return
    rr = _risk_reward(adjusted_plan)
    if rr is None or rr < min_rr:
        errors.append(f"RR {rr if rr is not None else '-'} 低于 {min_rr}")


def _rerun_engine(
    adjusted_plan: dict[str, Any],
    snapshot: dict[str, Any],
    decision_confidence: float,
    errors: list[str],
) -> dict[str, Any]:
    analysis_time = snapshot.get("analysis_time_utc")
    if not isinstance(analysis_time, int) or isinstance(analysis_time, bool) or analysis_time <= 0:
        result = {"ok": False, "reasons": ["snapshot.analysis_time_utc 缺失或非严格正整数"], "metrics": {}}
        errors.extend(result["reasons"])
        return result
    decision = {
        "has_trade_plan": True,
        "trade_plan": adjusted_plan,
        "analysis_time_utc": analysis_time,
        "confidence": decision_confidence if decision_confidence is not None else 0.0,
    }
    try:
        result = validate_trade_plan(decision, snapshot)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "reasons": [f"风险引擎重跑异常: {exc}"], "metrics": {}}
    if not result.get("ok"):
        errors.extend(result.get("reasons") or [])
    return result
