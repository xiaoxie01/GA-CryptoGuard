from __future__ import annotations

import json
from typing import Any

from plugins.crypto_guard.reasoning.llm_agent_judge import run_agent_json_task
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def list_strategy_versions(repo: CryptoGuardRepository, strategy_name: str | None = None) -> dict[str, Any]:
    versions = repo.list_strategy_versions(strategy_name)
    agent = run_agent_json_task(
        task_name="strategy_version_management_summary",
        payload={"strategy_name": strategy_name, "versions": versions},
        fallback={
            "summary": "策略版本已列出；candidate 仍需影子测试与人工确认。",
            "risks": [],
            "next_actions": [],
        },
        instructions=[
            "总结 active/candidate/deprecated 策略版本状态、风险和下一步 shadow/review 动作。",
            "不得建议直接绕过 candidate/shadow 流程。",
        ],
    )
    return {"ok": True, "versions": versions, "agent_summary": agent, "text": render_strategy_versions(versions, agent_summary=agent)}


def create_candidate_version_from_patch(repo: CryptoGuardRepository, patch_id: int) -> dict[str, Any]:
    row = repo.conn.execute("SELECT * FROM strategy_patches WHERE id=?", (int(patch_id),)).fetchone()
    if not row:
        return {"ok": False, "error": "strategy_patch 不存在", "patch_id": patch_id}
    patch = dict(row)
    config = _candidate_config(repo, patch)
    agent = run_agent_json_task(
        task_name="candidate_strategy_config_review",
        payload={"patch": patch, "candidate_config": config},
        fallback={
            "summary": "候选策略配置已根据补丁生成，仍需影子测试。",
            "config_notes": [],
            "risk_controls": ["candidate_only", "requires_shadow_testing"],
        },
        instructions=[
            "复核候选策略配置是否保守、是否需要补充风控说明。",
            "只能补充说明字段，不能将 candidate 改为 active。",
        ],
    )
    config["agent_review"] = agent
    version_id = repo.save_strategy_version(
        strategy_name=patch["strategy_name"],
        version=patch["candidate_version"],
        status="shadow_testing",
        config=config,
        change_reason=patch.get("reason") or "candidate_from_patch",
        created_from_review_id=_review_id_from_evidence(patch.get("evidence_json")),
    )
    return {"ok": True, "version_id": version_id, "status": "shadow_testing", "strategy_name": patch["strategy_name"], "version": patch["candidate_version"], "paper_order_permission": "observation_only"}


def rollback_active_strategy(repo: CryptoGuardRepository, strategy_name: str, target_version: str, *, change_reason: str) -> dict[str, Any]:
    if not change_reason:
        return {"ok": False, "error": "change_reason required"}
    return repo.rollback_active_strategy(strategy_name, target_version, change_reason)


def render_strategy_versions(versions: list[dict[str, Any]], agent_summary: dict[str, Any] | None = None) -> str:
    lines = ["**CryptoGuard 策略版本**", ""]
    if agent_summary and agent_summary.get("summary"):
        lines.extend(["**GA/LLM 策略管理摘要：**", str(agent_summary["summary"]), ""])
    if not versions:
        lines.append("- 暂无策略版本")
        return "\n".join(lines)
    for item in versions:
        reason = item.get("change_reason") or "-"
        lines.append(f"- {item['strategy_name']} `{item['version']}`：{item['status']}，reason={reason}")
    lines.append("")
    lines.append("GA 只能创建 candidate；active/rollback 需要显式工具或人工确认。")
    return "\n".join(lines)


def _candidate_config(repo: CryptoGuardRepository, patch: dict[str, Any]) -> dict[str, Any]:
    active = repo.active_strategy_version(patch["strategy_name"])
    base: dict[str, Any] = {}
    if active:
        try:
            base = json.loads(active.get("config_json") or "{}")
        except Exception:
            base = {}
    try:
        patch_json = json.loads(patch.get("patch_json") or "{}")
    except Exception:
        patch_json = {}
    return {
        **base,
        "strategy_name": patch["strategy_name"],
        "version": patch["candidate_version"],
        "status": "shadow_testing",
        "candidate_patch": patch_json,
        "safety": {"candidate_only": True, "requires_shadow_testing": True, "paper_order_permission": "observation_only_until_min_3_signals"},
    }


def _review_id_from_evidence(raw: Any) -> int | None:
    try:
        evidence = json.loads(raw or "{}")
    except Exception:
        return None
    value = evidence.get("review_id")
    return int(value) if value is not None else None
