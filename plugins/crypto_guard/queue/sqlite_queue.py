from __future__ import annotations

from typing import Any

from plugins.crypto_guard.storage.repository import CryptoGuardRepository


class SQLiteJobQueue:
    """agent_jobs 队列。priority 数值越小越优先。"""

    def __init__(self, repo: CryptoGuardRepository):
        self.repo = repo

    def enqueue(self, job_type: str, priority: int, source: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = self.repo.enqueue_job(job_type, priority, source, session_id, payload)
        return {"ok": True, "job_id": job_id, "priority": priority, "session_id": session_id}

    def claim_user_job(self) -> dict[str, Any] | None:
        return self.repo.claim_next_job(max_priority=2)

    def claim_background_job(self) -> dict[str, Any] | None:
        return self.repo.claim_next_job(background=True)
