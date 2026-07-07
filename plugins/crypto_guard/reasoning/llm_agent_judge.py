from __future__ import annotations

import json
import os
import re
from typing import Any

from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
from plugins.crypto_guard.risk.risk_engine import apply_risk_to_decision
from plugins.crypto_guard.strategy.strategy_scorer import score_snapshot
from plugins.crypto_guard.utils import _strict_positive_int_ms


SYSTEM_PROMPT = """你是 GA CryptoGuard 的市场研究 Agent。
你必须基于结构化模块证据做多周期 SOP 研判，而不是凭空预测。
边界：禁止实盘交易建议，禁止真实下单，只允许输出模拟盘/机会监控/观察/忽略相关决策。
只输出一个符合 GADecision schema 的 JSON 对象，不要 Markdown，不要额外解释。
"""


def run_agent_sop_decision(snapshot: dict[str, Any], *, use_llm: bool | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the LLM/GA SOP decision path, falling back to deterministic SOP if needed."""

    fallback = run_ga_sop_decision(snapshot)
    if use_llm is None:
        use_llm = os.environ.get("CRYPTO_GUARD_LLM_ANALYSIS", "1").lower() not in {"0", "false", "no"}
    if not use_llm:
        fallback["analysis_source"] = "deterministic_sop"
        fallback["llm_status"] = "disabled"
        return apply_risk_to_decision(fallback, snapshot)

    try:
        prompt = build_llm_decision_prompt(snapshot, fallback, context=context)
        raw = _call_ga_llm(prompt)
        candidate = _parse_json_object(raw)
        decision = _normalize_llm_decision(candidate, snapshot, fallback)
        ok, err = validate_json("ga_decision.schema.json", decision)
        if not ok:
            raise ValueError(err or "schema validation failed")
        return apply_risk_to_decision(decision, snapshot)
    except Exception as exc:
        fallback["analysis_source"] = "deterministic_fallback"
        fallback["llm_status"] = "failed"
        fallback["llm_error"] = str(exc)[:300]
        notes = list(fallback.get("risk_notes") or [])
        notes.append("LLM/GA 研判失败，本次使用规则 SOP 降级结果。")
        fallback["risk_notes"] = notes
        return apply_risk_to_decision(fallback, snapshot)


def run_agent_json_task(
    *,
    task_name: str,
    payload: dict[str, Any],
    fallback: dict[str, Any],
    schema_name: str | None = None,
    instructions: list[str] | None = None,
    use_llm: bool | None = None,
) -> dict[str, Any]:
    """Run a non-market-decision GA/LLM JSON task with deterministic fallback."""

    if use_llm is None:
        use_llm = os.environ.get("CRYPTO_GUARD_LLM_ANALYSIS", "1").lower() not in {"0", "false", "no"}
    if not use_llm:
        result = dict(fallback)
        result["agent_source"] = "deterministic_sop"
        result["llm_status"] = "disabled"
        return result
    try:
        prompt = build_agent_json_task_prompt(task_name=task_name, payload=payload, fallback=fallback, instructions=instructions)
        raw = _call_ga_llm(prompt)
        candidate = _parse_json_object(raw)
        result = dict(fallback)
        result.update(candidate)
        result["agent_source"] = "llm_agent"
        result["llm_status"] = "ok"
        if schema_name:
            ok, err = validate_json(schema_name, result)
            if not ok:
                raise ValueError(err or "schema validation failed")
        return result
    except Exception as exc:
        result = dict(fallback)
        result["agent_source"] = "deterministic_fallback"
        result["llm_status"] = "failed"
        result["llm_error"] = str(exc)[:300]
        return result


# R9 P2-2 fix: module-level constant so the final hard-cap fallback
# can be exercised behaviorally in tests via ``unittest.mock.patch``.
# Pre-R9 ``MAX_PROMPT_BYTES`` was a function-local variable, making the
# defensive safe_payload path effectively unreachable in tests — the
# only way to force it was to make SYSTEM_PROMPT itself huge, which is
# not possible from a test. With this constant at module scope, tests
# can patch it to a small value to fire the safe_payload path.
MAX_PROMPT_BYTES = 48 * 1024  # 2x feature pack budget


def build_llm_decision_prompt(snapshot: dict[str, Any], deterministic_decision: dict[str, Any], *, context: dict[str, Any] | None = None) -> str:
    from plugins.crypto_guard.config.loader import load_config
    scoring = score_snapshot(snapshot)
    risk_cfg = load_config().trading_mode.get("risk", {})
    min_rr = risk_cfg.get("min_rr", 1.5)
    min_conf = risk_cfg.get("min_confidence", 0.72)
    payload = {
        "schema_contract": {
            "decision": ["trade_plan_available", "wait_for_pullback", "wait_for_breakout", "wait_for_reclaim", "avoid_chop", "no_edge", "monitor_only"],
            "signal_grade": ["S", "A", "B", "C", "D"],
            "market_bias": ["bullish", "bearish", "neutral", "mixed", "unknown"],
            "trend_stage": ["early", "middle", "late", "range", "transition", "unknown"],
            "suggested_actions": ["create_paper_order", "create_opportunity_watch", "add_to_watchlist", "ignore", "monitor_only"],
        },
        "task": "按 SOP_MULTI_TIMEFRAME_MARKET_ANALYSIS 输出最终 GADecision JSON。",
        "sop": [
            "检查数据完整性和未来函数风险",
            "判断 4H 已收盘方向过滤器",
            "判断 1H/15M 已收盘趋势与结构",
            "检查 5M 入场、反转和触发机会",
            "主动寻找反向证据",
            "匹配策略评分和动作决策",
            "解释为什么有机会或为什么没有机会",
        ],
        "hard_rules": [
            "不得输出实盘交易或真实下单能力",
            "LLM 不负责几何计算；Swing/FVG/OB/中枢/指标数值必须以 deterministic_preprocessing 输出为准",
            "5M 只能触发入场，不能单独推翻 4H 方向；未收盘 4H/1H/15M 不得作为确认依据",
            "当 signal_grade 为 S 或 A 时，必须生成 trade_plan（包含 side/entry_price/stop_loss/take_profits/invalid_condition）",
            "trade_plan 的止损必须基于结构失效位（swing low/high、FVG 边界、order block 边界）",
            f"创建模拟盘必须经过风控：RR>={min_rr}、confidence>={min_conf}、高周期方向支持、非极端行情",
            "B 级可输出 opportunity_watch 但不强制 trade_plan",
            "C/D 级不得 create_paper_order，decision 应为 monitor_only 或 no_edge",
            f"反向证据存在不等于不能交易；只要 RR>={min_rr} 且止损明确，A/S 级仍应给出 trade_plan",
            "counter_evidence 至少 1 条",
            "entry_trigger_confirmation 必须是结构化对象（type/timeframe/event_type/direction/candle_close_time/price/source/symbol），不得使用裸字符串",
            "entry_trigger_confirmation 必须与 schema 完全匹配，字段不可省略；无法提供时设为 null",
            "entry_trigger_confirmation.symbol 必须等于顶层 decision.symbol — 禁止跨 symbol 匹配",
        ],
        "market_snapshot": _compact_snapshot(snapshot),
        "pre_score": scoring,
        "deterministic_reference": deterministic_decision,
        "output_requirements": {
            "format": "JSON object only",
            "language": "Chinese for summary/evidence/risk_notes",
            "must_keep": ["symbol", "analysis_time_utc", "strategy_name", "strategy_version"],
        },
    }

    # Inject historical memory from context
    if context:
        memory_section = _build_memory_section(context)
        if memory_section:
            payload["historical_memory"] = memory_section

        # Inject open position context
        open_orders = context.get("open_paper_orders") or []
        if open_orders:
            payload["open_positions"] = [
                {"symbol": o.get("symbol"), "side": o.get("side"), "entry_price": o.get("entry_price"), "status": o.get("status")}
                for o in open_orders[:5]
            ]

        # Inject active opportunity watches
        watches = context.get("active_opportunity_watches") or []
        if watches:
            payload["active_watches"] = [
                {"symbol": w.get("symbol"), "direction": w.get("direction"), "reason": w.get("reason")}
                for w in watches[:5]
            ]

    # R5 P1-2 fix: bound the final prompt size. The 24 KiB feature pack
    # budget only constrains ``multi_timeframe_feature_pack``; the full
    # prompt (with ``modules``, ``historical_memory``, ``open_positions``,
    # ``active_watches``, ``analysis_continuity``) can blow past 48 KiB
    # and exceed the LLM context window. Trim ``historical_memory``
    # first (least actionable), then ``open_positions``/``active_watches``
    # (context-only), then ``analysis_continuity`` (decision-useful but
    # redundant with market_snapshot), then ``modules`` (primary-TF detail
    # — last resort because it carries decision-critical indicator
    # values). Never trim ``market_snapshot.multi_timeframe_feature_pack``
    # or ``deterministic_reference`` here — those are decision-critical
    # and have their own bounded budgets.
    # R6 REC-R6-1: added ``modules`` as a final trim tier so an oversized
    # primary-TF modules dict cannot silently push the prompt past budget.
    # R8 P1 fix:
    #   - Added ``analysis_continuity`` as a trim tier (after
    #     open_positions/active_watches, before modules). Pre-R8 an
    #     oversized ``analysis_continuity`` (whose own 12 KiB budget is
    #     per-block, not per-prompt) could combine with other sections
    #     to push the prompt past 48 KiB, but the trim ladder never
    #     touched it. Now drop it before falling back to the minimal
    #     stub.
    #   - Final hard assertion: if every trim tier fails to bring the
    #     prompt under budget, replace the payload with a minimal safe
    #     fallback (symbol + analysis_time + hard_rules + deterministic
    #     decision only). Pre-R8 the function returned the oversized
    #     prompt as a last resort, blowing past the cap.
    #   - Minimal stub ready-path: read ``m.health.ready`` (correct
    #     path — feature pack module's health is a sub-dict, not a
    #     top-level field). Pre-R8 the stub read ``m.ready`` which is
    #     always ``None`` in production, hiding real readiness state
    #     behind a silent None.
    # R9 P2-2 fix: ``MAX_PROMPT_BYTES`` is now a module-level constant
    # so the safe_payload fallback can be exercised behaviorally in
    # tests via ``unittest.mock.patch``.
    prompt = SYSTEM_PROMPT + "\n\n输入：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        payload.pop("historical_memory", None)
        prompt = SYSTEM_PROMPT + "\n\n输入：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            payload.pop("open_positions", None)
            payload.pop("active_watches", None)
            prompt = SYSTEM_PROMPT + "\n\n输入：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            # R8 P1 fix: trim analysis_continuity before modules. The
            # continuity block is decision-useful but redundant with
            # market_snapshot's per-TF view; modules carry unique
            # primary-TF indicator values.
            if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                market_snapshot = payload.get("market_snapshot")
                if isinstance(market_snapshot, dict):
                    market_snapshot.pop("analysis_continuity", None)
                prompt = SYSTEM_PROMPT + "\n\n输入：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                    # Last-resort trim: drop primary-TF modules. The
                    # multi_timeframe_feature_pack already carries per-TF
                    # compact views, so the LLM still has TF context.
                    # R6 REC-R6-1 fix: ``modules`` is nested under
                    # ``market_snapshot`` (set by ``_compact_snapshot``),
                    # not at the payload top level. A top-level
                    # ``payload.pop("modules")`` was a silent no-op and the
                    # oversized prompt leaked past the budget.
                    if isinstance(market_snapshot, dict):
                        market_snapshot.pop("modules", None)
                    prompt = SYSTEM_PROMPT + "\n\n输入：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    # R7 P1 fix: final hard cap. If the prompt is STILL over
                    # budget after every trim tier, the oversized payload is
                    # ``market_snapshot.multi_timeframe_feature_pack`` or
                    # ``deterministic_reference`` — neither was trimmed above
                    # because both are decision-critical. Replace the
                    # feature pack with a minimal stub (symbol + per-TF
                    # ready flag only) and the deterministic_reference with
                    # a one-line summary. This guarantees the prompt stays
                    # under the LLM context window even when the upstream
                    # producer emits a pathological payload. Pre-R7 the
                    # function just returned the oversized prompt — a 100KB
                    # feature pack produced a 103KB prompt, blowing past
                    # the 48KB cap.
                    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                        if isinstance(market_snapshot, dict):
                            mtfp = market_snapshot.get("multi_timeframe_feature_pack")
                            if isinstance(mtfp, dict):
                                # Replace with a minimal stub: keep only
                                # symbol + per-TF ready/bias (decision-critical
                                # minimum), drop all indicator details.
                                # R8 P1 fix: ready path is ``m.health.ready``,
                                # NOT ``m.ready``. Feature pack module
                                # structure (decision_context.py:263):
                                # ``m = {"health": _compact_health(health),
                                #         "bias": ..., ...}``. Pre-R8 the stub
                                # read ``m.ready`` which is always None in
                                # production, hiding the real readiness
                                # state behind a silent None.
                                minimal_mtfp = {"symbol": mtfp.get("symbol")}
                                modules = mtfp.get("modules") or {}
                                if isinstance(modules, dict):
                                    minimal_mtfp["modules"] = {
                                        tf: {
                                            "ready": (
                                                (m.get("health") or {}).get("ready")
                                                if isinstance(m, dict) else None
                                            ),
                                            "bias": (m.get("bias") if isinstance(m, dict) else None),
                                        }
                                        for tf, m in modules.items()
                                    }
                                market_snapshot["multi_timeframe_feature_pack"] = minimal_mtfp
                        # Deterministic reference: trim to decision-critical
                        # fields only.
                        dr = payload.get("deterministic_reference")
                        if isinstance(dr, dict):
                            payload["deterministic_reference"] = {
                                k: dr.get(k)
                                for k in (
                                    "decision", "signal_grade", "confidence",
                                    "market_bias", "trend_stage", "symbol",
                                    "analysis_time_utc",
                                    "strategy_name", "strategy_version",
                                )
                                if k in dr
                            }
                        prompt = SYSTEM_PROMPT + "\n\n输入：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                        # Final assertion: if STILL over, drop historical_memory
                        # was already tried — drop deterministic_reference
                        # entirely (LLM still has market_snapshot + hard_rules).
                        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                            payload.pop("deterministic_reference", None)
                            prompt = SYSTEM_PROMPT + "\n\n输入：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                            # R8 P1 fix: final hard assertion. If EVERY
                            # trim tier failed, replace the payload with
                            # a minimal safe fallback (decision-critical
                            # fields only) so the prompt is guaranteed
                            # under budget. Pre-R8 the function returned
                            # the oversized prompt as a last resort.
                            if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                                safe_dr = deterministic_decision or {}
                                # R10 P1 fix: read symbol/analysis_time_utc
                                # from ``market_snapshot`` (the actual
                                # location) or ``deterministic_decision``,
                                # NOT from payload top level. Pre-R10 the
                                # code read ``payload.get("symbol")`` which
                                # returned None — ``symbol`` and
                                # ``analysis_time_utc`` live inside
                                # ``payload["market_snapshot"]`` (set by
                                # ``_compact_snapshot``). This caused the
                                # safe_payload to emit
                                # ``{"symbol": null, "analysis_time_utc": null}``
                                # at the top level, violating
                                # ``output_requirements.must_keep`` and
                                # losing the symbol context precisely when
                                # the prompt is under extreme budget
                                # pressure.
                                # R11 P2 fix: also surface
                                # ``strategy_name``/``strategy_version``
                                # from ``deterministic_decision`` (set by
                                # ``ga_judge.py:631-632``). Pre-R11 the
                                # safe_payload included
                                # ``output_requirements.must_keep``
                                # demanding these fields but never
                                # provided them — a self-contradicting
                                # prompt payload that the LLM could not
                                # satisfy.
                                safe_ms = payload.get("market_snapshot") or {}
                                safe_payload = {
                                    "symbol": safe_ms.get("symbol") or safe_dr.get("symbol"),
                                    "analysis_time_utc": safe_ms.get("analysis_time_utc") or safe_dr.get("analysis_time_utc"),
                                    "strategy_name": safe_dr.get("strategy_name"),
                                    "strategy_version": safe_dr.get("strategy_version"),
                                    "hard_rules": payload.get("hard_rules"),
                                    "deterministic_reference": {
                                        k: safe_dr.get(k)
                                        for k in (
                                            "decision", "signal_grade", "confidence",
                                            "market_bias", "trend_stage", "symbol",
                                            "analysis_time_utc",
                                            "strategy_name", "strategy_version",
                                        )
                                        if k in safe_dr
                                    },
                                    "output_requirements": payload.get("output_requirements"),
                                    "_trim_note": "prompt_over_budget_minimal_fallback",
                                }
                                prompt = SYSTEM_PROMPT + "\n\n输入：\n" + json.dumps(
                                    safe_payload, ensure_ascii=False, separators=(",", ":"),
                                )
    return prompt


def _build_memory_section(context: dict[str, Any]) -> dict[str, Any] | None:
    """Build historical memory section for LLM prompt from context."""
    feedback = context.get("skill_feedback_memory") or []
    if not feedback:
        return None

    # Group by skill and extract key insights
    by_skill: dict[str, list[dict]] = {}
    for item in feedback:
        skill = item.get("skill_name") or "unknown"
        if skill not in by_skill:
            by_skill[skill] = []

        # Parse suggested_adjustment_json (stored as JSON string in DB)
        adjustment_raw = item.get("suggested_adjustment_json") or ""
        adjustment = {}
        if adjustment_raw:
            try:
                adjustment = json.loads(adjustment_raw) if isinstance(adjustment_raw, str) else adjustment_raw
            except (json.JSONDecodeError, TypeError):
                adjustment = {"raw": adjustment_raw}

        by_skill[skill].append({
            "pattern": item.get("finding"),  # DB field is "finding", not "pattern_description"
            "adjustment": adjustment,
            "status": item.get("status"),
        })

    # Only include skills with feedback
    if not by_skill:
        return None

    return {
        "description": "历史分析反馈记忆。当同类行情/结构出现时，应参考这些经验调整置信度和决策。",
        "skills": {skill: items[:3] for skill, items in by_skill.items()},  # max 3 per skill
        "instruction": "如果当前行情结构与记忆中的模式相似，适当调整 confidence（+/-0.05~0.15）并在 risk_notes 中说明参考了哪条历史经验。",
    }


def build_agent_json_task_prompt(
    *,
    task_name: str,
    payload: dict[str, Any],
    fallback: dict[str, Any],
    instructions: list[str] | None = None,
) -> str:
    body = {
        "task_name": task_name,
        "task": "基于结构化证据执行 GA/LLM SOP 任务，并只输出一个 JSON 对象。",
        "instructions": instructions or [],
        "hard_rules": [
            "禁止实盘交易、真实下单、保存交易或提现权限 API Key",
            "策略变更只能进入 candidate/shadow/review 流程，不得直接 active，除非输入明确允许且门禁通过",
            "必须说明证据、反证和下一步动作",
            "如果证据不足，输出保守结论并说明缺口",
        ],
        "payload": payload,
        "deterministic_fallback": fallback,
        "output_requirements": {
            "format": "JSON object only",
            "language": "Chinese for human-facing text",
            "preserve_required_ids": True,
        },
    }
    return SYSTEM_PROMPT + "\n\n" + json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _call_ga_llm(prompt: str) -> str:
    cfg_name = _resolve_llm_config_name()
    import llmcore

    session = llmcore.resolve_session(cfg_name)
    session.system = SYSTEM_PROMPT
    if getattr(session, "thinking_type", None) == "enabled" and getattr(session, "thinking_budget_tokens", None) is None:
        session.thinking_type = "adaptive"
    if hasattr(session, "tools") and not getattr(session, "tools", None):
        session.tools = [
            {
                "type": "function",
                "function": {
                    "name": "crypto_guard_noop",
                    "description": "Placeholder only. Do not call this tool; answer with JSON text.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    if getattr(session, "read_timeout", 0) < 60:
        session.read_timeout = 60
    raw = "".join(session.raw_ask([{"role": "user", "content": [{"type": "text", "text": prompt}]}]))
    if not raw.strip():
        raise RuntimeError("empty LLM response")
    if raw.lstrip().startswith("!!!Error"):
        raise RuntimeError(raw.strip()[:300])
    return raw


def _resolve_llm_config_name() -> str:
    configured = os.environ.get("CRYPTO_GUARD_LLM_CONFIG")
    if configured:
        return configured
    import llmcore

    keys, _ = llmcore.reload_mykeys()
    candidates = [
        name
        for name, value in keys.items()
        if isinstance(value, dict)
        and "mixin" not in name.lower()
        and any(token in name.lower() for token in ("native", "oai", "claude"))
        and value.get("apikey")
        and value.get("apibase")
    ]
    if not candidates:
        raise RuntimeError("未找到可用 GA LLM 配置；请设置 CRYPTO_GUARD_LLM_CONFIG 或 mykey.py")
    native = [name for name in candidates if "native" in name.lower()]
    return (native or candidates)[0]


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Phase G (07-05): bounded JSON extraction with explicit error
    categories. The LLM occasionally emits responses that ``json.loads``
    cannot parse directly: Markdown fences, leading/trailing prose, or
    raw control characters (``\\x00`` from tool output). The PRD Fact 4
    scenario (DOGE) was caused by ``Invalid control character`` —
    ``json.loads`` raised and the chain collapsed to "缺交易计划" instead
    of preserving the deterministic candidate plan.

    Contract (PRD FR-6 / design.md §8):
    - Strip Markdown fences (one attempt).
    - Extract exactly one JSON object via ``\\{[\\s\\S]*\\}`` regex
      (one attempt) when the direct parse fails.
    - Record error category and retry count on the returned dict via
      ``_llm_parse_meta`` so diagnostics can distinguish "extracted"
      from "clean parse".
    - Never accept repaired output without schema + semantic validation
      (the caller ``run_agent_sop_decision`` runs ``validate_json``
      after this function returns).

    Out of scope (intentionally NOT repaired — fail-closed):
    - Invalid control characters (``\\x00``-``\\x1f``) — per PRD Fact 4,
      these MUST fail parse so the deterministic candidate plan is
      preserved under ``candidate_trade_plan`` / ``plan_status="withheld"``.
    - Truncated JSON — cannot be reliably repaired; fail-closed.
    - Schema-invalid JSON — semantic validation in the caller catches this.

    When parse fails, re-raise ``JSONDecodeError`` so the caller's
    except branch fail-closes the decision.
    """
    text = raw.strip()
    # Step 1: strip Markdown fences (one attempt).
    fenced = False
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()
        fenced = True
    else:
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    # Step 2: direct parse (clean path).
    retry_count = 0
    error_category: str | None = None
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("LLM response is not a JSON object")
        _attach_parse_meta(data, retry_count=retry_count, error_category=None, fenced=fenced)
        return data
    except json.JSONDecodeError as exc:
        error_category = _classify_json_error(exc, text)

    # Step 3: extract exactly one JSON object via regex (one attempt).
    # This handles leading/trailing prose around a valid JSON object.
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        retry_count += 1
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                _attach_parse_meta(
                    data, retry_count=retry_count, error_category=error_category,
                    fenced=fenced, extracted=True,
                )
                return data
        except json.JSONDecodeError:
            pass  # fall through to fail-closed

    # Step 4: fail-closed — could not recover. Re-raise so the caller's
    # except branch records llm_status="failed" and preserves the
    # deterministic candidate plan (PRD Fact 4 / FR-4 / FR-6).
    err = json.JSONDecodeError(
        f"LLM JSON parse failed (category={error_category}, retries={retry_count})",
        text, 0,
    )
    raise err


def _classify_json_error(exc: json.JSONDecodeError, text: str) -> str:
    """Classify a JSONDecodeError into a stable error category for diagnostics."""
    msg = str(exc).lower()
    if "control character" in msg or "\\x00" in text:
        return "invalid_control_character"
    if "unterminated string" in msg or "end of json" in msg or "eof" in msg:
        return "truncated_json"
    if "expecting" in msg and "delimiter" in msg:
        return "malformed_delimiter"
    if "unescaped" in msg or "invalid \\escape" in msg:
        return "invalid_escape"
    return "malformed_json"


def _attach_parse_meta(
    data: dict[str, Any],
    *,
    retry_count: int,
    error_category: str | None,
    fenced: bool = False,
    extracted: bool = False,
    repaired: bool = False,
) -> None:
    """Attach parse-time metadata to the parsed dict so diagnostics can
    reason about LLM output quality. The metadata is namespaced under
    ``_llm_parse_meta`` and stripped by ``_normalize_llm_decision`` before
    the candidate merges with the deterministic fallback (the schema does
    not allow these keys).
    """
    data["_llm_parse_meta"] = {
        "retry_count": retry_count,
        "error_category": error_category,
        "fenced": fenced,
        "extracted": extracted,
        "repaired": repaired,
    }


def _normalize_llm_decision(candidate: dict[str, Any], snapshot: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    decision = dict(fallback)
    # Phase F (07-05): capture raw_signal_grade / raw_score from the
    # deterministic fallback BEFORE the LLM candidate merges. These are
    # the pre-LLM, pre-gate deterministic conclusions and must persist
    # for audit/report so "原始评分 X% · 执行等级 Y" can be rendered
    # even when the LLM upgrades or downgrades the grade. canonical
    # signal_grade remains the effective (post-LLM, post-gate) grade.
    raw_signal_grade = fallback.get("raw_signal_grade") or fallback.get("signal_grade") or "D"
    raw_score = float(fallback.get("raw_score") if fallback.get("raw_score") is not None else fallback.get("confidence") or 0.0)
    decision["raw_signal_grade"] = raw_signal_grade
    decision["raw_score"] = round(raw_score, 4)
    # Pass 7 P1 #2: strip internal marker fields from the LLM candidate before
    # merge. ``_htf_conflict_original_grade`` and ``_htf_conflict_grade_downgraded``
    # are internal idempotency markers owned by ``market_semantics.normalize_market_semantics``
    # — they record the pre-downgrade grade so a subsequent call can detect
    # LLM-driven grade restoration. If the LLM candidate can write these fields
    # directly, a malicious or confused LLM could set
    # ``_htf_conflict_original_grade="X"`` to bypass the S→A downgrade (the
    # downgrade logic sees no expected terminal grade and skips). The fault
    # injection ``_htf_conflict_original_grade="X"`` + ``signal_grade=S`` +
    # ``htf_conflict=True`` reproduced this bypass: the marker
    # ``htf_conflict_grade_downgraded`` stays in market_reason_codes while
    # signal_grade remains S. The confidence cap (0.70) still blocks execution
    # for now, but the semantic contract is broken. Strip all internal ``_``-
    # prefixed fields from the candidate before merge — the LLM has no business
    # writing them.
    INTERNAL_FIELD_PREFIX = "_"
    if isinstance(candidate, dict):
        # Phase G (07-05): capture parse-time metadata before stripping so
        # diagnostics can reason about LLM output quality (fenced/extracted/
        # repaired + error_category + retry_count). The metadata is namespaced
        # under ``_llm_parse_meta`` by ``_parse_json_object``.
        llm_parse_meta = candidate.get("_llm_parse_meta")
        candidate = {
            k: v for k, v in candidate.items()
            if not (isinstance(k, str) and k.startswith(INTERNAL_FIELD_PREFIX))
        }
    else:
        llm_parse_meta = None
    decision.update(candidate)
    if llm_parse_meta is not None:
        # Surface parse metadata on the decision for downstream diagnostics.
        # ``llm_parse_meta`` is not in the ga_decision schema; it is stripped
        # before persistence by the controller (which only persists
        # schema-allowed keys plus raw_decision_json). Use a non-conflicting
        # key so it does not collide with schema fields.
        decision["llm_parse_meta"] = llm_parse_meta
    decision["symbol"] = snapshot["symbol"]
    # R11-6: snapshot.analysis_time_utc must be a strict positive int, else fail-closed
    _snap_time = _strict_positive_int_ms(snapshot.get("analysis_time_utc"))
    if _snap_time is None:
        raise ValueError("snapshot.analysis_time_utc 缺失或非严格正整数，拒绝产出 decision")
    decision["analysis_time_utc"] = _snap_time
    decision.setdefault("strategy_name", fallback.get("strategy_name", "llm_agent_sop"))
    decision.setdefault("strategy_version", fallback.get("strategy_version", "1.0"))
    decision["analysis_source"] = "llm_agent"
    decision["llm_status"] = "ok"
    # Persisted audit reference (NOT the prompt payload path). The same
    # key name ``deterministic_reference`` is used in two paths:
    # (1) here — persisted on the decision row for audit/debugging;
    # (2) in ``build_llm_decision_prompt`` trim tier + safe_payload
    #     fallback — included in the LLM prompt payload, where
    #     ``output_requirements.must_keep`` requires
    #     ``strategy_name``/``strategy_version`` to be surfaced.
    # This audit-path dict intentionally omits ``strategy_name``/
    # ``strategy_version`` because they are already on the parent
    # ``decision`` dict (lines 648-649 above). Adding them here would
    # be redundant and could confuse downstream consumers about which
    # dict is authoritative. Do NOT add them here without updating
    # ``decision_schema.py`` and ``feishu_cards.py``.
    decision["deterministic_reference"] = {
        "decision": fallback.get("decision"),
        "signal_grade": fallback.get("signal_grade"),
        "confidence": fallback.get("confidence"),
        "summary": fallback.get("summary"),
    }
    if not isinstance(decision.get("counter_evidence"), list) or not decision["counter_evidence"]:
        decision["counter_evidence"] = list(fallback.get("counter_evidence") or ["LLM 未给出反向证据，沿用规则 SOP 风险提示。"])
    if not isinstance(decision.get("risk_notes"), list):
        decision["risk_notes"] = list(fallback.get("risk_notes") or [])

    # P0-3: Generation layer fail-closed. When analysis_degraded=True (set by
    # market_state_builder when data health fails), force the decision to a
    # degraded-but-recorded shape regardless of LLM output. Skip the
    # auto-build trade plan block below — the data is too degraded to author
    # a trade plan, even if the LLM claims an A/S grade.
    analysis_degraded = bool(snapshot.get("analysis_degraded") or
                             ((snapshot.get("data_quality") or {}).get("analysis_degraded")))
    if analysis_degraded:
        decision["market_bias"] = "unknown"
        decision["signal_grade"] = "C"
        decision["confidence"] = min(float(decision.get("confidence") or 0.3), 0.3)
        # Phase F (07-05): preserve raw_signal_grade / raw_score set at
        # the top of _normalize_llm_decision so the report can still
        # surface "原始评分 X%" even in degraded mode. raw_score reflects
        # the deterministic fallback's score, not the degraded 0.3 cap.
        decision["has_trade_plan"] = False
        decision["trade_plan"] = None
        decision["decision"] = "monitor_only"
        decision["opportunity_watch"] = None
        decision["suggested_actions"] = ["monitor_only"]
        decision["trend_stage"] = "unknown"
        decision["degraded_reason"] = "analysis_degraded: market data health check failed (contiguity/freshness/gap)"
        # P1-6: overwrite summary and final_summary with a deterministic
        # degraded string. Previously the original LLM bullish/bearish text
        # (e.g. "强势看涨，可创建模拟盘多单") was preserved, contradicting
        # the forced market_bias=unknown. Do NOT keep the original LLM text.
        decision["summary"] = "分析降级，方向不可靠"
        decision["final_summary"] = "分析降级，方向不可靠"
        notes = list(decision.get("risk_notes") or [])
        if not any("分析降级" in str(n) for n in notes):
            notes.append("分析降级：数据不完整，强制 market_bias=unknown、signal_grade=C、无交易计划。")
        decision["risk_notes"] = notes
        # Skip the auto-build trade plan block — do NOT fall through to the
        # grade-based auto-build logic below.
        return decision

    # 当 LLM 给出 A/S 级但没有 trade_plan 时，自动补建
    grade = str(decision.get("signal_grade") or "D").upper()
    if grade in {"S", "A"} and not decision.get("trade_plan"):
        from plugins.crypto_guard.reasoning.ga_judge import _build_trade_plan
        side = "LONG" if decision.get("market_bias") == "bullish" else "SHORT" if decision.get("market_bias") == "bearish" else None
        if side:
            auto_plan = _build_trade_plan(snapshot, side)
            if auto_plan:
                decision["trade_plan"] = auto_plan
                decision["has_trade_plan"] = True
                decision["decision"] = "trade_plan_available"
                decision.setdefault("risk_notes", []).append("trade_plan 由系统自动补建（LLM 未生成）。")

    # 评分稳定：LLM 等级不能比确定性评分低超过 1 级
    from plugins.crypto_guard.strategy.grade_config import grade_order_value, grade_from_order_value
    det_grade_val = grade_order_value(str(fallback.get("signal_grade") or "D").upper())
    llm_grade_val = grade_order_value(grade)
    if llm_grade_val < det_grade_val - 1:
        stabilized_grade = grade_from_order_value(det_grade_val - 1)
        decision["signal_grade"] = stabilized_grade
        decision.setdefault("risk_notes", []).append(
            f"LLM 等级 {grade} 比确定性评分 {fallback.get('signal_grade')} 低超过 1 级，稳定为 {stabilized_grade}。"
        )

    if decision.get("has_trade_plan") and not decision.get("trade_plan"):
        decision["has_trade_plan"] = False
    if not decision.get("has_trade_plan"):
        decision["trade_plan"] = None
    # BTC#9: LLM failed/disabled must NOT fake entry_trigger_confirmation
    trade_plan = decision.get("trade_plan")
    if isinstance(trade_plan, dict):
        ec = trade_plan.get("entry_trigger_confirmation")
        if isinstance(ec, str):
            decision["trade_plan"]["entry_trigger_confirmation"] = None
            notes = list(decision.get("risk_notes") or [])
            notes.append("LLM 输出裸字符串 entry_trigger_confirmation，已清空为 null（需结构化对象）。")
            decision["risk_notes"] = notes
    if decision.get("decision") == "trade_plan_available":
        decision["has_trade_plan"] = bool(decision.get("trade_plan"))
    watch = decision.get("opportunity_watch")
    if isinstance(watch, dict):
        watch = dict(watch)
        watch["direction"] = _normalize_watch_direction(watch.get("direction"), decision.get("trade_plan"))
        decision["opportunity_watch"] = watch

    # Deterministic consistency override (P0): strip forbidden executable
    # phrases from final_summary when the structured execution gate is not
    # satisfied. apply_risk_to_decision may have already downgraded
    # decision/has_trade_plan; here we only silence the LLM's own text.
    # Phase C (07-03): the canonical summary builder is now the single source
    # of truth for the final rendered text. rewrite_inconsistent_summary still
    # runs first as a blacklist-defense layer, but the canonical builder
    # produces the final deterministic text that downstream consumers read.
    # R1-1 (07-03 final review): run normalize_market_semantics here as the
    # single semantic boundary so LLM-produced bullish/middle/0.95 is
    # normalized BEFORE the canonical builder reads structured fields. This
    # closes the gap where LLM candidate merge skips the bias+stage contract
    # and HTF-conflict cap. The final normalize call is idempotent: repeated
    # runs do not continue to downgrade or duplicate reason codes.
    from plugins.crypto_guard.notify.report_consistency import (
        rewrite_inconsistent_summary,
        execution_eligible,
    )
    from plugins.crypto_guard.reasoning.summary_builder import (
        build_canonical_market_summary,
    )
    from plugins.crypto_guard.reasoning.market_semantics import (
        normalize_market_semantics,
    )
    try:
        # Pass 6 P2 #5 (07-03 final review): read market_semantics config from
        # the real source (cfg.trading_mode.market_semantics) instead of the
        # non-existent snapshot.config. snapshot.config was always None, so
        # ms_cfg fell back to {} and normalize_market_semantics used default
        # caps (0.67) instead of the configured 0.70. Load_config is cheap
        # (cached) and matches how market_state_builder reads the same config.
        from plugins.crypto_guard.config.loader import load_config
        ms_cfg = (load_config().trading_mode.get("market_semantics") or {})
    except Exception:
        ms_cfg = {}
    # R1-1 (07-03 final review): normalize_market_semantics returns a new dict
    # (it does ``result = dict(decision)`` internally). The caller MUST assign
    # the return value — without assignment, bias/stage/confidence demotion is
    # silently discarded and the LLM's bullish/middle/0.95 penetrates the gate.
    decision = normalize_market_semantics(decision, snapshot, ms_cfg)
    summary_text = decision.get("final_summary") or decision.get("summary") or ""
    rewritten = rewrite_inconsistent_summary(summary_text, decision)
    # Phase C: always produce the canonical summary so final_summary and
    # rendered_summary carry the same deterministic text. The canonical
    # builder reads the structured fields (grade/bias/stage/alignment/
    # htf_conflict/market_reason_codes/risk_check/trade_plan) and emits a
    # concise Chinese summary. The original LLM text is preserved in
    # raw_legacy_decision["raw_llm_summary"] by the controller after this
    # function returns; here we only set final_summary/rendered_summary.
    canonical = build_canonical_market_summary(decision)
    decision["rendered_summary"] = canonical
    # R1-5 (07-03 final review): always set final_summary == summary ==
    # canonical. The original LLM text is preserved separately by the
    # controller in raw_decision_json["raw_llm_summary"]. Do not gate on
    # execution_eligible — all decisions use the canonical text.
    decision["final_summary"] = canonical
    if "summary" in decision:
        decision["summary"] = canonical
    if rewritten != summary_text:
        notes = list(decision.get("risk_notes") or [])
        notes.append("final_summary 已由 canonical deterministic summary 覆盖。")
        decision["risk_notes"] = notes
    return decision


def _normalize_watch_direction(value: Any, trade_plan: Any = None) -> str | None:
    if value in ("LONG", "SHORT", None):
        return value
    text = str(value).strip().lower()
    if text in {"long", "buy", "bull", "bullish", "up", "多", "做多", "看多"}:
        return "LONG"
    if text in {"short", "sell", "bear", "bearish", "down", "空", "做空", "看空"}:
        return "SHORT"
    if isinstance(trade_plan, dict):
        side = str(trade_plan.get("side") or "").upper()
        if side in {"LONG", "SHORT"}:
            return side
    return None


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    modules = snapshot.get("modules") or {}
    keep_modules = {}
    for name in ("price_action", "momentum", "trend_stage", "smc", "order_flow", "chanlun"):
        value = modules.get(name)
        if isinstance(value, dict):
            keep_modules[name] = value
    # Phase C (07-05): surface a bounded MultiTimeframeFeaturePack so the LLM
    # sees per-TF compact modules (sample_count, data_as_of, bias, structure,
    # momentum, key_levels) for ALL 5 timeframes — not just the primary TF.
    # The pack is built lazily here so snapshots constructed by tests or
    # fixtures (which may not call attach_feature_pack_to_snapshot) still
    # surface the pack to the LLM prompt. Raw candle arrays, full swing
    # histories, skill prompts, and logs are excluded by the builder's
    # size budget.
    from plugins.crypto_guard.reasoning.decision_context import (
        build_multi_timeframe_feature_pack,
    )
    feature_pack = snapshot.get("multi_timeframe_feature_pack") or build_multi_timeframe_feature_pack(snapshot)
    # Phase D (07-05): surface the analysis_continuity block (previous_compact
    # + delta with trigger_progress) so the LLM sees prior grade/bias/stage/
    # key_levels/next_triggers and confirmed/invalidated trigger status. The
    # block is built lazily here so snapshots constructed without an explicit
    # attach call still surface continuity to the LLM. When no previous row
    # is available, the block carries continuity_status="missing" and an empty
    # delta — the LLM still gets a structured signal that this is a first-round.
    continuity = snapshot.get("analysis_continuity")
    if continuity is None:
        # Best-effort: build with no previous row (continuity_status="missing").
        # The controller is responsible for attaching a real previous row
        # before the LLM prompt is built.
        try:
            from plugins.crypto_guard.reasoning.decision_context import (
                build_analysis_continuity,
            )
            continuity = build_analysis_continuity(
                snapshot, previous_row=None,
                current_batch_id=None, current_decision=None,
            )
        except Exception:
            continuity = None
    return {
        "symbol": snapshot.get("symbol"),
        "analysis_time_utc": snapshot.get("analysis_time_utc"),
        "mode": snapshot.get("mode"),
        "profiles": snapshot.get("profiles") or {},
        "modules": keep_modules,
        "multi_timeframe_feature_pack": feature_pack,
        # R5 P1-2 fix: ``timeframe_modules`` was a duplicate of
        # ``feature_pack.get("modules")`` — sending the same per-TF data
        # twice to the LLM, doubling token cost without adding any new
        # information. Removed; downstream consumers read
        # ``multi_timeframe_feature_pack.modules`` directly.
        "analysis_continuity": continuity,
        "counter_evidence": snapshot.get("counter_evidence") or {},
        "data_quality": snapshot.get("data_quality") or {},
        "global_context": snapshot.get("global_context") or {},
    }
