from __future__ import annotations

import json
import os
import re
from typing import Any

from plugins.crypto_guard.reasoning.decision_schema import validate_json
from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
from plugins.crypto_guard.risk.risk_engine import apply_risk_to_decision
from plugins.crypto_guard.strategy.strategy_scorer import score_snapshot


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
            "market_bias": ["bullish", "bearish", "neutral", "mixed"],
            "trend_stage": ["early", "middle", "late", "range", "transition", "unknown"],
            "suggested_actions": ["create_paper_order", "create_opportunity_watch", "add_to_watchlist", "ignore"],
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

    return SYSTEM_PROMPT + "\n\n输入：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM response is not a JSON object")
    return data


def _normalize_llm_decision(candidate: dict[str, Any], snapshot: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    decision = dict(fallback)
    decision.update(candidate)
    decision["symbol"] = snapshot["symbol"]
    decision["analysis_time_utc"] = int(snapshot.get("analysis_time_utc") or 0)
    decision.setdefault("strategy_name", fallback.get("strategy_name", "llm_agent_sop"))
    decision.setdefault("strategy_version", fallback.get("strategy_version", "1.0"))
    decision["analysis_source"] = "llm_agent"
    decision["llm_status"] = "ok"
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
    if decision.get("decision") == "trade_plan_available":
        decision["has_trade_plan"] = bool(decision.get("trade_plan"))
    watch = decision.get("opportunity_watch")
    if isinstance(watch, dict):
        watch = dict(watch)
        watch["direction"] = _normalize_watch_direction(watch.get("direction"), decision.get("trade_plan"))
        decision["opportunity_watch"] = watch
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
    return {
        "symbol": snapshot.get("symbol"),
        "analysis_time_utc": snapshot.get("analysis_time_utc"),
        "mode": snapshot.get("mode"),
        "profiles": snapshot.get("profiles") or {},
        "modules": keep_modules,
        "counter_evidence": snapshot.get("counter_evidence") or {},
        "data_quality": snapshot.get("data_quality") or {},
        "global_context": snapshot.get("global_context") or {},
    }
