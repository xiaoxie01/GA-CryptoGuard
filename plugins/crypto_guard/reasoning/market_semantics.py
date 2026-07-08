"""Structured market-semantic normalization (Phase B, 07-03).

This module is the single source of truth for the bias+stage contract,
HTF-conflict confidence cap, and grade-downgrade rules. It is a
deterministic function over ``decision`` / ``snapshot`` dicts and a
``config`` mapping (the ``market_semantics`` segment of
trading_mode.yaml) — no DB, no LLM, no network. Callers:

- ``reasoning.ga_judge.run_ga_sop_decision`` calls ``normalize_market_semantics``
  after assembling the result dict, before schema validation.
- ``reasoning.market_state_builder.build_market_state_snapshot`` calls
  ``normalize_snapshot_semantics`` to correct snapshot-level
  ``market_bias`` / ``trend_stage`` and to surface ``timeframe_context``,
  ``alignment``, ``htf_conflict``, ``market_reason_codes``.

Design rules (see ``.trellis/tasks/07-03-hourly-analysis-semantic-accuracy/
design.md`` §4):

1. bias ∈ {neutral, mixed, unknown} ⇒ stage ∈ {range, transition, unknown}
2. 1D opposite to 1H/15M with 4H not confirming the low-TF direction ⇒
   alignment=countertrend_rebound, htf_conflict=True
3. htf_conflict=True ⇒ confidence capped below MIN_CONFIDENCE_FOR_PAPER_ORDER
4. htf_conflict=True ⇒ S/A grades downgraded one tier
5. data incomplete / time not trustworthy ⇒ fail-closed

The cap and downgrade map come from config, never hardcoded.
"""

from __future__ import annotations

from typing import Any

from plugins.crypto_guard.strategy.grade_config import (
    GRADE_ORDER,
    MIN_CONFIDENCE_FOR_PAPER_ORDER,
)

# Timeframes surfaced in the structured ``timeframe_context`` field. 5m is
# intentionally excluded — it is data-only per CryptoGuard convention #1.
TIMEFRAME_CONTEXT_TFS: tuple[str, ...] = ("1d", "4h", "1h", "15m")

# Legal bias + stage enums (mirrors ga_decision.schema.json).
_DIRECTIONAL_BIAS: frozenset[str] = frozenset({"bullish", "bearish"})
_NON_DIRECTIONAL_BIAS: frozenset[str] = frozenset({"neutral", "mixed", "unknown"})
_DIRECTIONAL_STAGE: frozenset[str] = frozenset({"early", "middle", "late"})
_LEGAL_STAGE: frozenset[str] = frozenset({
    "early", "middle", "late", "range", "transition", "unknown",
})


def compute_alignment(
    profiles: dict[str, Any] | None,
    timeframe_context: dict[str, Any] | None,
) -> tuple[str, bool]:
    """Compute multi-timeframe alignment and HTF-conflict flag.

    Returns ``(alignment, htf_conflict)``. ``alignment`` is one of
    ``aligned`` / ``partial`` / ``countertrend_rebound`` / ``neutral`` /
    ``unknown``. ``htf_conflict`` is True only when 1D opposes 1H/15M and 4H
    does not confirm the low-TF direction.

    Rules (design §4):
    - 1D bearish, 1H/15M bullish, 4H ∉ bullish → countertrend_rebound
    - 1D bullish, 1H/15M bearish, 4H ∉ bearish → countertrend_rebound
    - 1D same direction as 1H/15M, 4H same → aligned
    - 1D mixed/unknown → neutral (do not force a conflict)
    - otherwise → partial

    Profiles/contexts missing the 1D key fall back to ``unknown``.
    """
    ctx = timeframe_context or {}
    bias_1d = _tf_bias(ctx.get("1d") or (profiles or {}).get("1d") or {})
    bias_4h = _tf_bias(ctx.get("4h") or (profiles or {}).get("4h") or {})
    bias_1h = _tf_bias(ctx.get("1h") or (profiles or {}).get("1h") or {})
    bias_15m = _tf_bias(ctx.get("15m") or (profiles or {}).get("15m") or {})

    if not bias_1d or bias_1d in _NON_DIRECTIONAL_BIAS:
        return "neutral", False

    low_tf_biases = {bias_1h, bias_15m}
    low_tf_opposite = "bullish" if bias_1d == "bearish" else "bearish"
    low_tf_confirms_opposite = low_tf_opposite in low_tf_biases

    if low_tf_confirms_opposite:
        # 4H must confirm the low-TF direction to dissolve the conflict.
        if bias_4h != low_tf_opposite:
            return "countertrend_rebound", True
        # 4H confirms the low-TF rebound — partial (still not aligned with 1D).
        return "partial", False

    # Low TF not opposite to 1D. Check alignment.
    if bias_1h == bias_1d and bias_15m == bias_1d and bias_4h == bias_1d:
        return "aligned", False
    if bias_1h == bias_1d or bias_15m == bias_1d:
        return "partial", False
    return "partial", False


