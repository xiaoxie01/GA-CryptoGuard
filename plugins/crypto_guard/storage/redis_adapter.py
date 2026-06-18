from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def should_use_redis_for_path(database_path: str | os.PathLike[str] | None) -> bool:
    if os.environ.get("CRYPTO_GUARD_REDIS_DISABLED") == "1":
        return False
    if not database_path:
        return True
    try:
        db = Path(database_path).resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
        return tmp not in db.parents and db != tmp
    except Exception:
        return True


class RedisAdapter:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("CRYPTO_GUARD_REDIS_URL") or DEFAULT_REDIS_URL
        self._client = None
        self._error: str | None = None

    @property
    def client(self) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            import redis

            self._client = redis.Redis.from_url(self.url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=1.0)
            self._client.ping()
            self._error = None
            return self._client
        except Exception as exc:
            self._error = str(exc)
            self._client = None
            return None

    def is_available(self) -> bool:
        return self.client is not None

    def health_check(self) -> dict[str, Any]:
        client = self.client
        if not client:
            return {"status": "degraded", "url": self.url, "error": self._error}
        try:
            client.set("health:redis:last_ping", str(int(time.time())), ex=300)
            return {
                "status": "ok",
                "url": self.url,
                "queues": {
                    "queue:user:feishu": int(client.llen("queue:user:feishu")),
                    "queue:ga:background": int(client.llen("queue:ga:background")),
                },
            }
        except Exception as exc:
            self._error = str(exc)
            return {"status": "degraded", "url": self.url, "error": str(exc)}

    def enqueue_user_job(self, payload: dict[str, Any]) -> str | None:
        return self._enqueue("queue:user:feishu", payload)

    def enqueue_background_job(self, payload: dict[str, Any]) -> str | None:
        return self._enqueue("queue:ga:background", payload)

    def pop_user_job(self) -> dict[str, Any] | None:
        return self._pop("queue:user:feishu")

    def pop_background_job(self) -> dict[str, Any] | None:
        return self._pop("queue:ga:background")

    def set_latest_price(self, symbol: str, price: float, ttl_seconds: int = 600) -> None:
        client = self.client
        if client:
            client.set(f"latest_price:{symbol}", str(float(price)), ex=int(ttl_seconds))

    def get_latest_price(self, symbol: str) -> float | None:
        client = self.client
        if not client:
            return None
        value = client.get(f"latest_price:{symbol}")
        return float(value) if value is not None else None

    def acquire_lock(self, name: str, ttl_seconds: int, owner: str | None = None) -> bool:
        client = self.client
        if not client:
            return False
        return bool(client.set(f"lock:{name}", owner or str(uuid.uuid4()), nx=True, ex=max(int(ttl_seconds), 1)))

    def release_lock(self, name: str) -> None:
        client = self.client
        if client:
            client.delete(f"lock:{name}")

    def is_quiet(self, symbol: str, alert_type: str) -> bool:
        client = self.client
        return bool(client and client.exists(f"quiet:{symbol}:{alert_type}"))

    def set_quiet(self, symbol: str, alert_type: str, ttl_seconds: int) -> None:
        client = self.client
        if client:
            client.set(f"quiet:{symbol}:{alert_type}", "1", ex=max(int(ttl_seconds), 1))

    def dedupe_event(self, event_id: str, ttl_seconds: int = 3600) -> bool:
        if not event_id:
            return True
        client = self.client
        if not client:
            return True
        return bool(client.set(f"dedupe:feishu_event:{event_id}", "1", nx=True, ex=max(int(ttl_seconds), 1)))

    def _enqueue(self, key: str, payload: dict[str, Any]) -> str | None:
        client = self.client
        if not client:
            return None
        job_id = str(uuid.uuid4())
        item = {"redis_job_id": job_id, **payload}
        client.rpush(key, json.dumps(item, ensure_ascii=False))
        return job_id

    def _pop(self, key: str) -> dict[str, Any] | None:
        client = self.client
        if not client:
            return None
        raw = client.lpop(key)
        if not raw:
            return None
        return json.loads(raw)
