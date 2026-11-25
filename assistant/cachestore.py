"""Response cache backends (CLAUDE.md §10).

Keys come from assistant/cache.py: SHA-256 of tenant, user, and normalised query, so a
tenant or user cannot read another's entries. TTL is 300 s with +/-45 s jitter.

RedisCache fails open: if Redis is unreachable, get() returns None and set() is skipped,
and `errors` counts the failures. That is the "bypass the cache" degradation (CLAUDE.md §10).
"""

from __future__ import annotations

import json
import os
import random
import threading
from typing import Any

TTL_S = 300
JITTER_S = 45
PREFIX = "ua:cache:"


class DictCache:
    """In-process cache. Used when REDIS_URL is not set."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.errors = 0

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data.get(key)
        return None if value is None else dict(value)

    def set(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = dict(value)


class RedisCache:
    """Redis-backed cache with a per-entry jittered TTL. Fails open."""

    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5,
                                            decode_responses=True)
        self.errors = 0

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = self._client.get(PREFIX + key)
        except Exception:  # noqa: BLE001 - fail open: treat as a miss
            self.errors += 1
            return None
        return None if raw is None else json.loads(raw)

    def set(self, key: str, value: dict[str, Any]) -> None:
        ttl = TTL_S + random.randint(-JITTER_S, JITTER_S)
        try:
            self._client.set(PREFIX + key, json.dumps(value, default=str), ex=ttl)
        except Exception:  # noqa: BLE001 - fail open: skip the write
            self.errors += 1


def make_cache():
    """RedisCache when REDIS_URL is set, otherwise the in-process cache."""
    url = os.environ.get("REDIS_URL", "")
    return RedisCache(url) if url else DictCache()
