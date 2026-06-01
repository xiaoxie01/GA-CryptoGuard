from __future__ import annotations

from typing import Any, Callable

from plugins.crypto_guard.scheduler.task_locks import acquire_lock, release_lock
from plugins.crypto_guard.storage.repository import CryptoGuardRepository


def run_scheduled_job(
    repo: CryptoGuardRepository,
    *,
    job_name: str,
    scheduled_time: int,
    task_fn: Callable[..., dict[str, Any]],
    lock_ttl_seconds: int = 600,
    **kwargs: Any,
) -> dict[str, Any]:
    if repo.scheduler_success_exists(job_name, scheduled_time):
        return {"ok": True, "skipped": True, "reason": "already_success", "job_name": job_name}

    lock_name = f"scheduler:{job_name}"
    locked, owner = acquire_lock(repo, lock_name, ttl_seconds=lock_ttl_seconds)
    if not locked:
        return {"ok": True, "skipped": True, "reason": "locked", "job_name": job_name}

    run_id = repo.create_scheduler_run(job_name, scheduled_time)
    try:
        result = task_fn(**kwargs)
        repo.finish_scheduler_run(run_id, status="success", result=result)
        return result
    except Exception as exc:
        repo.finish_scheduler_run(run_id, status="failed", error_message=str(exc))
        raise
    finally:
        release_lock(repo, lock_name, owner)
