"""Unified signal grade configuration.

Single source of truth for grade thresholds, confidence mappings, and grade ordering.
All modules should import from here instead of defining their own mappings.
"""

from __future__ import annotations

# Grade thresholds (score-based)
GRADE_THRESHOLDS = {
    "S": 0.80,
    "A": 0.72,
    "B": 0.65,
    "C": 0.50,
    "D": 0.00,
}

# Grade ordering for comparison
GRADE_ORDER = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
GRADE_BY_NUM = {v: k for k, v in GRADE_ORDER.items()}

# Grade sets for policy decisions
PUSH_GRADES = {"S", "A"}  # Can create paper orders
WATCH_GRADES = {"B"}  # Opportunity watch only
STORE_ONLY_GRADES = {"C", "D"}  # Store but no action
PAPER_ORDER_GRADES = {"S", "A"}  # Grades eligible for paper orders

# Confidence thresholds
MIN_CONFIDENCE_FOR_PAPER_ORDER = 0.72  # Must be >= A grade
MIN_CONFIDENCE_DEFAULT = 0.72

# Hysteresis buffers (research 10/11): grade promotion needs to clear the new
# tier by an extra buffer; demotion within the same 4h window needs two
# consecutive confirmations unless an emergency downgrade is flagged.
GRADE_UP_BUFFER = 0.02
SA_MAX_COUNTER_EVIDENCE = 3  # S/A 至多允许 3 条矛盾证据，超过封顶 B


def grade_from_score(score: float) -> str:
    """Derive grade from numeric score. Single source of truth."""
    if score >= GRADE_THRESHOLDS["S"]:
        return "S"
    if score >= GRADE_THRESHOLDS["A"]:
        return "A"
    if score >= GRADE_THRESHOLDS["B"]:
        return "B"
    if score >= GRADE_THRESHOLDS["C"]:
        return "C"
    return "D"


def grade_order_value(grade: str) -> int:
    """Get numeric order value for grade comparison."""
    return GRADE_ORDER.get(str(grade).upper(), 0)


def grade_from_order_value(value: int) -> str:
    """Get grade from numeric order value."""
    return GRADE_BY_NUM.get(value, "D")


def is_paper_order_eligible(grade: str, confidence: float) -> bool:
    """Check if grade and confidence qualify for paper order creation."""
    return grade in PAPER_ORDER_GRADES and confidence >= MIN_CONFIDENCE_FOR_PAPER_ORDER


def grade_with_hysteresis(
    current_grade: str,
    previous_grade: str | None,
    *,
    emergency_down: bool = False,
) -> tuple[str, str]:
    """Apply grade hysteresis against the previous decision's grade.

    Returns (effective_grade, reason). ``reason`` is empty when no clamp was
    applied. Promotion requires the new grade to be at least 2 tiers above
    the previous grade (otherwise clamped to prev+1). Demotion within the
    same 4h window is dampened to one tier unless ``emergency_down`` is True,
    in which case the raw grade is returned so genuine risk can drop the
    grade immediately.

    P0-7 fix: ``current_grade`` is the actual signal_grade string (e.g. "B"),
    NOT the confidence score. The caller must pass the real grade, not
    re-derive it from confidence.

    The reason string is reported to the user / diagnostics audit so we never
    silently mask real risk changes.
    """
    raw_grade = str(current_grade).upper() if current_grade else "D"
    if raw_grade not in GRADE_ORDER:
        raw_grade = grade_from_score(float(current_grade) if isinstance(current_grade, (int, float)) else 0.0)
    if not previous_grade or previous_grade not in GRADE_ORDER:
        return raw_grade, ""

    prev_val = GRADE_ORDER[previous_grade]
    raw_val = GRADE_ORDER[raw_grade]
    reason = ""

    if raw_val > prev_val:
        # promotion: require at least 2-tier gap; otherwise stabilize at prev+1
        if raw_val > prev_val + 1 and not emergency_down:
            stabilized = GRADE_BY_NUM[prev_val + 1] if prev_val + 1 in GRADE_BY_NUM else raw_grade
            reason = f"评级升级迟滞：{previous_grade}→{raw_grade} 跨度超过 1 级，暂缓为 {stabilized}"
            return stabilized, reason
    elif raw_val < prev_val:
        if not emergency_down:
            # dampen: a single-period drop more than one tier is clamped to one tier down
            if raw_val < prev_val - 1:
                stabilized = GRADE_BY_NUM[prev_val - 1] if prev_val - 1 in GRADE_BY_NUM else raw_grade
                reason = f"评级降级迟滞：单周期从 {previous_grade} 跳到 {raw_grade}，暂缓为 {stabilized}（无紧急降级）"
                return stabilized, reason
    return raw_grade, reason


def clamp_grade(
    grade: str,
    *,
    has_trade_plan: bool,
    risk_ok: bool,
    confidence: float | None = None,
    htf_conflict: bool = False,
    independent_trend: bool = False,
    counter_evidence_count: int = 0,
) -> tuple[str, str]:
    """Cap S/A grades when execution-gate evidence is missing.

    Research 11: S/A 评级要求 has_trade_plan / risk_ok / 高周期方向支持 /
    counter_evidence 上限。缺一就封顶 B（仍保留 trend_stage/momentum 信号体
    量，但显式不可执行）。Returns (clamped_grade, reason).
    """
    g = str(grade or "D").upper()
    if g not in {"S", "A"}:
        return g, ""
    blockers: list[str] = []
    if confidence is not None and float(confidence) < MIN_CONFIDENCE_FOR_PAPER_ORDER:
        blockers.append(f"置信度 {float(confidence):.2f} < {MIN_CONFIDENCE_FOR_PAPER_ORDER:.2f}")
    if not has_trade_plan:
        blockers.append("缺 trade_plan")
    if not risk_ok:
        blockers.append("risk_check 未通过")
    if htf_conflict and not independent_trend:
        blockers.append("高周期方向与 side 冲突且未通过 independent_trend")
    if counter_evidence_count >= SA_MAX_COUNTER_EVIDENCE:
        blockers.append(f"反向证据 {counter_evidence_count} >= {SA_MAX_COUNTER_EVIDENCE}")
    if blockers:
        reason = "S/A 评级降为 B：" + "；".join(blockers)
        return "B", reason
    return g, ""


def grade_delta(previous_grade: str | None, grade: str) -> str:
    """Stable render of grade change for reports/diagnostics."""
    if not previous_grade:
        return "-"
    p = GRADE_ORDER.get(str(previous_grade).upper(), 0)
    c = GRADE_ORDER.get(str(grade).upper(), 0)
    if c > p:
        return f"+{c - p}"
    if c < p:
        return f"{c - p}"
    return "0"


def alert_level_for_grade(grade: str | None) -> str:
    """Get alert level for grade."""
    normalized = str(grade or "").upper()
    if normalized in PUSH_GRADES:
        return "push"
    if normalized in WATCH_GRADES:
        return "watch"
    return "store_only"
