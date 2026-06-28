"""Deterministic consistency validator between structured execution state
and LLM/template summary text.

Shared by:
- plugins.crypto_guard.reasoning.llm_agent_judge._normalize_llm_decision
  (rewrites final_summary before persistence)
- plugins.crypto_guard.notify.hourly_report.render_ga_hourly_summary
  (double-check at render time)

The validator is intentionally pure — it reads only ``decision`` dict fields
(``risk_check`` / ``trade_plan`` / ``decision`` / ``market_bias`` /
``has_trade_plan`` / ``signal_grade``) — so it is fully unit-testable without
hitting the DB or the LLM.
"""

from __future__ import annotations

from typing import Any, Iterable

# Phrases that imply an opportunity is directly executable / risk-clear.
# Per PRD P0, when risk_check.ok is false or trade_plan is missing, the
# summary MUST NOT contain any of these.
FORBIDDEN_EXECUTABLE_PHRASES: tuple[str, ...] = (
    "具备模拟盘条件", "具备模拟盘做多条件", "具备模拟盘做空条件",
    "风控全部满足", "风控指标全部满足",
    "可创建订单", "风控通过", "存在可执行",
    # P1-9: expanded with high-frequency execution phrases
    "建议设置 limit", "建议设置 trigger",
    "建议做多", "建议做空",
    "可开仓", "可入场",
)

EXECUTION_OVERRIDE_PREFIX = "仅观察/未通过执行门禁："


def is_valid_trade_plan(plan: dict[str, Any] | None) -> bool:
    """Check if a trade_plan dict has the minimum required fields.

    P1-10: any non-empty dict previously passed, but placeholder dicts like
    {"note": "placeholder"} should NOT be treated as valid.
    Required fields: side, entry_type, (entry_price or trigger_price),
    stop_loss, (take_profit or take_profits). risk_reward_ratio is derived
    at execution time from these fields, so it is not checked here.
    """
    if not isinstance(plan, dict) or not plan:
        return False
    required_str = ("side", "entry_type")
    for field in required_str:
        if not plan.get(field):
            return False
    has_price = plan.get("entry_price") or plan.get("trigger_price")
    if not has_price:
        return False
    if not plan.get("stop_loss"):
        return False
    if not plan.get("take_profit") and not plan.get("take_profits"):
        return False
    return True


def execution_eligible(decision: dict[str, Any]) -> bool:
    """Return True iff the decision legitimately clears the executable gate.

    Mirrors render_ga_hourly_summary._opportunity_classifier:
    grade ∈ {S,A,B}, confidence ≥ min_confidence, has_trade_plan,
    risk_check.ok true, decision=create_paper_order or
    trade_plan_available, and not stale-missing a side.
    """
    from plugins.crypto_guard.strategy.grade_config import MIN_CONFIDENCE_FOR_PAPER_ORDER
    grade = str(decision.get("signal_grade") or "").upper()
    confidence = float(decision.get("confidence") or 0)
    risk = decision.get("risk_check") or {}
    plan = decision.get("trade_plan")
    has_plan = is_valid_trade_plan(plan)
    risk_ok = bool(risk.get("ok")) if isinstance(risk, dict) else False
    decision_name = str(decision.get("decision") or "")
    return (
        grade in {"S", "A", "B"}
        and confidence >= MIN_CONFIDENCE_FOR_PAPER_ORDER
        and has_plan
        and risk_ok
        and decision_name in {"create_paper_order", "trade_plan_available"}
    )


def rewrite_inconsistent_summary(text: str, decision: dict[str, Any]) -> str:
    """Strip forbidden phrases from ``text`` when the structured state forbids
    them, and generate a deterministic rendered summary.

    P1-8 (Round 3): When execution_eligible is False, do NOT rely solely on
    blacklist replacement. Instead, produce a completely deterministic
    rendered_summary that ignores the LLM's wording:
      - Keep original final_summary untouched as raw_summary
      - Generate deterministic summary: "[观察] {symbol} {grade}级；{gate_blockers}"
      - The report renderer prefers rendered_summary over final_summary
    The blacklist-based cleanup on final_summary is kept as a secondary defense.
    """
    if not text:
        return text
    if execution_eligible(decision):
        return text
    # P1-8 (Round 3): Generate a deterministic rendered_summary
    grade = str(decision.get("signal_grade") or "D").upper()
    symbol = str(decision.get("symbol") or "")
    blockers = _gate_blockers(decision)
    deterministic = f"[观察] {symbol} {grade}级；{blockers}"
    # Also clean the original text via blacklist as secondary defense
    cleaned = text
    altered = False
    for phrase in FORBIDDEN_EXECUTABLE_PHRASES:
        if phrase in cleaned:
            cleaned = cleaned.replace(phrase, "")
            altered = True
    if altered:
        cleaned = cleaned.strip("；;。., ")
        if cleaned:
            cleaned = cleaned + "。" if not cleaned.endswith(("。", ".", "；", ";")) else cleaned
        trailer = EXECUTION_OVERRIDE_PREFIX + blockers
        cleaned = (cleaned + " " + trailer) if cleaned else trailer
    # Return the deterministic summary as rendered_summary
    # If blacklist cleaned version differs from deterministic, prefer deterministic
    return deterministic


def _gate_blockers(decision: dict[str, Any]) -> str:
    blockers: list[str] = []
    plan = decision.get("trade_plan")
    has_plan = is_valid_trade_plan(plan)
    risk = decision.get("risk_check") or {}
    risk_ok = bool(risk.get("ok")) if isinstance(risk, dict) else False
    confidence = float(decision.get("confidence") or 0)
    if not has_plan:
        blockers.append("缺 trade_plan")
    if not risk_ok:
        reasons = (risk.get("reasons") if isinstance(risk, dict) else None) or []
        blockers.append("风控未通过" + ("：" + "；".join(str(r) for r in reasons[:2]) if reasons else ""))
    from plugins.crypto_guard.strategy.grade_config import MIN_CONFIDENCE_FOR_PAPER_ORDER
    if confidence < MIN_CONFIDENCE_FOR_PAPER_ORDER:
        blockers.append(f"置信度 {confidence:.2f} < {MIN_CONFIDENCE_FOR_PAPER_ORDER:.2f}")
    return "，".join(blockers)


def contains_forbidden_phrase(text: str) -> list[str]:
    """Return the list of forbidden phrases present in ``text``."""
    return [p for p in FORBIDDEN_EXECUTABLE_PHRASES if p in (text or "")]