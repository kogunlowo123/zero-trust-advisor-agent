"""Exact-match cache using content hash."""
from typing import Any
import hashlib


class ExactCache:
    def __init__(self):
        self._cache: dict[str, Any] = {}

    async def get(self, key: str) -> Any | None:
        h = hashlib.sha256(key.encode()).hexdigest()
        return self._cache.get(h)

    async def set(self, key: str, value: Any, ttl_seconds: int = 300):
        h = hashlib.sha256(key.encode()).hexdigest()
        self._cache[h] = value
