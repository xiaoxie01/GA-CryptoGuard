from __future__ import annotations

import json
import logging
from typing import Any

from plugins.crypto_guard.config.loader import cfg_threshold, load_config
from plugins.crypto_guard.analysis.market_regime_engine import EXTREME_REGIMES, score_market_regime
from plugins.crypto_guard.reasoning.watch_conditions import normalize_opportunity_watch
from plugins.crypto_guard.strategy.grade_config import PUSH_GRADES, WATCH_GRADES, STORE_ONLY_GRADES, is_paper_order_eligible
from plugins.crypto_guard.utils import _strict_positive_int_ms


logger = logging.getLogger(__name__)


def apply_risk_to_decision(decision: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    result = dict(decision)
    risk = validate_trade_plan(result, snapshot)
    result["risk_check"] = risk
    result["manual_bypass_allowed"] = False

    # BTC#9 fix: LLM failed/disabled fallback 不得直接创建模拟盘订单
    cfg = load_config().trading_mode
    risk_cfg = cfg.get("risk", {})
    block_fallback = bool(risk_cfg.get("fallback_llm_failed_blocks_paper_order", True))
    llm_status = str(result.get("llm_status") or "").lower()
    fallback_blocked = block_fallback and llm_status in {"failed", "disabled"}
    if fallback_blocked and result.get("has_trade_plan") and result.get("trade_plan"):
        # Phase E (07-05): Plan lifecycle separation. Preserve the
        # deterministic candidate plan as ``candidate_trade_plan`` BEFORE
        # clearing has_trade_plan so audit can see what the deterministic
        # path produced. Set structured plan_status / plan_blockers so
        # downstream consumers (report, diagnostics) can distinguish
        # "withheld due to LLM failure" from "no plan ever generated".
        candidate_plan = result.get("trade_plan")
        if candidate_plan and isinstance(candidate_plan, dict):
            result["candidate_trade_plan"] = candidate_plan
        result["has_trade_plan"] = False
        result["trade_plan"] = None
        result["decision"] = "monitor_only"
        result["plan_status"] = "withheld"
        result["plan_source"] = "deterministic_sop"
        result["plan_blockers"] = [
            {
                "code": "llm_parse_failed" if llm_status == "failed" else "llm_disabled",
                "stage": "synthesis",
                "detail": (
                    f"llm_status={llm_status}, "
                    f"fallback_llm_failed_blocks_paper_order=true"
                ),
            }
        ]
        # BTC#9 P2-2: audit fields for fallback downgrade
        result["fallback_trade_plan_blocked"] = True
        result["fallback_block_reason"] = f"llm_status={llm_status}, fallback_llm_failed_blocks_paper_order=true"
        result["original_decision"] = "trade_plan_available"
        result["downgraded_decision"] = "monitor_only"
        notes = list(result.get("risk_notes") or [])
        notes.append(f"LLM 状态为 {llm_status}，降级为 opportunity_watch，不创建模拟盘订单")
        result["risk_notes"] = notes
        # 让 risk_check 也反映降级 — 引用真实阻断阶段（LLM failure），
        # 不再 collapse 到 "缺少完整 trade_plan"
        risk = dict(risk)
        risk["ok"] = False
        risk["reasons"] = list(risk.get("reasons") or []) + [
            f"llm_status={llm_status} 降级，禁止开仓（候选计划已保留为 candidate_trade_plan）",
        ]
        result["risk_check"] = risk

    # Phase-2 P2-1 (07-27) requirement C: fail-closed the LLM FAILED direction
    # leak (symptom #2). When the LLM is enabled and a call FAILS, the
    # deterministic fallback's bullish/bearish ``market_bias`` is a LEAK — the
    # decision was supposed to be LLM-confirmed and was not. The fallback-
    # blocked block above only fires when has_trade_plan + trade_plan (so a
    # failed decision WITHOUT a plan bypasses it entirely) and even when it
    # fires it NEVER touches market_bias / signal_grade. The final
    # ``normalize_market_semantics`` gate only forces unknown via the
    # ``data_incomplete`` path — when the data is healthy the bullish/bearish
    # bias survives onto the persisted failed row, and
    # ``deterministic_direction_from_failed_llm`` re-fires hourly.
    #
    # Scope decision (07-27 final review): this block fires ONLY for
    # ``llm_status == "failed"`` — the LLM-was-enabled-but-the-call-failed
    # leak. It does NOT fire for ``llm_status == "disabled"``
    # (``use_llm=False`` / ``CRYPTO_GUARD_LLM_ANALYSIS=0``): the deterministic-
    # only operating mode is a legitimate product in which the deterministic
    # direction IS the intended output (07-03 semantic-accuracy tests
    # ``test_doge_countertrend_rebound_not_bullish_middle`` /
    # ``test_sol_short_bullish_but_explains_htf_mixed`` pin that HTF-aware
    # bias must survive on disabled rows). The breaker-skip path
    # (llm_agent_judge.py:164) and the preset-None / retry-None paths all set
    # ``llm_status="failed"``, so they ARE covered here. Production runs with
    # the LLM enabled, so the leaked rows in production are ``failed``; there
    # are no ``disabled`` production rows to fail-close.
    #
    # Requirement C + P1-1 (07-27 final review) verbatim:
    # - This block fail-closes ONLY when ``llm_status == "failed"`` (not
    #   ``disabled``). ``disabled`` is intentional deterministic-only product
    #   (CRYPTO_GUARD_LLM_ANALYSIS=0) and MUST keep HTF-aware bias.
    # - Force market_bias="unknown", no executable trade_plan, effective
    #   grade ≤ B, plan_execution_state=unconfirmed on the failed path BEFORE
    #   persistence; keep deterministic direction in candidate_trade_plan.
    # - Do NOT loosen or delete the
    #   ``deterministic_direction_from_failed_llm`` diagnostic. That
    #   diagnostic is ALSO scoped to ``llm_status == "failed"`` only
    #   (P1-1); it does NOT fire on ``disabled`` rows (current or
    #   historical). After this block, newly persisted ``failed`` rows
    #   carry market_bias=unknown so the diagnostic finds zero *new*
    #   failed+bullish/bearish leaks; pre-fix historical failed rows are
    #   classified by created_at vs marker (P1-2/P1-3).
    # P2-DOC-1: the previous comment wrongly claimed the diagnostic "may
    # still fire on disabled rows" / "stays as-is" in a way that invited
    # re-widening to {failed, disabled}. That wording is retired.
    #
    # P2-R1 (07-27 final review): direction fail-closed is INDEPENDENT of
    # ``fallback_llm_failed_blocks_paper_order`` (``block_fallback`` /
    # ``fallback_blocked``). That flag only governs the BTC#9 paper-order
    # withhold block above. Coupling C to it reopened the bias leak whenever
    # an operator set the paper-order flag false. Drive C from
    # ``llm_status == "failed"`` alone.
    if llm_status == "failed":
        result["market_bias"] = "unknown"
        result["trend_stage"] = "unknown"
        # Effective grade ≤ B (order_value ≤ 2). Preserve raw_signal_grade /
        # raw_score (set by _normalize_llm_decision on the success path; on
        # the failed/disabled path they are not set, so do not invent them).
        _grade = str(result.get("signal_grade") or "D").upper()
        try:
            from plugins.crypto_guard.strategy.grade_config import (
                grade_order_value, grade_from_order_value,
            )
            if grade_order_value(_grade) > grade_order_value("B"):
                result["signal_grade"] = "B"
        except Exception:
            # Fall back to a literal cap if the grade helper is unavailable.
            if _grade in {"S", "A"}:
                result["signal_grade"] = "B"
        # P1-1 (07-27 final review residual under P2-R1): preserve the
        # deterministic plan under ``candidate_trade_plan`` BEFORE clearing
        # the executable fields. When ``fallback_llm_failed_blocks_paper_order``
        # is false the paper-order withhold block above does not run, so this
        # C block is the only place that can keep the candidate for audit /
        # report ("候选计划详情"). Requirement C: keep deterministic direction
        # in candidate_trade_plan on the failed path.
        _plan_to_preserve = result.get("trade_plan")
        if (
            isinstance(_plan_to_preserve, dict)
            and _plan_to_preserve
            and not (
                isinstance(result.get("candidate_trade_plan"), dict)
                and result.get("candidate_trade_plan")
            )
        ):
            result["candidate_trade_plan"] = dict(_plan_to_preserve)
        # No executable trade_plan (the fallback-blocked block already cleared
        # it when has_trade_plan; this is idempotent for the no-plan path).
        result["has_trade_plan"] = False
        result["trade_plan"] = None
        result["decision"] = "monitor_only"
        # plan_execution_state: unconfirmed for the LLM-failed outcome
        # (matches the failed terminal paths' envelope). Do NOT override a
        # more-specific state already set upstream (e.g.
        # continuity_invalidated) — only normalize the generic confirmed /
        # risk_rejected states that contradict a failed outcome.
        _cur_state = str(result.get("plan_execution_state") or "").lower()
        if _cur_state in {"confirmed", "risk_rejected", ""}:
            result["plan_execution_state"] = "unconfirmed"
        # Force risk_check to fail so downstream consumers (report, paper
        # order gate) see the failed/disabled outcome as non-executable.
        risk = dict(result.get("risk_check") or risk)
        risk["ok"] = False
        _existing_reasons = list(risk.get("reasons") or [])
        _fail_reason = (
            f"llm_status={llm_status} fail-closed: market_bias=unknown, "
            f"grade ≤ B, no executable plan (deterministic direction "
            f"preserved under candidate_trade_plan)"
        )
        if _fail_reason not in _existing_reasons:
            risk["reasons"] = _existing_reasons + [_fail_reason]
        result["risk_check"] = risk

    if result.get("has_trade_plan") and result.get("trade_plan") and not risk["ok"]:
        # Phase E (07-05): risk gate rejected the executable plan. Preserve
        # the candidate as candidate_trade_plan for audit and set structured
        # plan_status / plan_blockers so the report can surface the actual
        # blocking stage (RR/confidence/HTF/etc.) instead of collapsing to
        # "缺交易计划".
        rejected_plan = result.get("trade_plan")
        if rejected_plan and isinstance(rejected_plan, dict) and not result.get("candidate_trade_plan"):
            result["candidate_trade_plan"] = rejected_plan
        result["has_trade_plan"] = False
        result["trade_plan"] = None
        result["decision"] = "monitor_only"
        result["plan_status"] = "risk_rejected"
        result["plan_source"] = "deterministic_sop"
        result["plan_blockers"] = [
            {
                "code": "risk_rejected",
                "stage": "risk_gate",
                "detail": "；".join(risk["reasons"][:6]) if risk.get("reasons") else "risk gate rejected",
            }
        ]
        notes = list(result.get("risk_notes") or [])
        notes.append("模拟盘风控未通过：" + "；".join(risk["reasons"]))
        result["risk_notes"] = notes

    result["suggested_actions"] = suggested_actions(result, risk)
    # 二次保险：即使 suggested_actions 误含 create_paper_order 也过滤掉
    if fallback_blocked and "create_paper_order" in result["suggested_actions"]:
        result["suggested_actions"] = [a for a in result["suggested_actions"] if a != "create_paper_order"]
    if "create_opportunity_watch" in result["suggested_actions"] and not result.get("opportunity_watch"):
        # P0-3 (08-02): the watch is now built deterministically from the
        # decision's plan via the shared normalizer. On fail-closed (None),
        # drop create_opportunity_watch so the manual button never fires on
        # a watch-less decision.
        watch = default_watch_from_decision(result, risk)
        if watch is None:
            result["suggested_actions"] = [
                a for a in result["suggested_actions"] if a != "create_opportunity_watch"
            ]
            if not result["suggested_actions"]:
                result["suggested_actions"] = ["add_to_watchlist", "ignore"]
            notes = list(result.get("risk_notes") or [])
            notes.append("机会监控条件无法结构化（无可用交易计划），fail-closed：不自动创建机会监控。")
            result["risk_notes"] = notes
        else:
            result["opportunity_watch"] = watch
    # R1-1 / R2-5 (07-03 final review): final idempotent semantic gate. Re-run
    # normalize_market_semantics so any bias+stage/HTF/closed drift induced
    # by risk adjustments is caught and the structured fields stay
    # consistent with the final decision. This is idempotent: a second call
    # does not continue to downgrade or duplicate reason codes because the
    # function checks the already-normalized state and only corrects
    # contradictions.
    #
    # R2-5: if normalize raises, the final safety gate has failed. The
    # prior risk-validated state may carry stale semantics that bypass
    # the bias+stage / HTF / fail-closed contract. Default to fail-closed:
    # strip trade plan, downgrade decision to monitor_only, and surface
    # the failure for audit. Never let the prior executable state stand
    # when the final gate cannot verify it.
    try:
        from plugins.crypto_guard.reasoning.market_semantics import normalize_market_semantics
        ms_cfg = (cfg.get("market_semantics") or {}) if isinstance(cfg, dict) else {}
        # R1-1 (07-03 final review): assign return value — normalize creates a
        # shallow copy and mutations on the copy are discarded without assignment.
        result = normalize_market_semantics(result, snapshot, ms_cfg)
    except Exception as exc:
        logger.warning(
            "normalize_market_semantics failed in risk final gate: %s",
            exc, exc_info=True,
        )
        result["normalize_gate_warning"] = str(exc)
        # R2-5: fail-closed — strip executable state. The final gate is the
        # last line of defense; if it cannot verify the decision's
        # semantics, the decision must not create a paper order.
        result["has_trade_plan"] = False
        result["trade_plan"] = None
        if result.get("decision") in {
            "trade_plan_available", "create_paper_order",
            "wait_for_pullback", "wait_for_breakout", "wait_for_reclaim",
        }:
            result["decision"] = "monitor_only"
        actions = result.get("suggested_actions") or []
        result["suggested_actions"] = [
            a for a in actions
            if a not in {"create_paper_order", "create_opportunity_watch"}
        ]
        if not result["suggested_actions"]:
            result["suggested_actions"] = ["add_to_watchlist", "ignore"]
        # Force risk_check to fail so downstream consumers see the gate fired.
        risk = dict(result.get("risk_check") or {"ok": False, "reasons": []})
        risk["ok"] = False
        risk["reasons"] = list(risk.get("reasons") or []) + [
            f"final semantic gate failed: {exc}",
        ]
        result["risk_check"] = risk
    return result


def validate_trade_plan(decision: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_config().trading_mode
    risk_cfg = cfg.get("risk", {})
    # 08-10 P2-1 (fresh reviewer P2): every threshold read is FAIL-CLOSED via
    # ``cfg_threshold`` — a present-but-invalid value (NaN/bool/0/negative)
    # raises (plan fails closed), never silently disables the gate
    # (``rr < nan`` is always False). Reads are hoisted once so the whole
    # function shares one deterministic threshold set.
    min_rr = cfg_threshold(risk_cfg, "min_rr", 2.0)
    min_conf = cfg_threshold(
        risk_cfg,
        "min_confidence",
        cfg_threshold(risk_cfg, "min_confidence_for_paper_order", 0.72),
    )
    min_sl_pct = cfg_threshold(risk_cfg, "min_sl_distance_pct", 0.8)
    min_tp_pct = cfg_threshold(risk_cfg, "min_tp_distance_pct", 1.0)
    rsi_ob_threshold = cfg_threshold(risk_cfg, "rsi_overbought_threshold", 75)
    rsi_os_threshold = cfg_threshold(risk_cfg, "rsi_oversold_threshold", 25)
    snapshot = snapshot or {}
    plan = decision.get("trade_plan") if decision.get("has_trade_plan") else None
    reasons: list[str] = []
    metrics: dict[str, Any] = {"min_rr": min_rr, "min_confidence": min_conf}

    if not isinstance(plan, dict):
        return {"ok": False, "reasons": ["缺少完整 trade_plan"], "metrics": metrics}

    required = ["side", "entry_type", "stop_loss", "take_profits"]
    missing = [key for key in required if plan.get(key) in (None, "", [])]
    entry = _entry_price(plan)
    if entry is None:
        missing.append("entry_price_or_trigger_price")
    if missing:
        reasons.append("trade_plan 字段不完整：" + ",".join(missing))

    # Extract side early — needed by entry_trigger_confirmation validation
    side = str(plan.get("side") or "").upper()

    # P0-D: Check entry_trigger_confirmation quality
    # BTC#9 fix: 结构化 closed_candle_confirmation 必须通过 _validate_entry_confirmation
    # 裸字符串确认（"x", "manual_close_above_60300", "5m 突破确认"）一律拒绝
    entry_confirmation = plan.get("entry_trigger_confirmation")
    require_ec = bool(risk_cfg.get("require_entry_confirmation_for_paper_order", True))
    # R11-2: snapshot.analysis_time_utc 必须是严格正整数（int，非 bool/float/字符串）
    snap_analysis_time = _strict_positive_int_ms((snapshot or {}).get("analysis_time_utc"))
    if snap_analysis_time is None:
        return {
            "ok": False,
            "reasons": ["snapshot.analysis_time_utc 缺失或非严格正整数，禁止开仓（无法校验未来函数）"],
            "metrics": metrics,
        }

    # R11-4: decision.analysis_time_utc 必须存在且为严格正整数，并与 snapshot 完全一致
    decision_time = _strict_positive_int_ms(decision.get("analysis_time_utc"))
    if decision_time is None:
        return {
            "ok": False,
            "reasons": ["decision.analysis_time_utc 缺失或非严格正整数，禁止开仓"],
            "metrics": metrics,
        }
    if decision_time != snap_analysis_time:
        return {
            "ok": False,
            "reasons": [f"analysis_time_mismatch: decision={decision_time} vs snapshot={snap_analysis_time}"],
            "metrics": metrics,
        }

    analysis_time = snap_analysis_time

    # R4: Second fail-closed gate — market data readiness.
    # If snapshot.data_quality.status != "complete", refuse to validate.
    data_quality = (snapshot or {}).get("data_quality") or {}
    dq_status = str(data_quality.get("status") or "")
    if dq_status and dq_status != "complete":
        reasons.append("market_data_not_ready: data_quality.status=" + dq_status)
        return {"ok": False, "reasons": reasons, "metrics": metrics}

    if entry_confirmation is None:
        metrics["has_entry_confirmation"] = False
        if require_ec:
            reasons.append("缺少入场确认（entry_trigger_confirmation 为空），禁止直接开仓")
    elif isinstance(entry_confirmation, str):
        metrics["has_entry_confirmation"] = False
        reasons.append(
            "裸字符串确认已废弃，需结构化 closed_candle_confirmation，"
            f"收到: {entry_confirmation!r}"
        )
    elif isinstance(entry_confirmation, dict):
        valid_ec, ec_reason = _validate_entry_confirmation(
            entry_confirmation, side, analysis_time,
            snapshot=snapshot,
        )
        metrics["has_entry_confirmation"] = valid_ec
        metrics["entry_confirmation_validation"] = ec_reason
        if not valid_ec:
            reasons.append(f"entry_trigger_confirmation 无效: {ec_reason}")
    else:
        metrics["has_entry_confirmation"] = False
        if require_ec:
            reasons.append(f"entry_trigger_confirmation 类型无效: {type(entry_confirmation).__name__}")

    rr = _risk_reward(plan)
    metrics["rr"] = rr
    if rr is None or rr < min_rr:
        reasons.append(f"RR {rr if rr is not None else '-'} 低于 {min_rr}")

    confidence = float(decision.get("confidence") or 0)
    metrics["confidence"] = confidence
    if confidence < min_conf:
        reasons.append(f"置信度 {confidence:.2f} 低于 {min_conf:.2f}")

    htf = _htf_support(side, snapshot)
    metrics["htf_support"] = htf
    if not htf["ok"]:
        # BTC#9 P2-1: weak structure 在有有效 entry_confirmation 时可豁免
        # 豁免必须: (1) reason 包含"结构偏弱" (2) entry_confirmation 是通过 _validate_entry_confirmation 的有效对象
        if "结构偏弱" in str(htf.get("reason") or ""):
            entry_confirmation = plan.get("entry_trigger_confirmation")
            if isinstance(entry_confirmation, dict):
                valid_ec, _ = _validate_entry_confirmation(
                    entry_confirmation, side, analysis_time,
                    snapshot=snapshot,
                )
                if valid_ec:
                    metrics["htf_support"] = dict(htf, ok=True, reason="")
                    metrics["weak_structure_confirmation_exemption"] = True
                else:
                    reasons.append(htf["reason"])
            else:
                reasons.append(htf["reason"])
        else:
            reasons.append(htf["reason"])

    alignment = _structure_momentum_alignment(side, snapshot)
    metrics["structure_momentum_alignment"] = alignment
    if risk_cfg.get("require_structure_momentum_alignment", True) and not alignment["ok"]:
        reasons.append(alignment["reason"])

    regime = ((snapshot.get("modules") or {}).get("market_regime") or {})
    regime_name = str(regime.get("regime") or "normal")
    metrics["market_regime"] = regime_name
    if regime_name in EXTREME_REGIMES or regime.get("extreme"):
        reasons.append(f"当前市场状态为 {regime_name}，禁止直接创建模拟盘订单")

    # TP/SL distance and precision validation
    entry = _entry_price(plan)
    if entry and entry > 0:
        stop = _safe_float(plan.get("stop_loss"))
        if stop:
            sl_distance = abs(entry - stop)
            sl_pct = sl_distance / entry * 100
            if sl_pct < min_sl_pct:
                reasons.append(f"止损距离 {sl_pct:.3f}% 低于最小要求 {min_sl_pct}%，交易空间不足")

        # BTC#9 fix: invalid_condition 与 stop_loss 必须保持缓冲 + 正确顺序
        invalid_cond = plan.get("invalid_condition")
        invalid_cond_price = _parse_invalid_condition_price(invalid_cond)
        if stop and invalid_cond_price is not None:
            dist_pct = abs(invalid_cond_price - stop) / stop * 100 if stop != 0 else 0.0
            min_dist_pct = 0.1  # minimum 0.1% buffer
            if dist_pct < min_dist_pct:
                reasons.append(
                    f"invalid_condition 价 {invalid_cond_price:.4f} 与 stop_loss {stop:.4f} "
                    f"距离 {dist_pct:.4f}% 低于最小缓冲 {min_dist_pct}%，缺少失效缓冲层"
                )
            # BTC#9 fix: 验证严格的顺序 (strict <, not <=)
            if side == "LONG":
                if not (stop < invalid_cond_price < entry):
                    reasons.append(
                        "invalid_condition 不在 stop_loss 和 entry 之间 (LONG, strict): "
                        f"stop={stop:.4f}, invalid_cond={invalid_cond_price:.4f}, entry={entry:.4f}"
                    )
            elif side == "SHORT":
                if not (entry < invalid_cond_price < stop):
                    reasons.append(
                        "invalid_condition 不在 entry 和 stop_loss 之间 (SHORT, strict): "
                        f"entry={entry:.4f}, invalid_cond={invalid_cond_price:.4f}, stop={stop:.4f}"
                    )

        tps = plan.get("take_profits") or []
        if tps:
            first_tp = tps[0] if isinstance(tps[0], dict) else {}
            tp_price = _safe_float(first_tp.get("price"))
            if tp_price:
                tp_distance = abs(tp_price - entry)
                tp_pct = tp_distance / entry * 100
                if tp_pct < min_tp_pct:
                    reasons.append(f"第一止盈距离 {tp_pct:.3f}% 低于最小要求 {min_tp_pct}%，交易空间不足")

    # Validate stop loss has enough buffer from recent price action
    # Use ATR-based buffer: max(0.2 * ATR, min_sl_distance)
    if entry and entry > 0 and snapshot:
        modules = snapshot.get("modules") or {}
        momentum = modules.get("momentum") or {}
        atr_current = _safe_float((momentum.get("atr") or {}).get("current"))
        stop = _safe_float(plan.get("stop_loss"))
        side = str(plan.get("side") or "").upper()

        if stop and atr_current:
            if side == "LONG":
                distance = entry - stop
                # Buffer: max(0.2 * ATR, min_sl_distance)
                min_buffer = max(atr_current * 0.2, entry * min_sl_pct / 100)
                if distance < min_buffer:
                    reasons.append(f"止损距离 {distance:.4f} 不足 ATR 缓冲 {min_buffer:.4f}（0.2×ATR={atr_current*0.2:.4f}），易被噪音打掉")
            elif side == "SHORT":
                distance = stop - entry
                min_buffer = max(atr_current * 0.2, entry * min_sl_pct / 100)
                if distance < min_buffer:
                    reasons.append(f"止损距离 {distance:.4f} 不足 ATR 缓冲 {min_buffer:.4f}（0.2×ATR={atr_current*0.2:.4f}），易被噪音打掉")

    # P0-B: Late trend stage gate — blocks trend continuation orders
    if snapshot:
        modules = snapshot.get("modules") or {}
        trend_stage_data = modules.get("trend_stage") or {}
        trend_stage = str(trend_stage_data.get("trend_stage") or "").lower()
        if trend_stage in {"late", "exhausted"}:
            # Check if this is a trend continuation order (not a reversal)
            # Trend continuation: side aligns with market structure
            modules = snapshot.get("modules") or {}
            pa = modules.get("price_action") or {}
            profiles = snapshot.get("profiles") or {}
            setup_profile = profiles.get("15m") or profiles.get("1h") or {}
            structure = str(pa.get("market_structure") or setup_profile.get("market_structure") or "unknown")
            is_continuation = (
                (side == "LONG" and structure in {"bullish", "range"}) or
                (side == "SHORT" and structure in {"bearish", "range"})
            )
            if is_continuation:
                reasons.append(f"趋势阶段已进入 {trend_stage}，不适合趋势延续方向开仓（{side} vs {structure}）")

    # P0-B: Overbought/oversold anti-chase gate — RSI-based
    if snapshot:
        modules = snapshot.get("modules") or {}
        momentum = modules.get("momentum") or {}
        rsi_value = _safe_float(momentum.get("rsi"))
        if rsi_value is not None:
            if side == "LONG" and rsi_value >= rsi_ob_threshold:
                reasons.append(f"RSI {rsi_value:.1f} 超买（>={rsi_ob_threshold}），禁止追多")
            elif side == "SHORT" and rsi_value <= rsi_os_threshold:
                reasons.append(f"RSI {rsi_value:.1f} 超卖（<={rsi_os_threshold}），禁止追空")

    # P0-C: Order flow gate — degraded or opposite order flow blocks
    if snapshot:
        modules = snapshot.get("modules") or {}
        order_flow = modules.get("order_flow") or {}
        of_signal = str(order_flow.get("signal") or "").lower()
        of_supports = str(order_flow.get("supports") or "").lower()
        if of_signal == "degraded":
            reasons.append(f"订单流信号退化（degraded），不适合作为主要入场依据")
        elif of_supports and side:
            if side == "LONG" and of_supports == "bearish":
                reasons.append(f"订单流偏向空方（supports={of_supports}），与做多方向冲突")
            elif side == "SHORT" and of_supports == "bullish":
                reasons.append(f"订单流偏向多方（supports={of_supports}），与做空方向冲突")

    # P0-C: Chanlun gate — opposite chanlun signal blocks
    if snapshot:
        modules = snapshot.get("modules") or {}
        chanlun = modules.get("chanlun") or {}
        chanlun_signal = str(chanlun.get("signal") or "").lower()
        chanlun_supports = str(chanlun.get("supports") or "").lower()
        if chanlun_supports and side:
            if side == "LONG" and chanlun_supports == "bearish":
                reasons.append(f"缠论信号偏空（supports={chanlun_supports}），与做多方向冲突")
            elif side == "SHORT" and chanlun_supports == "bullish":
                reasons.append(f"缠论信号偏多（supports={chanlun_supports}），与做空方向冲突")

    # P1-C: LONG quality gate — soft downgrade for low-quality LONG entries
    if side == "LONG" and snapshot:
        long_gate = _long_quality_gate(decision, snapshot)
        metrics["long_quality_gate"] = long_gate
        if not long_gate["ok"]:
            reasons.append("LONG 质量门禁未通过：" + "；".join(long_gate["reasons"]))

    return {"ok": not reasons, "reasons": reasons, "metrics": metrics}


def suggested_actions(decision: dict[str, Any], risk: dict[str, Any] | None = None) -> list[str]:
    risk = risk or {"ok": False}
    grade = str(decision.get("signal_grade") or "D").upper()
    confidence = float(decision.get("confidence") or 0)
    actions: list[str] = []
    has_plan = bool(decision.get("has_trade_plan") and decision.get("trade_plan"))
    decision_name = str(decision.get("decision") or "")
    watch = decision.get("opportunity_watch")
    if grade in STORE_ONLY_GRADES:
        actions.extend(["add_to_watchlist", "ignore"])
    elif has_plan and risk.get("ok") and grade in PUSH_GRADES and is_paper_order_eligible(grade, confidence):
        actions.append("create_paper_order")
        actions.append("create_opportunity_watch")
    elif grade in PUSH_GRADES | WATCH_GRADES and (watch or decision_name.startswith("wait_for") or decision_name in {"monitor_only", "trade_plan_available"}):
        actions.append("create_opportunity_watch")
    actions.extend(["add_to_watchlist", "ignore"])
    out: list[str] = []
    for action in actions:
        if action not in out:
            out.append(action)
    return out


def default_watch_from_decision(decision: dict[str, Any], risk: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """P0-3 (08-02): build a structured opportunity watch for a decision that
    fell short of the direct paper-order risk gate.

    Conditions are built deterministically from the decision's plan
    (``trade_plan`` or the preserved ``candidate_trade_plan``) via the shared
    normalizer — pullback/breakout/reclaim + stop invalidation. Text
    conditions are NEVER authored here, and the pseudo-kind ``risk_rejected``
    is eliminated: risk reasons ride in ``risk_notes`` (already appended by
    ``apply_risk_to_decision``) and the watch's ``reason`` instead of an
    un-triggerable condition object.

    Returns None on fail-closed (no usable plan structure / no direction);
    callers MUST then drop ``create_opportunity_watch`` from
    ``suggested_actions`` so the manual button never fires on a watch-less
    decision.
    """
    plan = decision.get("trade_plan") or decision.get("candidate_trade_plan")
    plan_side = str((plan or {}).get("side") or "").upper() if isinstance(plan, dict) else ""
    watch, _notes = normalize_opportunity_watch(
        {
            "needed": True,
            "direction": plan_side,
            "reason": "未达到直接模拟盘风控门槛，转为机会监控。",
            "expires_minutes": 240,
        },
        plan,
    )
    return watch


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


_STRUCTURED_EVENT_TYPES = {"BOS", "CHOCH", "RECLAIM", "BREAKOUT_RETEST"}
_STRUCTURED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h"}
_STRUCTURED_SOURCES = {"price_action", "smc", "deterministic_rule"}


def _validate_entry_confirmation_shape(
    confirmation: Any,
    trade_side: str,
    analysis_time_ms: int = 0,
) -> tuple[bool, str]:
    """Shape-only validation for structured entry confirmation.

    This helper CANNOT grant order eligibility on its own. It validates
    the structural fields but does NOT verify that the confirmation
    references a real event in persisted module output.

    Production risk paths must use _validate_entry_confirmation() with
    snapshot/repo/module_analysis_results for provenance-aware validation.
    """
    import math

    if not isinstance(confirmation, dict):
        return False, "entry_trigger_confirmation 必须是对象，不是字符串"

    if confirmation.get("type") != "closed_candle_confirmation":
        return False, f"type 必须为 closed_candle_confirmation，收到 {confirmation.get('type')!r}"

    timeframe = str(confirmation.get("timeframe") or "")
    if timeframe not in _STRUCTURED_TIMEFRAMES:
        return False, f"timeframe {timeframe!r} 不在支持集合 {sorted(_STRUCTURED_TIMEFRAMES)}"

    event_type = str(confirmation.get("event_type") or "")
    if event_type not in _STRUCTURED_EVENT_TYPES:
        return False, f"event_type {event_type!r} 不在支持集合 {sorted(_STRUCTURED_EVENT_TYPES)}"

    direction = str(confirmation.get("direction") or "").lower()
    expected_dir = "bullish" if trade_side == "LONG" else "bearish"
    if direction != expected_dir:
        return False, f"direction={direction!r} 与 trade_side={trade_side} 不匹配（期望 {expected_dir}）"

    close_time = _strict_positive_int_ms(confirmation.get("candle_close_time"))
    if close_time is None:
        return False, f"candle_close_time 必须是严格正整数，收到 {confirmation.get('candle_close_time')!r}"

    # R10-2: analysis_time_ms <= 0 时 fail-closed — 不允许跳过未来函数校验
    if analysis_time_ms <= 0:
        return False, "analysis_time_ms 缺失或非正整数，无法校验未来函数"
    if close_time > analysis_time_ms:
        return False, (
            f"candle_close_time={close_time} 晚于 analysis_time={analysis_time_ms}，"
            f"存在未来函数泄漏风险"
        )

    # Price: must be finite positive (reject NaN/Infinity/0/negative)
    price = confirmation.get("price", 0)
    try:
        price = float(price)
    except (TypeError, ValueError):
        return False, f"price 无法转换为数值，收到 {price!r}"
    if not math.isfinite(price) or price <= 0:
        return False, f"price 必须为有限正数，收到 {price}"

    source = str(confirmation.get("source") or "")
    if source not in _STRUCTURED_SOURCES:
        return False, f"source {source!r} 不在支持集合 {sorted(_STRUCTURED_SOURCES)}"

    # R8-1: symbol is MANDATORY — without it, cross-symbol matching
    # cannot be prevented in the snapshot or DB fallback path.
    symbol = str(confirmation.get("symbol") or "")
    if not symbol:
        return False, "symbol 字段必填（禁止跨 symbol 匹配）"

    # R3-A: deterministic_rule must reference a persisted rule/event ID
    if source == "deterministic_rule":
        rule_id = confirmation.get("rule_id") or confirmation.get("event_id")
        if not rule_id:
            return False, (
                "source=deterministic_rule 必须包含 rule_id 或 event_id "
                "以追溯确定性规则输出，缺少该字段"
            )

    return True, ""


def _validate_entry_confirmation(
    confirmation: Any,
    trade_side: str,
    analysis_time_ms: int = 0,
    *,
    repo: Any = None,
    snapshot: dict[str, Any] | None = None,
    module_analysis_results: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate structured entry confirmation. Returns (valid, reason).

    BTC#9 R3-A fix: When snapshot/repo/module_analysis_results are provided,
    verification goes beyond shape validation — the confirmation must
    reference a REAL event in the module output. LLM self-reported fields
    are not trusted.

    Shape-only validation (backward-compatible) runs when snapshot/repo/
    module_analysis_results are all None. This shape-only path CANNOT
    grant order eligibility in production risk paths — it is kept for
    diagnostics and backward compatibility only.

    Fail if:
    - Not a dict
    - Missing required fields
    - type != "closed_candle_confirmation"
    - timeframe not in supported set
    - event_type not in supported set
    - direction mismatches trade_side (LONG requires bullish, SHORT requires bearish)
    - candle_close_time > analysis_time (event after analysis = future leak)
    - candle_close_time <= 0 or price <= 0
    - price is NaN/Infinity/0/negative
    - source not in supported set
    - source="deterministic_rule" without rule_id/event_id
    - confirmation does not match any real event in module_analysis_results/snapshot

    Returns (True, "") only when ALL checks pass.
    """
    # Step 1: shape validation (shared with _validate_entry_confirmation_shape)
    valid_shape, shape_reason = _validate_entry_confirmation_shape(
        confirmation, trade_side, analysis_time_ms,
    )
    if not valid_shape:
        return False, shape_reason

    # Step 2: provenance-aware verification (when real-source data is available)
    if repo is not None or snapshot is not None or module_analysis_results is not None:
        real_match = _find_matching_real_event(
            confirmation, trade_side, analysis_time_ms,
            snapshot=snapshot,
            module_analysis_results=module_analysis_results,
            repo=repo,
        )
        if not real_match:
            return False, "confirmation_event_not_found_in_real_events"
    else:
        # R4-D3: When ALL provenance sources are None, we cannot verify the
        # confirmation against real persisted events. Shape-only validation
        # is insufficient to grant order eligibility — LLM self-reported
        # fields are untrusted. Fail-closed.
        return False, "provenance_unavailable"

    return True, ""


def _find_matching_real_event(
    confirmation: dict[str, Any],
    trade_side: str,
    analysis_time_ms: int,
    *,
    snapshot: dict[str, Any] | None = None,
    module_analysis_results: dict[str, Any] | None = None,
    repo: Any = None,
) -> bool:
    """Check if the confirmation references a real event in module output.

    Matches by: source/module, timeframe, event_type, direction,
    candle_close_time, price (within 0.01%), closed=True.

    R4-D6: When repo is provided and source="deterministic_rule", verify
    the rule_id against persisted deterministic events in the database.
    """
    import math

    conf_source = str(confirmation.get("source") or "")
    conf_timeframe = str(confirmation.get("timeframe") or "")
    conf_event_type = str(confirmation.get("event_type") or "").upper()
    conf_direction = str(confirmation.get("direction") or "").lower()
    # R11-4: conf_close_time must be strict positive int
    conf_close_time = _strict_positive_int_ms(confirmation.get("candle_close_time"))
    if conf_close_time is None:
        return False
    try:
        conf_price = float(confirmation.get("price") or 0)
    except (TypeError, ValueError):
        return False

    # R8-2: snapshot symbol must match confirmation symbol — no cross-symbol matching.
    conf_symbol = str(confirmation.get("symbol") or "")
    if snapshot and isinstance(snapshot, dict):
        snap_symbol = str(snapshot.get("symbol") or "")
        if not snap_symbol:
            return False  # snapshot 自身缺 symbol — fail-closed
        if not conf_symbol or conf_symbol != snap_symbol:
            return False

    # Build candidate list from snapshot modules
    candidates: list[dict[str, Any]] = []

    if snapshot and isinstance(snapshot, dict):
        modules = snapshot.get("modules") or {}
        for module_key in ("price_action", "smc"):
            module_data = modules.get(module_key) or {}
            events = module_data.get("structure_events")
            if isinstance(events, list):
                for event in events:
                    if isinstance(event, dict):
                        candidates.append({**event, "_module": module_key})

    # Also check module_analysis_results if provided
    if module_analysis_results and isinstance(module_analysis_results, dict):
        for module_key, module_data in module_analysis_results.items():
            if not isinstance(module_data, dict):
                continue
            events = module_data.get("structure_events")
            if isinstance(events, list):
                for event in events:
                    if isinstance(event, dict):
                        candidates.append({**event, "_module": module_key})

    if not candidates:
        # R4-D6: Even without in-memory candidates, deterministic_rule source
        # can be verified against persisted DB events via repo.
        if conf_source == "deterministic_rule" and repo is not None:
            if _verify_deterministic_rule_id(confirmation, repo, analysis_time_ms):
                return True
        return False

    for event in candidates:
        # Match source/module
        event_module = event.get("_module") or event.get("source") or ""
        if conf_source and conf_source != "deterministic_rule":
            if conf_source != event_module and conf_source != event.get("source"):
                continue

        # Match timeframe (no defaulting)
        event_timeframe = str(event.get("timeframe") or "")
        if event_timeframe != conf_timeframe:
            continue

        # Match event_type
        raw_event_type = str(event.get("event") or event.get("type") or "").upper()
        for prefix, canonical in [("BULLISH_BOS", "BOS"), ("BEARISH_BOS", "BOS"),
                                   ("BULLISH_CHOCH", "CHOCH"), ("BEARISH_CHOCH", "CHOCH"),
                                   ("BULLISH_RECLAIM", "RECLAIM"), ("BEARISH_RECLAIM", "RECLAIM"),
                                   ("BOS", "BOS"), ("CHOCH", "CHOCH"),
                                   ("RECLAIM", "RECLAIM"), ("BREAKOUT_RETEST", "BREAKOUT_RETEST")]:
            if raw_event_type == prefix:
                raw_event_type = canonical
                break
        if raw_event_type != conf_event_type:
            continue

        # Match direction (no defaulting, no trusting LLM self-report)
        event_direction = str(event.get("direction") or "").lower()
        if event_direction not in {"bullish", "bearish"}:
            raw_name = str(event.get("event") or event.get("type") or "").lower()
            if "bullish" in raw_name:
                event_direction = "bullish"
            elif "bearish" in raw_name:
                event_direction = "bearish"
            else:
                continue
        if event_direction != conf_direction:
            continue

        # Match candle_close_time (R11-4: strict positive int parser)
        event_close_time = _strict_positive_int_ms(event.get("candle_close_time") or event.get("close_time"))
        if event_close_time is None:
            continue
        if event_close_time != conf_close_time:
            continue

        # Match price within 0.01%
        event_price = event.get("price") or event.get("close")
        try:
            event_price = float(event_price)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(event_price) or event_price <= 0:
            continue
        if conf_price > 0 and event_price > 0:
            price_diff_pct = abs(event_price - conf_price) / event_price * 100
            if price_diff_pct > 0.01:
                continue

        # R8-3: closed must be strictly True (identity check), consistent with
        # the DB fallback path (R7-D2). String "true" is no longer accepted.
        closed = event.get("closed")
        if closed is not True:
            continue

        # R4-D6: For deterministic_rule source, verify rule_id matches a field
        # in the persisted event (rule_id, event_id, or id).
        if conf_source == "deterministic_rule":
            conf_rule_id = str(confirmation.get("rule_id") or confirmation.get("event_id") or "")
            event_rule_id = str(
                event.get("rule_id") or event.get("event_id") or event.get("id") or ""
            )
            if not conf_rule_id or not event_rule_id or conf_rule_id != event_rule_id:
                continue

        return True

    # R4-D6: If no in-memory candidate matched, but repo is available and
    # source is deterministic_rule, verify the rule_id against persisted DB data.
    if conf_source == "deterministic_rule" and repo is not None:
        if _verify_deterministic_rule_id(confirmation, repo, analysis_time_ms):
            return True

    return False


def _verify_deterministic_rule_id(
    confirmation: dict[str, Any],
    repo: Any,
    analysis_time_ms: int = 0,
) -> bool:
    """R5-D3/R6-D2: Verify that a deterministic_rule confirmation's rule_id references
    a real persisted event in the database, with FULL field matching.

    R4-D6 originally only checked if rule_id existed ANYWHERE in
    module_analysis_results.result_json or feishu_events.event_id — no
    symbol/direction/price/closed/time matching. An unrelated feishu_events
    row with the same event_id would pass verification.

    R5-D3 tightened to require symbol, direction, close_time, price, closed.

    R6-D2 closes 5 remaining holes:
    1. timeframe: now checked exactly (was missing entirely)
    2. event_type: now checked canonically (was missing entirely)
    3. direction: now MANDATORY — both confirmation and event must have a
       valid direction that matches (was short-circuited: if either was
       empty, the check was skipped)
    4. analysis_time: no more 60s future leak tolerance — the upper bound
       is exact (was `analysis_time_upper + 60000`)
    5. feishu payload: all fields required — missing direction/price/timeframe/
       event_type now FAIL instead of defaulting to ok=True

    ALL of the following must match:
    - symbol (exact)
    - timeframe (exact)
    - event_type (canonical match, e.g. BULLISH_BOS → BOS)
    - direction (exact, MANDATORY — both sides must have valid value)
    - candle_close_time (within +/-60000ms of confirmation's value)
    - price (finite, positive, within 0.01% of confirmation's price)
    - closed=True
    - event_time <= analysis_time (no future leak, exact upper bound)

    For feishu_events, the payload_json must contain ALL of:
    symbol, direction, candle_close_time, price, timeframe, event_type
    matching the confirmation.

    Returns True only if a fully matching persisted record is found.
    """
    import json
    import math

    rule_id = str(confirmation.get("rule_id") or confirmation.get("event_id") or "")
    if not rule_id:
        return False

    conf_symbol = str(confirmation.get("symbol") or "")
    # R11-4: conf_close_time must be a strict positive int
    conf_close_time = _strict_positive_int_ms(confirmation.get("candle_close_time"))
    if conf_close_time is None:
        return False
    conf_direction = str(confirmation.get("direction") or "").lower()
    try:
        conf_price = float(confirmation.get("price") or 0)
    except (TypeError, ValueError):
        conf_price = 0.0
    conf_event_type = str(confirmation.get("event_type") or "").upper()
    conf_timeframe = str(confirmation.get("timeframe") or "")

    # R7-D1: conf_symbol and conf_close_time are MANDATORY — without them
    # the DB query would either scan all rows (global fallback) or match
    # the wrong symbol. Fail closed instead.
    # (R11-4: conf_close_time is already a strict positive int or None here.)
    if not conf_symbol:
        return False

    # R7-D2: analysis_time_ms is MANDATORY — without a real analysis_time
    # there is no sound upper bound. The old fallback
    # (conf_close_time + 60000) allowed future events to leak through.
    if analysis_time_ms <= 0:
        return False
    analysis_time_upper = analysis_time_ms

    # R6-D2 hole 3: direction is MANDATORY — reject if confirmation has no direction
    if conf_direction not in {"bullish", "bearish"}:
        return False
    # R6-D2 hole 2: event_type is MANDATORY
    if not conf_event_type:
        return False
    # R6-D2 hole 1: timeframe is MANDATORY
    if not conf_timeframe:
        return False

    # Canonical event_type mapping (same as _find_matching_real_event)
    _EVENT_TYPE_CANONICAL = [
        ("BULLISH_BOS", "BOS"), ("BEARISH_BOS", "BOS"),
        ("BULLISH_CHOCH", "CHOCH"), ("BEARISH_CHOCH", "CHOCH"),
        ("BULLISH_RECLAIM", "RECLAIM"), ("BEARISH_RECLAIM", "RECLAIM"),
        ("BOS", "BOS"), ("CHOCH", "CHOCH"),
        ("RECLAIM", "RECLAIM"), ("BREAKOUT_RETEST", "BREAKOUT_RETEST"),
    ]

    def _canonical_event_type(raw: str) -> str:
        raw_upper = raw.upper()
        for prefix, canonical in _EVENT_TYPE_CANONICAL:
            if raw_upper == prefix:
                return canonical
        return raw_upper

    conf_event_type_canonical = _canonical_event_type(conf_event_type)

    # 1. Check module_analysis_results for matching rule_id in structure_events
    #    with FULL field matching
    try:
        # R7-D1: conf_symbol and conf_close_time are mandatory (checked above),
        # so the global fallback branch is removed. Query is always scoped.
        mod_rows = repo.conn.execute(
            "SELECT result_json, analysis_time, timeframe FROM module_analysis_results "
            "WHERE symbol=%s AND analysis_time >= %s "
            "ORDER BY analysis_time DESC LIMIT 50",
            (conf_symbol, conf_close_time - 86400000),  # 24h lookback
        ).fetchall()
        for row in mod_rows:
            # R6-D2 hole 4: event_time (analysis_time) must not be after the upper bound.
            # No 60s tolerance — the upper bound is exact.
            row_analysis_time = int(row["analysis_time"] or 0)
            if row_analysis_time > analysis_time_upper:
                continue
            try:
                _rr = row["result_json"]
                if isinstance(_rr, (dict, list)):
                    result = _rr
                elif isinstance(_rr, str):
                    result = json.loads(_rr)
                else:
                    result = {}
            except (json.JSONDecodeError, TypeError):
                continue
            events = result.get("structure_events") if isinstance(result, dict) else None
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_rid = str(event.get("rule_id") or event.get("event_id") or event.get("id") or "")
                if not event_rid or event_rid != rule_id:
                    continue
                # R6-D2 hole 1: Match timeframe (exact)
                # Prefer event-level timeframe, fall back to row-level timeframe
                event_timeframe = str(event.get("timeframe") or row["timeframe"] or "")
                if event_timeframe != conf_timeframe:
                    continue
                # R6-D2 hole 2: Match event_type (canonical)
                raw_event_type = str(event.get("event") or event.get("type") or event.get("event_type") or "").upper()
                event_type_canonical = _canonical_event_type(raw_event_type)
                if event_type_canonical != conf_event_type_canonical:
                    continue
                # R6-D2 hole 3: Match direction — MANDATORY, no short-circuit
                # Both confirmation and event must have a valid direction that matches.
                event_direction = str(event.get("direction") or "").lower()
                if event_direction not in {"bullish", "bearish"}:
                    raw_name = str(event.get("event") or event.get("type") or "").lower()
                    if "bullish" in raw_name:
                        event_direction = "bullish"
                    elif "bearish" in raw_name:
                        event_direction = "bearish"
                    else:
                        # Missing direction — reject (not skip)
                        continue
                if event_direction != conf_direction:
                    continue
                # R7-D2: candle_close_time must match EXACTLY (was ±60000ms)
                # R11-4: strict positive int parser
                event_ct = _strict_positive_int_ms(event.get("candle_close_time") or event.get("close_time"))
                if event_ct is None:
                    continue
                if event_ct != conf_close_time:
                    continue
                # Match price (finite, positive, within 0.01%)
                event_price = event.get("price") or event.get("close")
                try:
                    event_price = float(event_price)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(event_price) or event_price <= 0:
                    continue
                if conf_price > 0 and event_price > 0:
                    price_diff_pct = abs(event_price - conf_price) / event_price * 100
                    if price_diff_pct > 0.01:
                        continue
                # R7-D2: closed must be exactly True (strict identity check)
                closed = event.get("closed")
                if closed is not True:
                    continue
                # All fields match — verified
                return True
    except Exception:
        pass

    # 2. Check feishu_events for matching event_id with structured trading payload
    #    R6-D2 hole 5: ALL fields required — missing fields FAIL (not default-True)
    #    R7-D2: closed must be strictly True, close_time must match EXACTLY,
    #    and event close_time must not be after analysis_time_upper (no future leak).
    try:
        fe_row = repo.conn.execute(
            "SELECT event_id, payload_json FROM feishu_events WHERE event_id=%s LIMIT 1",
            (rule_id,),
        ).fetchone()
        if fe_row:
            _raw_payload = fe_row["payload_json"]
            try:
                if isinstance(_raw_payload, (dict, list)):
                    payload = _raw_payload
                elif isinstance(_raw_payload, str):
                    payload = json.loads(_raw_payload)
                else:
                    payload = {}
            except (json.JSONDecodeError, TypeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            # R6-D2 hole 5: ALL fields must be present AND match.
            # No default-True — missing fields mean rejection.
            payload_symbol = str(payload.get("symbol") or "")
            if payload_symbol != conf_symbol:
                pass  # symbol mismatch or missing — reject
            else:
                # R6-D2 hole 1: timeframe must be present and match
                payload_timeframe = str(payload.get("timeframe") or "")
                if payload_timeframe != conf_timeframe:
                    pass  # timeframe mismatch or missing — reject
                else:
                    # R6-D2 hole 2: event_type must be present and match canonically
                    payload_event_type_raw = str(payload.get("event_type") or payload.get("event") or payload.get("type") or "").upper()
                    payload_event_type_canonical = _canonical_event_type(payload_event_type_raw)
                    if payload_event_type_canonical != conf_event_type_canonical:
                        pass  # event_type mismatch or missing — reject
                    else:
                        # R6-D2 hole 3: direction must be present and match — MANDATORY
                        payload_direction = str(payload.get("direction") or "").lower()
                        if payload_direction not in {"bullish", "bearish"}:
                            pass  # missing or invalid direction — reject
                        elif payload_direction != conf_direction:
                            pass  # direction mismatch — reject
                        else:
                            # candle_close_time must be present and match EXACTLY
                            # R11-4: strict positive int parser
                            payload_ct = _strict_positive_int_ms(payload.get("candle_close_time") or payload.get("close_time"))
                            if payload_ct is None:
                                pass  # missing or invalid close_time — reject
                            elif payload_ct != conf_close_time:
                                pass  # R7-D2: exact match (was ±60000ms)
                            elif payload_ct > analysis_time_upper:
                                pass  # R7-D2: future event — reject
                            else:
                                # price must be present, finite, positive, within 0.01%
                                payload_price = payload.get("price") or payload.get("close")
                                try:
                                    payload_price = float(payload_price)
                                except (TypeError, ValueError):
                                    payload_price = 0.0
                                if payload_price <= 0 or not math.isfinite(payload_price):
                                    pass  # missing or invalid price — reject
                                else:
                                    price_diff_pct = abs(payload_price - conf_price) / payload_price * 100
                                    if price_diff_pct > 0.01:
                                        pass  # price mismatch — reject
                                    else:
                                        # R7-D2: closed must be exactly True (strict identity)
                                        payload_closed = payload.get("closed")
                                        if payload_closed is not True:
                                            pass  # closed missing or not strictly True — reject
                                        else:
                                            # All fields match — verified
                                            return True
    except Exception:
        pass

    return False


def _parse_invalid_condition_price(invalid_condition: Any) -> float | None:
    """BTC#9 fix: 从 invalid_condition 文本中解析失效价位。

    兼容格式：
    - "15m 收盘跌破 59750.2"
    - "15m 收盘站回 60250.5"
    - 纯数字字符串/数值

    解析失败返回 None（兼容 LLM 自由文本，不报错）。
    """
    if invalid_condition is None:
        return None
    if isinstance(invalid_condition, (int, float)):
        try:
            return float(invalid_condition)
        except (TypeError, ValueError):
            return None
    if not isinstance(invalid_condition, str):
        return None
    import re
    # BTC#9 fix: invalid_condition 文本可能包含非价格数字（如 "15m"），
    # 取最后一个数字作为失效价
    all_matches = re.findall(r"[-+]?\d+(?:\.\d+)?", invalid_condition)
    if not all_matches:
        return None
    try:
        return float(all_matches[-1])
    except (TypeError, ValueError):
        return None


def _entry_price(plan: dict[str, Any]) -> float | None:
    value = plan.get("entry_price")
    if value is None:
        value = plan.get("trigger_price")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _risk_reward(plan: dict[str, Any]) -> float | None:
    entry = _entry_price(plan)
    try:
        stop = float(plan.get("stop_loss"))
    except (TypeError, ValueError):
        return None
    if entry is None or entry == stop:
        return None
    tps = plan.get("take_profits") or []
    prices: list[float] = []
    for tp in tps:
        try:
            prices.append(float(tp.get("price") if isinstance(tp, dict) else tp))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None
    side = str(plan.get("side") or "").upper()
    reward = max((price - entry) if side == "LONG" else (entry - price) for price in prices)
    risk = abs(entry - stop)
    return round(reward / risk, 4) if risk > 0 else None


def _htf_support(side: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    profiles = snapshot.get("profiles") or {}
    direction = profiles.get("4h") or {}
    trend_1h = profiles.get("1h") or {}
    setup_15m = profiles.get("15m") or {}
    htf_structure = str(direction.get("market_structure") or "unknown")
    trend_structure = str(trend_1h.get("market_structure") or "unknown")
    setup_structure = str(setup_15m.get("market_structure") or "unknown")

    cfg = load_config().trading_mode
    risk_cfg = cfg.get("risk", {})
    require_stronger = bool(risk_cfg.get("require_stronger_confirmation_for_weak_structure", True))

    # 4H 允许 transition 和 range（区间不提供方向偏置但也不阻断），1H/15M 允许 range
    if side == "LONG":
        ok = htf_structure in {"bullish", "transition", "range"} and trend_structure in {"bullish", "range", "transition"} and setup_structure in {"bullish", "range", "transition"}
        reason = "" if ok else f"高周期不支持做多：4H={htf_structure}, 1H={trend_structure}, 15M={setup_structure}"
        # BTC#9 P2-1: weak structure fail-closed — multiple TFs with range/transition need stronger confirmation
        if ok and require_stronger:
            weak_tfs = []
            if htf_structure in {"range", "transition"}:
                weak_tfs.append("4H")
            if trend_structure in {"range", "transition"}:
                weak_tfs.append("1H")
            if setup_structure in {"range", "transition"}:
                weak_tfs.append("15M")
            if len(weak_tfs) >= 2:
                ok = False
                reason = f"多周期结构偏弱（{','.join(weak_tfs)}），需更强入场确认"
    elif side == "SHORT":
        ok = htf_structure in {"bearish", "transition", "range"} and trend_structure in {"bearish", "range", "transition"} and setup_structure in {"bearish", "range", "transition"}
        reason = "" if ok else f"高周期不支持做空：4H={htf_structure}, 1H={trend_structure}, 15M={setup_structure}"
        # BTC#9 P2-1: weak structure fail-closed
        if ok and require_stronger:
            weak_tfs = []
            if htf_structure in {"range", "transition"}:
                weak_tfs.append("4H")
            if trend_structure in {"range", "transition"}:
                weak_tfs.append("1H")
            if setup_structure in {"range", "transition"}:
                weak_tfs.append("15M")
            if len(weak_tfs) >= 2:
                ok = False
                reason = f"多周期结构偏弱（{','.join(weak_tfs)}），需更强入场确认"
    else:
        ok = False
        reason = "trade_plan 缺少 LONG/SHORT 方向"
    return {"ok": ok, "reason": reason, "4h": htf_structure, "1h": trend_structure, "15m": setup_structure}


def _structure_momentum_alignment(side: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    modules = snapshot.get("modules") or {}
    pa = modules.get("price_action") or {}
    momentum = modules.get("momentum") or {}
    profiles = snapshot.get("profiles") or {}
    setup_profile = profiles.get("15m") or profiles.get("1h") or {}
    structure = str(pa.get("market_structure") or setup_profile.get("market_structure") or "unknown")
    mom = str(momentum.get("direction") or setup_profile.get("momentum") or "neutral")
    # 允许 transition 结构（近突破位）
    if side == "LONG":
        ok = structure in {"bullish", "range", "transition"} and mom == "bullish"
        reason = f"结构与动能未共振做多：structure={structure}, momentum={mom}"
    elif side == "SHORT":
        ok = structure in {"bearish", "range", "transition"} and mom == "bearish"
        reason = f"结构与动能未共振做空：structure={structure}, momentum={mom}"
    else:
        ok = False
        reason = "缺少方向，无法确认结构 + 动能共振"
    return {"ok": ok, "reason": reason, "structure": structure, "momentum": mom}


def _long_quality_gate(decision: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """LONG quality gate — soft downgrade for low-quality LONG entries.

    Implements P1-C: Block LONG entries when:
    - HTF bias not bullish
    - Trend stage late/exhausted
    - Momentum exhausted/overextended
    - Range/chop market structure
    - BTC context risk_off
    - Historical avg_r < 0 for symbol+side
    """
    from plugins.crypto_guard.storage.repository import CryptoGuardRepository

    reasons: list[str] = []
    plan = decision.get("trade_plan") or {}
    symbol = decision.get("symbol") or plan.get("symbol", "")
    modules = snapshot.get("modules") or {}
    profiles = snapshot.get("profiles") or {}

    # Check HTF bias
    htf_4h = profiles.get("4h") or {}
    htf_structure = str(htf_4h.get("market_structure") or "unknown")
    if htf_structure not in {"bullish", "transition"}:
        reasons.append(f"4H 结构不支持做多：{htf_structure}")

    # Check trend stage
    trend_stage_data = modules.get("trend_stage") or {}
    trend_stage = str(trend_stage_data.get("trend_stage") or "unknown").lower()
    if trend_stage in {"late", "exhausted"}:
        reasons.append(f"趋势阶段不适合做多：{trend_stage}")

    # Check momentum
    momentum = modules.get("momentum") or {}
    momentum_state = str(momentum.get("state") or momentum.get("direction") or "neutral").lower()
    if momentum_state in {"exhausted", "overextended"}:
        reasons.append(f"动能状态不适合做多：{momentum_state}")

    # Check market structure (range/chop)
    pa = modules.get("price_action") or {}
    setup_profile = profiles.get("15m") or profiles.get("1h") or {}
    structure = str(pa.get("market_structure") or setup_profile.get("market_structure") or "unknown")
    entry_type = str(plan.get("entry_type") or "").lower()
    if structure in {"range", "chop"} and entry_type in {"breakout", "trend"}:
        reasons.append(f"区间市场禁止趋势型做多：{structure}")

    # Check BTC context
    btc_regime = modules.get("btc_context") or {}
    btc_risk_off = btc_regime.get("risk_off") or btc_regime.get("hard_risk_off")
    if btc_risk_off:
        reasons.append("BTC 上下文风险关闭，不适合做多")

    # Check historical performance (if repo available)
    # This is a soft check — we don't block, just warn
    # In production, this would query paper_trades for symbol+LONG avg_r

    return {"ok": not reasons, "reasons": reasons}


def risk_summary_from_signal(signal: dict[str, Any]) -> dict[str, Any]:
    raw = signal.get("ga_decision_json") or {}
    try:
        decision = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception:
        decision = {}
    return decision.get("risk_check") or {"ok": False, "reasons": ["signal 缺少风控记录"]}


def apply_regime_gate(
    repo: Any,
    *,
    symbol: str,
    side: str,
    signal_grade: str,
    confidence: float,
    analysis_time_utc: int,
    risk_percent: float = 0.5,
    order_type: str = "",
) -> dict[str, Any]:
    """Counter-regime soft gate: downgrade/restrict trades against market regime.

    Called before paper order creation. Does NOT hard-block — it downgrades
    and annotates, letting the normal risk pipeline make the final decision.

    Returns adjustments dict that callers merge into their decision.
    """
    cfg = load_config().trading_mode
    regime_cfg = cfg.get("market_regime", {})
    if not regime_cfg.get("enabled", True):
        return {"ok": True, "regime_gate_applied": False, "mode": regime_cfg.get("mode", "shadow"), "adjustments": {}}

    counter_cfg = regime_cfg.get("counter_regime", {})

    def _support(regime_score: float, s: str) -> tuple[float, str]:
        """Side-aware support score. Positive = regime supports this trade direction."""
        su = s.upper()
        if su == "LONG":
            return regime_score, su
        elif su == "SHORT":
            return -regime_score, su
        return 0.0, su

    # Score regime
    regime = score_market_regime(
        repo,
        symbol=symbol,
        analysis_time_utc=analysis_time_utc,
        decision_side=side,
    )

    alignment = regime.get("regime_alignment", "unclear")
    regime_weight = float(regime.get("market_regime_weight", 0.25))
    regime_score = float(regime.get("normalized_regime_score", 0.0))
    support_score, support_side = _support(regime_score, side)

    if alignment == "unclear":
        # Unclear data: require stronger confirmation, no penalty
        return {
            "ok": True,
            "regime_gate_applied": True,
            "mode": regime_cfg.get("mode", "shadow"),
            "market_regime": regime,
            "adjustments": {
                "confidence_adjustment": 0.0,
                "risk_multiplier": 1.0,
                "effective_grade": signal_grade,
                "original_grade": signal_grade,
                "effective_confidence": confidence,
                "original_confidence": confidence,
                "confidence_penalty": 0.0,
                "effective_risk_multiplier": 1.0,
                "min_rr": 0.0,
                "watch_only": False,
                "require_stronger_confirmation": True,
                "regime_alignment": "unclear",
                "market_phase": regime.get("market_phase"),
                "btc_bias": regime.get("btc_bias"),
                "eth_bias": regime.get("eth_bias"),
                "reasons": regime.get("reasons", []),
                "regime_score": regime_score,
                "regime_weight": regime_weight,
                "weighted_confidence_adjustment": 0.0,
                "effective_confidence_after_regime": confidence,
                "support_score": round(support_score, 4),
                "support_score_side": support_side,
            },
        }

    if alignment != "counter_regime":
        # BTC#9 fix: chop/transition/unknown market_phase 不提供 confidence boost
        # 只有 risk_on/rebound (for LONG) 或 risk_off/selloff (for SHORT) 才能 boost
        market_phase = str(regime.get("market_phase") or "normal")
        boost_suppressed = False
        boost_suppress_reason: str | None = None

        if alignment == "aligned":
            # Only explicit regimes can boost: risk_on/rebound (LONG aligned) or risk_off/selloff (SHORT aligned)
            boost_eligible_phases = {"risk_on", "rebound", "risk_off", "selloff"}
            if market_phase in boost_eligible_phases:
                effective_delta = max(0.0, min(0.05, support_score * regime_weight))
            elif market_phase in {"chop", "transition", "unknown"}:
                effective_delta = 0.0
                boost_suppressed = True
                boost_suppress_reason = f"market_phase={market_phase} 不提供信心加成"
            else:
                # "normal" or any other phase — current behavior (can add small boost)
                effective_delta = max(0.0, min(0.05, support_score * regime_weight))
        elif alignment == "independent_trend":
            effective_delta = 0.0
        else:
            effective_delta = 0.0

        if effective_delta != 0.0:
            effective_confidence = max(0.0, min(1.0, confidence + effective_delta))
            return {
                "ok": True,
                "regime_gate_applied": True,
                "mode": regime_cfg.get("mode", "shadow"),
                "market_regime": regime,
                "adjustments": {
                    "confidence_adjustment": round(effective_delta, 4),
                    "risk_multiplier": regime.get("suggested_risk_multiplier", 1.0),
                    "effective_grade": signal_grade,
                    "original_grade": signal_grade,
                    "effective_confidence": round(effective_confidence, 4),
                    "original_confidence": confidence,
                    "confidence_penalty": 0.0,
                    "effective_risk_multiplier": regime.get("suggested_risk_multiplier", 1.0),
                    "min_rr": 0.0,
                    "watch_only": False,
                    "require_stronger_confirmation": False,
                    "regime_alignment": alignment,
                    "market_phase": regime.get("market_phase"),
                    "btc_bias": regime.get("btc_bias"),
                    "eth_bias": regime.get("eth_bias"),
                    "reasons": regime.get("reasons", []),
                    "regime_score": regime_score,
                    "regime_weight": regime_weight,
                    "weighted_confidence_adjustment": round(effective_delta, 4),
                    "effective_confidence_after_regime": round(effective_confidence, 4),
                    "support_score": round(support_score, 4),
                    "support_score_side": support_side,
                    "confidence_boost_reason": (
                        f"aligned regime: side={support_side} support_score={support_score:+.3f} "
                        f"weight={regime_weight} boost={effective_delta:+.4f}"
                    ),
                },
            }
        # effective_delta == 0: no-op branch — record full audit fields
        return {
            "ok": True,
            "regime_gate_applied": False,
            "mode": regime_cfg.get("mode", "shadow"),
            "market_regime": regime,
            "adjustments": {
                "confidence_adjustment": 0.0,
                "risk_multiplier": regime.get("suggested_risk_multiplier", 1.0),
                "effective_grade": signal_grade,
                "original_grade": signal_grade,
                "effective_confidence": confidence,
                "original_confidence": confidence,
                "confidence_penalty": 0.0,
                "effective_risk_multiplier": regime.get("suggested_risk_multiplier", 1.0),
                "min_rr": 0.0,
                "watch_only": False,
                "require_stronger_confirmation": False,
                "regime_alignment": alignment,
                "market_phase": regime.get("market_phase"),
                "btc_bias": regime.get("btc_bias"),
                "eth_bias": regime.get("eth_bias"),
                "reasons": regime.get("reasons", []),
                "regime_score": regime_score,
                "regime_weight": regime_weight,
                "weighted_confidence_adjustment": 0.0,
                "effective_confidence_after_regime": confidence,
                "support_score": round(support_score, 4),
                "support_score_side": support_side,
                "confidence_boost_suppressed_reason": boost_suppress_reason,
            },
        }

    # Counter-regime: apply downgrades
    grade_downgrade_steps = int(counter_cfg.get("grade_downgrade", 1))
    grade_map = {"S": "A", "A": "B", "B": "C", "C": "D", "D": "D"}
    effective_grade = signal_grade
    for _ in range(grade_downgrade_steps):
        effective_grade = grade_map.get(effective_grade, effective_grade)

    confidence_penalty = float(counter_cfg.get("confidence_penalty", 0.10))
    effective_confidence = max(0.0, confidence - confidence_penalty)

    risk_mult = float(counter_cfg.get("risk_multiplier", 0.5))
    min_rr = float(counter_cfg.get("min_rr", 2.0))
    allowed_order_types = counter_cfg.get("allow_order_types", ["trigger", "retest"])

    # Check consecutive same-side losses today
    watch_only_after = int(counter_cfg.get("watch_only_after_same_side_losses", 2))
    watch_only = False
    if watch_only_after > 0:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        recent_trades = repo.conn.execute(
            """
            SELECT close_reason FROM paper_trades
            WHERE side=%s AND closed_at IS NOT NULL
              AND DATE(COALESCE(closed_at, NOW()))=%s
            ORDER BY closed_at DESC
            LIMIT %s
            """,
            (side, today, watch_only_after),
        ).fetchall()
        watch_only = len(recent_trades) >= watch_only_after and all(
            r["close_reason"] == "stop_loss" for r in recent_trades
        )

    adjustments = {
        "effective_grade": effective_grade,
        "original_grade": signal_grade,
        "effective_confidence": effective_confidence,
        "original_confidence": confidence,
        "confidence_penalty": confidence_penalty,
        "risk_multiplier": risk_mult,
        "effective_risk_multiplier": risk_mult,
        "min_rr": min_rr,
        "allowed_order_types": allowed_order_types,
        "watch_only": watch_only,
        "regime_alignment": alignment,
        "market_phase": regime.get("market_phase"),
        "btc_bias": regime.get("btc_bias"),
        "eth_bias": regime.get("eth_bias"),
        "reasons": regime.get("reasons", []),
        "regime_score": regime_score,
        "regime_weight": regime_weight,
        "weighted_confidence_adjustment": round(-confidence_penalty, 4),
        "effective_confidence_after_regime": round(effective_confidence, 4),
        "support_score": round(support_score, 4),
        "support_score_side": support_side,
    }

    return {
        "ok": True,
        "regime_gate_applied": True,
        "mode": regime_cfg.get("mode", "shadow"),
        "market_regime": regime,
        "adjustments": adjustments,
    }
