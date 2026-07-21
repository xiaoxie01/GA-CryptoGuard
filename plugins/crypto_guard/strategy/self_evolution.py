from __future__ import annotations

import json
from collections import Counter
from typing import Any

from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.review.evolution_engine import build_candidate_patch
from plugins.crypto_guard.storage.repository import CryptoGuardRepository, _decode_json
from plugins.crypto_guard.strategy.shadow_testing import promote_shadow_candidate, run_shadow_test
from plugins.crypto_guard.strategy.version_manager import create_candidate_version_from_patch


EXTREME_REVIEW_REGIMES = {"extreme_volatility", "funding_shock", "news_like_event", "low_liquidity"}


def _parse_regime_at_loss(value: Any) -> dict[str, Any]:
    """Parse market_regime_at_loss which can be None, dict, JSON string, or plain string.

    Handles:
    - None -> {}
    - dict -> returned as-is
    - JSON string that parses to dict -> returned as dict
    - JSON string that parses to a plain string (legacy) -> {"regime": value}
    - Unparseable string -> {"regime": str(value)}
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, str):
                return {"regime": parsed}
            # JSON parsed to number/bool/null — wrap the parsed value
            return {"regime": str(parsed)}
        except (json.JSONDecodeError, TypeError):
            return {"regime": str(value)}
    return {"regime": str(value)}


def _is_extreme_regime(market_regime_at_loss: Any) -> bool:
    """Check whether the regime at loss time indicates an extreme condition.

    Handles legacy plain-string values and new JSON dict values by
    parsing through _parse_regime_at_loss and checking relevant keys.
    """
    regime_info = _parse_regime_at_loss(market_regime_at_loss)
    if not regime_info:
        return False
    # Check known keys for extreme regime indicators
    for key in ("regime", "market_phase", "regime_alignment"):
        val = regime_info.get(key, "")
        if val and str(val) in EXTREME_REVIEW_REGIMES:
            return True
    return False


def run_self_evolution_cycle(
    repo: CryptoGuardRepository,
    *,
    strategy_name: str = "smc_pullback_long",
    min_reviews: int = 5,
    min_symbols: int = 2,
    min_shadow_samples: int = 30,
    allow_auto_promote: bool = False,
) -> dict[str, Any]:
    reviews = repo.list_trade_reviews_with_trades(limit=500)
    aggregation = aggregate_review_attribution(reviews)
    audit_steps: list[dict[str, Any]] = [{"step": "aggregate_reviews", "result": aggregation}]

    extreme_reviews = [r for r in reviews if _is_extreme_regime(r.get("market_regime_at_loss")) or int(r.get("evolution_trigger_allowed") if r.get("evolution_trigger_allowed") is not None else 1) == 0]
    if extreme_reviews:
        result = _blocked(
            "evolution_paused_extreme_market",
            "近期亏损样本包含极端行情/流动性异常，暂停自动生成策略补丁。",
            aggregation,
            audit_steps + [{"step": "market_regime_gate", "extreme_review_count": len(extreme_reviews)}],
        )
        result["run_id"] = repo.save_self_evolution_run(result)
        return result

    if aggregation["review_count"] < min_reviews:
        result = _blocked(
            "insufficient_reviews",
            f"复盘样本 {aggregation['review_count']} < {min_reviews}",
            aggregation,
            audit_steps,
        )
        result["run_id"] = repo.save_self_evolution_run(result)
        return result
    if aggregation["symbol_count"] < min_symbols:
        result = _blocked(
            "single_symbol_overfit_risk",
            f"覆盖品种 {aggregation['symbol_count']} < {min_symbols}",
            aggregation,
            audit_steps,
        )
        result["run_id"] = repo.save_self_evolution_run(result)
        return result

    existing_candidate = _latest_candidate_version(repo, strategy_name)
    if existing_candidate:
        shadow = run_shadow_test(
            repo,
            strategy_name=strategy_name,
            candidate_version=existing_candidate,
            min_samples=min_shadow_samples,
            allow_auto_promote=allow_auto_promote,
        )
        audit_steps.append({"step": "shadow_test_existing_candidate", "candidate_version": existing_candidate, "result": shadow})
        promoted = None
        if (
            allow_auto_promote
            and shadow.get("recommendation") == "candidate_can_be_promoted_with_manual_confirmation"
            and shadow.get("sample_count", 0) >= min_shadow_samples
        ):
            promoted = promote_shadow_candidate(
                repo,
                strategy_name=strategy_name,
                candidate_version=existing_candidate,
                config_allow_auto=True,
                change_reason="self_evolution_auto_promote_after_shadow_pass",
            )
            audit_steps.append({"step": "promote_existing_candidate", "result": promoted})
        if promoted and promoted.get("ok"):
            result = {
                "ok": True,
                "status": "promoted",
                "strategy_name": strategy_name,
                "aggregation": aggregation,
                "patch_id": None,
                "candidate_version": existing_candidate,
                "shadow_test": shadow,
                "promoted": promoted,
                "audit_steps": audit_steps,
                "explanation": _explain("promoted", aggregation, shadow),
            }
            result["run_id"] = repo.save_self_evolution_run(result)
            return result

        # P0: 止住重复创建 — 已有候选但 shadow 样本不足，等待而不是创建新补丁
        if shadow.get("recommendation") == "insufficient_samples" or shadow.get("status") == "running":
            result = {
                "ok": True,
                "status": "existing_candidate_pending_shadow",
                "strategy_name": strategy_name,
                "aggregation": aggregation,
                "patch_id": None,
                "candidate_version": existing_candidate,
                "shadow_test": shadow,
                "audit_steps": audit_steps,
                "explanation": f"已有候选 {existing_candidate}，影子测试样本不足（{shadow.get('sample_count', 0)}/{min_shadow_samples}），等待积累而非创建新补丁。",
            }
            result["run_id"] = repo.save_self_evolution_run(result)
            return result

    primary_reason = aggregation["top_reasons"][0]["reason"] if aggregation["top_reasons"] else "unknown"
    # Use real trade data from aggregation for context-aware patch, not synthetic MULTI stub
    representative_trade = aggregation.get("representative_trade") or {}
    fallback_patch = build_candidate_patch(representative_trade, primary_reason, strategy_name=strategy_name)
    agent_patch = run_agent_json_task(
        task_name="self_evolution_candidate_patch",
        payload={
            "strategy_name": strategy_name,
            "aggregation": aggregation,
            "recent_reviews": reviews[:50],
            "gates": {"min_reviews": min_reviews, "min_symbols": min_symbols, "min_shadow_samples": min_shadow_samples},
        },
        fallback={"patch": fallback_patch, "rationale": f"规则聚合触发：{primary_reason}", "needs_patch": bool(fallback_patch)},
        instructions=[
            "基于复盘聚合提出策略 candidate patch。",
            "必须避免单品种过拟合；只能输出 candidate patch，不能直接 active。",
            "patch 字段为空表示当前不应生成补丁。",
        ],
    )
    patch = agent_patch.get("patch") if isinstance(agent_patch.get("patch"), dict) else fallback_patch
    if not patch:
        result = _blocked("no_patch_needed", "当前聚合结果偏正向，不生成策略补丁。", aggregation, audit_steps)
        result["agent_patch"] = agent_patch
        result["run_id"] = repo.save_self_evolution_run(result)
        return result

    # Schema validation: reject illegal patches before persisting
    if not _validate_patch_schema(patch):
        result = _blocked("invalid_patch_schema", "LLM 生成的 patch schema 校验失败，拒绝落库。", aggregation, audit_steps)
        result["agent_patch"] = agent_patch
        result["run_id"] = repo.save_self_evolution_run(result)
        return result

    patch["strategy_name"] = strategy_name
    patch["candidate_version"] = _next_candidate_version(repo, strategy_name)
    patch["change_reason"] = f"自进化聚合触发：{primary_reason}"

    # Enforce candidate cap after creating new candidate — atomic with save + create + cap
    from plugins.crypto_guard.strategy.shadow_testing import _enforce_candidate_cap
    with repo.conn.transaction():
        # Check config gate: draft patches stay draft unless auto-promote is allowed
        from plugins.crypto_guard.config.loader import load_config as _load_cfg
        _evo_cfg = _load_cfg().trading_mode.get("evolution", {})
        allow_auto_promote_to_candidate = _evo_cfg.get("allow_auto_promote_to_candidate", False)

        initial_status = "candidate" if (allow_auto_promote or allow_auto_promote_to_candidate) else "draft"
        patch_id = repo.save_strategy_patch_candidate(patch, {"aggregation": aggregation}, status=initial_status)
        candidate = create_candidate_version_from_patch(repo, patch_id, initial_status=initial_status)
        _enforce_candidate_cap(repo, strategy_name, max_candidates=5)
    audit_steps.append({"step": "create_candidate_patch", "patch_id": patch_id, "candidate": candidate})
    audit_steps.append({"step": "ga_llm_candidate_patch", "result": agent_patch})

    # If draft status, skip backtest gate and shadow testing — draft stays draft
    if initial_status == "draft":
        result = {
            "ok": True,
            "status": "draft_pending_approval",
            "strategy_name": strategy_name,
            "aggregation": aggregation,
            "patch_id": patch_id,
            "candidate_version": patch["candidate_version"],
            "audit_steps": audit_steps,
            "agent_patch": agent_patch,
            "explanation": f"候选补丁已创建为 draft 状态（allow_auto_promote_to_candidate=false），等待人工审批。",
        }
        result["run_id"] = repo.save_self_evolution_run(result)
        return result

    # Run backtest gate immediately after candidate creation
    from plugins.crypto_guard.strategy.shadow_testing import run_backtest_gate
    backtest_result = run_backtest_gate(
        repo,
        strategy_name=strategy_name,
        candidate_version=patch["candidate_version"],
    )
    audit_steps.append({"step": "backtest_gate", "result": backtest_result})

    # Save backtest result to strategy_patches
    import json
    with repo.conn.transaction():
        repo.conn.execute(
            "UPDATE strategy_patches SET backtest_result_json=%s WHERE id=%s",
            (json.dumps(backtest_result, ensure_ascii=False), patch_id),
        )

    # If backtest truly fails (not skipped, not gate_disabled, not data_missing),
    # reject the candidate immediately.
    # Covers both ok=true/passed=false AND ok=false (exception) cases.
    skipped = backtest_result.get("skipped", False)
    gate_disabled = backtest_result.get("gate_disabled", False)
    no_data = backtest_result.get("reason") in ("no_valid_backtest_results", "skipped:data_unavailable")
    backtest_exception = backtest_result.get("reason") == "backtest_exception" or not backtest_result.get("ok")
    no_lookahead_failed = "no_lookahead" in str(backtest_result.get("reason", "")).lower() and not backtest_result.get("passed")
    backtest_failed = (
        backtest_exception
        or no_lookahead_failed
        or (not backtest_result.get("passed")
            and not skipped
            and not gate_disabled
            and not no_data)
    )
    if backtest_failed:
        # Update strategy version and patch status to rejected atomically
        with repo.conn.transaction():
            repo.conn.execute(
                "UPDATE strategy_versions SET status='rejected', change_reason=%s WHERE strategy_name=%s AND version=%s",
                (f"回测门禁未通过：{backtest_result.get('reason', 'unknown')}", strategy_name, patch["candidate_version"]),
            )
            repo.conn.execute(
                "UPDATE strategy_patches SET status='rejected' WHERE id=%s",
                (patch_id,),
            )

        result = _blocked(
            "backtest_gate_failed",
            f"回测门禁未通过：{backtest_result.get('reason', 'unknown')}",
            aggregation,
            audit_steps,
        )
        result["backtest_result"] = backtest_result
        result["run_id"] = repo.save_self_evolution_run(result)
        return result

    # Backtest passed or skipped — transition candidate to shadow_testing
    with repo.conn.transaction():
        repo.conn.execute(
            "UPDATE strategy_versions SET status='shadow_testing' WHERE strategy_name=%s AND version=%s AND status='candidate'",
            (strategy_name, patch["candidate_version"]),
        )

    shadow = run_shadow_test(
        repo,
        strategy_name=strategy_name,
        candidate_version=patch["candidate_version"],
        min_samples=min_shadow_samples,
        allow_auto_promote=allow_auto_promote,
    )
    audit_steps.append({"step": "shadow_test", "result": shadow})

    promoted = None
    if (
        allow_auto_promote
        and shadow.get("recommendation") == "candidate_can_be_promoted_with_manual_confirmation"
        and shadow.get("sample_count", 0) >= min_shadow_samples
    ):
        promoted = promote_shadow_candidate(
            repo,
            strategy_name=strategy_name,
            candidate_version=patch["candidate_version"],
            config_allow_auto=True,
            change_reason="self_evolution_auto_promote_after_shadow_pass",
        )
        audit_steps.append({"step": "promote_candidate", "result": promoted})

    status = "promoted" if promoted and promoted.get("ok") else "candidate_pending_shadow" if shadow.get("status") == "running" else "candidate_review_required"
    result = {
        "ok": True,
        "status": status,
        "strategy_name": strategy_name,
        "aggregation": aggregation,
        "patch_id": patch_id,
        "candidate_version": patch["candidate_version"],
        "shadow_test": shadow,
        "promoted": promoted,
        "audit_steps": audit_steps,
        "agent_patch": agent_patch,
        "explanation": _explain(status, aggregation, shadow),
    }
    result["run_id"] = repo.save_self_evolution_run(result)
    return result


def aggregate_review_attribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = Counter(str(r.get("primary_reason") or "unknown") for r in rows)
    symbols = {str(r.get("symbol")) for r in rows if r.get("symbol")}
    pnl_rs = [float(r.get("pnl_r") or 0) for r in rows]
    # Pick a representative trade with the most negative pnl_r for context-aware patch building
    representative = None
    worst_r = 0.0
    for r in rows:
        r_val = float(r.get("pnl_r") or 0)
        if r_val < worst_r:
            worst_r = r_val
            representative = r
    return {
        "review_count": len(rows),
        "symbol_count": len(symbols),
        "symbols": sorted(symbols),
        "avg_r": sum(pnl_rs) / len(pnl_rs) if pnl_rs else 0.0,
        "top_reasons": [{"reason": reason, "count": count} for reason, count in reasons.most_common(5)],
        "representative_trade": representative or {},
    }


def _validate_patch_schema(patch: dict[str, Any]) -> bool:
    """Validate LLM-generated patch schema before persisting.

    Checks:
    - strategy_name is present and non-empty
    - score_adjustments values are {value, when} dicts or floats
    - Nested conditional adjustments validated recursively
    - risk_controls is a list if present
    """
    if not patch.get("strategy_name"):
        return False

    score_adj = patch.get("score_adjustments") or patch.get("score_adjustment")
    if score_adj is not None:
        if not _validate_score_adjustments(score_adj):
            return False

    risk_controls = patch.get("risk_controls")
    if risk_controls is not None and not isinstance(risk_controls, list):
        return False

    return True


def _validate_score_adjustments(score_adj: Any) -> bool:
    """Recursively validate score_adjustments structure.

    Allowed shapes:
    - float/int (flat adjustment)
    - {"value": float, "when": {str: str}} (single conditional)
    - {"adj_name": float} (legacy named flat)
    - {"adj_name": {"value": float, "when": {str: str}}} (named conditional)
    - Recursive nesting via nested_score_adjustments key
    """
    if isinstance(score_adj, (int, float)):
        return True  # flat format
    if isinstance(score_adj, list):
        # List of adjustments with nested structure
        for item in score_adj:
            if not _validate_score_adjustments(item):
                return False
        return True
    if not isinstance(score_adj, dict):
        return False

    for key, val in score_adj.items():
        if key == "nested_score_adjustments":
            # Recurse into nested adjustments
            if not _validate_score_adjustments(val):
                return False
        elif isinstance(val, (int, float)):
            continue  # legacy flat format
        elif isinstance(val, dict):
            if "value" not in val:
                return False
            when = val.get("when", {})
            if not isinstance(when, dict):
                return False
            # Validate when values are strings or simple types (no deeper nesting)
            for _wk, wv in when.items():
                if isinstance(wv, dict):
                    return False  # when clauses cannot contain nested dicts
            # Recurse into nested if present
            if "nested_score_adjustments" in val:
                if not _validate_score_adjustments(val["nested_score_adjustments"]):
                    return False
        else:
            return False
    return True


def _next_candidate_version(repo: CryptoGuardRepository, strategy_name: str) -> str:
    versions = repo.list_strategy_versions(strategy_name)
    existing = len([v for v in versions if str(v.get("version", "")).endswith("-candidate")])
    return f"self-evo-{existing + 1}-candidate"


def _latest_candidate_version(repo: CryptoGuardRepository, strategy_name: str) -> str | None:
    """Return the PRIMARY candidate version (most real PnL samples, then oldest).

    With multi-candidate support, multiple shadow_testing candidates can coexist.
    This returns the primary for informational purposes — it does NOT block new
    candidate creation.
    """
    from plugins.crypto_guard.strategy.shadow_testing import _designate_primary_candidate
    diag = _designate_primary_candidate(repo, strategy_name)
    return diag.get("primary_version")


def _blocked(reason: str, explanation: str, aggregation: dict[str, Any], audit_steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "rejected",
        "reason": reason,
        "aggregation": aggregation,
        "audit_steps": audit_steps + [{"step": "gate", "result": reason}],
        "explanation": explanation,
    }


def _explain(status: str, aggregation: dict[str, Any], shadow: dict[str, Any]) -> str:
    if status == "promoted":
        return "复盘聚合、多品种约束和影子测试均通过，且配置允许自动升级。"
    if shadow.get("recommendation") == "insufficient_samples":
        return f"已生成 candidate，但影子测试样本 {shadow.get('sample_count')} 不足，暂不升级。"
    if shadow.get("recommendation") == "reject_candidate":
        return "影子测试指标未优于 active，拒绝升级。"
    return f"已生成 candidate，需人工确认；复盘样本 {aggregation.get('review_count')}，覆盖品种 {aggregation.get('symbol_count')}。"
