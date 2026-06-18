from __future__ import annotations

from typing import Any

from plugins.crypto_guard.storage.repository import CryptoGuardRepository


class SkillOrchestrator:
    """Collect dynamic Skill execution refs for a GADecision."""

    def __init__(self, repo: CryptoGuardRepository):
        self.repo = repo

    def result_refs(self, context: dict[str, Any]) -> dict[str, int]:
        refs = self.repo.latest_skill_result_refs(context["symbol"], int(context["analysis_time_utc"]))
        aliases = {
            "price_action_skill": "price_action",
            "momentum_skill": "momentum",
            "trend_stage_skill": "trend_stage",
            "smc_orderflow_skill": "smc_orderflow",
            "chanlun_skill": "chanlun",
        }
        for old, new in aliases.items():
            if old in refs and new not in refs:
                refs[new] = refs[old]
        return refs
