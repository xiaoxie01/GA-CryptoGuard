from __future__ import annotations

from typing import Any

from plugins.crypto_guard.ga_master.decision_schema import legacy_decision_from_ga_decision
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


class DecisionPersistence:
    def __init__(self, repo: CryptoGuardRepository):
        self.repo = repo

    def save(self, decision: dict[str, Any]) -> dict[str, Any]:
        ga_decision_id = self.repo.create_ga_decision(decision)
        decision["ga_decision_id"] = ga_decision_id
        decision["id"] = ga_decision_id
        if decision.get("analysis_state_id"):
            self.repo.attach_ga_decision_to_analysis_state(int(decision["analysis_state_id"]), ga_decision_id)

        # Compatibility read model while the rest of the system is migrated.
        legacy = legacy_decision_from_ga_decision(decision)
        signal_id = self.repo.create_signal(legacy, decision.get("snapshot_id"), ga_decision_id=ga_decision_id)
        decision["signal_id"] = signal_id
        return decision
