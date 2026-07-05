"""Canonical deterministic market summary builder (Phase C, 07-03).

This module is the single source of truth for the ``final_summary`` /
``rendered_summary`` text that downstream consumers (hourly report, signal
policy, alert delivery, feishu action builder, report adapter) read. It is
a pure function over the final structured decision dict — no DB, no LLM, no
network — so Phase E diagnostics can call it for ``summary_structured_state_mismatch``
comparisons.

Design (see ``.trellis/tasks/07-03-hourly-analysis-semantic-accuracy/
design.md`` §5):

- ``final_summary`` becomes the canonical deterministic summary generated
  AFTER all gates and semantic normalization.
- ``rendered_summary`` is set to the same canonical text for compatibility.
- The original LLM text is preserved only in
  ``raw_decision_json["raw_llm_summary"]`` for audit; it never enters
  business decisions.

Output format:

    [grade] symbol · bias/stage · alignment · 原因

Examples:

- DOGE B-grade observation:
  ``[B] DOGEUSDT · 中性/转换 · 反趋势反弹 · 高周期冲突``
- SOL B-grade observation:
  ``[B] SOLUSDT · 偏多/中段 · 部分对齐 · 4H 偏热追价风险``
- A-grade executable:
  ``[A] SOLUSDT · 偏多/中段 · 已对齐 · 执行门禁通过``
- Non-executable (any grade):
  ``[观察] symbol · ... · 门禁原因``

Rules:

- Non-executable decisions are prefixed ``[观察]``.
- Executable decisions are prefixed ``[grade]``.
- Never contains ``FORBIDDEN_EXECUTABLE_PHRASES``.
- Never contradicts ``signal_grade``/``market_bias``/``trend_stage``.
- Reflects ``timeframe_context`` and ``alignment`` when present.
- Data-degraded decisions emit ``[grade] symbol · 方向不可靠 · 数据降级``.
"""

from __future__ import annotations

from typing import Any

from plugins.crypto_guard.notify.report_consistency import (
    FORBIDDEN_EXECUTABLE_PHRASES,
    execution_eligible,
)


# Mapping from market_bias enum to concise Chinese label.
_BIAS_LABELS: dict[str, str] = {
    "bullish": "偏多",
    "bearish": "偏空",
    "neutral": "中性",
    "mixed": "混合",
    "unknown": "未知",
}

# Mapping from trend_stage enum to concise Chinese label.
_STAGE_LABELS: dict[str, str] = {
    "early": "初期",
    "middle": "中段",
    "late": "后段",
    "range": "区间",
    "transition": "转换",
    "unknown": "未知",
}

# Mapping from alignment enum to concise Chinese label.
_ALIGNMENT_LABELS: dict[str, str] = {
    "aligned": "已对齐",
    "partial": "部分对齐",
    "countertrend_rebound": "反趋势反弹",
    "neutral": "中性",
    "unknown": "未知",
}

# Mapping from market_reason_codes to concise Chinese reason phrases.
_REASON_CODE_LABELS: dict[str, str] = {
    "htf_conflict": "高周期冲突",
    "countertrend_rebound": "反趋势反弹",
    "bias_stage_contradiction": "方向与阶段矛盾",
    "overextended": "追价风险",
    "data_incomplete": "数据不完整",
}


