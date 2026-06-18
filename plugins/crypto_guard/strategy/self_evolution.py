from __future__ import annotations

from collections import Counter
from typing import Any

from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.review.evolution_engine import build_candidate_patch
from plugins.crypto_guard.storage.repository import CryptoGuardRepository
from plugins.crypto_guard.strategy.shadow_testing import promote_shadow_candidate, run_shadow_test
from plugins.crypto_guard.strategy.version_manager import create_candidate_version_from_patch


EXTREME_REVIEW_REGIMES = {"extreme_volatility", "funding_shock", "news_like_event", "low_liquidity"}


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

    extreme_reviews = [r for r in reviews if str(r.get("market_regime_at_loss") or "") in EXTREME_REVIEW_REGIMES or int(r.get("evolution_trigger_allowed") if r.get("evolution_trigger_allowed") is not None else 1) == 0]
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
    fallback_patch = build_candidate_patch({"symbol": "MULTI", "pnl_r": aggregation["avg_r"]}, primary_reason)
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
    patch["strategy_name"] = strategy_name
    patch["candidate_version"] = _next_candidate_version(repo, strategy_name)
    patch["change_reason"] = f"自进化聚合触发：{primary_reason}"
    patch_id = repo.save_strategy_patch_candidate(patch, {"aggregation": aggregation})
    candidate = create_candidate_version_from_patch(repo, patch_id)
    audit_steps.append({"step": "create_candidate_patch", "patch_id": patch_id, "candidate": candidate})
    audit_steps.append({"step": "ga_llm_candidate_patch", "result": agent_patch})

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
    repo.conn.execute(
        "UPDATE strategy_patches SET backtest_result_json=? WHERE id=?",
        (json.dumps(backtest_result, ensure_ascii=False), patch_id),
    )

    # If backtest truly fails (not skipped), reject the candidate immediately
    backtest_failed = (
        backtest_result.get("ok")
        and not backtest_result.get("passed")
        and not backtest_result.get("skipped")
    )
    if backtest_failed:
        # Update strategy version status to rejected
        repo.conn.execute(
            "UPDATE strategy_versions SET status='rejected', change_reason=? WHERE strategy_name=? AND version=?",
            (f"回测门禁未通过：{backtest_result.get('reason', 'unknown')}", strategy_name, patch["candidate_version"]),
        )
        # Update strategy patch status to rejected
        repo.conn.execute(
            "UPDATE strategy_patches SET status='rejected' WHERE id=?",
            (patch_id,),
        )
        repo.conn.commit()

        result = _blocked(
            "backtest_gate_failed",
            f"回测门禁未通过：{backtest_result.get('reason', 'unknown')}",
            aggregation,
            audit_steps,
        )
        result["backtest_result"] = backtest_result
        result["run_id"] = repo.save_self_evolution_run(result)
        return result

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
    return {
        "review_count": len(rows),
        "symbol_count": len(symbols),
        "symbols": sorted(symbols),
        "avg_r": sum(pnl_rs) / len(pnl_rs) if pnl_rs else 0.0,
        "top_reasons": [{"reason": reason, "count": count} for reason, count in reasons.most_common(5)],
    }


def _next_candidate_version(repo: CryptoGuardRepository, strategy_name: str) -> str:
    versions = repo.list_strategy_versions(strategy_name)
    existing = len([v for v in versions if str(v.get("version", "")).endswith("-candidate")])
    return f"self-evo-{existing + 1}-candidate"


def _latest_candidate_version(repo: CryptoGuardRepository, strategy_name: str) -> str | None:
    for version in repo.list_strategy_versions(strategy_name):
        if version.get("status") in {"candidate", "shadow_testing"}:
            return str(version["version"])
    return None


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