def _tf_bias(tf_obj: dict[str, Any]) -> str:
    """Read bias from a timeframe profile/context object.

    Prefers an explicit ``bias`` field (used by ``timeframe_context``);
    falls back to ``momentum`` (used by ``profiles``) when it carries a
    directional value. Returns "" when no directional signal is available.
    """
    if not isinstance(tf_obj, dict):
        return ""
    bias = str(tf_obj.get("bias") or "").lower()
    if bias in _DIRECTIONAL_BIAS or bias in _NON_DIRECTIONAL_BIAS:
        return bias
    mom = str(tf_obj.get("momentum") or "").lower()
    if mom in _DIRECTIONAL_BIAS:
        return mom
    return ""


def build_timeframe_context(
    profiles: dict[str, Any] | None,
    *,
    closed_candles_only: bool = True,
    analysis_degraded: bool = False,
    health_by_tf: dict[str, Any] | None = None,
    analysis_time_utc: int | None = None,
) -> dict[str, Any]:
    """Build the structured ``timeframe_context`` dict.

    For each TF in ``TIMEFRAME_CONTEXT_TFS``:
    - ``bias``: derived from ``momentum`` + ``market_structure``
      (bullish/bearish/neutral; ``unknown`` when degraded)
    - ``structure``: from ``market_structure`` (``unknown`` when degraded)
    - ``closed``: True only when the real candle boundary holds
    - ``close_time``: unix millis of the last closed candle (0 when unknown)

    R1-3 (07-03 final review): when ``health_by_tf`` and ``analysis_time_utc``
    are provided, ``closed`` is computed from the real candle boundary
    (``health.ready and last_close_time <= analysis_time_utc``) rather than
    the caller's ``closed_candles_only`` constant. When omitted, the legacy
    constant-based behavior is preserved with a structural warning.
    """
    profiles = profiles or {}
    health_by_tf = health_by_tf or {}
    # R1-3 (07-03 final review): real_closed_check requires a non-empty
    # health_by_tf dict AND an analysis_time_utc. An empty dict (default
    # when the snapshot has no health block) must NOT trigger the real
    # closed-check path — otherwise every TF gets closed=False because
    # health.last_close_time is missing, and R1-2 fail-closes every
    # decision. Fall back to the legacy constant-based behavior instead.
    real_closed_check = bool(health_by_tf) and analysis_time_utc is not None
    ctx: dict[str, Any] = {}
    for tf in TIMEFRAME_CONTEXT_TFS:
        profile = profiles.get(tf) or {}
        if analysis_degraded:
            ctx[tf] = {"bias": "unknown", "structure": "unknown", "closed": False, "close_time": 0}
            continue
        if real_closed_check:
            # R1-3: when health_by_tf is available, use the real candle
            # boundary check for EVERY required TF — including ones with
            # no profile (an absent profile with health.ready=False is
            # still a fail-closed signal).
            health = health_by_tf.get(tf) or {}
            last_close = int(health.get("last_close_time") or 0)
            ready = bool(health.get("ready"))
            closed = bool(ready and last_close > 0 and last_close <= int(analysis_time_utc or 0))
            if not profile:
                ctx[tf] = {
                    "bias": "unknown", "structure": "unknown",
                    "closed": closed, "close_time": last_close,
                }
                continue
            bias = _derive_tf_bias(profile)
            structure = str(profile.get("market_structure") or "unknown").lower()
            if structure not in _LEGAL_STAGE and structure not in {
                "bullish", "bearish", "range", "transition", "rebound", "downtrend", "uptrend", "unknown",
            }:
                structure = "unknown"
            ctx[tf] = {
                "bias": bias, "structure": structure,
                "closed": closed, "close_time": last_close,
            }
            continue
        if not profile:
            # R2-2 (07-03 final review P0): No health AND no profile means
            # the snapshot builder omitted this TF. Mark as ``closed=False``
            # so R1-2 fail-closed triggers. The previous behavior wrote
            # closed=True/close_time=0 which let bullish/middle/0.95 bypass
            # the data completeness gate — a direct violation of the
            # fail-closed contract.
            ctx[tf] = {"bias": "unknown", "structure": "unknown", "closed": False, "close_time": 0}
            continue
        bias = _derive_tf_bias(profile)
        structure = str(profile.get("market_structure") or "unknown").lower()
        if structure not in _LEGAL_STAGE and structure not in {
            "bullish", "bearish", "range", "transition", "rebound", "downtrend", "uptrend", "unknown",
        }:
            structure = "unknown"
        # R2-2 (07-03 final review P0): legacy constant-based path — when
        # health_by_tf is unavailable we cannot verify the real candle
        # boundary, so default to ``closed=False`` to trigger fail-closed.
        # The previous ``closed=bool(closed_candles_only)`` trusted a caller
        # constant, which LLM candidates could spoof.
        closed = False
        ctx[tf] = {
            "bias": bias, "structure": structure,
            "closed": closed, "close_time": 0,
        }
    return ctx