def build_canonical_market_summary(decision: dict[str, Any]) -> str:
    """Build the canonical deterministic summary for a GA decision.

    Pure function: reads only ``decision`` dict fields. No side effects.

    Args:
        decision: The final structured GA decision dict (after all gates and
            semantic normalization). Expected fields: ``symbol``,
            ``signal_grade``, ``market_bias``, ``trend_stage``, ``decision``,
            ``confidence``, ``timeframe_context``, ``alignment``,
            ``htf_conflict``, ``market_reason_codes``, ``risk_check``,
            ``trade_plan``.

    Returns:
        A deterministic Chinese summary string. Never empty. Never contains
        ``FORBIDDEN_EXECUTABLE_PHRASES``.
    """
    symbol = str(decision.get("symbol") or "")
    grade = str(decision.get("signal_grade") or "D").upper()
    bias = str(decision.get("market_bias") or "").lower()
    stage = str(decision.get("trend_stage") or "").lower()
    alignment = str(decision.get("alignment") or "").lower()
    htf_conflict = bool(decision.get("htf_conflict"))
    reason_codes = decision.get("market_reason_codes") or []
    if not isinstance(reason_codes, list):
        reason_codes = []
    risk = decision.get("risk_check") or {}
    if not isinstance(risk, dict):
        risk = {}
    plan = decision.get("trade_plan")
    tf_ctx = decision.get("timeframe_context") or {}
    if not isinstance(tf_ctx, dict):
        tf_ctx = {}

    # ── Data-degraded path ─────────────────────────────────────────────────
    # When the snapshot was marked analysis_degraded, the bias is forced to
    # "unknown" and the timeframe_context has closed=False entries. Emit a
    # short data-degraded summary so the report does not fabricate a bias.
    degraded = _is_data_degraded(decision)
    if degraded:
        prefix = "[观察]" if not execution_eligible(decision) else f"[{grade}]"
        summary = f"{prefix} {symbol} · 方向不可靠 · 数据降级"
        return _strip_forbidden(summary)

    # ── Build the directional / stage / alignment / reason segments ────────
    bias_label = _BIAS_LABELS.get(bias, bias or "未知")
    stage_label = _STAGE_LABELS.get(stage, stage or "未知")
    direction_segment = f"{bias_label}/{stage_label}"

    # Alignment segment: prefer the structured alignment field; fall back to
    # htf_conflict-derived label only when alignment is empty.
    if alignment:
        alignment_label = _ALIGNMENT_LABELS.get(alignment, alignment)
    elif htf_conflict:
        alignment_label = _ALIGNMENT_LABELS["countertrend_rebound"]
    else:
        alignment_label = ""

    # Reason segment: market reason codes first (HTF conflict, overextended,
    # etc.), then gate blockers when non-executable.
    market_reasons: list[str] = []
    for code in reason_codes:
        label = _REASON_CODE_LABELS.get(str(code))
        if label and label not in market_reasons:
            market_reasons.append(label)
    # When htf_conflict is True but no explicit reason code was emitted,
    # surface the conflict label so the summary is self-explanatory.
    if htf_conflict and "高周期冲突" not in market_reasons:
        market_reasons.append("高周期冲突")

    # If the alignment label already carries "反趋势反弹", remove the
    # duplicate label from market_reasons so it does not appear twice
    # (once in the alignment segment, once in the reason text).
    if alignment_label == "反趋势反弹":
        market_reasons = [r for r in market_reasons if r != "反趋势反弹"]

    # Timeframe-context detail: when 4H is range/transition or 1D is opposite,
    # add a concise qualifier so the report explains the HTF picture.
    tf_qualifier = _timeframe_context_qualifier(tf_ctx, bias, htf_conflict)
    if tf_qualifier and tf_qualifier not in market_reasons:
        market_reasons.append(tf_qualifier)

    gate_reasons: list[str] = []
    executable = execution_eligible(decision)
    if not executable:
        gate_reasons = _gate_blocker_labels(decision)

    # ── Compose the final summary ──────────────────────────────────────────
    if executable:
        prefix = f"[{grade}]"
        # Executable: direction · alignment · 执行门禁通过
        segments = [direction_segment]
        if alignment_label:
            segments.append(alignment_label)
        # Market reasons still appear (e.g. 追价风险) for transparency even
        # when executable, so the user sees the HTF context.
        reason_text = "；".join(market_reasons) if market_reasons else "执行门禁通过"
        summary = f"{prefix} {symbol} · {' · '.join(segments)} · {reason_text}"
    else:
        prefix = "[观察]"
        segments = [direction_segment]
        if alignment_label:
            segments.append(alignment_label)
        # Combine market reasons and gate reasons; market reasons first.
        all_reasons = market_reasons + gate_reasons
        # De-duplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for r in all_reasons:
            if r and r not in seen:
                seen.add(r)
                deduped.append(r)
        reason_text = "；".join(deduped) if deduped else "未通过执行门禁"
        summary = f"{prefix} {symbol} · {' · '.join(segments)} · {reason_text}"

    return _strip_forbidden(summary)


