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


def alert_level_for_grade(grade: str | None) -> str:
    """Get alert level for grade."""
    normalized = str(grade or "").upper()
    if normalized in PUSH_GRADES:
        return "push"
    if normalized in WATCH_GRADES:
        return "watch"
    return "store_only"