def _derive_tf_bias(profile: dict[str, Any]) -> str:
    """Derive a TF bias from momentum + market_structure.

    A directional structure (bullish/bearish) reinforced by same-direction
    momentum yields that direction. Opposing signals fall back to neutral.
    Mixed/unknown structures defer to momentum. This mirrors
    ``strategy_scorer.score_snapshot``'s bias logic at the per-TF level.
    """
    mom = str(profile.get("momentum") or "").lower()
    struct = str(profile.get("market_structure") or "unknown").lower()
    if struct in _DIRECTIONAL_BIAS:
        if mom == "neutral" or mom == "" or mom in _NON_DIRECTIONAL_BIAS:
            # structure says directional but momentum not confirming → mixed
            return "mixed"
        if mom == struct:
            return struct
        # structure and momentum disagree → mixed
        return "mixed"
    if struct == "transition":
        if mom in _DIRECTIONAL_BIAS:
            return mom
        return "mixed"
    if struct in {"range", "unknown"}:
        if mom in _DIRECTIONAL_BIAS:
            # range/unknown with directional momentum — keep momentum but
            # mark as mixed so it does not masquerade as a confirmed trend.
            return "mixed"
        return "neutral" if struct == "range" else "unknown"
    # Unknown structure — defer to momentum.
    if mom in _DIRECTIONAL_BIAS:
        return mom
    return "unknown"


