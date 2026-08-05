from __future__ import annotations

import copy
import json
import math
import multiprocessing as _mp_mod
import os
import re
import time
from typing import Any, Callable

import jsonschema

from plugins.crypto_guard.reasoning.decision_schema import (
    load_schema,
    normalize_entry_trigger_confirmation,
    normalize_suggested_actions,
    validate_json,
    validate_json_detail,
)
from plugins.crypto_guard.reasoning.ga_judge import run_ga_sop_decision
from plugins.crypto_guard.reasoning.watch_conditions import (
    is_structured_watch,
    normalize_opportunity_watch,
)
from plugins.crypto_guard.risk.risk_engine import apply_risk_to_decision
from plugins.crypto_guard.strategy.strategy_scorer import score_snapshot
from plugins.crypto_guard.utils import _strict_positive_int_ms


# 07-10 R5: sentinel distinguishing "preset key absent" from "preset value is
# None" (a None candidate is a legitimate fair-batch terminal failure that must
# route to the fail-closed fallback, NOT be treated as "no preset supplied").
_SENTINEL: Any = object()


SYSTEM_PROMPT = """你是 GA CryptoGuard 的市场研究 Agent。
你必须基于结构化模块证据做多周期 SOP 研判，而不是凭空预测。
边界：禁止实盘交易建议，禁止真实下单，只允许输出模拟盘/机会监控/观察/忽略相关决策。
只输出一个符合 GADecision schema 的 JSON 对象，不要 Markdown，不要额外解释。
"""

SYSTEM_PROMPT_STRICT_JSON = """你是 GA CryptoGuard 的市场研究 Agent。
只输出一个符合 GADecision schema 的 JSON 对象。
禁止 Markdown。禁止代码块。禁止自然语言解释。禁止前导文字。
第一个字符必须是 {。最后一个字符必须是 }。
"""

# ---------------------------------------------------------------------------
# 08-04 contract D (PRD): per-task system prompts for every
# ``run_agent_json_task`` task_name. The market-decision path keeps
# ``SYSTEM_PROMPT`` / ``SYSTEM_PROMPT_STRICT_JSON`` (GADecision marker) and is
# intentionally NOT part of this map — generic SOP tasks must never inherit the
# market-decision framing. ``build_agent_json_task_prompt`` returns the JSON
# body ALONE (the user-message text); ``run_agent_json_task`` stashes the
# per-task prompt as the thread-local ``system_override`` and ``_call_ga_llm``
# routes it into ``session.system`` (and forwards it to the subprocess child),
# so the user message never repeats the system text (reviewer round-2 G1/P1-1).
# ---------------------------------------------------------------------------
TASK_SYSTEM_PROMPTS: dict[str, str] = {
    "historical_replay_backtest_analysis": """你是 GA CryptoGuard 的历史回放/回测分析 Agent。
基于确定性回放统计（stats、strategy_comparison、no_lookahead、regime_distribution）评估行情状态、策略版本表现、过拟合风险与下一步 shadow/candidate 建议。
只输出一个符合本任务 schema 的 JSON 对象，禁止 Markdown，禁止实盘交易建议。""",
    "daily_paper_review_summary": """你是 GA CryptoGuard 的模拟盘日报总结 Agent。
基于昨日闭仓交易、复盘条目、策略记忆与演化聚合，输出适合直接推送飞书的模拟盘表现总结。
只输出一个符合本任务 schema 的 JSON 对象（summary_text 为摘要正文），禁止 Markdown，禁止实盘交易建议。""",
    "trade_review_attribution": """你是 GA CryptoGuard 的交易归因复盘 Agent。
基于单笔模拟盘交易与确定性快照上下文，判断亏损/盈利来自方向、入场、趋势阶段、反向证据、执行质量还是止盈止损设计。
只输出一个符合 trade_review schema 的 JSON 对象，禁止 Markdown，禁止实盘交易建议；策略补丁只能进入 candidate。""",
    "opportunity_watch_review": """你是 GA CryptoGuard 的机会监控复核 Agent。
复核机会监控条件是否真的值得提醒，解释触发/失效/继续等待原因。
只输出一个符合本任务 schema 的 JSON 对象，禁止 Markdown，只允许观察/提醒/失效等模拟盘研究建议。""",
    "higher_timeframe_kline_summary": """你是 GA CryptoGuard 的高周期 K 线总结 Agent。
基于已收盘高周期 K 线背景（compact_kline）提取趋势状态、关键区域与风险，供低周期巡航复用。
只输出一个符合本任务 schema 的 JSON 对象，禁止 Markdown，禁止未来函数，禁止实盘交易建议。""",
    "hourly_alert_quality_brief": """你是 GA CryptoGuard 的小时简报 Agent。
总结本小时各产品趋势状态、为什么有/没有机会、下一小时应重点观察什么，summary 字段适合放在简报顶部。
只输出一个符合本任务 schema 的 JSON 对象，禁止 Markdown，禁止实盘交易建议。""",
    "paper_execution_quality_update": """你是 GA CryptoGuard 的模拟盘执行质量 Agent。
总结模拟盘成交、止盈止损、MFE/MAE、回撤与执行质量。
只输出一个符合本任务 schema 的 JSON 对象，禁止 Markdown，只允许模拟盘/复盘建议，禁止实盘下单建议。""",
    "strategy_version_management_summary": """你是 GA CryptoGuard 的策略版本管理 Agent。
总结 active/candidate/deprecated 策略版本状态、风险与下一步 shadow/review 动作。
只输出一个符合本任务 schema 的 JSON 对象，禁止 Markdown，不得绕过 candidate/shadow 流程。""",
    "candidate_strategy_config_review": """你是 GA CryptoGuard 的候选策略配置复核 Agent。
复核候选策略配置是否保守、是否需要补充风控说明。
只输出一个符合本任务 schema 的 JSON 对象，禁止 Markdown，只能补充说明字段，不能将 candidate 改为 active。""",
    "shadow_test_strategy_verdict": """你是 GA CryptoGuard 的影子测试复核 Agent。
复核影子测试结果，判断候选策略是否样本不足、拒绝、或可进入人工确认升级；必须保守处理过拟合风险。
只输出一个符合本任务 schema 的 JSON 对象（仅 explanation 与 notes 字段），禁止 Markdown；不能绕过人工确认或配置门禁。""",
    "self_evolution_candidate_patch": """你是 GA CryptoGuard 的自进化候选补丁 Agent。
基于复盘聚合提出策略 candidate patch；必须避免单品种过拟合；只能输出 candidate patch，不能直接 active。
只输出一个符合本任务 schema 的 JSON 对象，禁止 Markdown；patch 字段为空表示当前不应生成补丁。""",
}

# Every generic task has a per-task schema under ``schemas/<name>.schema.json``
# with root ``additionalProperties: false``. ``trade_review_attribution`` reuses
# the existing ``trade_review.schema.json`` (the reviewer additionally
# re-validates the merged result at its call site). ``run_agent_json_task``
# resolves the schema when the caller did not pass one explicitly.
TASK_SCHEMAS: dict[str, str] = {
    "historical_replay_backtest_analysis": "historical_replay_backtest_analysis.schema.json",
    "daily_paper_review_summary": "daily_paper_review_summary.schema.json",
    "trade_review_attribution": "trade_review.schema.json",
    "opportunity_watch_review": "opportunity_watch_review.schema.json",
    "higher_timeframe_kline_summary": "higher_timeframe_kline_summary.schema.json",
    "hourly_alert_quality_brief": "hourly_alert_quality_brief.schema.json",
    "paper_execution_quality_update": "paper_execution_quality_update.schema.json",
    "strategy_version_management_summary": "strategy_version_management_summary.schema.json",
    "candidate_strategy_config_review": "candidate_strategy_config_review.schema.json",
    "shadow_test_strategy_verdict": "shadow_test_strategy_verdict.schema.json",
    "self_evolution_candidate_patch": "self_evolution_candidate_patch.schema.json",
}


def _semantic_self_evolution_candidate_patch(result: dict[str, Any]) -> tuple[bool, str | None]:
    """D3 semantic hook: ``needs_patch=True`` without a dict ``patch`` is
    schema-valid but semantically inconsistent (a self-evolution run that wants
    a patch must actually supply one). Fail closed to the deterministic
    fallback."""
    if bool(result.get("needs_patch")) and not isinstance(result.get("patch"), dict):
        return False, "needs_patch=True 但 patch 缺失或非对象，语义不一致"
    return True, None


# Per-task semantic validation hooks applied to the MERGED result (fallback +
# LLM candidate) after schema validation. Absent entries mean no extra hook.
TASK_SEMANTIC_VALIDATORS: dict[str, Callable[[dict[str, Any]], tuple[bool, str | None]]] = {
    "self_evolution_candidate_patch": _semantic_self_evolution_candidate_patch,
}


# LLM error taxonomy (design §3.1, §3.2)
LLM_ERROR_CATEGORIES = (
    "llm_config_error",
    "llm_transport_error",
    "llm_rate_limited",
    "llm_empty_response",
    "llm_json_parse_failed",
    "llm_schema_validation_failed",
    "llm_semantic_validation_failed",
    # 07-09-overtrigger follow-up: model produced stop_reason=tool_use with
    # no assistant text. Distinct from gateway empty (no HTTP response at
    # all) so the operator can remediate prompt/tool exposure vs. paging
    # the gateway on-call. See ``_classify_llm_failure`` for the detection
    # rule.
    "llm_tool_call_no_text",
    # 07-10 S4 (P0 #3): the process-isolation hard timeout killed a wedged
    # provider call (the child outlived ``proc.join(timeout=)`` and was
    # terminate/killed). This is a NON-retryable, terminal ``symbol_timeout``:
    # the provider was already wedged for the full provider-timeout window,
    # so retrying would burn all attempts on a known-bad provider and still
    # miss the symbol deadline. Distinct from ``llm_transport_error`` (a
    # single transient gateway timeout that may recover on retry) so the
    # coordinator stops at attempt 1 and Phase E accounting counts it as a
    # symbol-timeout, not a retry-exhaustion.
    "llm_subprocess_hard_timeout",
    # 07-10 R4-P0-2 (terminal-review-repair-plan-r4): subprocess FATAL errors
    # that MUST NOT be retried (retrying would spawn a NEW child while the
    # OLD unreaped child is still leaking resources / the response contract
    # was already violated / the subprocess runtime cannot start). Each is
    # terminal non-retryable; ``cleanup_failed`` in particular MUST stop the
    # symbol at attempt 1 (attempt_count=1) so the coordinator does not amplify
    # orphan processes. See ``_run_single_llm_attempt`` for the signature
    # detection (BEFORE ``_classify_llm_failure`` which would otherwise route
    # these RuntimeErrors to ``llm_transport_error`` / retryable).
    "llm_subprocess_cleanup_failed",
    "llm_subprocess_response_oversized",
    "llm_subprocess_start_failed",
    # 07-13 R6-D (P0-3.5 / §7.9): the model produced content but ran out of
    # output budget mid-JSON (stop_reason=max_tokens). A DISTINCT terminal
    # reason from a generic parse failure and from infrastructure errors so
    # it cannot open the breaker (see ``_BREAKER_INFRA_REASONS``). A truncated
    # Attempt 1 is retryable: a strict/minimal JSON retry inside the same
    # per-symbol deadline often fits the budget (P0-3.6).
    "llm_output_truncated",
)

LLM_ERROR_STAGES = ("call", "parse", "schema", "semantic", "retry_exhausted")

# Retryable categories (design §4.1.2). ``llm_tool_call_no_text`` is retryable:
# a tool-call-only turn typically recovers on a strict-JSON retry that
# forbids tools (see ``_call_ga_llm`` post-07-09-overtrigger - no placeholder
# tool is injected for JSON-only market-decision prompts).
# 07-13 R6-D: ``llm_output_truncated`` is retryable -- a strict/minimal JSON
# retry (smaller prompt / no optional sections) often fits the output budget
# inside the same per-symbol deadline (P0-3.6).
_RETRYABLE_CATEGORIES = frozenset({
    "llm_transport_error",
    "llm_rate_limited",
    "llm_empty_response",
    "llm_json_parse_failed",
    "llm_tool_call_no_text",
    "llm_output_truncated",
})
_NON_RETRYABLE_CATEGORIES = frozenset({
    "llm_config_error",
    "llm_schema_validation_failed",
    "llm_semantic_validation_failed",
    # 07-10 S4 (P0 #3): a subprocess hard-killed provider call is terminal
    # ``symbol_timeout`` - the provider was wedged for the full provider-timeout
    # window, so retrying burns attempts on a known-bad provider.
    "llm_subprocess_hard_timeout",
    # 07-10 R4-P0-2: subprocess FATAL errors are non-retryable - retrying
    # would amplify orphan processes (cleanup_failed), the response contract
    # was already violated (response_oversized), or the runtime cannot start
    # (start_failed). See LLM_ERROR_CATEGORIES for the per-reason rationale.
    "llm_subprocess_cleanup_failed",
    "llm_subprocess_response_oversized",
    "llm_subprocess_start_failed",
})