def _is_data_degraded(decision: dict[str, Any]) -> bool:
    """Return True when the decision was produced under data degradation."""
    # market_state_builder sets analysis_degraded=True on the snapshot; the
    # GA judge's _normalize_llm_decision forces market_bias=unknown,
    # trend_stage=unknown, confidence<=0.3, decision=monitor_only when
    # degraded. We detect this via the market_bias=unknown + a
    # degraded_reason marker, or via timeframe_context closed=False entries.
    bias = str(decision.get("market_bias") or "").lower()
    stage = str(decision.get("trend_stage") or "").lower()
    tf_ctx = decision.get("timeframe_context") or {}
    if not isinstance(tf_ctx, dict):
        tf_ctx = {}
    # Heuristic 1: explicit degraded marker (set by llm_agent_judge).
    if any(
        "降级" in str(note) or "degraded" in str(note).lower()
        for note in (decision.get("risk_notes") or [])
    ):
        return True
    # Heuristic 2: all TF entries closed=False with bias=unknown.
    if bias == "unknown" and tf_ctx:
        all_closed_false = all(
            isinstance(v, dict) and v.get("closed") is False
            for v in tf_ctx.values()
        )
        if all_closed_false:
            return True
    # Heuristic 3: bias=unknown + stage=unknown + confidence<=0.3.
    try:
        conf = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if bias == "unknown" and stage == "unknown" and conf <= 0.3:
        return True
    return False


def _timeframe_context_qualifier(
    tf_ctx: dict[str, Any], bias: str, htf_conflict: bool,
) -> str:
    """Build a concise qualifier describing the 4H/1D picture.

    Returns "" when no qualifier is warranted (e.g. aligned bullish trend).
    """
    if not tf_ctx:
        return ""
    tf_4h = tf_ctx.get("4h") or {}
    tf_1d = tf_ctx.get("1d") or {}
    if not isinstance(tf_4h, dict):
        tf_4h = {}
    if not isinstance(tf_1d, dict):
        tf_1d = {}
    bias_4h = str(tf_4h.get("bias") or "").lower()
    bias_1d = str(tf_1d.get("bias") or "").lower()
    struct_4h = str(tf_4h.get("structure") or "").lower()
    struct_1d = str(tf_1d.get("structure") or "").lower()

    # 4H 偏热: 4H bullish but overheated (structure=bullish + RSI high is
    # captured by the overextended reason code; here we surface 4H structure
    # when it is range/transition, which indicates lack of confirmation).
    if struct_4h in {"range", "transition", "unknown"} and bias_4h not in {"bullish", "bearish"}:
        if htf_conflict:
            return "4H 未同向确认"
        return "4H 震荡"

    # 1D opposite to low-TF direction.
    if htf_conflict and bias_1d in {"bullish", "bearish"}:
        # R1-4 (07-03 final review): surface the 1D direction itself, not
        # the opposite. When 1D is bullish and HTF conflict is detected
        # (low-TF rebounding against 1D), the qualifier must say "日线偏多"
        # so the report reader sees the 1D trend direction. The previous
        # code returned the opposite direction, which contradicted the
        # structured 1D bias.
        direction = "偏多" if bias_1d == "bullish" else "偏空"
        return f"日线{direction}"

    return ""


def _gate_blocker_labels(decision: dict[str, Any]) -> list[str]:
    """Return concise Chinese labels for the failing execution gates."""
    from plugins.crypto_guard.strategy.grade_config import MIN_CONFIDENCE_FOR_PAPER_ORDER
    from plugins.crypto_guard.notify.report_consistency import is_valid_trade_plan
    blockers: list[str] = []
    grade = str(decision.get("signal_grade") or "").upper()
    if grade not in {"S", "A", "B"}:
        blockers.append(f"等级 {grade or '?'} 非可执行")
    plan = decision.get("trade_plan")
    plan_ok = is_valid_trade_plan(plan)
    if not plan_ok:
        blockers.append("缺交易计划")
    risk = decision.get("risk_check") or {}
    if not (isinstance(risk, dict) and risk.get("ok")):
        blockers.append("风控未通过")
    try:
        conf = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < MIN_CONFIDENCE_FOR_PAPER_ORDER:
        blockers.append(f"置信度 {conf:.2f}")
    decision_name = str(decision.get("decision") or "")
    if decision_name not in {"create_paper_order", "trade_plan_available"}:
        blockers.append("决策非可执行")
    return blockers


def _strip_forbidden(text: str) -> str:
    """Defensive: ensure no FORBIDDEN_EXECUTABLE_PHRASES leak into the
    canonical summary. The canonical builder never introduces them, but
    this guard prevents regressions if a future caller passes through LLM
    text by mistake."""
    cleaned = text
    for phrase in FORBIDDEN_EXECUTABLE_PHRASES:
        if phrase in cleaned:
            cleaned = cleaned.replace(phrase, "")
    # Collapse any double spaces or trailing separators left by stripping.
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    cleaned = cleaned.strip(" ·；;，,。.")
    if cleaned and not cleaned.endswith(("。", "·", "；")):
        # Keep the summary as-is — it already ends with a reason phrase.
        pass
    return cleaned or "[观察] 未知"