def normalize_market_semantics(
    decision: dict[str, Any],
    snapshot: dict[str, Any] | None,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Normalize a GA decision dict and return the normalized copy.

    Executes the five-step contract from design §4:
    1. Validate timeframe_context closed status (fail-closed).
    2. Correct illegal market_bias + trend_stage combinations.
    3. Apply htf_conflict confidence cap.
    4. Apply htf_conflict grade-downgrade constraint.
    5. Emit structured ``market_reason_codes``.

    The function does NOT touch DB/LLM/network. It returns a shallow copy
    of ``decision`` (callers MUST assign the return value — ``result =
    normalize_market_semantics(result, snapshot, cfg)``). For ergonomics
    it ALSO surfaces ``timeframe_context`` / ``alignment`` / ``htf_conflict``
    onto ``snapshot`` when those fields were missing and had to be computed
    in Step 0; this mutation is intentional so downstream consumers reading
    the snapshot see the same structured fields as the decision. Callers
    that do not want their snapshot mutated should pass a copy.
    Config is the ``market_semantics`` segment of trading_mode.yaml
    (or {} for tests, which disables caps/downgrades).
    """
    result = dict(decision)
    snap = snapshot or {}
    cfg = config or {}
    reason_codes: list[str] = []

    # ── Step 0: ALWAYS recompute timeframe_context / alignment from snapshot ─
    # R2-1 (07-03 final review P0): NEVER trust candidate-supplied
    # timeframe_context / alignment / htf_conflict. LLM can spoof these to
    # bypass HTF gate. Recompute from snapshot.profiles + health_by_tf every
    # time and OVERWRITE any candidate values. The snapshot is built by
    # market_state_builder from real candle data; the candidate's copy is
    # untrusted.
    snap_dq = snap.get("data_quality") or {}
    snap_health = snap_dq.get("health_by_tf") or snap_dq.get("health") or {}
    snap_analysis_time = snap.get("analysis_time_utc")
    # analysis_time_utc may be int (millis) or ISO string. Normalize for
    # the build_timeframe_context call which expects int|None.
    if isinstance(snap_analysis_time, str):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(snap_analysis_time.replace("Z", "+00:00"))
            snap_analysis_time_int = int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            snap_analysis_time_int = None
    elif isinstance(snap_analysis_time, bool):
        snap_analysis_time_int = None
    elif isinstance(snap_analysis_time, int):
        snap_analysis_time_int = snap_analysis_time
    else:
        snap_analysis_time_int = None
    tf_ctx = build_timeframe_context(
        snap.get("profiles") or {},
        closed_candles_only=True,
        analysis_degraded=bool(snap.get("analysis_degraded")),
        health_by_tf=snap_health if snap_health else None,
        analysis_time_utc=snap_analysis_time_int,
    )
    result["timeframe_context"] = tf_ctx
    snap["timeframe_context"] = tf_ctx

    # alignment / htf_conflict: recompute from real tf_ctx, never trust
    # candidate values.
    alignment, htf_conflict = compute_alignment(snap.get("profiles") or {}, tf_ctx)
    result["alignment"] = alignment
    result["htf_conflict"] = htf_conflict
    snap["alignment"] = alignment
    snap["htf_conflict"] = htf_conflict

    # ── Step 1: timeframe_context closed status (fail-closed) ──────────────
    # R2-3 (07-03 final review P0): future-time leak check. JSON Schema
    # cannot express cross-field comparisons (close_time <= analysis_time_utc),
    # so enforce it at runtime. Any TF with close_time > analysis_time_utc
    # is treated as not-closed and triggers fail-closed.
    analysis_time_ms: int | None = None
    if isinstance(snap_analysis_time, int) and not isinstance(snap_analysis_time, bool):
        analysis_time_ms = snap_analysis_time
    elif isinstance(snap_analysis_time, str):
        # Already tried ISO parsing in Step 0; if that failed, treat as None.
        if snap_analysis_time_int is not None:
            analysis_time_ms = snap_analysis_time_int
    incomplete_tfs: list[str] = []
    for tf, ctx_t in tf_ctx.items():
        if not isinstance(ctx_t, dict) or ctx_t.get("closed") is not True:
            incomplete_tfs.append(tf)
            continue
        # Future-time leak check: close_time > analysis_time means the candle
        # closes AFTER analysis — a future leak. Treat as not-closed.
        close_time = ctx_t.get("close_time")
        if analysis_time_ms is not None and isinstance(close_time, int):
            if close_time > 0 and close_time > analysis_time_ms:
                incomplete_tfs.append(tf)
                # Mark the TF as not-closed so downstream consumers see the
                # corrected state in the persisted timeframe_context.
                ctx_t["closed"] = False
    if incomplete_tfs and not bool(snap.get("analysis_degraded")) and not bool(snap.get("partial_tf_mode")):
        # Only fail-closed when the snapshot is not already marked degraded.
        # A degraded snapshot has its own fail-closed path in market_state_builder.
        # Pass 7 P0: partial_tf_mode (shadow_test with <4 TFs) intentionally
        # omits some TFs — the loaded TFs have real, healthy candles, so
        # fail-closing would destroy real trade samples in historical_replay.
        reason_codes.append("data_incomplete")
        if bool(cfg.get("strict_timeframe_closed_check", True)):
            # R1-2 (07-03 final review): force the decision into a
            # non-executable state when any required TF is not closed.
            # This prevents bullish/middle/0.95 from creating paper orders
            # on stale/unclosed candle data.
            result["market_bias"] = "unknown"
            result["trend_stage"] = "unknown"
            try:
                cap = float(cfg.get("degraded_confidence_cap", 0.3))
            except (TypeError, ValueError):
                cap = 0.3
            try:
                conf = float(result.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf > cap:
                result["confidence"] = round(cap, 4)
            grade = str(result.get("signal_grade") or "D").upper()
            if GRADE_ORDER.get(grade, 0) > GRADE_ORDER.get("C", 0):
                result["signal_grade"] = "C"
            if result.get("decision") in {
                "trade_plan_available", "create_paper_order", "wait_for_pullback",
                "wait_for_breakout", "wait_for_reclaim", "avoid_chop",
            }:
                result["decision"] = "monitor_only"
            result["has_trade_plan"] = False
            result["trade_plan"] = None
            actions = result.get("suggested_actions") or []
            result["suggested_actions"] = [
                a for a in actions
                if a not in {"create_paper_order", "create_opportunity_watch"}
            ]
            if not result["suggested_actions"]:
                result["suggested_actions"] = ["add_to_watchlist", "ignore"]
            result["analysis_degraded"] = True
            result["degraded_reason"] = "required_timeframe_not_closed"
            result["risk_check"] = {
                "ok": False,
                "reasons": ["必需周期未收盘或缺失，数据降级"],
            }
            result["market_reason_codes"] = ["data_incomplete"]
            return result

    # ── Step 2a: htf_conflict detection + bias demotion ────────────────────
    # Detect HTF conflict first so the bias+stage contract (Step 2b) sees
    # the demoted non-directional bias. A countertrend rebound's directional
    # bias reflects the low-TF rebound, not the overall trend — demoting to
    # "mixed" keeps the report honest and lets the contract fix the stage.
    alignment = str(result.get("alignment") or snap.get("alignment") or "").lower()
    htf_conflict = bool(result.get("htf_conflict") if result.get("htf_conflict") is not None else snap.get("htf_conflict"))
    if alignment == "countertrend_rebound":
        htf_conflict = True
        result["alignment"] = "countertrend_rebound"
    result["htf_conflict"] = htf_conflict
    if not alignment:
        result["alignment"] = "neutral" if not htf_conflict else "countertrend_rebound"
    if htf_conflict:
        reason_codes.append("htf_conflict")
        if alignment == "countertrend_rebound":
            reason_codes.append("countertrend_rebound")
        # The directional bias (bullish/bearish) from score_snapshot reflects
        # the low-TF rebound, not the overall trend. When 1D opposes the
        # rebound and 4H does not confirm, the executable market_bias must be
        # non-directional so downstream reports/grades reflect "counter-trend
        # rebound, not a confirmed trend." See PRD FR-1/FR-2 and design §4.
        if str(result.get("market_bias") or "").lower() in _DIRECTIONAL_BIAS:
            result["market_bias"] = "mixed"

    # ── Step 2b: bias + stage contract ─────────────────────────────────────
    bias = str(result.get("market_bias") or "").lower()
    stage = str(result.get("trend_stage") or "").lower()
    bias_contract_active = bool(cfg.get("enforce_bias_stage_contract", True))
    if bias_contract_active and bias in _NON_DIRECTIONAL_BIAS:
        allowed = set(cfg.get(
            f"allowed_stages_for_{bias}_bias",
            ["range", "transition", "unknown"],
        ))
        if stage in _DIRECTIONAL_STAGE or (stage and stage not in allowed):
            # Illegal combination — demote to transition (preserves bias).
            reason_codes.append("bias_stage_contradiction")
            result["trend_stage"] = "transition"

    # ── Step 3: htf_conflict confidence cap ─────────────────────────────────
    if htf_conflict:
        cap_raw = cfg.get("htf_conflict_confidence_cap")
        try:
            cap = float(cap_raw) if cap_raw is not None else MIN_CONFIDENCE_FOR_PAPER_ORDER - 0.05
        except (TypeError, ValueError):
            cap = MIN_CONFIDENCE_FOR_PAPER_ORDER - 0.05
        # Clamp to [0, MIN_CONFIDENCE_FOR_PAPER_ORDER) defensively.
        if cap >= MIN_CONFIDENCE_FOR_PAPER_ORDER:
            cap = MIN_CONFIDENCE_FOR_PAPER_ORDER - 0.05
        try:
            conf = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf > cap:
            result["confidence"] = round(cap, 4)

    # ── Step 4: htf_conflict grade downgrade ───────────────────────────────
    if htf_conflict:
        grade = str(result.get("signal_grade") or "D").upper()
        # R1-1 / R2-4 (07-03 final review): idempotency — only apply the
        # downgrade once. A second normalize call (e.g. the risk-engine final
        # gate) must NOT further downgrade A→B. Track via the
        # ``htf_conflict_grade_downgraded`` reason code marker.
        #
        # CRITICAL: the marker must be preserved in BOTH the local
        # ``reason_codes`` list AND the persisted
        # ``result["market_reason_codes"]``. The dedupe step at the end
        # writes ``result["market_reason_codes"] = deduped(reason_codes)``.
        # If we skip appending the marker to ``reason_codes`` on the second
        # call (because already_downgraded=True), the marker would be lost
        # from the persisted list, and a THIRD call would see
        # already_downgraded=False and downgrade again (A→B). The reviewer
        # found this exact A→A→B pattern in fault injection.
        #
        # R3-1 (07-03 final review Pass 6 P1): the marker alone is NOT
        # sufficient proof that the current grade is downgraded. LLM can
        # restore the grade to S after the first downgrade while leaving
        # the marker in market_reason_codes. To detect restoration, we
        # store the original pre-downgrade grade in
        # ``_htf_conflict_original_grade`` on the first downgrade. A
        # subsequent call considers the grade "restored" only when the
        # current grade is HIGHER than the original pre-downgrade grade
        # (e.g. original S → downgraded A → LLM restores S). When the
        # current grade equals the downgraded result (A after S→A), no
        # further downgrade applies — A is the correct terminal grade.
        existing_reason_codes = result.get("market_reason_codes") or []
        already_downgraded = "htf_conflict_grade_downgraded" in (
            existing_reason_codes or reason_codes
        )
        downgrade_map = cfg.get("grade_downgrade_map") or {}
        original_grade_field = "_htf_conflict_original_grade"
        original_grade = result.get(original_grade_field)
        # Expected terminal grade after downgrade for the ORIGINAL grade.
        # If we have no original_grade on record, treat the current grade
        # as the original (first-call case).
        if original_grade is None:
            original_grade = grade
        expected_terminal_grade = downgrade_map.get(str(original_grade).upper())
        if expected_terminal_grade is None:
            # Original grade has no downgrade mapping (e.g. B/C/D) — no
            # downgrade applies. Marker should not be present.
            terminal_grade = grade
        else:
            terminal_grade = str(expected_terminal_grade).upper()
        # Detect restoration: current grade is HIGHER than the terminal
        # grade derived from the original. This means LLM (or any upstream)
        # restored the grade after the first downgrade.
        grade_was_restored = (
            already_downgraded
            and GRADE_ORDER.get(terminal_grade, 0) < GRADE_ORDER.get(grade, 0)
        )
        if not already_downgraded or grade_was_restored:
            # Apply (or re-apply) the downgrade. Record the original grade
            # so subsequent calls can detect restoration.
            if GRADE_ORDER.get(terminal_grade, 0) < GRADE_ORDER.get(grade, 0):
                result["signal_grade"] = terminal_grade
                reason_codes.append("htf_conflict_grade_downgraded")
                result[original_grade_field] = str(original_grade).upper()
        else:
            # Preserve the marker in the local list so it survives the
            # dedupe-rewrite of ``result["market_reason_codes"]``.
            reason_codes.append("htf_conflict_grade_downgraded")
        # If downgrade makes the decision non-executable — either because
        # the grade dropped below A OR because the confidence cap pushed
        # it below MIN_CONFIDENCE_FOR_PAPER_ORDER (S→A downgrade keeps the
        # grade in {S, A} but the cap may render it non-executable) —
        # collapse trade_plan/create_paper_order actions so downstream
        # consumers do not treat it as executable.
        try:
            capped_conf = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            capped_conf = 0.0
        non_executable = (
            result.get("signal_grade") not in {"S", "A"}
            or capped_conf < MIN_CONFIDENCE_FOR_PAPER_ORDER
        )
        if non_executable:
            if result.get("decision") in {"trade_plan_available", "create_paper_order"}:
                result["decision"] = "monitor_only"
            if result.get("has_trade_plan"):
                result["has_trade_plan"] = False
                result["trade_plan"] = None
            actions = result.get("suggested_actions") or []
            result["suggested_actions"] = [a for a in actions if a not in {"create_paper_order"}]
            if not result["suggested_actions"]:
                result["suggested_actions"] = ["add_to_watchlist", "ignore"]

    # ── Step 4b/4c/4d: HTF-alignment raw grade caps (Phase D, 07-07) ────────
    # Per design §7.1. Each cap is an independent grade downgrade applied to
    # the raw signal_grade BEFORE hysteresis / clamp_grade run in the
    # controller. Idempotency: the helper records the original pre-cap grade
    # in ``_htf_cap_original_grade`` on the first cap and never overwrites it
    # on subsequent calls, so repeated normalize calls do not double-downgrade.
    # Config flag ``htf_alignment_cap_enabled`` (default True) disables all
    # three caps for rollback (design §13.2).
    if bool(cfg.get("htf_alignment_cap_enabled", True)) and not bool(
        snap.get("analysis_degraded")
    ):
        _apply_htf_alignment_caps(result, snap)
        # Merge cap reason codes into the local reason_codes list so they
        # survive the final dedup-rewrite of result["market_reason_codes"].
        # _cap_grade writes directly to result["market_reason_codes"] for
        # immediate visibility; we re-read it here and append any new codes
        # to the local list so the dedup step at the end preserves them.
        for code in (result.get("market_reason_codes") or []):
            if code not in reason_codes:
                reason_codes.append(code)

    # ── Step 5: late + overextended追价风险 reason code ────────────────────
    if str(result.get("trend_stage") or "").lower() == "late":
        reason_codes.append("overextended")

    # De-duplicate reason codes while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for code in reason_codes:
        if code not in seen:
            seen.add(code)
            deduped.append(code)
    result["market_reason_codes"] = deduped
    return result


def _cap_grade(decision: dict[str, Any], *, max_grade: str, reason: str) -> bool:
    """Downgrade ``signal_grade`` to ``max_grade`` if it exceeds.

    Idempotent: the original pre-cap grade is recorded once in
    ``_htf_cap_original_grade`` and never overwritten on subsequent calls,
    so repeated normalize calls do not double-downgrade. The ``reason`` code
    is appended to ``decision["market_reason_codes"]`` (dedup-safe: only
    appends if not already present). Returns True if the grade was capped.
    """
    current = str(decision.get("signal_grade") or "D").upper()
    if current not in GRADE_ORDER:
        current = "D"
    target = str(max_grade or "D").upper()
    if target not in GRADE_ORDER:
        return False
    # Use GRADE_ORDER dict values (not .index()) — GRADE_ORDER is a dict.
    if GRADE_ORDER[current] > GRADE_ORDER[target]:
        # Record the original grade only on the first cap (idempotency).
        if not decision.get("_htf_cap_original_grade"):
            decision["_htf_cap_original_grade"] = current
        decision["signal_grade"] = target
        codes = list(decision.get("market_reason_codes") or [])
        if reason not in codes:
            codes.append(reason)
        decision["market_reason_codes"] = codes
        return True
    return False


def _apply_htf_alignment_caps(result: dict[str, Any], snap: dict[str, Any]) -> None:
    """Apply Step 4b/4c/4d HTF-alignment raw grade caps.

    Per design §7.1. Each cap is an independent grade downgrade:

    - Step 4b (htf_countertrend_cap): 1D AND 4H both opposite to candidate
      side → max B.
    - Step 4b (htf_4h_nondirectional_cap): 4H bias in {neutral, mixed,
      unknown, ""} → max B.
    - Step 4d (mtf_misalignment_cap): 1H AND 15M both not aligned with
      candidate side → max B.
    - Step 4d (low_tf_rebound_only_cap): only 5M supports the candidate
      side while 4H and 1H don't → max C, plus trend_stage remap
      early→transition and ``low_tf_rebound_only`` reason code.

    The candidate side is derived from ``market_bias``: bullish→LONG,
    bearish→SHORT. When market_bias is non-directional, no caps apply
    (nothing to be "opposite" to).

    5M bias is read from ``snapshot.profiles["5m"].momentum`` because 5M
    is excluded from ``TIMEFRAME_CONTEXT_TFS`` (data-only per convention).
    """
    ctx = result.get("timeframe_context") or {}
    bias_1d = str((ctx.get("1d") or {}).get("bias") or "").lower()
    bias_4h = str((ctx.get("4h") or {}).get("bias") or "").lower()
    bias_1h = str((ctx.get("1h") or {}).get("bias") or "").lower()
    bias_15m = str((ctx.get("15m") or {}).get("bias") or "").lower()
    profile_5m = (snap.get("profiles") or {}).get("5m") or {}
    bias_5m = str(profile_5m.get("momentum") or "").lower()

    # Surface 5M momentum on a top-level ``m5_bias`` field so downstream
    # consumers (diagnostics, reports) can read it without re-fetching
    # snapshot.profiles. The ga_decision.schema.json only allows
    # 1d/4h/1h/15m under timeframe_context (5M is data-only), so we expose
    # 5M bias as a separate top-level extension field. The top-level
    # schema object has no additionalProperties:false, so this is valid.
    if bias_5m:
        result["m5_bias"] = bias_5m

    market_bias = str(result.get("market_bias") or "").lower()
    if market_bias == "bullish":
        candidate_side = "LONG"
        candidate_side_lower = "bullish"
    elif market_bias == "bearish":
        candidate_side = "SHORT"
        candidate_side_lower = "bearish"
    else:
        # Non-directional bias: nothing to be "opposite" to. No caps apply.
        return

    opposite = "bearish" if candidate_side == "LONG" else "bullish"

    # Step 4b Cap 1: 1D and 4H both opposite to candidate → max B
    if bias_1d == opposite and bias_4h == opposite:
        _cap_grade(result, max_grade="B", reason="htf_countertrend_cap")

    # Step 4c: 4H non-directional (range/transition/mixed/unknown) → max B
    if bias_4h in ("", "neutral", "mixed", "unknown"):
        _cap_grade(result, max_grade="B", reason="htf_4h_nondirectional_cap")

    # Step 4d Cap 3: 1H and 15M both not aligned with candidate → max B
    if bias_1h != candidate_side_lower and bias_15m != candidate_side_lower:
        _cap_grade(result, max_grade="B", reason="mtf_misalignment_cap")

    # Step 4d Cap 4: only 5M supports, 4H and 1H don't → max C + trend_stage remap
    if (
        bias_5m == candidate_side_lower
        and bias_4h != candidate_side_lower
        and bias_1h != candidate_side_lower
    ):
        _cap_grade(result, max_grade="C", reason="low_tf_rebound_only_cap")
        # trend_stage remap: early → transition (no schema enum addition)
        if str(result.get("trend_stage") or "").lower() == "early":
            result["trend_stage"] = "transition"
            codes = list(result.get("market_reason_codes") or [])
            if "low_tf_rebound_only" not in codes:
                codes.append("low_tf_rebound_only")
            result["market_reason_codes"] = codes


def normalize_snapshot_semantics(
    snapshot: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    health_by_tf: dict[str, Any] | None = None,
    analysis_time_utc: int | None = None,
) -> dict[str, Any]:
    """Normalize a market_state_snapshot dict.

    Surfaces ``timeframe_context``, ``alignment``, ``htf_conflict`` and
    ``market_reason_codes`` on the snapshot, and corrects the top-level
    ``market_bias`` / ``trend_stage`` (which ``strategy_scorer`` reads) so
    downstream GA decisions inherit the corrected semantics.

    R1-3 (07-03 final review): when ``health_by_tf`` and ``analysis_time_utc``
    are provided, ``timeframe_context`` is built with real candle-boundary
    ``closed`` checks (no caller-constant spoofing).

    The snapshot is mutated in place and returned.
    """
    cfg = config or {}
    profiles = snapshot.get("profiles") or {}
    analysis_degraded = bool(snapshot.get("analysis_degraded"))

    # Build timeframe_context (fail-closed on degraded data).
    tf_ctx = build_timeframe_context(
        profiles,
        closed_candles_only=True,
        analysis_degraded=analysis_degraded,
        health_by_tf=health_by_tf,
        analysis_time_utc=analysis_time_utc,
    )
    snapshot["timeframe_context"] = tf_ctx

    # Compute alignment + htf_conflict.
    alignment, htf_conflict = compute_alignment(profiles, tf_ctx)
    snapshot["alignment"] = alignment
    snapshot["htf_conflict"] = htf_conflict

    reason_codes: list[str] = []
    if htf_conflict:
        reason_codes.append("htf_conflict")
        if alignment == "countertrend_rebound":
            reason_codes.append("countertrend_rebound")

    # Correct modules.market_bias + modules.trend_stage so strategy_scorer
    # reads normalized values. score_snapshot derives bias from PA/momentum;
    # we override at the modules level only when a contradiction is detected.
    modules = snapshot.get("modules") or {}
    bias = str(modules.get("market_bias") or "").lower()
    trend = modules.get("trend_stage") or {}
    stage = str(trend.get("trend_stage") or "").lower() if isinstance(trend, dict) else ""
    if cfg.get("enforce_bias_stage_contract", True) and bias in _NON_DIRECTIONAL_BIAS:
        allowed = set(cfg.get(
            f"allowed_stages_for_{bias}_bias",
            ["range", "transition", "unknown"],
        ))
        if stage in _DIRECTIONAL_STAGE or (stage and stage not in allowed):
            reason_codes.append("bias_stage_contradiction")
            if isinstance(trend, dict):
                trend["trend_stage"] = "transition"
                trend["stage"] = "transition"
                modules["trend_stage"] = trend

    # When HTF conflict is detected, force the snapshot market_bias to a
    # non-directional value so score_snapshot does not emit bullish/bearish
    # for a countertrend rebound. The 1D-directional rebound is still
    # visible via alignment/htf_conflict/timeframe_context.
    if htf_conflict and bias in _DIRECTIONAL_BIAS:
        modules["market_bias"] = "mixed"
        # Also demote the trend_stage so the bias+stage contract holds.
        if isinstance(modules.get("trend_stage"), dict):
            ts = modules["trend_stage"]
            ts_stage = str(ts.get("trend_stage") or "").lower()
            if ts_stage in _DIRECTIONAL_STAGE:
                ts["trend_stage"] = "transition"
                ts["stage"] = "transition"
                modules["trend_stage"] = ts

    snapshot["modules"] = modules

    if str(snapshot.get("trend_stage") or "").lower() == "late":
        reason_codes.append("overextended")

    # De-duplicate.
    seen: set[str] = set()
    deduped: list[str] = []
    for code in reason_codes:
        if code not in seen:
            seen.add(code)
            deduped.append(code)
    snapshot["market_reason_codes"] = deduped
    return snapshot
