"""Short-term (session) memory backed by Redis.

Responsibilities
----------------
* Keep the last *N* messages of each analyst conversation.
* Provide a per-session scratch-pad for in-progress investigation notes.
* Expire idle sessions automatically (default TTL = 1 hour).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TTL_SECONDS: int = 3600          # 1 hour
MAX_CONTEXT_MESSAGES: int = 50           # ring-buffer size


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------
class Message(BaseModel):
    """A single conversational turn."""

    role: str = Field(description="'user', 'assistant', or 'system'")
    content: str
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Short-term memory store
# ---------------------------------------------------------------------------
class ShortTermMemory:
    """Redis-backed session memory with TTL expiry."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_messages: int = MAX_CONTEXT_MESSAGES,
    ) -> None:
        self._redis_url = redis_url
        self._ttl = ttl_seconds
        self._max = max_messages
        self._pool: aioredis.Redis | None = None

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Initialise the async Redis connection pool."""
        if self._pool is None:
            self._pool = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                max_connections=20,
            )
            logger.info("short_term_memory.connected", redis_url=self._redis_url)

    async def close(self) -> None:
        """Drain the connection pool."""
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None
            logger.info("short_term_memory.closed")

    @property
    def _redis(self) -> aioredis.Redis:
        if self._pool is None:
            raise RuntimeError("ShortTermMemory not connected — call connect() first")
        return self._pool

    # -- key helpers ---------------------------------------------------------

    @staticmethod
    def _ctx_key(session_id: str) -> str:
        return f"soc:stm:ctx:{session_id}"

    @staticmethod
    def _scratch_key(session_id: str) -> str:
        return f"soc:stm:scratch:{session_id}"

    # -- context (conversation history) --------------------------------------

    async def get_context(self, session_id: str) -> list[dict[str, Any]]:
        """Return the last *N* messages for *session_id* (oldest-first)."""
        raw_items = await self._redis.lrange(self._ctx_key(session_id), 0, -1)
        return [json.loads(item) for item in raw_items]

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Push a message onto the conversation ring-buffer.

        Returns the current length of the context list.
        """
        msg = Message(role=role, content=content, metadata=metadata or {})
        key = self._ctx_key(session_id)

        pipe = self._redis.pipeline(transaction=True)
        pipe.rpush(key, msg.model_dump_json())
        pipe.ltrim(key, -self._max, -1)  # keep only the last N
        pipe.expire(key, self._ttl)
        results = await pipe.execute()

        length: int = results[0]  # rpush returns new length
        logger.debug(
            "short_term_memory.append",
            session_id=session_id,
            role=role,
            length=length,
        )
        return min(length, self._max)

    # -- scratch pad ---------------------------------------------------------

    async def get_scratch(self, session_id: str) -> dict[str, Any]:
        """Return the full scratch-pad hash for *session_id*."""
        raw = await self._redis.hgetall(self._scratch_key(session_id))
        result: dict[str, Any] = {}
        for k, v in raw.items():
            try:
                result[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                result[k] = v
        return result

    async def set_scratch(
        self,
        session_id: str,
        key: str,
        value: Any,
    ) -> None:
        """Set a single field in the session scratch-pad."""
        scratch_key = self._scratch_key(session_id)
        serialised = json.dumps(value) if not isinstance(value, str) else value

        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(scratch_key, key, serialised)
        pipe.expire(scratch_key, self._ttl)
        await pipe.execute()

        logger.debug(
            "short_term_memory.set_scratch",
            session_id=session_id,
            key=key,
        )

    # -- housekeeping --------------------------------------------------------

    async def clear_session(self, session_id: str) -> None:
        """Delete all data for *session_id*."""
        await self._redis.delete(
            self._ctx_key(session_id),
            self._scratch_key(session_id),
        )
        logger.info("short_term_memory.cleared", session_id=session_id)

    async def touch(self, session_id: str) -> None:
        """Refresh the TTL for an active session."""
        pipe = self._redis.pipeline(transaction=True)
        pipe.expire(self._ctx_key(session_id), self._ttl)
        pipe.expire(self._scratch_key(session_id), self._ttl)
        await pipe.execute()
