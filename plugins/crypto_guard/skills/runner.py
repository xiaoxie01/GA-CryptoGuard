from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from plugins.crypto_guard.analysis.chanlun_engine import analyze_chanlun
from plugins.crypto_guard.analysis.momentum_engine import analyze_momentum
from plugins.crypto_guard.analysis.order_flow_engine import analyze_order_flow
from plugins.crypto_guard.analysis.price_action_engine import analyze_price_action
from plugins.crypto_guard.analysis.smc_engine import analyze_smc
from plugins.crypto_guard.analysis.trend_stage_engine import analyze_trend_stage
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


SKILL_VERSION = "1.0"
SKILL_ROOT = Path(__file__).resolve().parent
_LOGGER = logging.getLogger("crypto_guard.skills.runner")


def _parse_yaml_simple(text: str) -> dict[str, Any]:
    """Minimal YAML-like parser for skill configs (no external dependency)."""
    result: dict[str, Any] = {}
    current_key = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped and not stripped.startswith("-"):
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                # Inline list: [a, b, c]
                items = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
                result[key] = items
            elif value.lower() in ("true", "yes"):
                result[key] = True
            elif value.lower() in ("false", "no"):
                result[key] = False
            elif value.replace(".", "").isdigit():
                result[key] = float(value) if "." in value else int(value)
            else:
                result[key] = value.strip("'\"")
            current_key = key
        elif stripped.startswith("- ") and current_key:
            if not isinstance(result.get(current_key), list):
                result[current_key] = []
            result[current_key].append(stripped[2:].strip().strip("'\""))
    return result