def run_agent_sop_decision(snapshot: dict[str, Any], *, use_llm: bool | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the LLM/GA SOP decision path, falling back to deterministic SOP if needed.

    Phase B (07-07): integrated with circuit breaker, bounded retry, and
    wall-clock budget. The breaker is obtained from context["llm_breaker"]
    (set by the controller per-batch). When absent, a _NullBreaker is used
    (backward compat for tests and non-controller callers).
    """

    fallback = run_ga_sop_decision(snapshot)
    if use_llm is None:
        use_llm = os.environ.get("CRYPTO_GUARD_LLM_ANALYSIS", "1").lower() not in {"0", "false", "no"}
    if not use_llm:
        fallback["analysis_source"] = "deterministic_sop"
        fallback["llm_status"] = "disabled"
        # Phase D §8: carry the metadata envelope on the disabled path too,
        # so every decision row (success, failure, disabled, breaker-skip)
        # has the same field shape for diagnostics. No provider call here.
        fallback["llm_attempt_count"] = 0
        fallback["llm_provider_call_count"] = 0
        fallback["llm_latency_ms"] = 0
        fallback["llm_prompt_bytes"] = None
        fallback["llm_continuity_included"] = None
        fallback["llm_model"] = None
        fallback["llm_terminal_reason"] = "llm_disabled"
        _sched_ctx_d = context or {}
        fallback["llm_schedule_round"] = _sched_ctx_d.get("schedule_round")
        fallback["llm_schedule_position"] = _sched_ctx_d.get("schedule_position")
        fallback["plan_origin"] = "deterministic_sop"
        fallback["plan_execution_state"] = "no_candidate" if not fallback.get("has_trade_plan") else "confirmed"
        fallback["llm_fallback_reason"] = "llm_disabled"
        return apply_risk_to_decision(fallback, snapshot)

    # Resolve breaker from context (controller wires it per-batch)
    from plugins.crypto_guard.reasoning.llm_breaker import _NullBreaker
    breaker = (context or {}).get("llm_breaker") or _NullBreaker()

    # 07-31 P0-1 (production batch 15m:1785487499999): fair-batch preset
    # injection is consumed BEFORE the breaker gate. The fair coordinator
    # has ALREADY run the provider call via ``fair_llm_call_adapter`` (one
    # call, no inner retry wrapper — directive #2) and hands the produced
    # candidate + §8 attempt_meta here through ``context``. Pre-fix the
    # breaker gate ran FIRST, so 5 schema failures (P1-1/P1-2 emissions)
    # opened the breaker and every symbol's preset candidate was DISCARDED —
    # all 10 persisted rows became breaker_skipped with provider_call_count=0,
    # destroying 8 coordinator successes. The controller path must NOT
    # re-call the provider, re-record the breaker, or double-count; the
    # coordinator owns those records for the fair path.
    preset_candidate = (context or {}).get("llm_preset_candidate", _SENTINEL)
    if preset_candidate is not _SENTINEL:
        attempt_meta = (context or {}).get("llm_preset_attempt_meta") or {}
        candidate = preset_candidate
        if candidate is not None:
            # The fair adapter's ``_run_single_llm_attempt`` already normalized
            # + validated the candidate and set plan_origin / plan_execution_state.
            # The coordinator's outcome survives even an open breaker — it was
            # already produced and recorded before this call.
            return apply_risk_to_decision(candidate, snapshot)
        # Preset candidate is None -> the fair batch's terminal outcome for
        # this symbol was a failure/skip (symbol_timeout / schema fail /
        # breaker / budget). Fail closed to the deterministic fallback with
        # the fair batch's attempt_meta so the §8 envelope is accurate —
        # the coordinator's REAL terminal reason is preserved, NOT
        # overwritten with breaker_skipped just because the breaker is open.
        fallback["analysis_source"] = "deterministic_fallback"
        fallback["llm_status"] = "failed"
        fallback.update(attempt_meta)
        fallback["plan_origin"] = "deterministic_fallback"
        fallback["plan_execution_state"] = "unconfirmed"
        notes = list(fallback.get("risk_notes") or [])
        _tr = attempt_meta.get("llm_terminal_reason")
        if _tr == "breaker_skipped":
            notes.append("LLM/GA 研判失败（熔断），本次使用规则 SOP 降级结果。")
        elif _tr == "prompt_budget_contract_violation":
            notes.append("LLM/GA 研判失败（提示词预算超限），本次使用规则 SOP 降级结果。")
        elif _tr == "llm_schema_validation_failed":
            notes.append("LLM/GA 研判失败（schema 校验未通过），本次使用规则 SOP 降级结果。")
        elif _tr in ("symbol_timeout", "batch_deadline_skipped"):
            notes.append("LLM/GA 研判失败（时间预算耗尽），本次使用规则 SOP 降级结果。")
        elif _tr == "single_flight_skipped":
            # 07-10 P1-2: the fair coordinator skipped this symbol because an
            # overlapping tick (cross-batch single-flight) is still analyzing
            # it - a legitimate dedup, NOT a failure. The note must NOT claim
            # "研判失败" (that would mislead the operator).
            notes.append("LLM/GA 跳过：同品种跨批次分析仍在进行（single-flight），本次使用规则 SOP 结果。")
        elif _tr == "missing_snapshot":
            # 07-10 P1-2: defensive policy skip (no market snapshot). Also a
            # legit upstream skip, not a failure.
            notes.append("LLM/GA 跳过：缺少市场快照（missing_snapshot），本次使用规则 SOP 结果。")
        else:
            notes.append("LLM/GA 研判失败，本次使用规则 SOP 降级结果。")
        fallback["risk_notes"] = notes
        return apply_risk_to_decision(fallback, snapshot)

    # Legacy path (NO preset in context): check breaker state before
    # attempting LLM. This stays breaker-gated exactly as before — the
    # P0-1 reorder only covers the fair-coordinator preset path.
    if not breaker.should_call():
        breaker.record_skip()
        fallback["analysis_source"] = "deterministic_fallback"
        # Phase D §8: full per-decision metadata envelope even on the
        # pre-call breaker skip path. No provider call was made, so
        # llm_provider_call_count=0 and llm_latency_ms=0. The terminal
        # reason is the exact structured value (design §9), never generic.
        fallback["llm_status"] = "failed"
        fallback["llm_error_category"] = None  # no call was made
        fallback["llm_error_stage"] = None
        fallback["llm_error"] = "circuit breaker open; LLM call skipped"
        fallback["llm_fallback_reason"] = "circuit_breaker_open"
        fallback["llm_attempt_count"] = 0
        fallback["llm_provider_call_count"] = 0
        fallback["llm_latency_ms"] = 0
        fallback["llm_prompt_bytes"] = None
        fallback["llm_continuity_included"] = None
        fallback["llm_model"] = None
        fallback["llm_terminal_reason"] = "breaker_skipped"
        _sched_ctx = context or {}
        fallback["llm_schedule_round"] = _sched_ctx.get("schedule_round")
        fallback["llm_schedule_position"] = _sched_ctx.get("schedule_position")
        fallback["plan_origin"] = "deterministic_fallback"
        fallback["plan_execution_state"] = "unconfirmed"
        notes = list(fallback.get("risk_notes") or [])
        notes.append("LLM/GA 研判失败（熔断），本次使用规则 SOP 降级结果。")
        fallback["risk_notes"] = notes
        return apply_risk_to_decision(fallback, snapshot)

    # Attempt LLM with retry wrapper
    candidate, attempt_meta = _call_ga_llm_with_retry(
        snapshot=snapshot,
        fallback=fallback,
        context=context,
        breaker=breaker,
        prompt_builders=(build_llm_decision_prompt, build_llm_strict_json_prompt, build_llm_minimal_safe_prompt),
    )

    if candidate is None:
        # Fail-closed: use deterministic fallback with attempt metadata.
        # 07-10 R1-1: the retry wrapper now owns ALL breaker.record_attempt
        # calls (the single-attempt unit surfaces the outcome, the wrapper
        # emits the physical/repairable/non-retryable events). Do NOT
        # re-record here - that would double-count failed attempts.
        fallback["analysis_source"] = "deterministic_fallback"
        fallback["llm_status"] = "failed"
        fallback.update(attempt_meta)
        fallback["plan_origin"] = "deterministic_fallback"
        fallback["plan_execution_state"] = "unconfirmed"
        notes = list(fallback.get("risk_notes") or [])
        # Tailor the risk note to the exact terminal reason so the report
        # labels match the failure class (budget skip / breaker / schema /
        # retry-exhausted), not a generic parse-failure blanket.
        _tr = attempt_meta.get("llm_terminal_reason")
        if _tr == "breaker_skipped":
            notes.append("LLM/GA 研判失败（熔断），本次使用规则 SOP 降级结果。")
        elif _tr == "prompt_budget_contract_violation":
            notes.append("LLM/GA 研判失败（提示词预算超限），本次使用规则 SOP 降级结果。")
        elif _tr == "llm_schema_validation_failed":
            notes.append("LLM/GA 研判失败（schema 校验未通过），本次使用规则 SOP 降级结果。")
        elif _tr in ("symbol_timeout", "batch_deadline_skipped"):
            notes.append("LLM/GA 研判失败（时间预算耗尽），本次使用规则 SOP 降级结果。")
        else:
            notes.append("LLM/GA 研判失败，本次使用规则 SOP 降级结果。")
        fallback["risk_notes"] = notes
        return apply_risk_to_decision(fallback, snapshot)

    # LLM returned a SUCCESS candidate. 07-10 R1-1: the single-attempt unit
    # (``_run_single_llm_attempt``, invoked via ``_call_ga_llm_with_retry``)
    # now performs the unwrap + schema validation + schema-alias repair and
    # returns a normalized, validated decision with the §8 envelope merged
    # and ``plan_origin="llm_confirmed"`` / ``plan_execution_state="confirmed"``
    # set. The retry wrapper already emitted the breaker events (physical-ok
    # + repairable for a repaired success, one physical-ok for a plain
    # success). So this function's job is ONLY the final risk-gate pass —
    # do NOT re-unwrap, re-validate, or re-record the breaker here.
    return apply_risk_to_decision(candidate, snapshot)


# 07-31 P1-1: the schema's flat string enum for ``decision`` (must NEVER be
# loosened - requirement D). Shared by the decision-array repair and the
# P1-3 prompt type-contract (schema_contract.decision). Tuple keeps the
# canonical order for the prompt enum.
_LEGAL_LLM_DECISIONS = (
    "trade_plan_available", "wait_for_pullback", "wait_for_breakout",
    "wait_for_reclaim", "avoid_chop", "no_edge", "monitor_only",
)

# 07-31 P1-3: verbatim type-contract rules embedded in EVERY real provider
# tier (main decision prompt, strict-JSON retry, minimal-safe retry). The
# P1-3 test asserts these exact strings inside each tier's hard_rules, so a
# future drift between tiers fails the test.
_PROMPT_DECISION_TYPE_RULE = (
    "decision 必须是单个字符串，绝不允许输出为数组。"
    "合法示例：\"monitor_only\"。"
    "非法示例：[\"monitor_only\"]、[\"trade_plan_available\",\"wait_for_pullback\"]"
)
_PROMPT_SUGGESTED_ACTIONS_TYPE_RULE = (
    "suggested_actions 必须是字符串数组，每个元素只能是 "
    "create_paper_order、create_opportunity_watch、add_to_watchlist、ignore、"
    "monitor_only 之一。"
    "合法示例：[\"monitor_only\"]。"
    "非法示例：[\"monitor_only\",\"wait_for_breakout\"]"
)
_PROMPT_TAKE_PROFITS_TYPE_RULE = (
    "take_profits 必须是对象数组，每个元素必须是 {\"price\": 数字, \"ratio\": 数字}。"
    "合法示例：[{\"price\":196.0,\"ratio\":1.0}]。"
    "非法示例：[196.0]、[100.0,200.0]"
)


def _schema_contract() -> dict[str, Any]:
    """07-31 final review P1-2: canonical typed ``schema_contract`` shared by
    EVERY real provider tier (main decision prompt, strict-JSON retry, and
    minimal-safe retry).

    Every scalar field is ``{"type": "string", "enum": [...]}`` — NEVER a
    bare array (a bare array teaches the model that the field may BE an
    array; production evidence #1: models emitted ``decision`` as an array
    and numeric ``take_profits`` items). ``suggested_actions`` is
    ``{"type": "array", "items": {"type": "string", "enum": [...]}}``.
    The P1-2 test asserts each field's exact type + enum on all three tiers.
    """
    return {
        "decision": {"type": "string", "enum": list(_LEGAL_LLM_DECISIONS)},
        "signal_grade": {"type": "string", "enum": ["S", "A", "B", "C", "D"]},
        "market_bias": {"type": "string", "enum": ["bullish", "bearish", "neutral", "mixed", "unknown"]},
        "trend_stage": {"type": "string", "enum": ["early", "middle", "late", "range", "transition", "unknown"]},
        "suggested_actions": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["create_paper_order", "create_opportunity_watch", "add_to_watchlist", "ignore", "monitor_only"],
            },
        },
    }


def _downgrade_to_monitor_only(
    decision: dict[str, Any], note: str
) -> tuple[dict[str, Any], list[str], bool]:
    """07-31 P1-1/P1-2: conservative semantic downgrade shared by the
    decision-array and take_profits repairs.

    When the LLM output is semantically ambiguous or unsafe (multi-element
    decision array, multiple/unsafe take_profits numbers), the only safe
    outcome is ``monitor_only`` with any executable trade_plan cancelled
    and the grade capped at B - never guess the model's intent and never
    keep a plan whose correctness is in doubt.
    """
    repaired = dict(decision)
    repaired["decision"] = "monitor_only"
    repaired["has_trade_plan"] = False
    if "trade_plan" in repaired:
        del repaired["trade_plan"]
    grade = repaired.get("signal_grade")
    if grade in ("S", "A"):
        repaired["signal_grade"] = "B"
    repaired["suggested_actions"] = ["monitor_only"]
    return repaired, [note], True


def _try_repair_decision(
    decision: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[dict[str, Any], list[str], bool]:
    """07-31 P1-1: repair ``decision`` emitted as an ARRAY back to a string.

    Production evidence #1: models repeatedly emitted ``"decision":
    [...]`` (single- and multi-element arrays) despite the prompt contract.
    Runs FIRST in the schema-repair chain (BEFORE suggested_actions repair),
    so the decision field is fixed before any downstream semantic mapping.

    Returns ``(repaired_decision, audit_notes, changed_flag)``. Never
    touches a non-list decision (bare string / missing key -> changed
    False). Fail-closed shapes (empty array, mixed types, illegal enum
    values) also return changed False so the caller routes them to the
    hard schema-fail path - never guess the model's intent.
    """
    raw = decision.get("decision")
    if not isinstance(raw, list):
        return decision, [], False
    if not raw:
        # Empty array: nothing to fold, fail-closed (hard schema failure).
        return decision, [], False
    if not all(isinstance(v, str) for v in raw):
        # Mixed types (e.g. [\"monitor_only\", 123]): fail-closed.
        return decision, [], False
    if not all(v in _LEGAL_LLM_DECISIONS for v in raw):
        # Illegal enum value inside the array: fail-closed.
        return decision, [], False

    repaired = dict(decision)
    if len(raw) == 1:
        # Single legal value: safe collapse with zero semantic loss.
        repaired["decision"] = raw[0]
        return repaired, [f"decision 数组折叠为单字符串 {raw[0]!r}"], True

    # Multi-element array of legal values: semantic ambiguity - we cannot
    # know which position the model meant. Conservative downgrade.
    return _downgrade_to_monitor_only(
        decision,
        "decision 为多元素数组（语义歧义），保守降级为 monitor_only 并取消交易计划",
    )


def _try_repair_entry_trigger_confirmation(
    decision: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[dict[str, Any], list[str], bool]:
    """Apply alias normalization to ``trade_plan.entry_trigger_confirmation``.

    Returns ``(repaired_decision, audit_notes, changed_flag)``. When the
    decision has no trade plan, no confirmation, or the confirmation is
    already canonical, returns ``(decision, [], False)`` and the caller
    falls through to the hard-fail path.
    """
    trade_plan = decision.get("trade_plan")
    if not isinstance(trade_plan, dict):
        return decision, [], False
    confirmation = trade_plan.get("entry_trigger_confirmation")
    if confirmation is None:
        return decision, [], False
    decision_symbol = decision.get("symbol") or snapshot.get("symbol")
    if not isinstance(decision_symbol, str) or not decision_symbol:
        return decision, [], False
    analysis_time = snapshot.get("analysis_time_utc")
    if not isinstance(analysis_time, int) or analysis_time <= 0:
        return decision, [], False
    normalized, notes, changed = normalize_entry_trigger_confirmation(
        confirmation,
        decision_symbol=decision_symbol,
        analysis_time_utc=analysis_time,
    )
    if not changed or normalized is None:
        return decision, [], False
    repaired = dict(decision)
    repaired_trade_plan = dict(trade_plan)
    repaired_trade_plan["entry_trigger_confirmation"] = normalized
    repaired["trade_plan"] = repaired_trade_plan
    return repaired, notes, True


def _try_repair_take_profits(
    decision: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[dict[str, Any], list[str], bool]:
    """07-31 P1-2 + final review P1-1: repair ``trade_plan.take_profits``
    numeric items.

    Production evidence #2: models sometimes emit bare numbers where the
    schema requires ``{"price": number, "ratio": number}`` objects
    (ga_decision.schema.json items -> object). Never guess position
    ratios, never bypass the risk gate - the repaired decision is
    re-validated by the chain before it counts as a success.

    Object contract (final review P1-1): ``price > 0``, ``0 < ratio <= 1``,
    both finite, and the object ratios must sum to ~1.0 — an object outside
    the contract (non-positive / non-finite price or ratio, ratio sum != 1.0)
    downgrades the WHOLE list.

    Returns ``(repaired_decision, audit_notes, changed_flag)``:

    - no trade_plan / take_profits not a list -> unchanged.
    - fully-valid object list whose ratios sum to ~1.0 -> unchanged
      (``repaired is decision``).
    - take_profits EXACTLY ``[single finite positive number]`` -> repaired
      to ``[{"price": <n>, "ratio": 1.0}]`` (a SOLE position is
      unambiguous), keep the decision.
    - numbers MIXED with object items, multiple numbers, or ANY non-finite
      / non-positive / bool / junk item -> conservative downgrade to
      monitor_only (never guess which number meant what — a guessed ratio
      could overlap the objects' coverage).
    """
    trade_plan = decision.get("trade_plan")
    if not isinstance(trade_plan, dict):
        return decision, [], False
    raw = trade_plan.get("take_profits")
    if not isinstance(raw, list) or not raw:
        return decision, [], False

    def _is_valid_object(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        price, ratio = item.get("price"), item.get("ratio")
        return (
            isinstance(price, (int, float)) and not isinstance(price, bool)
            and isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
            and math.isfinite(float(price)) and math.isfinite(float(ratio))
            and price > 0 and 0.0 < ratio <= 1.0
        )

    def _is_safe_numeric(item: Any) -> bool:
        # A single finite positive non-bool number is the only numeric shape
        # we can unambiguously treat as a target price.
        return (
            isinstance(item, (int, float)) and not isinstance(item, bool)
            and math.isfinite(float(item)) and item > 0
        )

    if all(_is_valid_object(item) for item in raw):
        # Fully-valid object list: the plan is only trustworthy when the
        # exit ratios cover the whole position (sum ~ 1.0). Incomplete or
        # overlapping coverage cannot be interpreted reliably.
        total_ratio = sum(float(item["ratio"]) for item in raw)
        if math.isclose(total_ratio, 1.0):
            return decision, [], False
        return _downgrade_to_monitor_only(
            decision,
            "take_profits 对象比例总和不为 1.0（仓位覆盖不完整或重叠），"
            "保守降级为 monitor_only 并取消交易计划",
        )

    numeric_indices = [i for i, item in enumerate(raw) if _is_safe_numeric(item)]
    unsafe = [
        item for item in raw
        if not _is_safe_numeric(item) and not _is_valid_object(item)
    ]

    # The ONLY safe repair is a take_profits list consisting of exactly one
    # finite positive number (a sole position is unambiguous). Numbers MIXED
    # with object items, several numbers, or any unsafe item -> we cannot
    # guess position ratios - conservatively cancel the plan.
    if unsafe or len(numeric_indices) != 1 or len(raw) != 1:
        return _downgrade_to_monitor_only(
            decision,
            "take_profits 数字项形状不安全（多数字/混合对象/非有限/非正/非数字），"
            "保守降级为 monitor_only 并取消交易计划",
        )

    # take_profits == [one finite positive number]: repair to
    # {price, ratio: 1.0} and keep the decision.
    repaired_trade_plan = dict(trade_plan)
    repaired_trade_plan["take_profits"] = [
        {"price": raw[0], "ratio": 1.0}
    ]
    repaired = dict(decision)
    repaired["trade_plan"] = repaired_trade_plan
    return repaired, [f"take_profits 数字项 {raw[0]!r} 已修复为对象 {{price, ratio: 1.0}}"], True


def _try_repair_suggested_actions(
    decision: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[dict[str, Any], list[str], bool]:
    """Repair ``suggested_actions`` by rebuilding the canonical list from
    decision semantics.

    Phase-2 D (07-27): mirrors ``_try_repair_entry_trigger_confirmation``.
    Returns ``(repaired_decision, audit_notes, changed_flag)``. When the
    decision's ``suggested_actions`` is already canonical and valid, returns
    ``(decision, [], False)`` and the caller falls through. The canonical
    rebuild mapping is in ``normalize_suggested_actions`` (decision_schema.py)
    — the raw list is NOT filtered against the enum (filtering would silently
    drop the LLM's intent, e.g. ``wait_for_breakout`` should map to
    ``add_to_watchlist``, not be dropped).
    """
    raw = decision.get("suggested_actions")
    normalized, notes, changed = normalize_suggested_actions(
        raw,
        decision=decision.get("decision"),
        has_trade_plan=decision.get("has_trade_plan"),
        trade_plan=decision.get("trade_plan"),
        opportunity_watch=decision.get("opportunity_watch"),
    )
    if not changed or normalized is None:
        return decision, [], False
    repaired = dict(decision)
    repaired["suggested_actions"] = normalized
    return repaired, notes, True


def _try_repair_opportunity_watch(
    decision: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[dict[str, Any], list[str], bool]:
    """Repair ``opportunity_watch`` to the structured watcher contract.

    08-02 P0-3: the ga_decision schema now FORBIDS bare-string
    ``conditions`` (text like "15M 收盘突破上沿或跌破下沿" cannot be
    evaluated by the watcher — it waited forever and the alert never
    enqueued). When the raw payload fails schema here, rebuild the watch
    deterministically from the decision's plan (``trade_plan`` or the
    preserved ``candidate_trade_plan``) via ``normalize_opportunity_watch``
    — never translate free text into conditions. Runs BEFORE
    ``_try_repair_suggested_actions`` so the canonical rebuild sees the
    corrected (or fail-closed None) watch.

    Returns ``(repaired_decision, audit_notes, changed_flag)``. When the
    watch is already structured (or None with no buildable plan), returns
    ``(decision, [], False)`` and the caller falls through. On fail-closed
    (unbuildable), sets ``opportunity_watch=None`` and drops
    ``create_opportunity_watch`` from ``suggested_actions`` so the manual
    button never fires on a watch-less decision.
    """
    raw = decision.get("opportunity_watch")
    if is_structured_watch(raw):
        return decision, [], False
    plan = decision.get("trade_plan") or decision.get("candidate_trade_plan")
    normalized, notes = normalize_opportunity_watch(raw, plan)
    if normalized is None and not notes:
        # No watch, no plan — nothing to repair.
        return decision, [], False
    repaired = dict(decision)
    repaired["opportunity_watch"] = normalized
    if normalized is None:
        sa = repaired.get("suggested_actions")
        if isinstance(sa, list) and "create_opportunity_watch" in sa:
            repaired["suggested_actions"] = [a for a in sa if a != "create_opportunity_watch"]
    return repaired, notes, True


# 07-09-overtrigger P0-2: unwrap the ``{"decision": {...}}`` wrapper that
# some models emit even when instructed to output a bare ``GADecision``.
# The unwrap must run BEFORE schema validation so the inner object is what
# gets validated, and BEFORE ``_normalize_llm_decision`` strips internal
# ``_``-prefixed fields. Conflict detection (both top-level schema fields
# AND nested ``decision`` present with different values) fails closed by
# returning ``None`` - the caller then routes to the hard schema-fail path.
_GA_DECISION_SCHEMA_TOP_LEVEL_KEYS = frozenset({
    "symbol", "analysis_time_utc", "decision", "signal_grade", "market_bias",
    "trend_stage", "confidence", "summary", "evidence", "counter_evidence",
    "risk_notes", "has_trade_plan", "trade_plan", "opportunity_watch",
    "suggested_actions", "timeframe_context", "alignment", "htf_conflict",
    "market_reason_codes",
})


def _unwrap_wrapped_decision(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    """Unwrap ``{"decision": {...}}`` to the inner object.

    Returns ``(unwrapped_or_None, changed_flag)``.

    - If ``candidate`` is not a dict or has no ``decision`` key, returns
      ``(candidate, False)`` (no change).
    - If ``candidate["decision"]`` is a dict AND the wrapper has no other
      GADecision schema top-level keys, returns ``(inner, True)``.
    - If ``candidate["decision"]`` is a dict AND the wrapper ALSO has
      top-level schema keys (e.g. both ``candidate["symbol"]`` and
      ``candidate["decision"]["symbol"]``), this is a conflict - fail
      closed by returning ``(None, True)``. The caller routes None to the
      hard schema-fail path.
    - If ``candidate["decision"]`` is NOT a dict (e.g. the string
      ``"trade_plan_available"`` - the schema's ``decision`` enum value),
      this is a real GADecision, NOT a wrapper. Return ``(candidate, False)``.
    - ``_llm_parse_meta`` is always preserved across unwrap.
    """
    if not isinstance(candidate, dict):
        return candidate, False
    inner = candidate.get("decision")
    if not isinstance(inner, dict):
        # ``decision`` is the schema enum string (or missing) - real
        # GADecision, not a wrapper. Leave as-is.
        return candidate, False
    # Detect conflict: any other schema top-level key present on the
    # wrapper means the model emitted BOTH a wrapper AND top-level fields
    # - we cannot safely pick one.
    wrapper_schema_keys = _GA_DECISION_SCHEMA_TOP_LEVEL_KEYS & set(candidate.keys()) - {"decision"}
    if wrapper_schema_keys:
        # Conflict: fail closed.
        return None, True
    # Safe to unwrap. Preserve _llm_parse_meta from the wrapper.
    parse_meta = candidate.get("_llm_parse_meta")
    unwrapped = dict(inner)
    if isinstance(parse_meta, dict):
        meta = dict(parse_meta)
        meta["llm_unwrapped_decision_object"] = True
        # Preserve the inner object's own parse_meta if present (rare but
        # possible if the model nested a prior parse).
        inner_meta = inner.get("_llm_parse_meta")
        if isinstance(inner_meta, dict):
            for k, v in inner_meta.items():
                meta.setdefault(k, v)
        unwrapped["_llm_parse_meta"] = meta
    else:
        unwrapped["_llm_parse_meta"] = {"llm_unwrapped_decision_object": True}
    return unwrapped, True


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
        # 08-04 contract D4: route the per-task system prompt into
        # ``session.system`` via the thread-local ``system_override`` (read by
        # ``_call_ga_llm`` BEFORE ``_llm_call_state_reset``). Scoped to this
        # call window with a ``finally`` cleanup so a patched/stubbed
        # ``_call_ga_llm`` cannot leak a stale override into a later
        # market-decision call on the same worker thread.
        _llm_call_state.system_override = TASK_SYSTEM_PROMPTS.get(task_name) or SYSTEM_PROMPT
        try:
            raw = _call_ga_llm(prompt)
        finally:
            if hasattr(_llm_call_state, "system_override"):
                delattr(_llm_call_state, "system_override")
        candidate = _parse_json_object(raw)
        # 08-04 contract D3: strip internal ``_``-prefixed keys (e.g. the
        # ``_llm_parse_meta`` marker attached by ``_parse_json_object``) from
        # the candidate BEFORE merge + schema validation so the strict
        # ``additionalProperties: false`` schemas accept the LLM output. The
        # LLM has no business writing internal marker fields.
        candidate_clean = {
            k: v for k, v in candidate.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
        result = dict(fallback)
        result.update(candidate_clean)
        result["agent_source"] = "llm_agent"
        result["llm_status"] = "ok"
        # D3: resolve the per-task schema when the caller did not pass one, and
        # ALWAYS validate the LLM CANDIDATE against it (root
        # ``additionalProperties: false`` + present-field types). Top-level
        # ``required`` is satisfied by the deterministic fallback merge, so the
        # candidate is validated with ``required`` stripped — a valid candidate
        # that omits a fallback-only key (e.g. ``trade_id`` on the review path)
        # must not be spuriously rejected. Unknown top-level keys / wrong types
        # fail closed to ``deterministic_fallback``.
        resolved_schema = schema_name or TASK_SCHEMAS.get(task_name)
        if resolved_schema:
            schema = load_schema(resolved_schema)
            loose = dict(schema)
            loose["required"] = []
            try:
                jsonschema.validate(candidate_clean, loose)
            except Exception as exc:
                raise ValueError(f"{resolved_schema} 候选校验失败: {exc}") from exc
        semantic = TASK_SEMANTIC_VALIDATORS.get(task_name)
        if semantic is not None:
            ok, err = semantic(result)
            if not ok:
                raise ValueError(err or "semantic validation failed")
        return result
    except Exception as exc:
        result = dict(fallback)
        result["agent_source"] = "deterministic_fallback"
        result["llm_status"] = "failed"
        result["llm_error"] = str(exc)[:300]
        return result


# ---------------------------------------------------------------------------
# B2: LLM failure classification (design §3.3)
# ---------------------------------------------------------------------------

def _classify_llm_failure(exc: BaseException | None, raw: str | None, stage: str) -> str:
    """Classify an LLM failure into a stable error category.

    Pure function, no side effects. Maps exception / raw response / stage
    to one of LLM_ERROR_CATEGORIES.

    Secret hygiene: never persist API keys, headers, full response bodies
    beyond the first 300 chars of error text.
    """
    raw_text = str(raw or "")[:300]
    raw_lower = raw_text.lower()
    exc_msg = str(exc or "")[:300].lower()

    if stage == "call":
        # Config errors: model not found, auth, invalid request
        if raw_text.startswith("!!!Error"):
            if any(tok in raw_lower for tok in ("model not found", "invalid_model_error", "401", "403", "invalid api key")):
                return "llm_config_error"
            # Rate limited
            if any(tok in raw_lower for tok in ("429", "quota", "overload")):
                return "llm_rate_limited"
            # Transport errors: timeout, connection, gateway
            if any(tok in raw_lower for tok in ("timeout", "connection reset", "502", "503", "504")):
                return "llm_transport_error"
            # Default for unknown gateway errors
            return "llm_transport_error"

        # When ``raw`` is None/empty but the exception carries the gateway
        # error text (RuntimeError raised by ``_call_ga_llm`` for
        # ``!!!Error`` responses), classify from the exception message so
        # config errors are not misclassified as ``llm_empty_response``.
        if not raw_text.strip() and exc_msg.startswith("!!!error"):
            if any(tok in exc_msg for tok in ("model not found", "invalid_model_error", "401", "403", "invalid api key")):
                return "llm_config_error"
            if any(tok in exc_msg for tok in ("429", "quota", "overload")):
                return "llm_rate_limited"
            if any(tok in exc_msg for tok in ("timeout", "connection reset", "502", "503", "504")):
                return "llm_transport_error"
            return "llm_transport_error"

        # Empty response
        if not raw_text.strip():
            # 07-09-overtrigger R5/R6: distinguish "model called a tool
            # with no assistant text" from "gateway returned nothing".
            # ``_call_ga_llm`` encodes the session stop_reason into the
            # RuntimeError message when the raw text is empty AND the
            # stop reason indicates a tool/function call - those route to
            # ``llm_tool_call_no_text`` (prompt remediation) instead of
            # ``llm_empty_response`` (gateway on-call).
            if "tool_call_no_text" in exc_msg or (
                "stop_reason" in exc_msg and any(tok in exc_msg for tok in ("tool", "function"))
            ):
                return "llm_tool_call_no_text"
            return "llm_empty_response"

        # Exception-based classification for call stage
        if any(tok in exc_msg for tok in ("timeout", "timed out")):
            return "llm_transport_error"
        if any(tok in exc_msg for tok in ("connection", "reset", "refused")):
            return "llm_transport_error"
        if any(tok in exc_msg for tok in ("429", "quota", "overload")):
            return "llm_rate_limited"
        if any(tok in exc_msg for tok in ("model not found", "invalid_model", "401", "403")):
            return "llm_config_error"
        # Generic call failure
        return "llm_transport_error"

    if stage == "parse":
        # Map _classify_json_error categories to LLM error categories
        return "llm_json_parse_failed"

    if stage == "schema":
        return "llm_schema_validation_failed"

    if stage == "semantic":
        return "llm_semantic_validation_failed"

    # Fallback
    return "llm_transport_error"


# ---------------------------------------------------------------------------
# 07-13 R6-D (P0-3.5 / §7.9): truncation taxonomy + breaker isolation.
#
# A provider response that hit the output-token cap (``stop_reason=max_tokens``
# / ``finish_reason=length`` / ``max_output``) is a STRUCTURED terminal reason
# (``llm_output_truncated``), NOT a generic parse failure and NOT an
# infrastructure breaker input. The model produced content -- it simply ran out
# of output budget mid-JSON -- so it is isolated from the breaker: one symbol's
# truncation cannot breaker-skip the remaining symbols (AC8). A truncated
# Attempt 1 may strict/minimal retry within the same per-symbol deadline; the
# ``_RETRYABLE_CATEGORIES`` membership below makes that the default, and the
# ``_NON_RETRYABLE`` / ``_terminal_reason_for`` map in the coordinator stops at
# a deterministic fallback when the deadline is exhausted.
# ---------------------------------------------------------------------------

# stop_reason / finish_reason tokens the provider emits when the output hit the
# max_output_tokens cap. All map to ``llm_output_truncated``. Lowercased and
# matched as substrings so a gateway-wrapped ``"stop_reason=max_tokens"`` or an
# OpenAI-style ``"finish_reason":"length"`` both classify the same way.
_TRUNCATION_STOP_TOKENS = (
    "max_tokens",
    "max_output",
    "length",
)


def _classify_stop_reason(stop_reason: str | None) -> str | None:
    """Map a provider ``stop_reason`` / ``finish_reason`` to a structured
    terminal reason.

    07-13 R6-D (P0-3.5 / §7.9): ``stop_reason=max_tokens`` (and the
    OpenAI-style ``finish_reason=length`` / gateway ``max_output`` variants)
    classify as ``llm_output_truncated`` -- the model produced content but ran
    out of output budget mid-JSON. This is a DISTINCT terminal reason from a
    generic parse failure (``llm_json_parse_failed``) and from infrastructure
    errors (transport / empty / config), so one symbol's truncation cannot
    open the breaker and skip the remaining symbols (AC8).

    Returns ``None`` for a non-truncation stop reason (e.g. ``stop``,
    ``tool_use``, ``end_turn``) so the caller falls through to the normal
    parse / classify path.
    """
    if not stop_reason:
        return None
    sr = str(stop_reason).strip().lower()
    if not sr:
        return None
    for tok in _TRUNCATION_STOP_TOKENS:
        if tok in sr:
            return "llm_output_truncated"
    return None


# The categories that open the infrastructure circuit breaker: transport /
# empty / tool-call-no-text / rate_limited drive the consecutive-infra path
# and the rate window, ``llm_config_error`` opens immediately. Schema /
# format / truncation failures are EXPLICITLY ABSENT -- they are
# model-output problems for the symbol, not gateway health signals, so they
# cannot breaker-skip unrelated symbols (AC8). 07-31 P0-2: the single source
# of truth moved to ``llm_breaker.BREAKER_DRIVING_CATEGORIES``; this name is
# now an import ALIAS of the same object so judge / breaker / diagnostics
# cannot drift again. Diagnostics/tests import this name to assert isolation.
from plugins.crypto_guard.reasoning.llm_breaker import (
    BREAKER_DRIVING_CATEGORIES as _BREAKER_INFRA_REASONS,
)


# ---------------------------------------------------------------------------
# B3: Strict JSON and minimal safe prompt builders (design §8.2, §8.3)
# ---------------------------------------------------------------------------

def _market_user_body(payload: dict[str, Any]) -> str:
    """Serialize a market-decision payload into the user message body.

    08-04 Codex-P2 (D4): the three market builders return the user message
    ALONE — the structured JSON input payload — and select this round's system
    prompt by stashing a one-shot ``system_override`` on the thread-local.
    ``_call_ga_llm`` consumes that override to set ``session.system`` and sends
    this body verbatim as the user text (never duplicating the system prompt).
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _market_total_context_bytes(system_text: str, user_body: str) -> int:
    """Real provider total context bytes = system bytes + user bytes.

    08-04 Codex-P2 (D4): after the D4 split the prompt-builder budget must
    still account for the FULL context the provider sees (``session.system``
    plus the user message), NOT under-report to the user body alone. The old
    measurement summed system + 10-byte separator + body; the new one is the
    strict system+user total and is therefore a slightly tighter but never
    under-reported ceiling.
    """
    return len(system_text.encode("utf-8")) + len(user_body.encode("utf-8"))


def build_llm_strict_json_prompt(
    snapshot: dict[str, Any],
    deterministic_decision: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> str:
    """Build a strict JSON-only prompt for retry attempt 2.

    Reuses build_llm_decision_prompt's payload but overrides the selected
    system prompt with SYSTEM_PROMPT_STRICT_JSON to force pure JSON output.
    08-04 Codex-P2 (D4): the builder returns the user payload ALONE (NOT the
    system prompt + separator + payload) and selects this round's system
    prompt via a one-shot ``system_override`` on the thread-local. The strict
    tier shares the main tier's payload verbatim — no string-prefix
    replacement, so the three tiers can never drift on payload shape.
    """
    # Build the main tier's payload. ``build_llm_decision_prompt`` stashes the
    # prompt metadata (bytes + continuity_included) into the thread-local; its
    # byte measurement used SYSTEM_PROMPT, so we re-stash with the STRICT
    # system text below.
    normal_prompt = build_llm_decision_prompt(snapshot, deterministic_decision, context=context)
    _cont = getattr(_llm_call_state, "continuity_included", False)
    # Phase D §8: re-stash the FINAL (strict) prompt byte size as the REAL
    # provider total context = strict system bytes + user body bytes. The
    # strict system prompt differs in length from SYSTEM_PROMPT, so the bytes
    # stashed by build_llm_decision_prompt are stale. Continuity inclusion is
    # unchanged (same payload).
    _llm_call_state.system_override = SYSTEM_PROMPT_STRICT_JSON
    _llm_call_state.prompt_bytes = _market_total_context_bytes(
        SYSTEM_PROMPT_STRICT_JSON, normal_prompt
    )
    _llm_call_state.continuity_included = _cont
    return normal_prompt


def build_llm_minimal_safe_prompt(
    snapshot: dict[str, Any],
    deterministic_decision: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> str:
    """Build a minimal safe prompt for retry attempt 3.

    Reuses the R8 P1 safe_payload fallback path: symbol + analysis_time +
    hard_rules + deterministic_reference + minimal output_requirements.
    No market_snapshot, no modules, no multi_timeframe_feature_pack.

    07-10 Phase D (design §7.1): the minimal-safe prompt MUST still include
    ``analysis_continuity``. Continuity is protected across EVERY prompt tier
    (design §5.1) — dropping it entirely on attempt 3 would break the
    cross-round continuity contract precisely when the symbol is under the
    most retry pressure. ``_compact_snapshot`` already surfaces a (possibly
    "missing"-status) continuity block; surface it here as a top-level key
    so the LLM still sees prior grade/bias/trigger state.
    """
    from plugins.crypto_guard.config.loader import load_config
    risk_cfg = load_config().trading_mode.get("risk", {})
    min_rr = risk_cfg.get("min_rr", 1.5)
    min_conf = risk_cfg.get("min_confidence", 0.72)
    safe_dr = deterministic_decision or {}
    safe_ms = _compact_snapshot(snapshot) if snapshot else {}
    payload = {
        # 07-31 final review P1-2: the minimal-safe tier must carry the SAME
        # typed schema_contract as the main tier (pre-fix it had NO
        # schema_contract key at all — the model was left without a type
        # contract exactly when it was most degraded). Shared via
        # _schema_contract() so the three tiers can never drift.
        "schema_contract": _schema_contract(),
        "symbol": safe_ms.get("symbol") or safe_dr.get("symbol"),
        "analysis_time_utc": safe_ms.get("analysis_time_utc") or safe_dr.get("analysis_time_utc"),
        "strategy_name": safe_dr.get("strategy_name"),
        "strategy_version": safe_dr.get("strategy_version"),
        "hard_rules": [
            "不得输出实盘交易或真实下单能力",
            f"创建模拟盘必须经过风控：RR>={min_rr}、confidence>={min_conf}",
            "只输出一个 JSON 对象，禁止 Markdown",
            # 07-31 P1-3: verbatim type contracts (production evidence #1/#2).
            # These strings are asserted verbatim by test_pg_prompt_type_contract_p1_3.
            _PROMPT_DECISION_TYPE_RULE,
            _PROMPT_SUGGESTED_ACTIONS_TYPE_RULE,
            _PROMPT_TAKE_PROFITS_TYPE_RULE,
        ],
        # Phase D §7.1: continuity survives even the minimal-safe tier.
        "analysis_continuity": safe_ms.get("analysis_continuity"),
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
        "output_requirements": {
            "format": "JSON object only",
            "language": "Chinese for summary/evidence/risk_notes",
            "must_keep": ["symbol", "analysis_time_utc", "strategy_name", "strategy_version"],
        },
        "_trim_note": "prompt_over_budget_minimal_fallback",
    }
    # 08-04 Codex-P2 (D4): the builder returns the user payload ALONE and
    # selects this round's system prompt (SYSTEM_PROMPT_STRICT_JSON) via a
    # one-shot ``system_override`` on the thread-local — never embedding the
    # system prompt or the legacy ``输入：`` separator into the user message.
    _user_body = _market_user_body(payload)
    # Phase D §8: stash prompt metadata for the retry wrapper as the REAL
    # provider total context = strict system bytes + user body bytes. The
    # minimal-safe tier surfaces continuity as a TOP-LEVEL payload key (not
    # under ``market_snapshot``), so check both locations.
    _cont = payload.get("analysis_continuity")
    if _cont is None:
        _ms = payload.get("market_snapshot")
        _cont = _ms.get("analysis_continuity") if isinstance(_ms, dict) else None
    _llm_call_state.system_override = SYSTEM_PROMPT_STRICT_JSON
    _llm_call_state.prompt_bytes = _market_total_context_bytes(
        SYSTEM_PROMPT_STRICT_JSON, _user_body
    )
    _llm_call_state.continuity_included = _cont is not None
    return _user_body


# ---------------------------------------------------------------------------
# B4: LLM call wrapper with retry (design §4.1)
# ---------------------------------------------------------------------------

def _call_ga_llm_with_retry(
    *,
    snapshot: dict[str, Any],
    fallback: dict[str, Any],
    context: dict[str, Any] | None,
    breaker: Any,  # CircuitBreaker | _NullBreaker
    prompt_builders: tuple[
        Callable[[dict, dict, Any], str],  # normal
        Callable[[dict, dict, Any], str],  # strict JSON
        Callable[[dict, dict, Any], str],  # minimal safe
    ],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Per-symbol retry wrapper. Returns (candidate_dict_or_None, attempt_meta).

    The wrapper checks (in order, BEFORE every LLM call including Attempt 1):
    1. breaker.should_call() — if False, skip LLM.
    2. batch_wall_clock_budget.remaining_ms() > estimated_call_ms + jitter_ms
       — if not, skip call with wall_clock_budget_exhausted.
    3. For retry attempts only (Attempt 2/3): batch_retry_budget.remaining() > 0
       — if 0, skip retry with retry_budget_exhausted.

    The wrapper does NOT raise — fail-closed is the caller's job.
    """
    from plugins.crypto_guard.reasoning.llm_breaker import (
        BatchRetryBudget,
        BatchWallClockBudget,
    )
    from plugins.crypto_guard.config.loader import load_config

    # Load LLM config
    llm_cfg = load_config().trading_mode.get("llm", {})
    retry_cfg = llm_cfg.get("retry", {})
    retry_enabled = retry_cfg.get("enabled", True)
    max_attempts = retry_cfg.get("max_attempts_per_symbol", 3) if retry_enabled else 1
    jitter_min = retry_cfg.get("jitter_seconds_min", 2)
    jitter_max = retry_cfg.get("jitter_seconds_max", 20)

    # Resolve config name and model (cached on breaker)
    cfg_name = breaker.llm_config_name
    if cfg_name is None:
        try:
            cfg_name = _resolve_llm_config_name()
            breaker.llm_config_name = cfg_name
        except Exception:
            cfg_name = "unknown"
            breaker.llm_config_name = cfg_name

    model_name = breaker.llm_model
    if model_name is None:
        model_name = _resolve_llm_model(cfg_name)
        breaker.llm_model = model_name

    # Resolve budgets from context (set by controller per-batch)
    retry_budget = (context or {}).get("llm_retry_budget") or BatchRetryBudget(
        max_batch_retry_calls=retry_cfg.get("max_batch_retry_calls", 9),
    )
    wall_clock_budget = (context or {}).get("llm_wall_clock_budget") or BatchWallClockBudget(
        budget_seconds=retry_cfg.get("batch_wall_clock_budget_seconds", 90),
    )
    # 07-10 Phase B: per-symbol deadline. When the fair scheduler (Phase C)
    # injects ``llm_per_symbol_deadline``, it is the admission gate — the
    # fixed 30s ESTIMATED_CALL_MS estimate is NOT used. When the deadline is
    # absent (legacy callers, the Phase A repro tests that exercise the
    # known-starving path), the old ESTIMATED_CALL_MS gate remains byte-for-
    # byte unchanged so the rollback point ("config defaults retain old
    # execution until fair scheduler is wired") holds.
    per_symbol_deadline = (context or {}).get("llm_per_symbol_deadline")

    # Estimated call time: 30s for normal, 30s for strict, 30s for minimal.
    # Used ONLY on the legacy admission path (no per-symbol deadline).
    ESTIMATED_CALL_MS = 30_000

    attempt_meta: dict[str, Any] = {
        "llm_status": "failed",
        "llm_error_category": None,
        "llm_error_stage": None,
        "llm_error": None,
        "llm_attempt_count": 0,
        "llm_retry_round": None,
        "llm_config_name": cfg_name,
        "llm_model": model_name,
        "llm_fallback_reason": None,
        # 07-10 Phase D §8 per-decision metadata. Defaults are
        # zero/None/False so they persist for BOTH successes and failures.
        # ``llm_provider_call_count`` counts physical provider calls made
        # for this symbol (a successful parse on attempt 2 = 2 calls).
        # ``llm_latency_ms`` is the last attempt's provider-call latency.
        # ``llm_prompt_bytes`` / ``llm_continuity_included`` reflect the
        # last attempt's built prompt. ``llm_terminal_reason`` is the exact
        # structured terminal reason (design §9) - never the generic
        # ``llm_parse_failed``. ``llm_schedule_round`` /
        # ``llm_schedule_position`` are injected by the Phase C fair
        # scheduler via context when the symbol runs through ``run_fair_batch``.
        "llm_provider_call_count": 0,
        "llm_latency_ms": None,
        "llm_prompt_bytes": None,
        "llm_continuity_included": None,
        "llm_terminal_reason": None,
        "llm_schedule_round": None,
        "llm_schedule_position": None,
        # 07-10 Phase D §6 effective generation controls (what actually
        # landed on the llmcore session, NOT just the configured values).
        "llm_effective_thinking_budget_tokens": None,
        "llm_effective_max_output_tokens": None,
        "llm_effective_temperature": None,
        # ``llm_provider_timeout_ms`` (Phase B) - effective per-attempt
        # provider timeout derived from the per-symbol deadline.
        "llm_provider_timeout_ms": None,
    }

    # 07-10 Phase D §8: when the symbol runs through the Phase C fair
    # scheduler (``run_fair_batch``), the coordinator injects its schedule
    # position/round via context. Surface them so the per-decision metadata
    # is complete even on the legacy serial path (None when absent).
    _sched_ctx = context or {}
    attempt_meta["llm_schedule_round"] = _sched_ctx.get("llm_schedule_round")
    attempt_meta["llm_schedule_position"] = _sched_ctx.get("llm_schedule_position")

    last_category: str | None = None
    last_error: str | None = None
    last_stage: str | None = None

    for attempt in range(1, max_attempts + 1):
        # --- Pre-call checks (before EVERY call including Attempt 1) ---

        # Check 1: breaker
        if not breaker.should_call():
            breaker.record_skip()
            attempt_meta["llm_fallback_reason"] = "circuit_breaker_open"
            attempt_meta["llm_terminal_reason"] = "breaker_skipped"
            attempt_meta["llm_attempt_count"] = attempt - 1
            attempt_meta["llm_error_category"] = last_category
            attempt_meta["llm_error_stage"] = last_stage
            attempt_meta["llm_error"] = last_error or "circuit breaker open; LLM call skipped"
            return None, attempt_meta

        # Check 2: wall-clock budget. 07-10 Phase B introduces a per-symbol
        # deadline that REPLACES the fixed 30s admission estimate. When the
        # per-symbol deadline is present, the symbol's own remaining time is
        # the gate — a symbol with remaining time gets its call even if the
        # shared batch budget is low, and a symbol whose deadline elapsed is
        # skipped with ``symbol_timeout`` (Phase C/E wiring; the wrapper
        # records ``wall_clock_budget_exhausted`` here for the legacy label
        # until Phase E replaces the terminal reason). When the per-symbol
        # deadline is absent, the legacy ``ESTIMATED_CALL_MS + jitter`` gate
        # against the shared batch budget is used unchanged.
        jitter_ms = 0
        if attempt > 1:
            jitter_ms = int((jitter_min + (jitter_max - jitter_min) * (attempt / max_attempts)) * 1000)
        if per_symbol_deadline is not None:
            # Per-symbol deadline path (Phase B primitive). The provider call
            # timeout for THIS attempt is derived from remaining symbol time.
            # Persist it on attempt_meta so Phase D can record the effective
            # timeout. If the deadline is already exhausted, skip the call.
            if per_symbol_deadline.exhausted():
                attempt_meta["llm_fallback_reason"] = "wall_clock_budget_exhausted"
                attempt_meta["llm_terminal_reason"] = "symbol_timeout"
                attempt_meta["llm_attempt_count"] = attempt - 1
                attempt_meta["llm_error_category"] = last_category
                attempt_meta["llm_error_stage"] = last_stage
                attempt_meta["llm_error"] = last_error or "per-symbol deadline exhausted before call"
                attempt_meta["llm_provider_timeout_ms"] = 0
                return None, attempt_meta
            attempt_meta["llm_provider_timeout_ms"] = per_symbol_deadline.provider_timeout_ms()
        else:
            # Legacy admission path (unchanged, for rollback parity).
            if wall_clock_budget.remaining_ms() < ESTIMATED_CALL_MS + jitter_ms:
                attempt_meta["llm_fallback_reason"] = "wall_clock_budget_exhausted"
                attempt_meta["llm_terminal_reason"] = "batch_deadline_skipped"
                attempt_meta["llm_attempt_count"] = attempt - 1
                attempt_meta["llm_error_category"] = last_category
                attempt_meta["llm_error_stage"] = last_stage
                attempt_meta["llm_error"] = last_error or "batch wall-clock budget exhausted"
                return None, attempt_meta

        # Check 3: retry budget (only for Attempt 2/3)
        if attempt > 1:
            if not retry_budget.consume():
                attempt_meta["llm_fallback_reason"] = "retry_budget_exhausted"
                attempt_meta["llm_terminal_reason"] = "retry_budget_exhausted"
                attempt_meta["llm_attempt_count"] = attempt - 1
                attempt_meta["llm_error_category"] = last_category
                attempt_meta["llm_error_stage"] = last_stage
                attempt_meta["llm_error"] = last_error or "batch retry budget exhausted"
                return None, attempt_meta
            breaker.record_retry()

        # --- Jitter sleep for retry attempts ---
        if attempt > 1 and jitter_ms > 0:
            import random
            actual_jitter = random.randint(
                int(jitter_min * 1000),
                min(jitter_ms, int(jitter_max * 1000)),
            )
            time.sleep(actual_jitter / 1000.0)

        # --- Build prompt, call LLM, parse, unwrap, validate, repair ---
        # 07-10 R1-1: the single-attempt unit is now ``_run_single_llm_attempt``
        # (shared with the fair adapter). It does prompt build + budget-
        # contract check + ONE provider call + parse + unwrap + schema
        # validation + schema-alias repair, returning ``(candidate_or_None,
        # per_attempt_meta)`` with ``llm_repair_event`` surfaced. The retry
        # wrapper owns the admission gates above and the breaker record below.
        _cand, _am = _run_single_llm_attempt(
            snapshot=snapshot, fallback=fallback, context=context,
            attempt=attempt, max_attempts=max_attempts, breaker=breaker,
            cfg_name=cfg_name, model_name=model_name,
            prompt_builders=prompt_builders, last_category=last_category,
            budget_violation_is_skip=False,  # legacy path: budget violation is a failed terminal
            # 07-10 R2-1 + Phase B P1-1 (07-22): pass the deadline object so
            # post-prompt admission re-checks remaining time after prompt build
            # and captures an immutable effective timeout. Do NOT pre-resolve
            # provider_timeout_seconds here (that freezes a pre-prompt value
            # and lets floors re-admit a zero remaining). When the deadline is
            # absent (legacy callers, Phase A repro), None preserves the 60s
            # read-timeout floor + the session's default max_retries.
            provider_timeout_seconds=None,
            deadline=per_symbol_deadline,
        )

        # Merge the per-attempt §8 envelope fields onto the wrapper's outer
        # attempt_meta (prompt bytes, continuity, latency, effective settings,
        # terminal reason). The provider_call_count accumulator is additive
        # across attempts (a success on attempt 2 = 2 calls).
        attempt_meta["llm_prompt_bytes"] = _am.get("llm_prompt_bytes")
        attempt_meta["llm_continuity_included"] = _am.get("llm_continuity_included")
        attempt_meta["llm_latency_ms"] = _am.get("llm_latency_ms")
        attempt_meta["llm_effective_thinking_budget_tokens"] = _am.get("llm_effective_thinking_budget_tokens")
        attempt_meta["llm_effective_max_output_tokens"] = _am.get("llm_effective_max_output_tokens")
        attempt_meta["llm_effective_temperature"] = _am.get("llm_effective_temperature")
        # Phase B P1-1 (07-22): prefer the immutable timeout captured at
        # post-prompt admission inside ``_run_single_llm_attempt``. A post-call
        # re-read of ``deadline.provider_timeout_ms()`` can collapse to 0 after
        # a successful call (production d49) and must NOT overwrite the
        # admitted positive timeout. Fall back to the pre-call admission value
        # already on attempt_meta when the single-attempt unit did not set one.
        if _am.get("llm_provider_timeout_ms") is not None:
            attempt_meta["llm_provider_timeout_ms"] = _am.get("llm_provider_timeout_ms")
        _prov_calls = int(_am.get("llm_provider_call_count") or 0)
        if _prov_calls:
            attempt_meta["llm_provider_call_count"] = attempt_meta.get("llm_provider_call_count", 0) + _prov_calls

        _attempt_status = str(_am.get("llm_status") or "failed")
        _attempt_category = _am.get("llm_error_category")
        _repair_event = bool(_am.get("llm_repair_event"))

        # Success (plain or repaired): persist envelope + emit breaker events.
        if _attempt_status == "ok" and _cand is not None:
            attempt_meta["llm_status"] = "ok"
            attempt_meta["llm_attempt_count"] = attempt
            attempt_meta["llm_retry_round"] = attempt
            attempt_meta["llm_error_category"] = None
            attempt_meta["llm_error_stage"] = None
            attempt_meta["llm_error"] = None
            attempt_meta["llm_fallback_reason"] = None
            attempt_meta["llm_terminal_reason"] = _am.get("llm_terminal_reason")
            attempt_meta["llm_repair_event"] = _repair_event
            # 07-10 R1-1 (P0-1): mirror the legacy run_agent_sop_decision
            # path (llm_agent_judge.py:223-226 / :278) - a repaired success
            # emits the PHYSICAL success first (drives the state machine),
            # THEN the repairable event (tracks repairable_count only). A
            # plain success emits one physical-ok record.
            breaker.record_attempt(category=None, ok=True)
            if _repair_event:
                breaker.record_attempt(
                    category="llm_schema_repairable", ok=True, repairable=True,
                )
            # Merge the full envelope onto the success decision (mirrors
            # legacy llm_agent_judge.py:246/:289).
            _cand.update(attempt_meta)
            return _cand, attempt_meta

        # Prompt-budget contract violation: terminal non-retryable (D5). No
        # provider call, no breaker event (budget skip, not a failed call).
        if _attempt_status == "skipped" or \
                _attempt_category == "llm_prompt_budget_violation":
            attempt_meta["llm_status"] = _attempt_status
            attempt_meta["llm_fallback_reason"] = _am.get("llm_fallback_reason") or "prompt_budget_contract_violation"
            attempt_meta["llm_terminal_reason"] = _am.get("llm_terminal_reason") or "prompt_budget_contract_violation"
            attempt_meta["llm_error_category"] = _attempt_category
            attempt_meta["llm_error_stage"] = _am.get("llm_error_stage")
            attempt_meta["llm_error"] = _am.get("llm_error")
            attempt_meta["llm_attempt_count"] = attempt
            attempt_meta["llm_retry_round"] = attempt
            last_category = _attempt_category
            last_error = _am.get("llm_error")
            last_stage = _am.get("llm_error_stage")
            return None, attempt_meta

        # Failed attempt (parse / schema-conflict / transport / empty). The
        # provider call DID happen - record the breaker failure. Schema-
        # validation-failed (incl. unwrap conflict) is non-retryable.
        category = _attempt_category
        last_category = category
        last_error = _am.get("llm_error")
        last_stage = _am.get("llm_error_stage")
        attempt_meta["llm_error_category"] = category
        attempt_meta["llm_error_stage"] = last_stage
        attempt_meta["llm_error"] = last_error

        breaker.record_attempt(category=category, ok=False)

        # Check if the error is non-retryable
        if category in _NON_RETRYABLE_CATEGORIES:
            attempt_meta["llm_status"] = "failed"
            attempt_meta["llm_fallback_reason"] = "non_retryable_error"
            attempt_meta["llm_terminal_reason"] = category
            attempt_meta["llm_attempt_count"] = attempt
            return None, attempt_meta

        # Special case: json_parse_failed on strict JSON attempt -> allow
        # attempt 3 with minimal safe only if the failure is empty/transport
        if attempt == 2 and category == "llm_json_parse_failed":
            # Strict JSON didn't help; allow attempt 3 with minimal safe
            # (smaller payload may avoid token-limit truncation)
            pass

        # Continue to next attempt if retryable and budget allows

    # All attempts exhausted
    attempt_meta["llm_status"] = "failed"
    attempt_meta["llm_fallback_reason"] = "retry_exhausted"
    attempt_meta["llm_terminal_reason"] = last_category or "retry_exhausted"
    attempt_meta["llm_attempt_count"] = max_attempts
    attempt_meta["llm_error_category"] = last_category
    attempt_meta["llm_error_stage"] = "retry_exhausted"
    attempt_meta["llm_error"] = last_error
    return None, attempt_meta


def _resolve_llm_model(cfg_name: str) -> str:
    """Best-effort resolution of the LLM model name from the config."""
    try:
        import llmcore
        session = llmcore.resolve_session(cfg_name)
        # Try common attribute paths for model name
        for attr in ("model", "model_name", "config"):
            val = getattr(session, attr, None)
            if val:
                if isinstance(val, str):
                    return val
                if isinstance(val, dict) and val.get("model"):
                    return str(val["model"])
        # Fallback: return the config name itself
        return cfg_name
    except Exception:
        return cfg_name


# R9 P2-2 fix: module-level constant so the final hard-cap fallback
# can be exercised behaviorally in tests via ``unittest.mock.patch``.
# Pre-R9 ``MAX_PROMPT_BYTES`` was a function-local variable, making the
# defensive safe_payload path effectively unreachable in tests — the
# only way to force it was to make SYSTEM_PROMPT itself huge, which is
# not possible from a test. With this constant at module scope, tests
# can patch it to a small value to fire the safe_payload path.
MAX_PROMPT_BYTES = 48 * 1024  # 2x feature pack budget


def _run_single_llm_attempt(
    *,
    snapshot: dict[str, Any],
    fallback: dict[str, Any],
    context: dict[str, Any] | None,
    attempt: int,
    max_attempts: int,
    breaker: Any,
    cfg_name: str,
    model_name: str,
    prompt_builders: tuple[
        Callable[[dict, dict, Any], str],  # normal
        Callable[[dict, dict, Any], str],  # strict JSON
        Callable[[dict, dict, Any], str],  # minimal safe
    ],
    last_category: str | None = None,
    budget_violation_is_skip: bool = False,
    provider_timeout_seconds: float | None = None,
    subprocess_hard_timeout: bool = False,
    deadline: Any = None,  # optional PerSymbolDeadline for post-prompt admission
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """07-10 R1-1: ONE LLM attempt - prompt build + budget-contract check +
    ONE provider call + JSON parse + unwrap + schema validation + schema-alias
    repair. Shared by the legacy retry wrapper (3x loop) and the new
    ``fair_llm_call_adapter`` (1 call, no inner retry).

    Does NOT own admission gates (breaker.should_call / wall-clock budget /
    retry_budget.consume) and does NOT call ``breaker.record_attempt`` - the
    CALLER owns those (retry wrapper / fair coordinator). This matches the
    coordinator contract (llm_fair_scheduler.py ``_run_one_attempt`` owns the
    breaker) and the directive: "fair coordinator 调用单次-attempt adapter，
    不能再套内部三次 retry wrapper" - the adapter must do ONE provider call,
    not wrap the 3x retry wrapper.

    07-10 R2-1: ``provider_timeout_seconds`` threads the per-symbol deadline's
    provider timeout into the ONE ``_call_ga_llm`` call (``session.read_timeout``
    + ``session.max_retries = 0``). None preserves the legacy 60s read-timeout
    floor + the session's default max_retries (the legacy retry wrapper has its
    OWN 3x loop and relies on the breaker for backoff).

    Returns ``(candidate_or_None, attempt_meta)``:
    - ``candidate``: the normalized + validated decision dict on success
      (including a schema-alias / unwrap repaired success), else None.
    - ``attempt_meta``: §8 envelope with ``llm_status`` ("ok" | "failed" |
      "skipped"), ``llm_error_category`` / ``llm_error_stage`` / ``llm_error``,
      ``llm_fallback_reason``, ``llm_terminal_reason``, ``llm_provider_call_count``
      (1 on a real call, 0 on budget violation), ``llm_latency_ms``,
      ``llm_prompt_bytes``, ``llm_continuity_included``, ``llm_repair_event``
      (True when a schema-alias or unwrap repair succeeded - the caller emits
      BOTH the physical-success and repairable breaker events, mirroring the
      legacy ``run_agent_sop_decision`` path), and the effective generation
      settings.

    ``budget_violation_is_skip``: when True (fair adapter path), a prompt-budget
    contract violation returns ``llm_status="skipped"`` +
    ``llm_fallback_reason="prompt_budget_contract_violation"`` (P0-2: no
    provider call, recorded as a skip not a failure, terminal non-retryable).
    When False (legacy retry wrapper path), it returns ``llm_status="failed"``
    with the exact same terminal reason - preserving the D5 contract
    (test_smoke.py prompt_budget_contract_violation test).
    """
    # Phase D §8: reset the per-call metadata thread-local BEFORE building
    # the prompt so stale values from a prior attempt cannot leak. The
    # builder stashes ``prompt_bytes`` + ``continuity_included`` into the
    # thread-local; ``_call_ga_llm`` stashes latency + effective settings.
    _llm_call_state_reset()

    # --- Build prompt: tier selection by attempt + last failure category ---
    builder_idx = min(attempt - 1, len(prompt_builders) - 1)
    # For attempt 2 with json_parse_failed, use strict JSON builder.
    # For attempt 3, use minimal safe builder.
    if attempt == 2 and last_category == "llm_json_parse_failed":
        builder_idx = 1  # strict JSON
    elif attempt >= 3:
        builder_idx = 2  # minimal safe
    prompt_builder = prompt_builders[builder_idx]
    prompt = prompt_builder(snapshot, fallback, context=context)

    # Phase D §8: capture prompt metadata for this attempt BEFORE the
    # provider call so it persists even if the call fails.
    _prompt_meta = _prompt_meta_snapshot()

    # Per-attempt attempt_meta. The caller's outer attempt_meta carries the
    # cross-attempt accumulators (cfg_name / model_name / schedule context /
    # running provider_call_count); the caller merges the relevant fields
    # from this dict.
    attempt_meta: dict[str, Any] = {
        "llm_status": "failed",
        "llm_error_category": None,
        "llm_error_stage": None,
        "llm_error": None,
        "llm_attempt_count": attempt,
        "llm_retry_round": attempt,
        "llm_config_name": cfg_name,
        "llm_model": model_name,
        "llm_fallback_reason": None,
        "llm_terminal_reason": None,
        "llm_provider_call_count": 0,
        "llm_latency_ms": None,
        "llm_prompt_bytes": _prompt_meta.get("prompt_bytes"),
        "llm_continuity_included": _prompt_meta.get("continuity_included"),
        "llm_repair_event": False,
        "llm_effective_thinking_budget_tokens": None,
        "llm_effective_max_output_tokens": None,
        "llm_effective_temperature": None,
        "llm_provider_timeout_ms": None,
    }

    # Phase D §5.1 / R3-1 (design §5.1): fail-closed when the MANDATORY CORE
    # exceeds the hard cap. 07-10 R3-1: continuity is PROTECTED and is NEVER
    # trimmed - it survives every tier. If the minimal stub STILL exceeds
    # ``max_prompt_bytes`` (with continuity retained), the prompt is over-
    # budget on mandatory core alone. Do NOT call the provider - return a
    # structured terminal reason. Continuity is preserved in the metadata.
    _gen_cfg = _resolve_generation_config()
    _prompt_bytes = _prompt_meta.get("prompt_bytes")
    if isinstance(_prompt_bytes, int) and _prompt_bytes > _gen_cfg["max_prompt_bytes"]:
        if budget_violation_is_skip:
            # Fair adapter path (P0-2): no provider call -> a SKIP, not a
            # failure. The coordinator records a budget skip (no breaker
            # event) and treats it as terminal non-retryable.
            attempt_meta["llm_status"] = "skipped"
        else:
            attempt_meta["llm_status"] = "failed"
        attempt_meta["llm_fallback_reason"] = "prompt_budget_contract_violation"
        attempt_meta["llm_terminal_reason"] = "prompt_budget_contract_violation"
        attempt_meta["llm_error_category"] = "llm_prompt_budget_violation"
        attempt_meta["llm_error_stage"] = "prompt_build"
        attempt_meta["llm_error"] = (
            f"prompt mandatory core {_prompt_bytes}B exceeds "
            f"max_prompt_bytes {_gen_cfg['max_prompt_bytes']}B; provider call suppressed"
        )
        attempt_meta["llm_provider_call_count"] = 0
        attempt_meta["llm_latency_ms"] = None
        # 08-04 Codex-P2 (D4): the builder stashed a one-shot
        # ``system_override``; a budget skip suppresses the provider call, so
        # the override was never consumed. Clear it so it cannot leak into the
        # next symbol on this worker thread.
        _llm_call_input_state_reset()
        return None, attempt_meta

    # Phase B P1-1 (07-22): provider admission AFTER prompt build, BEFORE the
    # provider call. Prefer a fresh read from the optional ``deadline`` object
    # so wall-clock spent on prompt build is accounted for; fall back to the
    # pre-resolved ``provider_timeout_seconds`` when no deadline is supplied
    # (legacy retry wrapper). Capture a single IMMUTABLE effective timeout; if
    # it is already exhausted (<=0) return a skip envelope WITHOUT calling the
    # provider. Socket floors (max(15, ...)) and subprocess floors (max(1.0,
    # ...)) must never re-admit a zero/negative timeout into a real call —
    # that is the production d49 defect (pcc>=1 with timeout_ms=0). Never use
    # max(1, remaining) to mask exhaustion.
    effective_provider_timeout_seconds = provider_timeout_seconds
    if deadline is not None:
        try:
            if bool(deadline.exhausted()):
                effective_provider_timeout_seconds = 0.0
            else:
                _pt_ms = deadline.provider_timeout_ms()
                effective_provider_timeout_seconds = (
                    None if _pt_ms is None else float(_pt_ms) / 1000.0
                )
        except Exception:
            # Keep the pre-resolved value if the deadline probe fails closed
            # only when it was already known; otherwise force skip.
            if effective_provider_timeout_seconds is None:
                effective_provider_timeout_seconds = 0.0
    if effective_provider_timeout_seconds is not None:
        try:
            _eff_s = float(effective_provider_timeout_seconds)
        except (TypeError, ValueError):
            _eff_s = 0.0
        if _eff_s <= 0:
            attempt_meta["llm_status"] = "skipped"
            attempt_meta["llm_fallback_reason"] = "wall_clock_budget_exhausted"
            attempt_meta["llm_terminal_reason"] = "symbol_timeout"
            attempt_meta["llm_error_category"] = None
            attempt_meta["llm_error_stage"] = "admission"
            attempt_meta["llm_error"] = (
                "per-symbol provider deadline exhausted after prompt build; "
                "provider call suppressed"
            )
            attempt_meta["llm_provider_call_count"] = 0
            attempt_meta["llm_provider_timeout_ms"] = 0
            attempt_meta["llm_latency_ms"] = None
            # 08-04 Codex-P2 (D4): deadline admission skip suppresses the
            # provider call, so the builder's one-shot ``system_override`` was
            # never consumed. Clear it so it cannot leak into the next symbol.
            _llm_call_input_state_reset()
            return None, attempt_meta
        # Immutable positive timeout for socket/subprocess/envelope. Do NOT
        # re-read the deadline after the call (post-call remaining may be 0
        # even when the call was admitted with remaining>0).
        effective_provider_timeout_seconds = _eff_s
        attempt_meta["llm_provider_timeout_ms"] = int(round(_eff_s * 1000.0))

    # --- ONE provider call + JSON parse ---
    # 07-10 R2-1: thread the per-symbol deadline's provider timeout into the
    # call via the thread-local ``_llm_call_state.provider_timeout_seconds``
    # (same channel as ``prompt_bytes`` / ``continuity_included``). This keeps
    # ``_call_ga_llm(prompt)``'s single-positional-arg signature unchanged so
    # existing test mocks (``def fake_call(prompt)``) work without modification.
    # When set (fair adapter path, or legacy wrapper with an injected deadline),
    # ``_call_ga_llm`` sets ``session.read_timeout`` + ``session.max_retries=0``
    # so the call is bounded. None (no deadline) preserves the 60s floor +
    # default retries.
    _llm_call_state.provider_timeout_seconds = effective_provider_timeout_seconds
    # 07-10 S4 (P0 #3): the fair adapter opts the call into process-isolation
    # hard timeout. When True (and a provider timeout is set), ``_call_ga_llm``
    # runs ``session.raw_ask`` in a child process bounded by
    # ``proc.join(timeout=)`` + ``terminate``/``kill`` - the guarantee the R2-2
    # thread barrier could not provide. False (legacy retry wrapper, default)
    # keeps the in-process call byte-for-byte unchanged.
    _llm_call_state.subprocess_hard_timeout = bool(subprocess_hard_timeout)
    raw: str | None = None
    category: str | None = None
    error_str: str | None = None
    stage: str | None = None
    try:
        try:
            raw = _call_ga_llm(prompt)
        finally:
            # Tests and alternate providers may replace _call_ga_llm entirely,
            # bypassing its normal one-shot input consumption. Always clear
            # call-control inputs at the owning attempt boundary so a later
            # call on the same worker thread cannot inherit hard isolation.
            _llm_call_input_state_reset()
        candidate = _parse_json_object(raw)
    except json.JSONDecodeError as exc:
        # 07-13 R6-D (P0-3.5 / §7.9): a truncated response (the provider hit
        # max_output_tokens mid-JSON -> incomplete JSON -> JSONDecodeError)
        # MUST classify as ``llm_output_truncated``, NOT a generic
        # ``llm_json_parse_failed``. The provider WROTE content; it ran out of
        # output budget. ``llm_output_truncated`` is retryable (strict/minimal
        # JSON retry) and isolated from the breaker (AC8) so one symbol's
        # truncation cannot skip the remaining symbols. Probe the thread-local
        # stop_reason captured by ``_call_ga_llm`` (max_tokens / length).
        _sr = _llm_call_effective_snapshot().get("stop_reason")
        _trunc = _classify_stop_reason(_sr)
        if _trunc is not None:
            category = _trunc
            error_str = f"truncated JSON output; stop_reason={_sr} ({str(exc)[:200]})"
            stage = "parse"
        else:
            category = _classify_llm_failure(exc, raw, "parse")
            error_str = str(exc)[:300]
            stage = "parse"
        candidate = None
    except ValueError as exc:
        err_msg = str(exc)[:300]
        # 07-13 R6-D (P0-3.5 / §7.9): a truncated response may parse to a
        # scalar / fragment (``not a JSON object``) rather than a clean
        # JSONDecodeError -- still reclassify as ``llm_output_truncated`` when
        # the provider's stop_reason confirms the output cap was hit.
        _sr_v = _llm_call_effective_snapshot().get("stop_reason")
        _trunc_v = _classify_stop_reason(_sr_v)
        if _trunc_v is not None:
            category = _trunc_v
            error_str = f"truncated JSON output; stop_reason={_sr_v} ({err_msg[:200]})"
            stage = "parse"
        elif "not a JSON object" in err_msg:
            category = "llm_json_parse_failed"
            error_str = err_msg
            stage = "schema"
        else:
            category = "llm_schema_validation_failed"
            error_str = err_msg
            stage = "schema"
        candidate = None
    except RuntimeError as exc:
        # 07-10 S4 (P0 #3) + R4-P0-2: a subprocess FATAL error is TERMINAL
        # non-retryable, NOT a transient transport error.
        # ``_run_subprocess_with_target`` raises a RuntimeError whose message
        # carries a DISTINCT structured signature for each fatal case:
        #   - ``llm_subprocess_hard_timeout``: the child outlived
        #     ``proc.join(timeout=)`` and was killed -> terminal ``symbol_timeout``.
        #   - ``llm_subprocess_cleanup_failed``: the child sent a result but
        #     could NOT be reaped after terminate+kill (orphan risk) -> terminal
        #     non-retryable. The provider WAS physically called, but retrying
        #     would spawn a NEW child while the old one leaks -> amplify orphan
        #     + resource exhaustion (R4-P0-2). MUST stop the symbol at
        #     attempt_count=1.
        #   - ``llm_subprocess_response_oversized``: the child response exceeded
        #     the IPC byte contract -> terminal non-retryable (the contract was
        #     violated; a retry would re-violate it).
        #   - ``llm_subprocess_start_failed``: ``proc.start()`` raised -> the
        #     subprocess runtime cannot spawn -> terminal non-retryable.
        # Detect EACH signature here - BEFORE ``_classify_llm_failure`` (which
        # would route them to ``llm_transport_error`` / retryable -> the
        # coordinator would retry, amplifying the orphan). The category is in
        # ``_NON_RETRYABLE_CATEGORIES`` so the coordinator's retry loop stops
        # at attempt 1 (``return None, attempt_meta`` with
        # ``llm_terminal_reason=<category>``).
        _exc_msg_s4 = str(exc)
        _subproc_fatal = None
        for _sig in (
            "llm_subprocess_hard_timeout",
            "llm_subprocess_cleanup_failed",
            "llm_subprocess_response_oversized",
            "llm_subprocess_start_failed",
        ):
            if _sig in _exc_msg_s4:
                _subproc_fatal = _sig
                break
        if _subproc_fatal is not None:
            category = _subproc_fatal
            error_str = _exc_msg_s4[:300]
            stage = "call"
            candidate = None
        else:
            category = _classify_llm_failure(exc, raw, "call")
            error_str = str(exc)[:300]
            stage = "call"
            candidate = None
    except Exception as exc:
        category = _classify_llm_failure(exc, raw, "call")
        error_str = str(exc)[:300]
        stage = "call"
        candidate = None

    # Phase D §8: the provider call DID happen (raw returned or raised after
    # a network/gateway round-trip). Count it and capture the latency +
    # effective settings from the thread-local (accurate even on the exception
    # path - ``_call_ga_llm`` stashes them in a ``finally`` block).
    if candidate is None:
        _failed_effective = _llm_call_effective_snapshot()
        attempt_meta["llm_provider_call_count"] = 1
        attempt_meta["llm_latency_ms"] = _failed_effective.get("latency_ms")
        attempt_meta["llm_effective_thinking_budget_tokens"] = _failed_effective.get("effective_thinking_budget_tokens")
        attempt_meta["llm_effective_max_output_tokens"] = _failed_effective.get("effective_max_output_tokens")
        attempt_meta["llm_effective_temperature"] = _failed_effective.get("effective_temperature")
        # 07-13 R6-D (P0-3.4): persist the provider's stop_reason so a
        # truncation (max_tokens) is auditable on the decision row / Phase F
        # diagnostics, distinct from a parse failure.
        attempt_meta["llm_stop_reason"] = _failed_effective.get("stop_reason")
        attempt_meta["llm_status"] = "failed"
        attempt_meta["llm_error_category"] = category
        attempt_meta["llm_error_stage"] = stage
        attempt_meta["llm_error"] = error_str
        # Terminal reason is set by the caller (retry wrapper / coordinator)
        # based on retryability; surface the category + stage so the caller
        # can classify. Non-retryable categories short-circuit upstream.
        return None, attempt_meta

    # --- Success path: unwrap + schema validate + schema-alias repair ---
    # 07-10 R1-1: moved INTO the single-attempt unit (was post-loop in
    # ``run_agent_sop_decision``). schema-validation-failed is non-retryable
    # (in ``_NON_RETRYABLE_CATEGORIES``), so classifying it here preserves
    # legacy behavior - a schema-invalid-but-parseable response fails closed
    # rather than retrying. Confirmed sound by the R1-1 reviewer.
    _effective = _llm_call_effective_snapshot()
    attempt_meta["llm_provider_call_count"] = 1
    attempt_meta["llm_latency_ms"] = _effective.get("latency_ms")
    attempt_meta["llm_effective_thinking_budget_tokens"] = _effective.get("effective_thinking_budget_tokens")
    attempt_meta["llm_effective_max_output_tokens"] = _effective.get("effective_max_output_tokens")
    attempt_meta["llm_effective_temperature"] = _effective.get("effective_temperature")
    attempt_meta["llm_stop_reason"] = _effective.get("stop_reason")

    unwrapped_candidate, unwrap_changed = _unwrap_wrapped_decision(candidate)
    if unwrapped_candidate is None:
        # Conflict: wrapper + top-level schema keys. Hard schema failure.
        attempt_meta["llm_status"] = "failed"
        attempt_meta["llm_error_category"] = "llm_schema_validation_failed"
        attempt_meta["llm_error_stage"] = "schema"
        attempt_meta["llm_error"] = "wrapped decision conflict: top-level + nested decision both present"
        attempt_meta["llm_fallback_reason"] = "schema_validation_failed"
        attempt_meta["llm_terminal_reason"] = "llm_schema_validation_failed"
        return None, attempt_meta

    candidate = unwrapped_candidate
    repair_event = False
    if unwrap_changed:
        # A successful unwrap is a repairable event. Surface it so the caller
        # emits the physical-success breaker event + the repairable event
        # (mirrors legacy llm_agent_judge.py:192 + :278).
        repair_event = True

    decision = _normalize_llm_decision(candidate, snapshot, fallback)
    ok, err = validate_json("ga_decision.schema.json", decision)
    if not ok:
        # Phase B/C (07-09): schema-alias repair path. Phase-2 D (07-27):
        # generalized to try ALL repairs in sequence (entry-trigger alias
        # first, then suggested_actions rebuild), re-validate ONCE at the
        # end. 07-31 P1-1: decision-array repair runs FIRST (before
        # suggested_actions), so a ``decision: [...]`` is folded back to a
        # string before any downstream semantic mapping. Each repair
        # function returns ``(repaired, notes, changed)``; when a repair
        # changes the decision, the next repair runs on the already-repaired
        # working copy so ALL repairs get a chance. A clean approach:
        # collect a list of repair functions, apply them in sequence to a
        # working copy, re-validate once at the end.
        repair_fns = (
            _try_repair_decision,
            _try_repair_entry_trigger_confirmation,
            _try_repair_take_profits,
            _try_repair_opportunity_watch,
            _try_repair_suggested_actions,
        )
        working = decision
        all_notes: list[str] = []
        decision_changed = False
        original_decision: Any = None
        entry_trigger_changed = False
        take_profits_changed = False
        original_take_profits: Any = None
        suggested_actions_changed = False
        original_entry_trigger_note: str | None = None
        original_suggested_actions: Any = None
        opportunity_watch_changed = False
        original_opportunity_watch: Any = None
        for _fn in repair_fns:
            working, notes, changed = _fn(working, snapshot)
            if changed:
                if _fn is _try_repair_decision:
                    decision_changed = True
                    # Capture the original (pre-repair) decision field for
                    # audit before the fold/downgrade overwrote it.
                    original_decision = decision.get("decision")
                elif _fn is _try_repair_entry_trigger_confirmation:
                    entry_trigger_changed = True
                    if notes:
                        original_entry_trigger_note = notes[0]
                elif _fn is _try_repair_take_profits:
                    take_profits_changed = True
                    # Capture the original (pre-repair) take_profits for
                    # audit before the numeric item was rewritten.
                    orig_tp = decision.get("trade_plan")
                    if isinstance(orig_tp, dict):
                        original_take_profits = orig_tp.get("take_profits")
                elif _fn is _try_repair_opportunity_watch:
                    opportunity_watch_changed = True
                    # Capture the original (pre-repair) opportunity_watch for
                    # audit before the rebuild overwrote it.
                    original_opportunity_watch = decision.get("opportunity_watch")
                elif _fn is _try_repair_suggested_actions:
                    suggested_actions_changed = True
                    # Capture the original (pre-repair) suggested_actions for
                    # audit before the rebuild overwrote it.
                    original_suggested_actions = decision.get("suggested_actions")
                all_notes.extend(notes)
        ok2, err2, err2_full = validate_json_detail("ga_decision.schema.json", working)
        if ok2:
            # Repaired success: ONE physical provider call that succeeded
            # after a schema-alias repair. Surface ``llm_repair_event``
            # so the caller emits BOTH breaker events (physical ok +
            # repairable), mirroring legacy llm_agent_judge.py:223-226.
            # P1-4 (07-27): set ``plan_origin="llm_confirmed"`` /
            # ``plan_execution_state="confirmed"`` ONLY when the repaired
            # decision actually carries a confirmed trade_plan. When the
            # LLM succeeded (repaired) but produced NO plan, keep the
            # fallback's ``plan_origin`` — nothing was confirmed.
            if (working.get("has_trade_plan") and working.get("trade_plan")
                    and working.get("llm_plan_source") == "llm_provided"):
                working["plan_origin"] = "llm_confirmed"
                working["plan_execution_state"] = "confirmed"
            existing_notes = list(working.get("risk_notes") or [])
            existing_notes.extend(all_notes)
            working["risk_notes"] = existing_notes
            parse_meta = working.get("llm_parse_meta") or {}
            if not isinstance(parse_meta, dict):
                parse_meta = {}
            if decision_changed:
                parse_meta["decision_repaired"] = True
                if original_decision is not None:
                    parse_meta["original_decision"] = original_decision
            if entry_trigger_changed and original_entry_trigger_note is not None:
                parse_meta["original_entry_trigger_type"] = original_entry_trigger_note
            if take_profits_changed:
                parse_meta["take_profits_repaired"] = True
                if original_take_profits is not None:
                    parse_meta["original_take_profits"] = original_take_profits
            if suggested_actions_changed:
                parse_meta["suggested_actions_repaired"] = True
                if original_suggested_actions is not None:
                    parse_meta["original_suggested_actions"] = original_suggested_actions
            if opportunity_watch_changed:
                parse_meta["opportunity_watch_repaired"] = True
                if original_opportunity_watch is not None:
                    parse_meta["original_opportunity_watch"] = original_opportunity_watch
            working["llm_parse_meta"] = parse_meta
            attempt_meta["llm_status"] = "ok"
            attempt_meta["llm_error_category"] = None
            attempt_meta["llm_error_stage"] = None
            attempt_meta["llm_error"] = None
            attempt_meta["llm_fallback_reason"] = None
            attempt_meta["llm_terminal_reason"] = "schema_repaired"
            attempt_meta["llm_repair_event"] = True
            # Merge the §8 envelope onto the repaired decision so the
            # success row carries complete attempt metadata (mirrors
            # legacy llm_agent_judge.py:246).
            working.update(attempt_meta)
            return working, attempt_meta
        err = err2
        decision = working
        # Hard schema failure - non-retryable, fail-closed.
        attempt_meta["llm_status"] = "failed"
        attempt_meta["llm_error_category"] = "llm_schema_validation_failed"
        attempt_meta["llm_error_stage"] = "schema"
        # P1-4 (07-31): llm_error carries the COMPACT field-path + type error
        # (single line, fits the Feishu recent-failure llm_error[:100] display
        # slice) — the multi-line jsonschema traceback is preserved in the new
        # llm_error_detail audit field (raw_decision_json §8 envelope).
        attempt_meta["llm_error"] = err2
        attempt_meta["llm_error_detail"] = err2_full
        attempt_meta["llm_fallback_reason"] = "schema_validation_failed"
        attempt_meta["llm_terminal_reason"] = "llm_schema_validation_failed"
        return None, attempt_meta

    # Normal success (no repair, or unwrap-only repair). Surface an unwrap
    # repair event so the caller emits both breaker events (mirrors legacy
    # :192 + :278); a plain success has no repair event (caller emits one
    # physical-ok record).
    # P1-4 (07-27): set ``plan_origin="llm_confirmed"`` /
    # ``plan_execution_state="confirmed"`` ONLY when the LLM actually
    # confirmed a trade_plan. When the LLM succeeded but produced NO plan
    # (monitor_only / no_edge), nothing was confirmed — setting
    # ``plan_origin=llm_confirmed`` would mislabel the row. In that case keep
    # ``plan_origin`` as the fallback's value (e.g. ``deterministic_sop``,
    # cleared of stale deterministic_* by _normalize_llm_decision only when a
    # plan WAS confirmed) and leave ``plan_execution_state`` as normalize left
    # it. The §8 attempt_meta envelope is still merged (it records that the
    # call succeeded regardless of whether a plan was produced).
    if (decision.get("has_trade_plan") and decision.get("trade_plan")
            and decision.get("llm_plan_source") == "llm_provided"):
        decision["plan_origin"] = "llm_confirmed"
        decision["plan_execution_state"] = "confirmed"
    attempt_meta["llm_status"] = "ok"
    attempt_meta["llm_error_category"] = None
    attempt_meta["llm_error_stage"] = None
    attempt_meta["llm_error"] = None
    attempt_meta["llm_fallback_reason"] = None
    attempt_meta["llm_terminal_reason"] = None
    attempt_meta["llm_repair_event"] = repair_event
    decision.update(attempt_meta)
    return decision, attempt_meta


# 08-02 P0-1: keys the raw deterministic reference is FORBIDDEN to carry.
# The old ``run_agent_sop_decision(use_llm=False)`` prompt fallback injected
# these (llm_status="disabled", llm_disabled blocker, plan_status="withheld",
# fallback_trade_plan_blocked=True, analysis_source, stale
# plan_execution_state) so the LLM read "deterministic engine disabled and
# plan withheld". ``run_ga_sop_decision`` never sets llm_* fields, so the
# builder below uses it as the source and strips any future drift.
_RAW_DETERMINISTIC_REFERENCE_FORBIDDEN_KEYS = frozenset({
    "llm_status",
    "llm_terminal_reason",
    "llm_fallback_reason",
    "llm_attempt_count",
    "llm_provider_call_count",
    "llm_latency_ms",
    "llm_model",
    "llm_prompt_bytes",
    "llm_continuity_included",
    "llm_schedule_round",
    "llm_schedule_position",
    "plan_execution_state",
    "fallback_trade_plan_blocked",
    "fallback_block_reason",
    "llm_fallback_blocked",
    "analysis_source",
})
_RAW_DETERMINISTIC_REFERENCE_FORBIDDEN_BLOCKER_CODES = frozenset({
    "llm_disabled",
    "llm_failed_fallback",
    "fallback_blocked",
})


def _build_raw_deterministic_reference(
        snapshot: dict[str, Any]) -> dict[str, Any]:
    """08-02 P0-1: build the prompt's ``deterministic_reference`` from the
    CLEAN raw deterministic SOP instead of the disabled ``use_llm=False``
    fallback.

    ``run_agent_sop_decision(use_llm=False)`` marks the decision
    ``llm_status="disabled"`` / ``llm_terminal_reason="llm_disabled"`` and then
    ``apply_risk_to_decision`` adds an ``llm_disabled`` blocker +
    ``plan_status="withheld"`` and clears the trade plan (fallback stripping).
    Feeding that into the prompt makes the LLM read "deterministic engine
    disabled and plan withheld" — the audit's P0-1 pollution chain.

    ``run_ga_sop_decision`` never touches llm_* fields, so it is the correct
    source: it preserves the original candidate/trade plan, grade, confidence,
    direction and structural evidence, and only carries REAL gate blockers
    (continuity-trigger invalidated / direction-flip without closed-candle
    confirmation). The reference is normalized so it can never smuggle the
    forbidden markers even if the raw SOP drifts.
    """
    raw = run_ga_sop_decision(snapshot) or {}
    reference = dict(raw)
    # Deterministic SOP is the origin; never inherit anything else.
    reference["plan_origin"] = "deterministic_sop"
    for key in _RAW_DETERMINISTIC_REFERENCE_FORBIDDEN_KEYS:
        reference.pop(key, None)
    blockers = reference.get("plan_blockers") or []
    if blockers:
        reference["plan_blockers"] = [
            b for b in blockers
            if not (isinstance(b, dict) and str(b.get("code") or "")
                    in _RAW_DETERMINISTIC_REFERENCE_FORBIDDEN_BLOCKER_CODES)
        ]
    return reference


def fair_llm_call_adapter(
    *,
    snapshot: dict[str, Any],
    deadline: Any,  # PerSymbolDeadline
    breaker: Any,
    retry_budget: Any,
    wall_clock_budget: Any,
    attempt: int,
    max_attempts: int,
    schedule_position: int | None = None,
    schedule_round: int | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """07-10 R1-1: the fair-scheduler ``llm_call_fn`` adapter. Matches the
    signature the coordinator invokes (llm_fair_scheduler.py
    ``_run_one_attempt``: ``snapshot, deadline, breaker, retry_budget,
    wall_clock_budget, attempt, max_attempts, schedule_position,
    schedule_round, context``).

    Does ONE provider call via the shared single-attempt unit
    (``_run_single_llm_attempt``). It does NOT wrap the 3x retry wrapper —
    the coordinator owns retry (next round), admission gates
    (``retry_budget.consume`` / ``deadline.exhausted`` / ``breaker.should_call``),
    and the breaker record. This satisfies the directive: "fair coordinator
    调用单次-attempt adapter，不能再套内部三次 retry wrapper".

    Provider-timeout admission is NOT done in this adapter. The adapter passes
    ``deadline=`` (and ``provider_timeout_seconds=None``) into
    ``_run_single_llm_attempt``, which re-reads ``deadline.provider_timeout_ms()``
    AFTER prompt build / BEFORE the provider call and captures an immutable
    ``llm_provider_timeout_ms`` (0 on skip; positive admitted ms on call). The
    adapter must never re-read or overwrite ``llm_provider_timeout_ms`` after
    the attempt returns — that post-call overwrite was the production d49 path.

    Returns ``(candidate_or_None, attempt_meta)`` exactly as the coordinator
    contract expects: ``llm_status`` ("ok" | "failed" | "skipped"),
    ``llm_error_category``, ``llm_fallback_reason``, ``llm_terminal_reason``,
    ``llm_repair_event`` (True on a schema-alias / unwrap repaired success so
    the coordinator emits BOTH breaker events — see the P0-1 fix in
    ``_run_one_attempt``), and the §8 fields. A prompt-budget contract
    violation returns ``llm_status="skipped"`` (P0-2: no provider call, a
    budget skip not a failure) so the coordinator records a skip and treats
    it as terminal non-retryable.
    """
    # 08-02 P0-1: build the deterministic reference for the prompt from the
    # CLEAN raw SOP. The old ``run_agent_sop_decision(use_llm=False)`` path
    # injected llm_status="disabled" + an llm_disabled fallback blocker +
    # plan_status="withheld" and stripped the trade plan, so the LLM read
    # "deterministic engine disabled and plan withheld". The raw SOP preserves
    # the original candidate/trade plan, grade, confidence and direction.
    fallback = _build_raw_deterministic_reference(snapshot)

    # Resolve config name + model (cached on breaker, same as the retry
    # wrapper). The adapter does NOT call breaker.record_attempt — the
    # coordinator does — but it reads breaker.llm_config_name /
    # breaker.llm_model so the §8 envelope carries the model name.
    cfg_name = getattr(breaker, "llm_config_name", None)
    if cfg_name is None:
        try:
            cfg_name = _resolve_llm_config_name()
            breaker.llm_config_name = cfg_name
        except Exception:
            cfg_name = "unknown"
            breaker.llm_config_name = cfg_name
    model_name = getattr(breaker, "llm_model", None)
    if model_name is None:
        model_name = _resolve_llm_model(cfg_name)
        breaker.llm_model = model_name

    # Phase B P1-1 (07-22): do NOT resolve/overwrite the provider timeout here.
    # Admission + the immutable effective timeout MUST be captured inside
    # ``_run_single_llm_attempt`` AFTER prompt build and BEFORE the provider
    # call (passing ``deadline=`` so wall-clock spent on fallback + prompt is
    # accounted for). A post-call re-read of ``deadline.provider_timeout_ms()``
    # was the production d49 defect path: a call admitted with remaining>0
    # finished with remaining=0, then the adapter overwrote the envelope to
    # timeout_ms=0 while pcc>=1. Never mask exhaustion with max(1, remaining).

    # 07-10 S4 (P0 #3): opt the fair-path call into process-isolation hard
    # timeout. Read from ``llm.scheduling.subprocess_hard_timeout`` (default
    # True - the fair path's hard-timeout guarantee is the P0 #3 fix; the
    # legacy serial path never reaches the fair adapter). Tests that patch
    # ``_call_ga_llm`` (which REPLACES the function, so the subprocess block
    # inside it never runs) are unaffected by this flag - it only governs
    # whether the REAL ``_call_ga_llm`` spawns a child for the provider call.
    subprocess_hard_timeout = _resolve_subprocess_hard_timeout()

    candidate, attempt_meta = _run_single_llm_attempt(
        snapshot=snapshot, fallback=fallback, context=context,
        attempt=attempt, max_attempts=max_attempts, breaker=breaker,
        cfg_name=cfg_name, model_name=model_name,
        prompt_builders=(
            build_llm_decision_prompt,
            build_llm_strict_json_prompt,
            build_llm_minimal_safe_prompt,
        ),
        last_category=None,  # the coordinator retries with a fresh attempt;
        # the single-attempt unit's tier selection is by ``attempt`` index.
        budget_violation_is_skip=True,  # P0-2: budget violation is a skip
        provider_timeout_seconds=None,  # resolved post-prompt from deadline
        subprocess_hard_timeout=subprocess_hard_timeout,  # S4: hard-kill child
        deadline=deadline,  # P1-1: post-prompt admission + immutable timeout
    )

    # Surface schedule context only. Do NOT overwrite
    # ``llm_provider_timeout_ms`` — the immutable value from admission lives
    # on attempt_meta already (0 on skip; positive admitted timeout on call).
    attempt_meta["llm_schedule_round"] = schedule_round
    attempt_meta["llm_schedule_position"] = schedule_position

    return candidate, attempt_meta


def build_llm_decision_prompt(snapshot: dict[str, Any], deterministic_decision: dict[str, Any], *, context: dict[str, Any] | None = None) -> str:
    from plugins.crypto_guard.config.loader import load_config
    scoring = score_snapshot(snapshot)
    risk_cfg = load_config().trading_mode.get("risk", {})
    min_rr = risk_cfg.get("min_rr", 1.5)
    min_conf = risk_cfg.get("min_confidence", 0.72)
    payload = {
        # 07-31 final review P1-2: EVERY scalar contract field is a
        # {type: string, enum: [...]} dict and suggested_actions a
        # {type: array, items: {type: string, enum: [...]}} dict — NEVER a
        # bare array (a bare array teaches the model the field may BE an
        # array; production evidence #1). Shared verbatim by all three real
        # provider tiers via _schema_contract().
        "schema_contract": _schema_contract(),
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
            # Phase D (07-09): tighten the type contract. The schema enum
            # only allows "closed_candle_confirmation"; alias values such
            # as price_rejection/pullback_rejection/breakout_retest/
            # reclaim_confirmation are LLM-invented and trigger a
            # schema-validation failure. The semantic trigger style must
            # be encoded in event_type/reason/evidence/risk_notes instead.
            "entry_trigger_confirmation.type 必须恒等于 \"closed_candle_confirmation\"；禁止使用 price_rejection / pullback_rejection / breakout_retest / reclaim_confirmation 等别名",
            "若无法提供完整 closed-candle 确认对象，请将 entry_trigger_confirmation 设为 null，不要发明 type 值",
            "触发风格（price_rejection/pullback/breakout_retest/reclaim）请写入 event_type、reason、evidence 或 risk_notes，不要写入 type",
            # Phase-2 D (07-27): tighten the suggested_actions contract. The
            # schema enum only allows the 5 flat string values below; the LLM
            # sometimes emits decision-enum values (wait_for_breakout /
            # wait_for_reclaim / avoid_chop) inside suggested_actions, which
            # are schema-invalid. The repair rebuilds the canonical list from
            # decision semantics, but the prompt must instruct the LLM to emit
            # a flat array of ONLY the 5 enum values so the repair is a
            # fallback, not the common path.
            "suggested_actions 必须是扁平字符串数组，仅取以下 5 个值之一或多个：create_paper_order、create_opportunity_watch、add_to_watchlist、ignore、monitor_only。合法示例：[\"monitor_only\"]、[\"create_paper_order\"]。非法示例：[\"monitor_only\",\"wait_for_breakout\",\"avoid_chop\"]（wait_for_breakout/avoid_chop 属于 decision 字段，不得放入 suggested_actions）",
            # 07-31 P1-3: verbatim type contracts (production evidence #1/#2).
            # These strings are asserted verbatim by test_pg_prompt_type_contract_p1_3.
            _PROMPT_DECISION_TYPE_RULE,
            _PROMPT_SUGGESTED_ACTIONS_TYPE_RULE,
            _PROMPT_TAKE_PROFITS_TYPE_RULE,
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
            # 08-04 contract C7 (reviewer round-2 P2-1): the repository returns
            # ``SELECT *`` rows keyed ``watch_reason`` (schema
            # ``opportunity_watches.watch_reason``); the deterministic reason is
            # authoritative and MUST reach the prompt. Fall back to the legacy
            # ``reason`` key for any hand-built context dicts that still use it.
            payload["active_watches"] = [
                {"symbol": w.get("symbol"), "direction": w.get("direction"),
                 "reason": w.get("watch_reason") or w.get("reason")}
                for w in watches[:5]
            ]

    # R5 P1-2 fix: bound the final prompt size. The 24 KiB feature pack
    # budget only constrains ``multi_timeframe_feature_pack``; the full
    # prompt (with ``modules``, ``historical_memory``, ``open_positions``,
    # ``active_watches``, ``analysis_continuity``) can blow past 48 KiB
    # and exceed the LLM context window. Trim ``historical_memory``
    # first (least actionable), then ``open_positions``/``active_watches``
    # (context-only), then ``modules`` (primary-TF detail — last resort
    # because it carries decision-critical indicator values). Never trim
    # ``market_snapshot.multi_timeframe_feature_pack`` or
    # ``deterministic_reference`` here — those are decision-critical and
    # have their own bounded budgets.
    # 07-10 R3-1 (design §5.1): ``analysis_continuity`` is PROTECTED and is
    # NEVER a trim tier. Pre-R3 (R8 P1) the ladder popped it after
    # open_positions/active_watches, silently breaking the cross-round
    # continuity contract precisely when the symbol was under budget
    # pressure. Now continuity survives every trim tier and is surfaced in
    # the minimal-stub fallback (R3-2) as a top-level key. If the minimal
    # stub WITH continuity still exceeds the budget, the retry wrapper's
    # prompt-budget-contract check (§5.1) fails closed with
    # ``prompt_budget_contract_violation`` rather than dropping continuity.
    # R6 REC-R6-1: added ``modules`` as a final trim tier so an oversized
    # primary-TF modules dict cannot silently push the prompt past budget.
    # R8 P1 fix:
    #   - Final hard assertion: if every trim tier fails to bring the
    #     prompt under budget, replace the payload with a minimal safe
    #     fallback (symbol + analysis_time + hard_rules + deterministic
    #     decision + continuity). Pre-R8 the function returned the
    #     oversized prompt as a last resort, blowing past the cap.
    #   - Minimal stub ready-path: read ``m.health.ready`` (correct
    #     path — feature pack module's health is a sub-dict, not a
    #     top-level field). Pre-R8 the stub read ``m.ready`` which is
    #     always ``None`` in production, hiding real readiness state
    #     behind a silent None.
    # R9 P2-2 fix: ``MAX_PROMPT_BYTES`` is now a module-level constant
    # so the safe_payload fallback can be exercised behaviorally in
    # tests via ``unittest.mock.patch``.
    if _market_total_context_bytes(SYSTEM_PROMPT, _market_user_body(payload)) > MAX_PROMPT_BYTES:
        payload.pop("historical_memory", None)
        if _market_total_context_bytes(SYSTEM_PROMPT, _market_user_body(payload)) > MAX_PROMPT_BYTES:
            payload.pop("open_positions", None)
            payload.pop("active_watches", None)
            # 07-10 R3-1 (design §5.1): ``analysis_continuity`` is PROTECTED
            # across EVERY prompt tier - it carries the cross-round grade/
            # bias/trigger state the LLM needs to keep decisions consistent
            # under retry pressure. Pre-R3 the ladder popped it here (before
            # modules), silently breaking the continuity contract precisely
            # when the symbol was under budget pressure. The pop is removed:
            # continuity survives every trim tier and is surfaced in the
            # minimal-stub fallback (R3-2) too. If the mandatory core
            # (minimal stub WITH continuity) still exceeds the budget, the
            # minimal-stub tier below emits a ``prompt_budget_contract_violation``
            # fail-closed rather than dropping continuity.
            if _market_total_context_bytes(SYSTEM_PROMPT, _market_user_body(payload)) > MAX_PROMPT_BYTES:
                market_snapshot = payload.get("market_snapshot")
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
                # R7 P1 fix: final hard cap. If the prompt is STILL over
                # budget after every trim tier, the oversized payload is
                # ``market_snapshot.multi_timeframe_feature_pack`` or
                # ``deterministic_reference`` - neither was trimmed above
                # because both are decision-critical. Replace the
                # feature pack with a minimal stub (symbol + per-TF
                # ready flag only) and the deterministic_reference with
                # a one-line summary. This guarantees the prompt stays
                # under the LLM context window even when the upstream
                # producer emits a pathological payload. Pre-R7 the
                # function just returned the oversized prompt - a 100KB
                # feature pack produced a 103KB prompt, blowing past
                # the 48KB cap.
                if _market_total_context_bytes(SYSTEM_PROMPT, _market_user_body(payload)) > MAX_PROMPT_BYTES:
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
                    # Final assertion: if STILL over, drop historical_memory
                    # was already tried - drop deterministic_reference
                    # entirely (LLM still has market_snapshot + hard_rules).
                    if _market_total_context_bytes(SYSTEM_PROMPT, _market_user_body(payload)) > MAX_PROMPT_BYTES:
                        payload.pop("deterministic_reference", None)
                        # R8 P1 fix: final hard assertion. If EVERY
                        # trim tier failed, replace the payload with
                        # a minimal safe fallback (decision-critical
                        # fields only) so the prompt is guaranteed
                        # under budget. Pre-R8 the function returned
                        # the oversized prompt as a last resort.
                        if _market_total_context_bytes(SYSTEM_PROMPT, _market_user_body(payload)) > MAX_PROMPT_BYTES:
                            safe_dr = deterministic_decision or {}
                            # R10 P1 fix: read symbol/analysis_time_utc
                            # from ``market_snapshot`` (the actual
                            # location) or ``deterministic_decision``,
                            # NOT from payload top level. Pre-R10 the
                            # code read ``payload.get("symbol")`` which
                            # returned None - ``symbol`` and
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
                            # provided them - a self-contradicting
                            # prompt payload that the LLM could not
                            # satisfy.
                            safe_ms = payload.get("market_snapshot") or {}
                            safe_payload = {
                                "symbol": safe_ms.get("symbol") or safe_dr.get("symbol"),
                                "analysis_time_utc": safe_ms.get("analysis_time_utc") or safe_dr.get("analysis_time_utc"),
                                "strategy_name": safe_dr.get("strategy_name"),
                                "strategy_version": safe_dr.get("strategy_version"),
                                "hard_rules": payload.get("hard_rules"),
                                # 07-10 R3-2 (design §5.1): continuity is
                                # PROTECTED across EVERY prompt tier, including
                                # this minimal-stub fallback. Pre-R3 the stub
                                # dropped continuity entirely, breaking the
                                # cross-round continuity contract precisely
                                # when the symbol was under the most budget
                                # pressure. Surface it as a top-level key
                                # (mirroring build_llm_minimal_safe_prompt
                                # line ~592) so the LLM still sees prior
                                # grade/bias/trigger state. If the stub WITH
                                # continuity still exceeds MAX_PROMPT_BYTES,
                                # the metadata-capture below records
                                # continuity_included=True and the retry
                                # wrapper's budget-contract check fails closed
                                # with prompt_budget_contract_violation rather
                                # than silently dropping continuity.
                                "analysis_continuity": safe_ms.get("analysis_continuity"),
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
                            # Phase D §8: reassign ``payload`` so the
                            # metadata-capture block below sees the
                            # minimal-stub payload. R3-2 surfaces
                            # ``analysis_continuity`` as a top-level key
                            # in the stub, so the capture block below
                            # reads continuity_included=True even here
                            # (not False as pre-R3).
                            payload = safe_payload
    # 08-04 Codex-P2 (D4): the market builder returns the user payload ALONE
    # (the structured JSON input) and selects this round's system prompt via a
    # one-shot ``system_override`` on the thread-local — ``_call_ga_llm`` sets
    # ``session.system`` from it and sends the body verbatim as user text,
    # never duplicating the system prompt.
    # 07-10 Phase D §5.1/§8: stash prompt byte size + whether
    # ``analysis_continuity`` survived the trim ladder. The retry wrapper
    # reads this via ``_prompt_meta_snapshot()`` to persist
    # ``llm_prompt_bytes`` / ``llm_continuity_included`` for both successes
    # and failures. Continuity is PROTECTED (design §5.1): R3-1 removed the
    # trim step that popped it, and R3-2 surfaces it in the minimal-stub
    # fallback as a top-level key. So continuity survives EVERY tier; the
    # minimal-stub path reassigns ``payload = safe_payload`` (no
    # ``market_snapshot``) but carries ``analysis_continuity`` at the top
    # level — check BOTH locations (mirroring build_llm_minimal_safe_prompt
    # line ~616-619) so continuity_included is accurate for every trim
    # outcome.
    _cont = payload.get("analysis_continuity")
    if _cont is None:
        _ms = payload.get("market_snapshot")
        _cont = _ms.get("analysis_continuity") if isinstance(_ms, dict) else None
    # D4 R7: prompt_bytes is the REAL provider total context = system bytes +
    # user body bytes (NOT under-reported to the body alone).
    _user_body = _market_user_body(payload)
    _llm_call_state.system_override = SYSTEM_PROMPT
    _llm_call_state.prompt_bytes = _market_total_context_bytes(SYSTEM_PROMPT, _user_body)
    _llm_call_state.continuity_included = _cont is not None
    return _user_body


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

    # 08-04 contract C6: historical memory must NOT directly grant confidence,
    # S/A grade or order eligibility. The old ``instruction`` told the model to
    # adjust confidence +/-0.05~0.15 from memory — removed. Memory is now
    # untrusted background context only; the deterministic score/grade pipeline
    # is the only confidence/eligibility source.
    return {
        "description": "历史分析反馈记忆（非权威参考，不得直接据此调整置信度或评级）。",
        "skills": {skill: items[:3] for skill, items in by_skill.items()},  # max 3 per skill
        "untrusted_data": True,
        "instruction": "历史记忆仅作为背景参考（untrusted_data），不得依据记忆调整评级或下单资格。",
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
    # 08-04 contract D2 + D4 (reviewer round-2 G1/P1-1): the returned string is
    # the USER-MESSAGE text sent to the model. The per-task system prompt is
    # routed to ``session.system`` ONLY (``run_agent_json_task`` stashes it as
    # the thread-local ``system_override`` read by ``_call_ga_llm``) — it must
    # NOT be prepended here, or the model receives the same prompt text twice
    # (once in ``session.system`` and once at the head of the user message).
    # Return the JSON body alone; the caller resolves the system prompt.
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 07-10 Phase D: generation controls + per-call metadata capture.
#
# Design §6: scheduled JSON analysis must not use unbounded adaptive
# thinking. The ``llm.generation`` config segment (validated by
# ``config/loader.py``) sets explicit thinking-budget / max-output-token /
# temperature values for the JSON synthesis call. ``_call_ga_llm`` applies
# them to the resolved ``llmcore`` session and stashes the EFFECTIVE values
# (what actually landed on the session) into a thread-local so the retry
# wrapper can persist them without changing ``_call_ga_llm``'s signature
# (tests patch ``_call_ga_llm`` as a single-arg str→str callable; adding a
# kwarg would break ~20 patches).
#
# Thread-local (not module-global) because Phase C runs provider calls
# concurrently inside a bounded executor — each in-flight call must record
# its own latency/effective-settings without cross-thread clobbering.
# ---------------------------------------------------------------------------
import threading as _threading_mod

_llm_call_state = _threading_mod.local()


def _llm_call_state_reset() -> None:
    """Clear the per-call metadata thread-local before a provider call.

    Also resets the prompt-builder metadata (``prompt_bytes`` /
    ``continuity_included``) so a stale value from a previous build cannot
    leak into a new attempt's persisted metadata.
    """
    for attr in (
        "latency_ms", "effective_thinking_budget_tokens",
        "effective_max_output_tokens", "effective_temperature",
        "effective_thinking_type",
        "prompt_bytes", "continuity_included",
        "provider_timeout_seconds",
        "subprocess_hard_timeout",
        "stop_reason",
        "system_override",
    ):
        if hasattr(_llm_call_state, attr):
            delattr(_llm_call_state, attr)


def _llm_call_input_state_reset() -> None:
    """Clear one-shot provider-call controls without erasing output metadata.

    08-04 Codex-P2 (D4): ``system_override`` is one-shot input state — the
    builders stash it, ``_call_ga_llm`` consumes it. Clearing it here (called
    at the owning attempt boundary in ``_run_single_llm_attempt``'s finally)
    guarantees a stale override cannot leak into the next symbol on the same
    worker thread even when ``_call_ga_llm`` is replaced/mocked.
    """
    for attr in (
        "provider_timeout_seconds",
        "subprocess_hard_timeout",
        "system_override",
    ):
        if hasattr(_llm_call_state, attr):
            delattr(_llm_call_state, attr)

def _llm_call_effective_snapshot() -> dict[str, Any]:
    """Return the effective generation settings stashed by ``_call_ga_llm``.

    Returns an empty dict when nothing was recorded (e.g. when the test
    patches ``_call_ga_llm`` with a bare ``return_value=...`` that never
    touches the thread-local). The retry wrapper treats missing values as
    ``None`` so downstream persistence is safe.
    """
    out: dict[str, Any] = {}
    for attr in (
        "latency_ms", "effective_thinking_budget_tokens",
        "effective_max_output_tokens", "effective_temperature",
        "effective_thinking_type", "stop_reason",
    ):
        if hasattr(_llm_call_state, attr):
            out[attr] = getattr(_llm_call_state, attr)
    return out


def _prompt_meta_snapshot() -> dict[str, Any]:
    """Return the prompt-builder metadata stashed by ``build_llm_decision_prompt``.

    Captures the FINAL prompt byte size and whether ``analysis_continuity``
    survived the trim ladder (design §5.1 — continuity is protected; the
    trim ladder only drops it as a last resort before the minimal stub).
    Returns an empty dict when nothing was recorded.
    """
    out: dict[str, Any] = {}
    for attr in ("prompt_bytes", "continuity_included"):
        if hasattr(_llm_call_state, attr):
            out[attr] = getattr(_llm_call_state, attr)
    return out


def _resolve_subprocess_hard_timeout() -> bool:
    """07-10 S4 (P0 #3): whether the fair-path provider call runs in a child
    process with a hard wall-clock bound.

    Reads ``llm.scheduling.subprocess_hard_timeout`` from the loaded config
    (default True - the fair path's hard-timeout guarantee is the P0 #3 fix).
    The ``CRYPTO_GUARD_LLM_SUBPROCESS_HARD_TIMEOUT`` env var overrides the
    config: ``0``/``false``/``no`` disables it (the S4 test uses this to
    keep existing fair-path tests in-process while it exercises the
    subprocess path in isolation), any other value enables it.

    Only the fair adapter consults this; the legacy retry wrapper never opts
    in, so the legacy in-process ``_call_ga_llm`` is unchanged.
    """
    env_val = os.environ.get("CRYPTO_GUARD_LLM_SUBPROCESS_HARD_TIMEOUT")
    if env_val is not None:
        return env_val.strip().lower() not in {"0", "false", "no", "off", ""}
    try:
        from plugins.crypto_guard.config.loader import load_config
        sched = (
            load_config().trading_mode.get("llm", {}).get("scheduling", {})
            or {}
        )
        val = sched.get("subprocess_hard_timeout", True)
        return bool(val)
    except Exception:
        return True


def _resolve_generation_config() -> dict[str, Any]:
    """Read ``llm.generation`` from ``trading_mode.yaml`` with safe defaults.

    Design §5.1/§6 contract:
    - ``max_prompt_bytes``: mandatory-core hard cap. Exceeding it (with the
      mandatory core still assembled) returns
      ``prompt_budget_contract_violation`` WITHOUT a provider call.
    - ``target_prompt_bytes``: optional-section trim target (<=32 KiB).
    - ``max_output_tokens``: explicit output token limit for structured JSON.
    - ``thinking_budget_tokens``: explicit thinking budget; 0 disables
      extended thinking for the JSON synthesis call.
    - ``temperature``: low temperature for deterministic structured JSON.

    Defaults match ``config/trading_mode.yaml`` so a missing segment does not
    change behavior (and so tests that build a minimal ``trading_mode.yaml``
    still get sane values).
    """
    try:
        from plugins.crypto_guard.config.loader import load_config
        gen = load_config().trading_mode.get("llm", {}).get("generation", {}) or {}
    except Exception:
        gen = {}
    if not isinstance(gen, dict):
        gen = {}
    # 07-13 R6-D (P0-3) + R7 (P1-1): safe defaults MUST be a valid pair --
    # thinking strictly less than max_output, AND the structured-answer reserve
    # (max_output - thinking) at least ``min_structured_answer_tokens``. The
    # pre-fix defaults (max_output=4096, thinking=6000) violated the contract
    # and truncated output at 4096 tokens (stop_reason=max_tokens).
    # ``config/loader.py`` enforces both invariants at load time; these
    # defaults are the fallback when the generation segment is absent.
    max_out = int(gen.get("max_output_tokens", 8192))
    think = int(gen.get("thinking_budget_tokens", 2048))
    min_reserve = int(gen.get("min_structured_answer_tokens", 4096))
    if think > 0 and think >= max_out:
        # Defense in depth: the loader already rejects this, but a caller
        # building a minimal config dict in-process (bypassing the loader
        # validator) must not silently get a truncating pair. Disable
        # extended thinking so the answer reserve is the full max_output.
        think = 0
    if think > 0 and (max_out - think) < min_reserve:
        # Defense in depth for the reserve-minimum invariant (plan P0-3 item
        # 1 / AC6). A caller bypassing the loader validator must not silently
        # get a below-floor reserve. Disable thinking so the full max_output
        # is the answer reserve.
        think = 0
    return {
        "max_prompt_bytes": int(gen.get("max_prompt_bytes", 48 * 1024)),
        "target_prompt_bytes": int(gen.get("target_prompt_bytes", 32 * 1024)),
        "max_output_tokens": max_out,
        "thinking_budget_tokens": think,
        "min_structured_answer_tokens": min_reserve,
        "temperature": float(gen.get("temperature", 0.2)) if gen.get("temperature") is not None else 0.2,
    }


# ---------------------------------------------------------------------------
# 07-10 S4 (P0 #3): process-isolation hard timeout for the provider call.
#
# The fair scheduler's barrier ``fut.result(timeout=)`` (R2-2) bounds the
# WAIT, but Python cannot kill a running thread, so a truly hung provider call
# keeps the worker thread (and thus ``executor.shutdown(wait=True)``) alive
# until the socket ``read_timeout`` (R2-1) eventually unblocks - which is a
# per-packet bound, NOT a whole-call bound. A slow-but-steady stream or a
# wedged gateway can outlast it indefinitely.
#
# S4 closes that hole with PROCESS isolation: when the fair adapter opts in
# (``subprocess_hard_timeout``), ``_call_ga_llm`` runs the actual
# ``session.raw_ask`` in a child process and ``proc.join(timeout=)`` +
# ``terminate``/``kill`` guarantees the call is hard-bounded by the per-symbol
# deadline's provider timeout. The child is a fresh ``multiprocessing``
# process (Windows spawn re-imports this module), so the target MUST be a
# module-level function (closures / lambdas are not picklable under spawn).
#
# 07-10 R3-P1-3 (terminal-review-repair-plan-r3 §5): the production target
# ``_llm_subprocess_target`` MUST NOT read any test-only environment variable.
# Pre-R3 the target read ``CRYPTO_GUARD_LLM_SUBPROC_TEST_SLEEP`` /
# ``_RESPONSE`` / ``_RESPONSE_FILE`` to drive the S4 tests via env pollution --
# but those same env vars, if ever set in the PRODUCTION environment (a CI
# leak, an inherited shell, a misconfigured wrapper), would silently replace
# the real LLM response with a canned string, bypassing the provider entirely.
# That is an injectable-backdoor into the production LLM path. R3 removes every
# production read of those env vars. Tests now drive the subprocess lifecycle
# through an EXPLICIT injected module-level picklable test target
# (``_test_subprocess_target``) passed to the generic private runner
# ``_run_subprocess_with_target`` -- the production wrapper
# ``_run_provider_call_in_subprocess`` ALWAYS supplies the real
# ``_llm_subprocess_target`` and never accepts a test override through env.
# ---------------------------------------------------------------------------
# 07-10 R3-P1-2 (terminal-review-repair-plan-r3 §4): a single unified
# ``_reap_child`` helper is used on EVERY subprocess exit path (normal
# payload / child error / EOF-no-result / hard timeout / parent recv failure /
# proc.start() failure / unexpected exception). Pre-R3 the success path did a
# bare ``proc.join(timeout=2.0)`` with NO ``is_alive()`` check, so a child
# that sent a valid payload but hung in interpreter shutdown (daemon thread
# cleanup, atexit handlers, lingering socket) left an ORPHAN process whose
# PID kept consuming resources / holding the port after the parent reported
# success. ``_reap_child`` joins -> terminate -> join -> kill -> final join ->
# verify-dead -> close-handle, so NO path can leave a live child.
# ---------------------------------------------------------------------------


# 07-10 R3-P1-3 §5: maximum accepted IPC response size. A provider that sends
# an unbounded payload would let the child block the parent's pipe drain
# indefinitely (the P0-2 feeder-deadlock class, just without the feeder). Cap
# the accepted payload well above the 128 KiB regression test but below the
# point where the child's ``Pipe.send`` could stall the parent: 2 MiB is far
# larger than any legitimate structured GADecision JSON (the largest real
# response is a few KiB; the 128 KiB test exercises the pipe-buffer path and
# stays well under this cap). An oversized response FAILS CLOSED with the
# distinct structured reason ``llm_subprocess_response_oversized`` so the
# operator can distinguish a runaway provider from a normal large response.
DEFAULT_MAX_SUBPROCESS_RESPONSE_BYTES = 2 * 1024 * 1024


def _safe_send(conn: Any, payload: tuple) -> None:
    """Send ``payload`` on a Pipe write end, swallowing a broken pipe.

    P0-2: when the parent times out / kills the child, it CLOSES its read end
    of the pipe. If the child then tries to ``send`` its result, the write end
    raises ``BrokenPipeError``. That is EXPECTED (the parent already declared a
    hard-timeout) and must NOT mask the parent's timeout by raising a different
    exception that the child's top-level ``except BaseException`` would wrap as
    an ``error`` envelope -- which could then race the hard-timeout and confuse
    the parent. So a broken/empty pipe is a silent no-op here: the child is
    being killed regardless, and the parent's timeout decision stands.
    """
    try:
        conn.send(payload)
    except (BrokenPipeError, OSError):
        pass


def _measure_payload_bytes(payload: tuple) -> int:
    """Measure the encoded byte length of the raw-response element of a
    subprocess payload tuple, fail-safe.

    The payload contract is ``("ok", raw_str, effective_dict)`` /
    ``("error", exc_type, exc_msg)``. The byte-heavy element is the SECOND one
    (the raw response string). We measure ITS UTF-8 byte length -- NOT the
    whole pickled envelope -- because the raw response is what a runaway
    provider could make unbounded; the small wrapper fields are bounded.
    Returns 0 on any shape mismatch so the check never false-triggers.
    """
    try:
        if isinstance(payload, tuple) and len(payload) > 1:
            raw = payload[1]
            if isinstance(raw, str):
                return len(raw.encode("utf-8", "replace"))
            if isinstance(raw, (bytes, bytearray)):
                return len(raw)
    except Exception:
        pass
    return 0


def _send_subprocess_payload(
    conn: Any,
    payload: tuple,
    *,
    max_response_bytes: int = DEFAULT_MAX_SUBPROCESS_RESPONSE_BYTES,
) -> None:
    """07-10 R4-P1-3: send a subprocess payload, checking the encoded byte
    length of the raw response BEFORE it crosses IPC.

    Pre-R4 the 2 MiB contract was enforced ONLY on the parent side AFTER
    ``parent_conn.recv()`` (llm_agent_judge.py ~2426). That meant an oversized
    response had ALREADY been pickled by the child, sent across the Pipe,
    allocated in the parent's memory, and -- critically -- the child's
    ``Pipe.send`` could have STALLED blocking on the OS pipe buffer waiting
    for the parent to drain, exactly the feeder-deadlock class the P0-2 Pipe
    fix was meant to eliminate. The comment "防止 runaway IPC payload" was
    not honored: the runaway payload had already crossed IPC by the time it
    was rejected.

    R4-P1-3 moves the FIRST line of defense into the child, BEFORE
    ``conn.send``: if the raw response's UTF-8 byte length exceeds
    ``max_response_bytes``, the child sends ONLY a small structured error
    envelope -- ``("error", "llm_subprocess_response_oversized", <compact
    reason>)`` -- instead of the oversized payload. That envelope is well
    under the cap (a few hundred bytes), so it crosses IPC instantly and the
    child exits cleanly. The parent then surfaces the distinct
    ``llm_subprocess_response_oversized`` reason (terminal non-retryable,
    R4-P0-2) instead of receiving + discarding a multi-MiB blob.

    The parent KEEPS its post-``recv`` size check (in
    ``_run_subprocess_with_target``) as defense-in-depth: a malicious /
    buggy child that bypasses this helper would still be rejected at the
    parent. The child-side check is the primary guard; the parent check is
    the backstop.

    ``payload`` shapes other than ``("ok", <raw>, ...)`` (e.g. the small
    ``("error", ...)`` envelopes a child sends on its OWN exception) are
    always tiny and are sent unchanged -- only the raw-response-bearing ``ok``
    envelope is size-gated.
    """
    try:
        _n = _measure_payload_bytes(payload)
        if _n > int(max_response_bytes):
            # Send a SMALL error envelope instead of the oversized payload.
            # The parent's runner sees tag=="error" with this reason and
            # surfaces ``llm_subprocess_response_oversized`` (non-retryable).
            _safe_send(conn, (
                "error", "llm_subprocess_response_oversized",
                (
                    f"llm_subprocess_response_oversized: child response "
                    f"{_n} bytes exceeds the {max_response_bytes}-byte "
                    f"contract (rejected pre-send)"
                )[:300],
            ))
            return
        _safe_send(conn, payload)
    except (BrokenPipeError, OSError):
        # Parent already closed the pipe (timeout/kill). Same semantics as
        # ``_safe_send``: the child is being killed regardless; do not mask
        # the parent's timeout decision.
        pass


def _reap_child(proc: Any, *, grace_seconds: float = 2.0, force: bool = False) -> bool:
    """07-10 R3-P1-2 (terminal-review-repair-plan-r3 §4.1): unified child
    cleanup used on EVERY subprocess exit path so NO path can leave a live
    orphan process behind.

    Sequence (§4.1):
        join(grace)              # skipped when ``force`` (already known wedged)
        if alive: terminate
        join(grace)
        if alive: kill
        final bounded join
        verify not alive
        close the Process handle when supported

    ``force=True`` skips the initial grace ``join`` and goes straight to
    ``terminate`` -- used on the HARD-TIMEOUT path where the caller has already
    established the child is wedged (the deadline elapsed with the child still
    alive), so waiting the grace window again only delays the kill without
    changing the outcome. The success/EOF/error paths keep ``force=False`` to
    give a child that is finishing a clean send/return the chance to exit on
    its own before escalation (§4.1).

    Returns ``True`` iff the child is verified DEAD at the end. Returns
    ``False`` if even after ``kill`` the child is still alive -- a terminal
    cleanup failure (e.g. a zombie the OS will not reap, or a process owned by
    another session). The caller MUST treat ``False`` as a terminal cleanup
    failure and NEVER report the call as healthy on that path (§4.1 last
    paragraph): if a complete valid payload was already received but the child
    hangs in shutdown, reap it and THEN return the payload; if the child
    cannot be reaped even after ``kill``, return a terminal cleanup-failure
    reason, not a healthy result.

    Preserves the P0-2 / R3 drain-before-reap design and the NON-retryable
    hard-timeout classification: this helper only reaps; it does not change how
    the caller classifies the outcome (the hard-timeout RuntimeError is raised
    by the caller before/after reaping, as appropriate to the path).
    """
    grace = max(0.1, float(grace_seconds))
    if not force:
        # Step 1: grace join (give a finishing child a chance to exit cleanly).
        try:
            proc.join(timeout=grace)
        except Exception:
            pass
        if not proc.is_alive():
            _close_process_handle(proc)
            return True
    # Step 2: terminate (SIGTERM-equivalent). Re-terminate is idempotent if the
    # first SIGTERM landed but the child ignored it; either way we then re-join
    # and re-check before escalating to kill.
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.join(timeout=grace)
    except Exception:
        pass
    if not proc.is_alive():
        _close_process_handle(proc)
        return True
    # Step 3: kill (SIGKILL-equivalent). Fall back to terminate on Python <
    # 3.7 (where Process.kill does not exist).
    try:
        proc.kill()
    except AttributeError:
        try:
            proc.terminate()
        except Exception:
            pass
    except Exception:
        pass
    # Step 4: final bounded join.
    try:
        proc.join(timeout=grace)
    except Exception:
        pass
    if not proc.is_alive():
        _close_process_handle(proc)
        return True
    # Terminal cleanup failure: the child survived terminate + kill. Do NOT
    # close the handle (it may still be holding OS resources); the caller
    # surfaces this as a structured cleanup failure.
    return False


def _close_process_handle(proc: Any) -> None:
    """Close the Process handle when the platform supports it (Python 3.7+
    exposes ``Process.close``). Swallow any error -- the child is already dead
    so a handle-close failure is a non-fatal resource-management warning."""
    try:
        proc.close()
    except AttributeError:
        # Older Python: no close() on Process; the handle is GC'd normally.
        pass
    except Exception:
        pass


def _llm_subprocess_target(
    prompt: str,
    cfg_name: str,
    provider_timeout_seconds: float,
    system_prompt: str,
    child_conn: Any,
) -> None:
    """Module-level (picklable) child-process body for the PRODUCTION provider
    call.

    Rebuilds the llmcore session with the same generation controls + bounded
    read_timeout / max_retries=0 as the in-process ``_call_ga_llm`` path, then
    issues ONE ``session.raw_ask`` and SENDS the result on ``child_conn``
    (a unidirectional ``multiprocessing.Pipe`` write end):

        ("ok", raw_str, effective_settings_dict)
        ("error", exc_type_name, exc_msg_first_300)

    P0-2: this uses a ``Pipe`` (NOT a ``Queue``). ``multiprocessing.Queue`` has
    a background feeder thread: ``put`` returns immediately and the feeder
    flushes the pickled bytes to the underlying pipe asynchronously. If the
    response exceeds the OS pipe buffer, the feeder thread blocks waiting for
    the parent to drain -- and the child process will NOT exit until the feeder
    finishes. The parent (which did ``proc.join()`` BEFORE ``queue.get()``) then
    blocks on the join, the child blocks on the feeder, and the parent's join
    times out -> a FALSE hard-timeout on a perfectly healthy (just large)
    response. ``Pipe.send`` is SYNCHRONOUS in the caller's thread (no feeder),
    so the parent can ``poll(deadline) -> recv()`` to DRAIN the pipe first, then
    ``join`` a child that has already finished sending and is exiting cleanly.

    07-10 R3-P1-3: this target is the PRODUCTION body ONLY. It reads NO
    environment variable -- in particular it NEVER reads
    ``CRYPTO_GUARD_LLM_SUBPROC_TEST_SLEEP`` / ``_RESPONSE`` /
    ``_RESPONSE_FILE`` (those were the pre-R3 test backdoors that could be
    injected into production via env pollution). The S4 / P0-2 tests now drive
    the subprocess lifecycle through an EXPLICIT injected
    ``_test_subprocess_target`` (see below) which is supplied to
    ``_run_subprocess_with_target`` directly, never selected by env. A send
    that fails (parent already closed the pipe after a timeout/kill) is
    swallowed -- the child is being killed anyway and a BrokenPipeError here
    must NOT mask the parent's hard-timeout.
    """
    try:
        # Production path: rebuild the session exactly like ``_call_ga_llm``.
        import llmcore  # local import; the child re-imports the package
        gen = _resolve_generation_config()
        session = llmcore.resolve_session(cfg_name)
        # 08-04 contract D4 (reviewer round-2 G1/P1-1): the per-task system
        # prompt is supplied by the parent (``_call_ga_llm`` forwards the
        # resolved ``system_override or SYSTEM_PROMPT``), because the child
        # process cannot read the parent's thread-local override. This keeps
        # the subprocess session.system identical to the in-process path now
        # that the prompt no longer embeds the system text in the user message.
        session.system = system_prompt
        if gen["thinking_budget_tokens"] <= 0:
            if hasattr(session, "thinking_type"):
                try:
                    session.thinking_type = "disabled"
                except Exception:
                    pass
        else:
            if hasattr(session, "thinking_budget_tokens"):
                try:
                    session.thinking_budget_tokens = gen["thinking_budget_tokens"]
                except Exception:
                    pass
            if getattr(session, "thinking_type", None) == "enabled" and getattr(session, "thinking_budget_tokens", None) is None:
                session.thinking_type = "adaptive"
        if hasattr(session, "max_tokens"):
            try:
                session.max_tokens = gen["max_output_tokens"]
            except Exception:
                pass
        if hasattr(session, "temperature"):
            try:
                session.temperature = gen["temperature"]
            except Exception:
                pass
        if hasattr(session, "tools"):
            session.tools = []
        # Bound the socket read + kill the llmcore retry loop (R2-1), so even
        # if the parent's join/terminate races, the child's own call is bounded.
        try:
            session.read_timeout = max(15, int(provider_timeout_seconds))
        except Exception:
            if getattr(session, "read_timeout", 0) < 60:
                session.read_timeout = 60
        if hasattr(session, "max_retries"):
            try:
                session.max_retries = 0
            except Exception:
                pass
        raw = "".join(session.raw_ask(
            [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        ))
        effective = {
            "effective_thinking_type": getattr(session, "thinking_type", None),
            "effective_thinking_budget_tokens": getattr(session, "thinking_budget_tokens", None),
            "effective_max_output_tokens": getattr(session, "max_tokens", None),
            "effective_temperature": getattr(session, "temperature", None),
            # 07-13 R6-D (P0-3.4): relay the provider's stop_reason so the
            # parent can classify a truncated response (max_tokens) as
            # ``llm_output_truncated``. Probe the same attrs the in-process
            # path uses; OpenAI-style gateways expose ``finish_reason``.
        }
        _child_sr = None
        for _sr_attr in ("last_stop_reason", "stop_reason", "last_finish_reason", "finish_reason"):
            _v = getattr(session, _sr_attr, None)
            if _v:
                _child_sr = str(_v)
                break
        effective["stop_reason"] = _child_sr
        # 07-10 R4-P1-3: size-check the raw response BEFORE it crosses IPC.
        # ``_send_subprocess_payload`` measures the raw response's UTF-8 byte
        # length and, if it exceeds ``DEFAULT_MAX_SUBPROCESS_RESPONSE_BYTES``,
        # sends ONLY a small ``error`` envelope (a few hundred bytes) carrying
        # the structured ``llm_subprocess_response_oversized`` reason -- instead
        # of the oversized blob. This honors the "防止 runaway IPC payload"
        # comment at the CHILD boundary, before the payload could stall the
        # Pipe / consume parent memory. The parent keeps a post-recv size check
        # as defense-in-depth.
        _send_subprocess_payload(child_conn, ("ok", raw, effective))
    except BaseException as exc:  # noqa: BLE001 - relay ANY failure to parent
        _safe_send(child_conn, (
            "error", type(exc).__name__, str(exc)[:300],
        ))


def _test_subprocess_target(
    control: dict,
    child_conn: Any,
) -> None:
    """07-10 R3-P1-3 (§5): module-level picklable TEST child target. Replaces
    the pre-R3 env-var test backdoors (``CRYPTO_GUARD_LLM_SUBPROC_TEST_*``).
    Driven ONLY by an explicit ``control`` dict pickled into the spawned child
    by the test -- never by an environment variable, so production env can
    never select a test behavior.

    ``control`` keys (all optional; absent = no-op):
        ``sleep`` (float): real wall-clock sleep before any send (proves the
            parent's hard-kill bounds a wedged call).
        ``response`` (str): canned raw response to send (normal-return path).
        ``response_file`` (str): path whose file CONTENTS are sent (unbounded
            payload > OS env limit, for the >128 KiB pipe-buffer regression).
        ``hang_after_send`` (bool): send the payload, THEN sleep a long time so
            the child does NOT exit cleanly -- the parent must reap it and
            still return the already-received payload (R3 §7.2.4).
        ``raise_exc`` (str): raise ``RuntimeError(<str>)`` instead of sending
            (child-error path; R3 §7.2.5).
        ``exit_without_send`` (bool): ``os._exit(0)`` without sending anything
            (no-result path; R3 §7.2.6).
        ``oversized_response`` (int): send a response of this many bytes --
            the size contract MUST reject it. R4-P1-3: the CHILD rejects
            pre-send via ``_send_subprocess_payload``; the parent's post-recv
            check is the backstop (R3 §7.2.8).
        ``max_response_bytes`` (int): R4-P1-3 test override for the CHILD-side
            pre-send size cap. When absent, the module default (2 MiB) applies.
            Lets the ``oversized_response`` test trigger the child-side
            rejection at a SMALL cap (e.g. 1024) instead of spawning a >2 MiB
            child -- the production path always uses the 2 MiB default, so a
            test override does not weaken the production contract.
        ``effective`` (dict): effective-settings dict to relay with the
            payload (so the normal-return test can assert the relay).
    """
    import os as _os
    try:
        _sleep = float(control.get("sleep") or 0.0)
        if _sleep > 0:
            time.sleep(_sleep)
        if control.get("exit_without_send"):
            # Exit with NO result -- distinct from an exception: the child dies
            # cleanly, the pipe EOFs (no envelope sent), and the parent sees
            # ``llm_subprocess_exited_without_result``. Use a plain ``return``
            # so the multiprocessing cleanup runs and closes the child's pipe
            # write end (sending EOF to the parent). ``os._exit`` skips that
            # cleanup and on Windows spawn can leave the parent's poll hanging
            # until the deadline (the child's inherited handle is not released
            # cleanly) -- a plain return is the reliable no-result signal.
            return
        _raise = control.get("raise_exc")
        if _raise:
            raise RuntimeError(str(_raise))
        _eff = dict(control.get("effective") or {})
        # Default effective settings so the normal-return relay test can
        # assert the round-trip even without an explicit ``effective`` dict.
        _eff.setdefault("effective_thinking_type", None)
        _eff.setdefault("effective_thinking_budget_tokens", None)
        _eff.setdefault("effective_max_output_tokens", None)
        _eff.setdefault("effective_temperature", None)
        # 07-10 R4-P1-3: the child-side pre-send size cap. Production uses the
        # 2 MiB module default; tests may override via ``control`` so the
        # ``oversized_response`` rejection is exercisable at a small cap without
        # spawning a >2 MiB child.
        _max_bytes = control.get("max_response_bytes")
        _max_bytes = int(_max_bytes) if _max_bytes else DEFAULT_MAX_SUBPROCESS_RESPONSE_BYTES
        _resp_file = control.get("response_file")
        if _resp_file:
            with open(_resp_file, "r", encoding="utf-8") as _fh:
                _canned_file = _fh.read()
            # 07-10 R4-P1-3: route through the pre-send size guard so a
            # file-backed response that exceeds the contract is rejected at
            # the CHILD (small error envelope) instead of crossing IPC.
            _send_subprocess_payload(
                child_conn, ("ok", _canned_file, _eff),
                max_response_bytes=_max_bytes,
            )
        elif "oversized_response" in control:
            # 07-10 R3 §7.2.8 + R4-P1-3: build an oversized response to
            # exercise the size contract. Pre-R4 this proved the PARENT rejects
            # an oversized payload post-recv. R4-P1-3 moves the FIRST line of
            # defense to the CHILD: ``_send_subprocess_payload`` measures the
            # raw response's byte length and, on overflow, sends ONLY a small
            # ``error`` envelope carrying the structured
            # ``llm_subprocess_response_oversized`` reason. So this path now
            # proves BOTH layers: the child rejects pre-send, and the parent's
            # post-recv check remains as defense-in-depth for a child that
            # bypasses the guard.
            _n = int(control.get("oversized_response") or 0)
            _send_subprocess_payload(
                child_conn, ("ok", "y" * _n, _eff),
                max_response_bytes=_max_bytes,
            )
        elif "response" in control:
            _send_subprocess_payload(
                child_conn, ("ok", str(control["response"]), _eff),
                max_response_bytes=_max_bytes,
            )
        if control.get("hang_after_send"):
            # A send already happened (above). Now hang so the child does NOT
            # exit -- the parent must reap it (terminate/kill) and STILL return
            # the payload it already drained. Sleep well past any grace join.
            time.sleep(30.0)
    except BaseException as exc:  # noqa: BLE001 - relay ANY failure to parent
        _safe_send(child_conn, ("error", type(exc).__name__, str(exc)[:300]))


def _run_subprocess_with_target(
    target: Callable[..., Any],
    target_args: tuple,
    *,
    provider_timeout_seconds: float,
    max_response_bytes: int = DEFAULT_MAX_SUBPROCESS_RESPONSE_BYTES,
) -> tuple:
    """07-10 R3-P1-2 + R3-P1-3: generic private subprocess runner that spawns a
    module-level ``target`` with explicit ``target_args`` and bounds it by a
    HARD wall-clock deadline. Used by:

    - the production wrapper ``_run_provider_call_in_subprocess`` (always
      supplies ``_llm_subprocess_target`` + the real ``(prompt, cfg_name,
      timeout, child_conn)`` args); and
    - the tests (which supply ``_test_subprocess_target`` + a ``control`` dict
      to drive the lifecycle paths -- R3 §7.2).

    ``target_args`` MUST include the child's Pipe write end as its LAST
    element (the runner creates the Pipe, closes the child end in the parent
    immediately, and passes it to the child). This keeps the runner generic:
    every target receives its own write end without the runner knowing the
    target's other parameters.

    Returns ``(tag, raw_or_exc_type, eff_or_exc_msg)`` on a drained payload, or
    raises ``RuntimeError`` carrying a DISTINCT structured reason on:
    - hard timeout (``llm_subprocess_hard_timeout: ...``);
    - oversized IPC response (``llm_subprocess_response_oversized: ...``);
    - cleanup failure (``llm_subprocess_cleanup_failed: ...`` -- the child
      could not be reaped even after kill, §4.1 last paragraph);
    - start failure (``llm_subprocess_start_failed: ...`` -- ``proc.start()``
      raised, §7.2.7; both Pipe ends are closed in finally).

    The caller interprets the returned tuple (``tag == "ok"`` -> raw response
    + effective dict; ``tag == "error"`` -> child exception). The
    NON-retryable hard-timeout classification is preserved: the hard-timeout
    message contains "timeout" so ``_classify_llm_failure`` routes it to
    ``llm_subprocess_hard_timeout``.
    """
    ctx = _mp_mod.get_context("spawn")
    # P0-2: unidirectional Pipe (NOT Queue). See _llm_subprocess_target docstring
    # for the feeder-deadlock rationale. The parent only reads; the child only
    # writes. The child end is closed in the parent immediately after spawn so
    # the only remaining writer is the child -- once it closes/exits,
    # ``parent_conn.poll()`` returns False (EOF), the clean "done" signal.
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    # The target's args are the caller-supplied args (minus the child end) +
    # the child write end as the LAST element.
    full_args = tuple(target_args) + (child_conn,)
    proc = ctx.Process(target=target, args=full_args, daemon=True)
    # ``proc`` created -> both Pipe ends MUST be closed in finally on EVERY
    # path (start failure, recv failure, timeout, success). Track them so the
    # finally block can close both unconditionally (§4.1).
    _started = False
    try:
        try:
            proc.start()
            _started = True
        except Exception as start_exc:
            # §7.2.7: proc.start() failed -> both Pipe endpoints close, no
            # handle leaks. Surface a distinct start-failure reason.
            raise RuntimeError(
                f"llm_subprocess_start_failed: {type(start_exc).__name__}: "
                f"{str(start_exc)[:200]}"
            )
        # Close the CHILD end in the parent immediately: the parent only reads.
        try:
            child_conn.close()
        except Exception:
            pass

        _deadline_s = max(1.0, float(provider_timeout_seconds))
        _payload = None
        _poll_eof = False  # child closed the pipe WITHOUT a real payload (EOFError)
        # DRAIN BEFORE REAP: poll the pipe for the result up to the deadline.
        # If the child sends and exits within the deadline, ``poll`` returns
        # True and ``recv`` drains the envelope; we then reap a child that is
        # ALREADY done (fast, no feeder deadlock). ``poll(timeout)`` returns
        # False on timeout; on Windows spawn a child that closes the pipe
        # without sending may either return False (poll blocks to deadline) OR
        # return True and then ``recv`` raises ``EOFError``. Either way, no real
        # payload was received. We classify AFTER refreshing the child's
        # liveness state (see below): a dead child with no payload is
        # ``exited_without_result`` (distinct reason); only a child that is
        # STILL alive after the deadline is a real hard-timeout.
        if parent_conn.poll(timeout=_deadline_s):
            try:
                _payload = parent_conn.recv()
            except (EOFError, OSError):
                _payload = None
                _poll_eof = True
        # else: poll timed out -> _payload stays None; classify below.

        # Windows-spawn nuance: when the child dies during/after the poll window
        # WITHOUT sending a real payload (clean no-result return, or a crash in
        # the target), the parent's ``Process.is_alive()`` flag is STALE until a
        # ``join`` refreshes it. Without this refresh, a just-dead child reads
        # as alive and is misclassified as a hard-timeout. So whenever NO real
        # payload was received (poll timed out OR recv EOFed), do a NON-blocking
        # ``join(0)`` to refresh liveness BEFORE classifying. Only a child that
        # is STILL alive after the refresh is a real wedged-call hard-timeout.
        if _payload is None:
            try:
                proc.join(timeout=0)
            except Exception:
                pass
            # If recv EOFed, the child has closed its write end -- it is exiting
            # or already exited. A short blocking join forces the liveness
            # refresh on platforms where ``join(0)`` is insufficient.
            if _poll_eof:
                try:
                    proc.join(timeout=_deadline_s)
                except Exception:
                    pass

        if _payload is not None:
            # 07-10 R4-P1-3: a child ``error`` envelope carrying a REGISTERED
            # non-retryable subprocess-fatal signature (e.g. the
            # ``llm_subprocess_response_oversized`` envelope the child sends
            # when its OWN pre-send size check rejects an oversized response)
            # is re-raised as a ``RuntimeError`` carrying that signature -- so
            # ``_run_single_llm_attempt``'s signature loop classifies it as the
            # fatal non-retryable category (R4-P0-2) instead of returning a
            # raw tuple the generic-runner contract normally hands back. This
            # keeps the child-side rejection and the parent-side backstop
            # uniformly surfaced as a structured ``RuntimeError``. A GENERIC
            # child error envelope (R3 §7.2.5 ``raise_exc`` -- exception type
            # is NOT a registered signature) is still returned as a tuple per
            # the existing contract.
            if isinstance(_payload, tuple) and _payload and \
                    str(_payload[0]) == "error" and len(_payload) > 1:
                _child_err_type = str(_payload[1])
                if _child_err_type in (
                    "llm_subprocess_response_oversized",
                    "llm_subprocess_cleanup_failed",
                    "llm_subprocess_start_failed",
                    "llm_subprocess_hard_timeout",
                ):
                    _child_err_msg = str(_payload[2]) if len(_payload) > 2 else ""
                    _reap_child(proc)
                    raise RuntimeError(
                        _child_err_msg or
                        f"{_child_err_type}: (child-reported fatal)"
                    )
            # R3 §5 max-response contract: reject an oversized IPC payload with
            # a DISTINCT structured reason BEFORE reaping (so a runaway
            # provider cannot stall the parent). The payload's second element is
            # the raw response string; measure its byte length.
            _raw_len = 0
            try:
                if isinstance(_payload, tuple) and len(_payload) > 1:
                    _raw = _payload[1]
                    if isinstance(_raw, str):
                        _raw_len = len(_raw.encode("utf-8", "replace"))
                    elif isinstance(_raw, (bytes, bytearray)):
                        _raw_len = len(_raw)
            except Exception:
                _raw_len = 0
            if _raw_len > int(max_response_bytes):
                # Oversized: reap the child (it may still be writing), then
                # fail CLOSED with the distinct reason. NO healthy result.
                _reap_child(proc)
                raise RuntimeError(
                    f"llm_subprocess_response_oversized: child response "
                    f"{_raw_len} bytes exceeds the {max_response_bytes}-byte "
                    f"contract"
                )
            # Child sent a valid-sized result. Reap it (R3-P1-2: the unified
            # helper -- NOT a bare join -- so a child that hangs in shutdown
            # after sending is still reaped and no orphan remains). If the
            # child cannot be reaped even after kill, that is a terminal
            # cleanup failure (§4.1): we have a valid payload but we MUST NOT
            # report the call healthy while a live child remains.
            _reaped_ok = _reap_child(proc)
            if not _reaped_ok:
                raise RuntimeError(
                    "llm_subprocess_cleanup_failed: child sent a result but "
                    "could not be reaped after terminate+kill (orphan risk)"
                )
            return _payload

        # No real payload was received (poll timed out OR recv EOFed). The
        # liveness refresh above has updated ``proc.is_alive()``. A child that
        # is STILL alive is a wedged provider call -> HARD timeout (reap +
        # non-retryable). A child that is now dead closed the pipe without
        # sending -> ``exited_without_result`` (distinct reason).
        if proc.is_alive():
            # Hard timeout: the child is still running (wedged provider call).
            # Reap it deterministically (terminate -> kill). ``force=True`` skips
            # the grace join -- the deadline already elapsed, so waiting again
            # only delays the kill. This is the P0 #3 guarantee the thread-based
            # R2-2 barrier could NOT provide.
            _reaped_ok = _reap_child(proc, force=True)
            # Drain any partial result the child may have written before being
            # killed so the pipe buffer does not leak; then close both ends.
            try:
                while parent_conn.poll(0):
                    parent_conn.recv()
            except (EOFError, OSError):
                pass
            if not _reaped_ok:
                # §4.1 last paragraph: if the child cannot be reaped even after
                # kill, return a terminal cleanup failure and NEVER leave the
                # call reported as healthy.
                raise RuntimeError(
                    "llm_subprocess_hard_timeout: provider call exceeded "
                    f"{provider_timeout_seconds}s and could not be reaped "
                    "after terminate+kill (orphan process)"
                )
            raise RuntimeError(
                "llm_subprocess_hard_timeout: provider call exceeded "
                f"{provider_timeout_seconds}s and was killed"
            )

        # _payload is None and child is dead: exited without a result.
        _reap_child(proc)  # idempotent on a dead child; closes the handle
        raise RuntimeError(
            "llm_subprocess_exited_without_result: child process ended without "
            "returning a response"
        )
    finally:
        # §4.1: close BOTH Pipe endpoints in finally on EVERY path (success,
        # error, timeout, start failure). The child end is closed above; this
        # is belt-and-suspenders for the start-failure path and any exception
        # before the close. ``proc`` handle is closed by ``_reap_child`` on the
        # success/timeout paths; for the start-failure / unexpected-exception
        # path the handle may be unstarted -- best-effort close.
        try:
            parent_conn.close()
        except Exception:
            pass
        try:
            child_conn.close()
        except Exception:
            pass
        if _started:
            try:
                _reap_child(proc)
            except Exception:
                pass


def _run_provider_call_in_subprocess(
    prompt: str,
    *,
    provider_timeout_seconds: float,
    cfg_name: str,
    effective_out: dict[str, Any],
    system_prompt: str | None = None,
) -> str:
    """07-10 S4 (P0 #3) + R3-P1-2/P1-3: run ``session.raw_ask`` in a child
    process with a HARD wall-clock bound via the generic runner
    ``_run_subprocess_with_target``. This is the PRODUCTION wrapper: it ALWAYS
    supplies the real ``_llm_subprocess_target`` (never a test target, never an
    env-var-selected target) so a test backdoor cannot be injected into the
    production LLM path through environment pollution (R3-P1-3).

    Returns the raw response string on success (and fills ``effective_out``
    with the child's effective generation settings so the parent can persist
    them into the §8 envelope, mirroring the in-process path).

    Raises ``RuntimeError`` carrying a DISTINCT structured reason on:
    - child error: ``llm_subprocess_error [<type>]: <msg>``;
    - hard timeout: ``llm_subprocess_hard_timeout: ...`` (NON-retryable, maps
      to ``llm_subprocess_hard_timeout`` category -> terminal ``symbol_timeout``);
    - oversized response: ``llm_subprocess_response_oversized: ...``;
    - cleanup failure: ``llm_subprocess_cleanup_failed: ...``;
    - start failure: ``llm_subprocess_start_failed: ...``;
    - no result: ``llm_subprocess_exited_without_result: ...``.

    The caller (``_call_ga_llm``) lets these propagate so
    ``_run_single_llm_attempt``'s existing ``except RuntimeError`` branch
    classifies them; the hard-timeout message contains "timeout" so
    ``_classify_llm_failure`` routes it to ``llm_subprocess_hard_timeout``.
    """
    _payload = _run_subprocess_with_target(
        _llm_subprocess_target,
        (prompt, cfg_name, float(provider_timeout_seconds),
         system_prompt or SYSTEM_PROMPT),
        provider_timeout_seconds=provider_timeout_seconds,
    )
    tag = _payload[0]
    if tag == "ok":
        raw = _payload[1]
        eff = _payload[2] if len(_payload) > 2 else {}
        if isinstance(eff, dict):
            for k, v in eff.items():
                effective_out[k] = v
        return raw
    # tag == "error"
    exc_type = _payload[1] if len(_payload) > 1 else "UnknownError"
    exc_msg = _payload[2] if len(_payload) > 2 else ""
    # 07-10 R4-P1-3: when the child itself rejected an oversized response
    # (via ``_send_subprocess_payload``), it sends an ``error`` envelope whose
    # ``exc_type`` IS the structured ``llm_subprocess_response_oversized``
    # signature. Propagate it as the CLEAN structured reason (NOT wrapped in
    # ``llm_subprocess_error [...]``) so ``_run_single_llm_attempt``'s
    # signature loop classifies it as ``llm_subprocess_response_oversized``
    # (non-retryable, R4-P0-2) rather than letting it fall through to a generic
    # transport error. The ``exc_msg`` already carries the full structured
    # reason; surface it verbatim.
    #
    # NOTE: the generic runner ``_run_subprocess_with_target`` now re-raises a
    # REGISTERED child-fatal signature (including this one) BEFORE returning,
    # so this branch is normally unreachable for the oversized case. It is kept
    # as a defense-in-depth backstop: if a future caller bypasses the generic
    # runner's re-raise, the production wrapper still surfaces the clean
    # structured reason instead of a generic ``llm_subprocess_error [...]``.
    if exc_type == "llm_subprocess_response_oversized":
        raise RuntimeError(str(exc_msg)[:300] or exc_type)
    raise RuntimeError(f"llm_subprocess_error [{exc_type}]: {exc_msg}")


def _call_ga_llm(prompt: str) -> str:
    cfg_name = _resolve_llm_config_name()
    import llmcore

    # 07-10 Phase D §6: apply explicit generation controls to the session so
    # scheduled JSON analysis does NOT use unbounded adaptive thinking. Read
    # from ``llm.generation`` (validated by config/loader.py) with safe
    # defaults. Stash the EFFECTIVE values into a thread-local so the retry
    # wrapper can persist ``llm_effective_*`` without a signature change.
    #
    # 07-10 R2-1 (bugfix found by test_r2_1b): ``_llm_call_state_reset()`` below
    # DELETES ``provider_timeout_seconds`` from the thread-local. But the fair
    # adapter / ``_run_single_llm_attempt`` stash the per-symbol deadline's
    # provider timeout INTO that same thread-local BEFORE calling
    # ``_call_ga_llm`` — so a naive reset here wipes it and the session never
    # receives ``read_timeout`` / ``max_retries=0``. Consume the timeout into a
    # local BEFORE the reset and use that local below. Do not re-stash it: these
    # are one-call inputs, and retaining either input leaks process isolation
    # into the next call on the same worker thread. Legacy callers without a
    # stashed timeout keep None (60s floor + default retries).
    _provider_timeout_seconds = getattr(
        _llm_call_state, "provider_timeout_seconds", None,
    )
    # 07-10 S4 (P0 #3): the fair adapter stashes a process-isolation flag
    # alongside the provider timeout. Consume it into a local in the same way.
    _subprocess_hard_timeout = bool(getattr(
        _llm_call_state, "subprocess_hard_timeout", False,
    ))
    # 08-04 contract D4: ``run_agent_json_task`` stashes the per-task system
    # prompt here; the market-decision path leaves it unset and falls back to
    # ``SYSTEM_PROMPT``. Consume BEFORE ``_llm_call_state_reset`` (which clears
    # it) exactly like the provider-timeout inputs above.
    _system_override = getattr(_llm_call_state, "system_override", None)
    _llm_call_state_reset()
    gen = _resolve_generation_config()
    session = llmcore.resolve_session(cfg_name)
    session.system = _system_override or SYSTEM_PROMPT
    # Thinking control. ``thinking_budget_tokens=0`` disables extended
    # thinking entirely (forces a bounded non-thinking JSON synthesis call).
    # A non-zero budget pins the budget so adaptive-mode drift cannot starve
    # retry attempts later in the same symbol's deadline window.
    _eff_thinking_type = getattr(session, "thinking_type", None)
    if gen["thinking_budget_tokens"] <= 0:
        # Disable extended thinking for JSON synthesis.
        if hasattr(session, "thinking_type"):
            try:
                session.thinking_type = "disabled"
                _eff_thinking_type = "disabled"
            except Exception:
                pass
    else:
        if hasattr(session, "thinking_budget_tokens"):
            try:
                session.thinking_budget_tokens = gen["thinking_budget_tokens"]
            except Exception:
                pass
        # Only switch to an explicit-thinking mode when a budget is set;
        # leave adaptive alone if the session defaulted to it.
        if getattr(session, "thinking_type", None) == "enabled" and getattr(session, "thinking_budget_tokens", None) is None:
            session.thinking_type = "adaptive"
            _eff_thinking_type = "adaptive"
        else:
            _eff_thinking_type = getattr(session, "thinking_type", None)
    # Explicit output-token limit. The llmcore session exposes this as
    # ``max_tokens`` (NOT ``max_output_tokens`` — see the session attrs probe
    # in design investigation). Apply when supported.
    if hasattr(session, "max_tokens"):
        try:
            session.max_tokens = gen["max_output_tokens"]
        except Exception:
            pass
    # Explicit low temperature for deterministic structured JSON output.
    if hasattr(session, "temperature"):
        try:
            session.temperature = gen["temperature"]
        except Exception:
            pass
    # 07-09-overtrigger P0-1: DO NOT inject the ``crypto_guard_noop``
    # placeholder tool for JSON-only market-decision prompts. With a complex
    # market prompt + an exposed (placeholder) tool, the model can choose
    # ``stop_reason=tool_use`` and emit empty assistant text, which the
    # wrapper classified as ``llm_empty_response`` and the breaker opened
    # after only 3 attempts. JSON-only tasks must have tools absent so the
    # model is forced to produce text output. If the session arrives with
    # leftover tools from a prior call, clear them.
    if hasattr(session, "tools"):
        session.tools = []
    # 07-13 R6-F (P1-2): mark this JSON-only session as tool-free-by-intent so
    # ``NativeClaudeSession.raw_ask`` suppresses the ``[ERROR] No tools
    # provided for this session.`` diagnostic it would otherwise print on
    # every market-decision call. Per repair-plan §5 P1-2: "make tool
    # requirement explicit per session" rather than emitting an ERROR for an
    # expected no-tools mode. Never restore a placeholder tool.
    if hasattr(session, "tools_optional"):
        session.tools_optional = True
    else:
        try:
            setattr(session, "tools_optional", True)
        except Exception:
            pass
    # 07-10 R2-1: thread the per-symbol deadline's provider timeout into the
    # session so the provider call is actually bounded. The timeout is passed
    # through the thread-local ``_llm_call_state.provider_timeout_seconds``
    # (same channel as ``prompt_bytes`` / ``continuity_included``) rather than
    # a signature kwarg so existing test mocks of ``_call_ga_llm(prompt)`` keep
    # working unchanged (a kwarg would force every ``def fake_call(prompt)`` in
    # the suite to add ``**kwargs``). Two levers:
    #
    # 1. ``read_timeout`` — the per-packet socket read timeout. ``requests``
    #    resets this between packets, so for a slow-but-steady stream it does
    #    NOT bound the total call duration. It bounds a stuck socket (gateway
    #    hang, no bytes at all).
    # 2. ``max_retries = 0`` — kills the llmcore internal retry loop
    #    (``_stream_with_retry`` loops ``range(max_retries + 1)``; default
    #    max_retries=4 -> 5 attempts with exponential backoff up to ~30s each,
    #    letting ONE ``_call_ga_llm`` reach ~322s and blow past the 180s
    #    per-attempt / 300s per-symbol deadline). With max_retries=0 the
    #    llmcore loop runs EXACTLY once; the fair scheduler's barrier
    #    ``fut.result(timeout=)`` (R2-2) bounds the total wall-clock.
    #
    # The legacy 60s read-timeout floor is preserved when the thread-local has
    # no provider timeout (legacy retry-wrapper callers and ad-hoc script
    # callers). On the fair path the sole setter of
    # ``_llm_call_state.provider_timeout_seconds`` is post-prompt admission
    # inside ``_run_single_llm_attempt`` (via the immutable positive timeout);
    # this function only reads that already-admitted value.
    provider_timeout_seconds = _provider_timeout_seconds
    if provider_timeout_seconds is not None:
        try:
            session.read_timeout = max(15, int(provider_timeout_seconds))
        except Exception:
            if getattr(session, "read_timeout", 0) < 60:
                session.read_timeout = 60
        if hasattr(session, "max_retries"):
            try:
                session.max_retries = 0
            except Exception:
                pass
    elif getattr(session, "read_timeout", 0) < 60:
        session.read_timeout = 60
    # 07-10 S4 (P0 #3): when the fair adapter opts into process isolation
    # (``subprocess_hard_timeout``), run the actual ``session.raw_ask`` in a
    # child process with a HARD wall-clock bound via ``proc.join(timeout=)`` +
    # ``terminate``/``kill``. This is the guarantee the R2-2 thread barrier
    # could NOT provide: a running thread cannot be killed, so a wedged
    # provider call could outlive the barrier wait and block
    # ``executor.shutdown(wait=True)``. The child rebuilds its own session
    # (same generation controls + bounded read_timeout / max_retries=0) and
    # relays the raw response + effective settings back to the parent.
    #
    # ``_llm_call_state.subprocess_hard_timeout`` is stashed by the fair
    # adapter (``fair_llm_call_adapter``) from the scheduling config. Legacy
    # callers and the legacy retry wrapper leave it unset (False) so the
    # in-process ``session.raw_ask`` path is byte-for-byte unchanged - the
    # deterministic rollback target and every test that patches
    # ``_call_ga_llm`` (which REPLACES this whole function, so the subprocess
    # block never runs) are unaffected.
    # Measure wall-clock latency of the provider call (network + gateway +
    # model). Stash into the thread-local for the retry wrapper. Use
    # ``time.perf_counter`` (monotonic) — NOT wall-clock ``time.time`` — so
    # latency is unaffected by system clock jumps (design §3/§6).
    _t0 = time.perf_counter()
    _effective_out: dict[str, Any] = {}
    _used_subprocess = False
    try:
        if _subprocess_hard_timeout and provider_timeout_seconds is not None:
            _used_subprocess = True
            raw = _run_provider_call_in_subprocess(
                prompt,
                provider_timeout_seconds=provider_timeout_seconds,
                cfg_name=cfg_name,
                effective_out=_effective_out,
                # 08-04 contract D4 (reviewer round-2 G1/P1-1): forward the
                # resolved per-task system prompt so the child session.system
                # matches the in-process path (the child cannot read the
                # parent's thread-local override).
                system_prompt=_system_override or SYSTEM_PROMPT,
            )
            # Relay the child's effective generation settings so the §8
            # envelope is complete (the in-process path reads them off the
            # session below; the subprocess path gets them from the child).
            _eff_thinking_type = _effective_out.get("effective_thinking_type", _eff_thinking_type)
        else:
            raw = "".join(session.raw_ask([{"role": "user", "content": [{"type": "text", "text": prompt}]}]))
    finally:
        _latency_ms = int((time.perf_counter() - _t0) * 1000)
        _llm_call_state.latency_ms = _latency_ms
        _llm_call_state.effective_thinking_type = _eff_thinking_type
        if _used_subprocess:
            # The child rebuilt its own session; the parent's ``session``
            # object was never asked, so read the effective settings from the
            # child's relayed payload (None when the child did not report).
            _llm_call_state.effective_thinking_budget_tokens = (
                _effective_out.get("effective_thinking_budget_tokens")
            )
            _llm_call_state.effective_max_output_tokens = (
                _effective_out.get("effective_max_output_tokens")
            )
            _llm_call_state.effective_temperature = (
                _effective_out.get("effective_temperature")
            )
        else:
            _llm_call_state.effective_thinking_budget_tokens = (
                getattr(session, "thinking_budget_tokens", None)
            )
            _llm_call_state.effective_max_output_tokens = (
                getattr(session, "max_tokens", None)
            )
            _llm_call_state.effective_temperature = (
                getattr(session, "temperature", None)
            )
        # 07-13 R6-D (P0-3.5 / §7.9): capture the provider's stop_reason /
        # finish_reason so a truncated JSON response (stop_reason=max_tokens)
        # classifies as ``llm_output_truncated`` instead of a generic
        # ``llm_json_parse_failed``. The provider WROTE content -- it ran out
        # of output budget mid-JSON -- so this is a model-output defect for
        # THIS symbol, isolated from the breaker (AC8). Probe the same attrs
        # the empty-response path uses (``last_stop_reason`` /
        # ``stop_reason`` / ``last_finish_reason``) plus the OpenAI-style
        # ``finish_reason``. The subprocess path relays the child's stop_reason
        # via ``_effective_out`` (see ``_llm_subprocess_target``).
        _sr_val = None
        if _used_subprocess:
            _sr_val = _effective_out.get("stop_reason")
        else:
            for _sr_attr in ("last_stop_reason", "stop_reason", "last_finish_reason", "finish_reason"):
                _v = getattr(session, _sr_attr, None)
                if _v:
                    _sr_val = str(_v)
                    break
        _llm_call_state.stop_reason = _sr_val
    if not raw.strip():
        # 07-09-overtrigger R5/R6: distinguish "gateway empty" (no HTTP
        # response at all) from "model called a (non-existent) tool with
        # no assistant text". Post-P0-1 fix no tools are exposed, but a
        # hallucinated tool_use turn still produces empty text. Probe the
        # session's last stop_reason if available so the classifier can
        # route to ``llm_tool_call_no_text`` instead of the generic
        # ``llm_empty_response`` - the remediation differs (prompt tuning
        # vs. paging the gateway on-call).
        stop_reason = ""
        for attr in ("last_stop_reason", "stop_reason", "last_finish_reason"):
            val = getattr(session, attr, None)
            if val:
                stop_reason = str(val)
                break
        if stop_reason and any(tok in stop_reason.lower() for tok in ("tool", "function")):
            raise RuntimeError(f"empty LLM response; stop_reason={stop_reason} (tool_call_no_text)")
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


def _build_allowed_evidence_ids(snapshot: dict[str, Any]) -> set[str]:
    """08-04 contract D6: deterministic set of evidence_ids that can ground an
    LLM-provided trade_plan. Built from the snapshot's deterministic modules
    (``<module>:<symbol>:<timeframe>:<as_of>``) plus any explicit
    ``evidence_id`` on the context envelope's trusted_facts / derived_evidence.
    An empty set means the snapshot is ungrounded and can ground NOTHING."""
    allowed: set[str] = set()
    symbol = str(snapshot.get("symbol") or "")
    modules = snapshot.get("modules") or {}
    if not isinstance(modules, dict):
        modules = {}
    snap_as_of = snapshot.get("analysis_time_utc")
    for mod_name, mod in modules.items():
        if not isinstance(mod, dict):
            continue
        tf = str(mod.get("timeframe") or mod.get("timeframe_label") or "")
        as_of = mod.get("as_of") if mod.get("as_of") is not None else snap_as_of
        if symbol and tf and as_of is not None:
            try:
                allowed.add(f"{mod_name}:{symbol}:{tf}:{int(as_of)}")
            except (TypeError, ValueError):
                pass
    env = snapshot.get("context_envelope")
    if isinstance(env, dict):
        for group in ("trusted_facts", "derived_evidence"):
            items = env.get(group)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("evidence_id"):
                    allowed.add(str(item["evidence_id"]))
    return allowed


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
    # 08-02 P1-2: record whether the LLM itself provided an executable
    # trade_plan BEFORE the S/A auto-build block below. An S/A grade with no
    # LLM plan triggers a SYSTEM auto-build; that system-built plan must
    # never be marked ``plan_origin="llm_confirmed"`` (the caller gates the
    # confirmed marking on ``llm_plan_source=="llm_provided"``). These are
    # plain locals that survive the ``decision = normalize_market_semantics(...)``
    # rebind at the end of this function.
    _llm_provided_plan = bool(
        isinstance(candidate, dict)
        and isinstance(candidate.get("trade_plan"), dict)
        and bool(candidate.get("trade_plan"))
    )
    _auto_built_plan = False
    # 08-04 contract D6: evidence_id fail-closed (anti-hallucination). An
    # LLM-provided trade_plan that explicitly claims ``evidence_refs`` which
    # are NOT backed by the snapshot's deterministic evidence set is neutralized
    # to monitor_only / no-plan / grade C — never order-eligible. System
    # auto-built plans and plans without explicit refs on a grounded snapshot
    # are preserved (backward compatibility).
    if _llm_provided_plan and isinstance(candidate, dict):
        _tp = candidate.get("trade_plan")
        if isinstance(_tp, dict):
            _refs = _tp.get("evidence_refs")
            if isinstance(_refs, list) and _refs:
                _allowed = _build_allowed_evidence_ids(snapshot)
                _missing = [str(r) for r in _refs if str(r) not in _allowed]
                if _missing:
                    decision["has_trade_plan"] = False
                    decision["trade_plan"] = None
                    decision["decision"] = "monitor_only"
                    decision["signal_grade"] = "C"
                    _actions = decision.get("suggested_actions") or []
                    decision["suggested_actions"] = [
                        a for a in _actions if a != "create_paper_order"
                    ]
                    decision["evidence_fail_closed"] = True
                    _notes = list(decision.get("risk_notes") or [])
                    _notes.append({
                        "code": "evidence_ungrounded",
                        "message": (
                            "evidence fail-closed: LLM trade_plan 引用未受支持的 "
                            f"evidence_id {_missing}，已中立化为观察（不具下单资格）"
                        ),
                    })
                    decision["risk_notes"] = _notes
    # Phase-2 P2-1 (07-27): clear/rebuild fallback-only transient fields on
    # LLM success. The ``decision = dict(fallback)`` merge (line 3331) copies
    # the risk-processed disabled fallback's transient fields onto the
    # LLM-success decision. On the fair-adapter path the fallback is built
    # from ``run_agent_sop_decision(snapshot, use_llm=False)`` which runs
    # ``apply_risk_to_decision`` with llm_status="disabled", so the fallback
    # carries ``plan_blockers=[{code:"llm_disabled"...}]`` +
    # ``fallback_trade_plan_blocked`` / ``fallback_block_reason`` /
    # ``plan_status="withheld"`` / ``original_decision`` /
    # ``downgraded_decision``. The LLM candidate never writes ``plan_blockers``
    # (it is not an LLM-emitted field), so ``decision.update(candidate)`` (line
    # 3370) leaves these stale fallback-only fields in place — polluting the
    # LLM-success row with "LLM 已禁用" (symptom #1).
    #
    # Requirement B: on LLM success, the fallback-only transient fields MUST
    # be cleared/rebuilt. The deterministic candidate plan stays preserved
    # under ``candidate_trade_plan`` (Phase E invariant — do NOT touch it).
    # Continuity / direction-flip blockers from the deterministic fallback are
    # NOT fallback-only (they reflect real deterministic gates) and are kept.
    # If the LLM did not confirm a plan, the blocker becomes
    # ``llm_not_confirmed`` (never ``llm_disabled``) per requirement B.
    _FALLBACK_ONLY_BLOCKER_CODES = {"llm_disabled", "llm_parse_failed"}
    _fallback_only_transient_keys = (
        "fallback_trade_plan_blocked",
        "fallback_block_reason",
        "original_decision",
        "downgraded_decision",
    )
    _existing_blockers = decision.get("plan_blockers")
    if isinstance(_existing_blockers, list):
        _kept_blockers = [
            b for b in _existing_blockers
            if not (isinstance(b, dict)
                    and str(b.get("code") or "") in _FALLBACK_ONLY_BLOCKER_CODES)
        ]
        # Only rewrite when something actually changed — do not perturb a
        # clean blocker list (e.g. continuity_invalidated only).
        if len(_kept_blockers) != len(_existing_blockers):
            decision["plan_blockers"] = _kept_blockers
    else:
        _kept_blockers = list(_existing_blockers or [])
    for _k in _fallback_only_transient_keys:
        if _k in decision:
            decision.pop(_k, None)
    # Clear the stale fallback-only ``plan_status="withheld"`` from the
    # disabled fallback. The downstream ``apply_risk_to_decision`` (line 286)
    # re-derives ``plan_status`` from the LLM-confirmed outcome, so clearing
    # here is safe and prevents the stale "withheld" label from persisting.
    if decision.get("plan_status") == "withheld":
        decision.pop("plan_status", None)
    # P1-4 (07-27): when the LLM confirms a trade_plan, the fallback's
    # ``plan_origin`` (``deterministic_sop`` / ``deterministic_fallback``) is a
    # stale fallback-only value that must NOT remain on the LLM-success row.
    # The caller (``_run_single_llm_attempt`` lines 1473-1474) sets
    # ``plan_origin="llm_confirmed"`` when a plan is confirmed; this defensive
    # clear ensures that even if a future caller forgets to set it, the
    # normalize output does not carry the stale deterministic value on a
    # confirmed-plan row. When the LLM did NOT confirm a plan, the fallback's
    # ``plan_origin`` is intentionally kept (it correctly records that the
    # deterministic SOP produced the candidate) — do NOT clear it there.
    if decision.get("has_trade_plan") and decision.get("trade_plan"):
        _cur_origin = decision.get("plan_origin")
        if _cur_origin in {"deterministic_sop", "deterministic_fallback"}:
            decision.pop("plan_origin", None)
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
        # 08-02 P1-2: immutable synthesis evidence on the degraded path. Data
        # degradation is a synthesis-layer fail-closed gate (not a risk gate):
        # record the FINAL normalized synthesis state so diagnostics never read
        # a cleared-then-relabelled plan as "LLM confirmed". A degraded row is
        # additionally identifiable via ``analysis_degraded`` /
        # ``degraded_reason``, so the lost pre-degradation plan signal is not
        # a gap.
        decision["llm_synthesis_signal_grade"] = "C"
        decision["llm_synthesis_decision"] = "monitor_only"
        decision["llm_synthesis_has_trade_plan"] = False
        decision["llm_synthesis_trade_plan"] = None
        decision["llm_plan_verdict"] = "no_plan"
        decision["llm_plan_source"] = "none"
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
                # 08-02 P1-2: this plan was SYSTEM-built (LLM did not provide
                # it) — it must never be marked llm_confirmed downstream.
                _auto_built_plan = True

    # 评分稳定：LLM 等级不能比确定性评分低超过 1 级
    from plugins.crypto_guard.strategy.grade_config import grade_order_value, grade_from_order_value
    det_grade_val = grade_order_value(str(fallback.get("signal_grade") or "D").upper())
    llm_grade_val = grade_order_value(grade)
    # 08-04 D6: a D6 evidence-fail-closed neutralization (grade C) must NOT be
    # re-bumped by the stabilization block — the fail-closed verdict is final.
    if not decision.get("evidence_fail_closed") and llm_grade_val < det_grade_val - 1:
        stabilized_grade = grade_from_order_value(det_grade_val - 1)
        decision["signal_grade"] = stabilized_grade
        decision.setdefault("risk_notes", []).append(
            f"LLM 等级 {grade} 比确定性评分 {fallback.get('signal_grade')} 低超过 1 级，稳定为 {stabilized_grade}。"
        )

    if decision.get("has_trade_plan") and not decision.get("trade_plan"):
        decision["has_trade_plan"] = False
    if not decision.get("has_trade_plan"):
        decision["trade_plan"] = None
    # Phase-2 P2-1 (07-27) requirement B / Codex final-review P1-4: if the LLM
    # did NOT confirm a candidate plan (no executable trade_plan after the S/A
    # auto-build block above), surface a precise ``llm_not_confirmed`` blocker
    # so the report/diagnostics label the outcome correctly (the LLM ran and
    # returned ok, but produced no executable plan) — never the stale
    # ``llm_disabled`` that was cleared earlier. This runs AFTER the auto-build
    # so a bullish A-grade decision that auto-builds a plan is NOT marked
    # llm_not_confirmed (it has a plan). The deterministic candidate plan stays
    # preserved under ``candidate_trade_plan`` (Phase E invariant).
    #
    # P1-4 gating: the blocker is added ONLY when a dict-typed NON-EMPTY
    # ``candidate_trade_plan`` exists AND the LLM did not confirm a trade_plan.
    # A normal ``monitor_only`` / ``no_edge`` result WITHOUT a
    # ``candidate_trade_plan`` must NOT get ``llm_not_confirmed`` — there was
    # never a candidate plan to confirm, so labeling it "LLM 未确认" would
    # pollute a clean observation row with a spurious blocker. Pre-fix the
    # append was gated ONLY on ``not has_trade_plan`` and fired on every
    # no-plan row regardless of whether a candidate existed.
    _candidate_plan = decision.get("candidate_trade_plan")
    _has_candidate = isinstance(_candidate_plan, dict) and bool(_candidate_plan)
    if _has_candidate and (not decision.get("has_trade_plan") or not decision.get("trade_plan")):
        _not_confirmed_codes = [
            str(b.get("code") or "")
            for b in (decision.get("plan_blockers") or [])
            if isinstance(b, dict)
        ]
        if "llm_not_confirmed" not in _not_confirmed_codes:
            _blockers = list(decision.get("plan_blockers") or [])
            _blockers.append({
                "code": "llm_not_confirmed",
                "stage": "llm_synthesis",
                "detail": "llm_status=ok 但 LLM 未给出可执行 trade_plan，候选计划保留为 candidate_trade_plan",
            })
            decision["plan_blockers"] = _blockers
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
    # P0-3 (08-02): normalize the LLM/synthesis watch to the structured
    # watcher contract. Bare-string conditions are DROPPED (never translated
    # from free text); if none survive, conditions are built deterministically
    # from the decision's plan (``trade_plan`` or the preserved
    # ``candidate_trade_plan``) — pullback/breakout/reclaim + stop
    # invalidation; if unbuildable, fail-closed to None and the
    # ``create_opportunity_watch`` action is dropped. When the LLM explicitly
    # produced NO watch, it stays None (the P0-2 effect layer decides on auto
    # materialization).
    watch = decision.get("opportunity_watch")
    if watch is not None:
        plan_for_watch = decision.get("trade_plan") or decision.get("candidate_trade_plan")
        normalized_watch, watch_notes = normalize_opportunity_watch(watch, plan_for_watch)
        if normalized_watch is None and watch_notes:
            # Fail-closed: no auto watch -> drop the create action so the
            # manual button never fires on a watch-less decision.
            sa = decision.get("suggested_actions")
            if isinstance(sa, list) and "create_opportunity_watch" in sa:
                decision["suggested_actions"] = [a for a in sa if a != "create_opportunity_watch"]
            notes = list(decision.get("risk_notes") or [])
            notes.extend(watch_notes)
            decision["risk_notes"] = notes
            decision["opportunity_watch"] = None
        else:
            decision["opportunity_watch"] = normalized_watch
            if watch_notes:
                notes = list(decision.get("risk_notes") or [])
                notes.extend(watch_notes)
                decision["risk_notes"] = notes

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
    # 08-02 P1-2: immutable LLM synthesis evidence captured BEFORE any risk
    # gate strips the plan. ``apply_risk_to_decision`` re-validates and may
    # clear trade_plan / has_trade_plan on fallback-block or risk rejection,
    # and the controller finalizer then rewrites plan_execution_state — so the
    # synthesis fields record what the synthesis layer actually produced,
    # distinguishing provider/schema success (llm_status="ok") from PLAN
    # CONFIRMATION (llm_plan_verdict). ``llm_synthesis_trade_plan`` is
    # deep-copied so later in-place gate mutations (e.g. the controller's
    # risk_percent injection) can never alter the audit record.
    #
    # Verdict/source mapping:
    #   - LLM provided a trade_plan and it survived synthesis  -> confirmed / llm_provided
    #   - S/A grade but NO LLM plan, system auto-built one      -> auto_built / auto_built
    #   - no plan at all                                         -> no_plan / none
    # The caller gates plan_origin="llm_confirmed" on
    # llm_plan_source=="llm_provided", so auto-built plans are never labelled
    # as LLM-confirmed.
    _syn_plan = decision.get("trade_plan")
    _syn_has_plan = bool(decision.get("has_trade_plan")) and isinstance(_syn_plan, dict)
    if _auto_built_plan and _syn_has_plan:
        _llm_verdict, _llm_source = "auto_built", "auto_built"
    elif _llm_provided_plan and _syn_has_plan:
        _llm_verdict, _llm_source = "confirmed", "llm_provided"
    else:
        _llm_verdict, _llm_source = "no_plan", "none"
    decision["llm_synthesis_signal_grade"] = str(decision.get("signal_grade") or "D").upper()
    decision["llm_synthesis_decision"] = str(decision.get("decision") or "")
    decision["llm_synthesis_has_trade_plan"] = _syn_has_plan
    decision["llm_synthesis_trade_plan"] = copy.deepcopy(_syn_plan) if _syn_has_plan else None
    decision["llm_plan_verdict"] = _llm_verdict
    decision["llm_plan_source"] = _llm_source
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


def _strip_skill_instruction_text(value: Any) -> Any:
    """08-04 contract C8: recursively strip prompt.md / skill_yaml_text /
    skill_contract / ga_interpretation free text from a skill module so
    Markdown/system-prompt instructions never reach the LLM as high-trust
    content. Deterministic numeric/string facts are preserved."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("prompt", "prompt_md", "skill_yaml_text", "skill_contract", "ga_interpretation"):
                continue
            out[k] = _strip_skill_instruction_text(v)
        return out
    if isinstance(value, list):
        return [_strip_skill_instruction_text(x) for x in value]
    return value


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    modules = snapshot.get("modules") or {}
    keep_modules = {}
    for name in ("price_action", "momentum", "trend_stage", "smc", "order_flow", "chanlun"):
        value = modules.get(name)
        if isinstance(value, dict):
            keep_modules[name] = _strip_skill_instruction_text(value)
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