def _validate_against_schema(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Lightweight JSON Schema validation (required fields + basic types)."""
    errors: list[str] = []
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    for field in required:
        if field not in data:
            errors.append(f"missing required field: {field}")
    for field, spec in properties.items():
        if field not in data:
            continue
        expected_type = spec.get("type")
        value = data[field]
        if expected_type == "string" and not isinstance(value, str):
            errors.append(f"{field}: expected string, got {type(value).__name__}")
        elif expected_type == "number" and not isinstance(value, (int, float)):
            errors.append(f"{field}: expected number, got {type(value).__name__}")
        elif expected_type == "boolean" and not isinstance(value, bool):
            errors.append(f"{field}: expected boolean, got {type(value).__name__}")
        elif expected_type == "array" and not isinstance(value, list):
            errors.append(f"{field}: expected array, got {type(value).__name__}")
        elif expected_type == "object" and not isinstance(value, dict):
            errors.append(f"{field}: expected object, got {type(value).__name__}")
    return errors


def execute_market_skills(
    repo: CryptoGuardRepository,
    *,
    symbol: str,
    timeframe: str,
    candles: list[dict[str, Any]],
    analysis_time_utc: int,
    previous_analysis_state: dict[str, Any] | None = None,
    skill_log_sink: list[Any] | None = None,
    batch_id: str | None = None,
    attempt_id: int | None = None,
) -> dict[str, Any]:
    """Execute v2 dynamic skills with deterministic tools and persistent logs.

    07-14 R8 P2-NEW-1: LAYERED lifecycle. When ``skill_log_sink`` is provided
    (the producer/Phase-1 path) each skill log is written ``commit_state=
    'prepared'`` (immediate immutable audit) and the would-be feedback payload
    is COLLECTED into the sink (NOT written) + stashed in the log's audit JSON
    (contract #6). The producer then marks the logs ``committed`` and writes the
    deferred feedback inside the Phase-2 COMMIT (contract #2), or marks them
    ``aborted_unsealed`` on a seal failure with ZERO feedback (contract #3).
    When ``skill_log_sink`` is None (legacy/direct path) the log is written
    ``commit_state='committed'`` and feedback is written immediately (prior
    behavior preserved, now that the dedup-bind bug is fixed).
    """

    modules: dict[str, Any] = {}
    input_summary = {
        "symbol": symbol,
        "timeframe": timeframe,
        "closed_candles": len(candles),
        "previous_analysis_state_id": (previous_analysis_state or {}).get("id"),
    }
    modules["price_action"] = _run_skill(repo, "price_action", symbol, timeframe, analysis_time_utc, input_summary, lambda: analyze_price_action(candles, analysis_time_utc=analysis_time_utc, timeframe=timeframe), skill_log_sink=skill_log_sink, batch_id=batch_id, attempt_id=attempt_id)
    modules["momentum"] = _run_skill(repo, "momentum", symbol, timeframe, analysis_time_utc, input_summary, lambda: analyze_momentum(candles, analysis_time_utc=analysis_time_utc), skill_log_sink=skill_log_sink, batch_id=batch_id, attempt_id=attempt_id)
    modules["trend_stage"] = _run_skill(
        repo,
        "trend_stage",
        symbol,
        timeframe,
        analysis_time_utc,
        input_summary,
        lambda: analyze_trend_stage(modules["price_action"], modules["momentum"], analysis_time_utc=analysis_time_utc),
        skill_log_sink=skill_log_sink, batch_id=batch_id, attempt_id=attempt_id,
    )
    modules["smc"] = _run_skill(repo, "smc_orderflow", symbol, timeframe, analysis_time_utc, input_summary, lambda: analyze_smc(candles, modules["price_action"], analysis_time_utc=analysis_time_utc), skill_log_sink=skill_log_sink, batch_id=batch_id, attempt_id=attempt_id)
    modules["order_flow"] = _run_skill(repo, "smc_orderflow", symbol, timeframe, analysis_time_utc, input_summary, lambda: analyze_order_flow(candles, analysis_time_utc=analysis_time_utc), skill_log_sink=skill_log_sink, batch_id=batch_id, attempt_id=attempt_id)
    modules["chanlun"] = _run_skill(repo, "chanlun", symbol, timeframe, analysis_time_utc, input_summary, lambda: analyze_chanlun(candles, analysis_time_utc=analysis_time_utc), skill_log_sink=skill_log_sink, batch_id=batch_id, attempt_id=attempt_id)
    for result in modules.values():
        result["deterministic_preprocessing"] = True
        result["geometry_authority"] = True
        result["skill_version"] = SKILL_VERSION
    return modules


def _run_skill(
    repo: CryptoGuardRepository,
    skill_name: str,
    symbol: str,
    timeframe: str,
    analysis_time_utc: int,
    input_summary: dict[str, Any],
    tool_fn: Callable[[], dict[str, Any]],
    *,
    log_name: str | None = None,
    skill_log_sink: list[Any] | None = None,
    batch_id: str | None = None,
    attempt_id: int | None = None,
) -> dict[str, Any]:
    tool_result = tool_fn()
    contract = _load_skill_contract(skill_name)

    # Validate tool output against schema
    schema = contract.get("output_schema")
    schema_errors: list[str] = []
    if schema:
        schema_errors = _validate_against_schema(tool_result, schema)

    interpretation = {
        "ga_role": "logic_synthesis",
        "geometry_source": "deterministic_tool",
        "memory_policy": "daily_review_updates_skill_feedback_memory",
        "skill_contract": contract,
    }
    # Include prompt.md content for GA interpretation
    if contract.get("prompt_md"):
        interpretation["prompt"] = contract["prompt_md"]

    final = dict(tool_result)
    final["skill"] = log_name or skill_name
    _normalize_skill_contract(final, log_name or skill_name)
    final["ga_interpretation"] = interpretation

    if schema_errors:
        final["schema_validation_errors"] = schema_errors

    effective_name = log_name or skill_name
    confidence = final.get("confidence")

    # 07-14 R8 P2-NEW-1: collect the would-be feedback WITHOUT writing it. On
    # the producer/Phase-1 path (sink provided) the payload is deferred to the
    # Phase-2 COMMIT and stashed into the audit JSON (contract #6); on the
    # legacy/direct path it is written immediately.
    feedback_payload = _collect_skill_feedback(
        effective_name, symbol, timeframe, confidence, schema_errors, final,
    )

    if skill_log_sink is not None:
        # Producer/Phase-1 path: immutable audit row marked 'prepared'. The
        # deferred feedback intent is stashed in the audit JSON so it is
        # inspectable without polluting the active memory table (contract #6).
        if feedback_payload is not None:
            final["pending_feedback"] = feedback_payload
        log_id = repo.save_skill_execution_log(
            skill_name=effective_name,
            skill_version=SKILL_VERSION,
            symbol=symbol,
            timeframe=timeframe,
            analysis_time=analysis_time_utc,
            input_summary=input_summary,
            tool_result=tool_result,
            ga_interpretation=interpretation,
            final_result=final,
            confidence=confidence,
            commit_state="prepared",
            batch_id=batch_id,
            attempt_id=attempt_id,
        )
        # Hand (log_id, payload) to the producer; it writes the feedback inside
        # the Phase-2 COMMIT (contract #2) or drops it on a seal failure (#3).
        skill_log_sink.append((log_id, feedback_payload))
    else:
        # Legacy/direct path: log is immediately 'committed' and feedback is
        # written now (prior behavior, now that the dedup-bind bug is fixed).
        log_id = repo.save_skill_execution_log(
            skill_name=effective_name,
            skill_version=SKILL_VERSION,
            symbol=symbol,
            timeframe=timeframe,
            analysis_time=analysis_time_utc,
            input_summary=input_summary,
            tool_result=tool_result,
            ga_interpretation=interpretation,
            final_result=final,
            confidence=confidence,
        )
        if feedback_payload is not None:
            _write_skill_feedback(repo, log_id, feedback_payload)

    return final


def _normalize_skill_contract(result: dict[str, Any], skill_name: str) -> None:
    if skill_name in {"price_action", "price_action_skill"}:
        levels = result.get("key_levels") or {}
        result.setdefault("pattern", result.get("range_status") or result.get("last_event"))
        result.setdefault("key_support", levels.get("support") or [])
        result.setdefault("key_resistance", levels.get("resistance") or [])
    elif skill_name in {"momentum", "momentum_skill"}:
        result.setdefault("volume_price_alignment", bool(result.get("volume_confirmed")))
        result.setdefault("indicator_divergence", bool(result.get("divergence")))
    elif skill_name in {"trend_stage", "trend_stage_skill"}:
        result.setdefault("stage", result.get("trend_stage"))
        result.setdefault("clarity", result.get("confidence"))
        result.setdefault("features", result.get("stage_scores") or [])
        result.setdefault("late_stage_risk", result.get("trend_stage") == "late")
        result.setdefault("next_evolution", "观察 5M 是否能生长为 15M/1H 结构确认")
    elif skill_name in {"smc_orderflow", "smc_orderflow_skill"}:
        if "order_flow" not in result and result.get("module") == "order_flow":
            result["order_flow"] = {
                "cvd_slope": result.get("cvd_slope"),
                "aggressive_buy_ratio": result.get("aggressive_buy_ratio"),
                "delta_divergence": result.get("delta_divergence"),
            }
        result.setdefault("setup", result.get("entry_context") or result.get("confirmation") or "monitor")
    elif skill_name in {"chanlun", "chanlun_skill"}:
        result.setdefault("trend_structure", result.get("structure") or result.get("trend") or "unknown")
        result.setdefault("current_bi_direction", result.get("bi_direction") or result.get("current_bi_direction") or "unknown")
        result.setdefault("zhongshu", result.get("central_zone") or {"exists": False})
        result.setdefault("divergence", result.get("divergence") if isinstance(result.get("divergence"), dict) else {"exists": bool(result.get("divergence")), "type": None})
        result.setdefault("buy_sell_point", result.get("buy_sell_point") or {"type": None, "valid": False, "confidence": 0.0})
        result.setdefault("risk_notes", result.get("risk_notes") or [])
        result.setdefault("next_condition", result.get("next_condition") or "等待低周期结构确认")


def _load_skill_contract(skill_name: str) -> dict[str, Any]:
    skill_dir = SKILL_ROOT / skill_name
    files = ("skill.yaml", "prompt.md", "tools.py", "schema.json", "feedback_rules.yaml")
    contract: dict[str, Any] = {
        "skill_name": skill_name,
        "contract_dir": str(skill_dir),
        "files_present": {name: (skill_dir / name).exists() for name in files},
        "dynamic_skill": True,
        "llm_role": "rule_judgement_and_synthesis",
        "tool_role": "deterministic_preprocessing_only",
    }

    # Load skill.yaml content
    yaml_path = skill_dir / "skill.yaml"
    if yaml_path.exists():
        try:
            text = yaml_path.read_text(encoding="utf-8")
            contract["skill_yaml_text"] = text
            contract["skill_yaml"] = _parse_yaml_simple(text)
        except Exception:
            pass

    # Load prompt.md content for GA interpretation
    prompt_path = skill_dir / "prompt.md"
    if prompt_path.exists():
        try:
            contract["prompt_md"] = prompt_path.read_text(encoding="utf-8")
        except Exception:
            pass

    # Load schema.json for validation
    schema_path = skill_dir / "schema.json"
    if schema_path.exists():
        try:
            contract["output_schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Load feedback_rules.yaml
    feedback_path = skill_dir / "feedback_rules.yaml"
    if feedback_path.exists():
        try:
            contract["feedback_rules"] = _parse_yaml_simple(feedback_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return contract


def _collect_skill_feedback(
    skill_name: str,
    symbol: str,
    timeframe: str,
    confidence: float | None,
    schema_errors: list[str],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Collect the would-be feedback payload WITHOUT writing it.

    07-14 R8 P2-NEW-1: split out of the old ``_maybe_write_skill_feedback`` so
    the producer/Phase-1 path can DEFER the write to the Phase-2 COMMIT
    (contract #2) while still stashing the intent in the audit JSON (contract
    #6). Returns the feedback payload dict, or None when no finding fires.
    """
    findings: list[str] = []

    # Low confidence threshold
    if confidence is not None and confidence < 0.30:
        findings.append(f"confidence_below_threshold ({confidence:.2f})")

    # Schema validation failures
    if schema_errors:
        findings.append(f"schema_validation_errors: {'; '.join(schema_errors[:3])}")

    # Specific anomaly patterns per skill
    if skill_name in {"price_action", "price_action_skill"}:
        if result.get("market_structure") == "range" and confidence and confidence > 0.6:
            findings.append("range_with_high_confidence_possible_misclassification")
    elif skill_name in {"momentum", "momentum_skill"}:
        if result.get("divergence") and result.get("quality") == "exhausted":
            findings.append("exhausted_divergence_detected")
    elif skill_name in {"trend_stage", "trend_stage_skill"}:
        if result.get("trend_stage") == "late":
            findings.append("late_stage_risk_active")

    if not findings:
        return None

    return {
        "skill_name": skill_name,
        "skill_version": SKILL_VERSION,
        "feedback_type": "auto_analysis",
        "source_type": "skill_execution",
        "finding": f"[{symbol}/{timeframe}] {'; '.join(findings)}",
        "suggested_adjustment": {"symbol": symbol, "timeframe": timeframe, "findings": findings},
    }


def _write_skill_feedback(
    repo: CryptoGuardRepository,
    source_id: int,
    payload: dict[str, Any],
    *,
    strict: bool = False,
) -> None:
    """Persist a collected feedback payload pointing at ``source_id``.

    07-14 R8 P2-NEW-1: the dedup-bind bug in ``save_skill_feedback_memory`` is
    fixed, so this now actually persists on the auto_analysis path. Called from
    the Phase-2 COMMIT (producer) and the legacy/direct path.

    07-15 R9-P1a: ``strict`` selects the failure contract.
      * ``strict=False`` (default -- the legacy/direct path and the
        non-transactional callers): best-effort. An exception is LOGGED (never
        silently swallowed -- R9-P1a forbids silent ``pass``) but does NOT
        propagate, preserving prior behavior for callers that do not run
        inside a transaction.
      * ``strict=True`` (the producer Phase-2 COMMIT path): the exception is
        RE-RAISED so it propagates into the caller's ``BEGIN IMMEDIATE`` ->
        ``ROLLBACK``. The batch/snapshot/module/feedback all revert and the
        prepared skill log is terminalized to ``aborted_unsealed``. A feedback
        write is part of the atomic Phase-2 contract, so it MUST NOT be silently
        dropped on the producer path.
    """
    try:
        repo.save_skill_feedback_memory(
            skill_name=payload["skill_name"],
            skill_version=payload["skill_version"],
            feedback_type=payload["feedback_type"],
            source_type=payload["source_type"],
            source_id=source_id,
            finding=payload["finding"],
            suggested_adjustment=payload.get("suggested_adjustment"),
        )
    except Exception:
        if strict:
            # Producer Phase-2 path: a feedback-write failure MUST propagate so
            # the surrounding transaction rolls back and the prepared log is
            # aborted. Re-raise the original exception.
            raise
        # Legacy/direct path: best-effort. Log so the failure is observable,
        # but do NOT crash a non-transactional caller (prior behavior preserved
        # -- only the silent ``pass`` is removed).
        _LOGGER.exception(
            "skills.runner: best-effort feedback write failed for source_id=%s "
            "skill_name=%s (non-strict path; not propagated).",
            source_id, payload.get("skill_name"),
        )


def _maybe_write_skill_feedback(
    repo: CryptoGuardRepository,
    skill_name: str,
    symbol: str,
    timeframe: str,
    confidence: float | None,
    schema_errors: list[str],
    result: dict[str, Any],
    source_id: int,
) -> None:
    """Legacy/direct-path convenience: collect + immediately write feedback.

    07-14 R8 P2-NEW-1: retained for any caller that still drives
    ``_run_skill`` without a sink (the non-producer path). The producer path
    uses ``_collect_skill_feedback`` (defer) + ``_write_skill_feedback`` (commit)
    so feedback lands inside the Phase-2 transaction.
    """
    payload = _collect_skill_feedback(
        skill_name, symbol, timeframe, confidence, schema_errors, result,
    )
    if payload is not None:
        _write_skill_feedback(repo, source_id, payload)
