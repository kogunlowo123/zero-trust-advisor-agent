"""Token budget manager.

Tracks token usage per user (daily) and per organization (monthly).
Enforces configurable limits with warn-at-80% and block-at-100% thresholds.
Uses tiktoken for accurate token counting and Redis for state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

DEFAULT_USER_DAILY_LIMIT = 100_000  # tokens per user per day
DEFAULT_ORG_MONTHLY_LIMIT = 5_000_000  # tokens per org per month
WARN_THRESHOLD = 0.80  # warn when 80% consumed
BLOCK_THRESHOLD = 1.00  # block at 100%

# TTLs for Redis keys
_USER_KEY_TTL_SECONDS = 86_400 * 2  # 2 days (covers daily reset + buffer)
_ORG_KEY_TTL_SECONDS = 86_400 * 35  # 35 days (covers monthly reset + buffer)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCheckResult:
    allowed: bool
    reason: str
    remaining: int
    limit: int
    usage: int
    utilization: float  # 0..1


# ---------------------------------------------------------------------------
# Token counter (tiktoken)
# ---------------------------------------------------------------------------


class _TokenCounter:
    """Lazy-initialised tiktoken encoder."""

    def __init__(self, model: str = "gpt-4o") -> None:
        self._model = model
        self._encoder: Any = None

    def _get_encoder(self) -> Any:
        if self._encoder is None:
            import tiktoken

            try:
                self._encoder = tiktoken.encoding_for_model(self._model)
            except KeyError:
                self._encoder = tiktoken.get_encoding("cl100k_base")
        return self._encoder

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._get_encoder().encode(text))


# ---------------------------------------------------------------------------
# Budget manager
# ---------------------------------------------------------------------------


class TokenBudgetManager:
    """Track and enforce token budgets in Redis."""

    def __init__(
        self,
        redis_url: str,
        user_daily_limit: int = DEFAULT_USER_DAILY_LIMIT,
        org_monthly_limit: int = DEFAULT_ORG_MONTHLY_LIMIT,
    ) -> None:
        self._redis_url = redis_url
        self._redis: Any = None
        self._counter = _TokenCounter()
        self._user_daily_limit = user_daily_limit
        self._org_monthly_limit = org_monthly_limit

    async def _get_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                self._redis_url, decode_responses=True
            )
        return self._redis

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def estimate_tokens(self, text: str) -> int:
        """Count tokens in *text* using tiktoken."""
        return self._counter.count(text)

    async def check_budget(
        self,
        user_id: str,
        org_id: str,
        estimated_tokens: int,
    ) -> BudgetCheckResult:
        """Check if the request is within budget.

        Returns a result indicating allow/deny and remaining quota.
        Checks both user-daily and org-monthly limits; the more
        restrictive one wins.
        """
        user_result = await self._check_user(user_id, estimated_tokens)
        if not user_result.allowed:
            return user_result

        org_result = await self._check_org(org_id, estimated_tokens)
        if not org_result.allowed:
            return org_result

        # Return the tighter of the two (higher utilization)
        if user_result.utilization >= org_result.utilization:
            return user_result
        return org_result

    async def record_usage(
        self,
        user_id: str,
        org_id: str,
        tokens: int,
    ) -> None:
        """Record actual token usage after a successful request."""
        redis = await self._get_redis()

        user_key = self._user_key(user_id)
        org_key = self._org_key(org_id)

        pipe = redis.pipeline()
        pipe.incrby(user_key, tokens)
        pipe.expire(user_key, _USER_KEY_TTL_SECONDS)
        pipe.incrby(org_key, tokens)
        pipe.expire(org_key, _ORG_KEY_TTL_SECONDS)
        await pipe.execute()

        logger.debug(
            "token_usage_recorded",
            user_id=user_id,
            org_id=org_id,
            tokens=tokens,
        )

    async def get_remaining(
        self, user_id: str, org_id: str
    ) -> dict[str, int]:
        """Return remaining budgets for response headers."""
        redis = await self._get_redis()
        user_used = int(await redis.get(self._user_key(user_id)) or 0)
        org_used = int(await redis.get(self._org_key(org_id)) or 0)
        return {
            "user_remaining": max(0, self._user_daily_limit - user_used),
            "user_limit": self._user_daily_limit,
            "user_used": user_used,
            "org_remaining": max(0, self._org_monthly_limit - org_used),
            "org_limit": self._org_monthly_limit,
            "org_used": org_used,
        }

    async def reset_user(self, user_id: str) -> None:
        """Manually reset a user's daily budget."""
        redis = await self._get_redis()
        await redis.delete(self._user_key(user_id))
        logger.info("user_budget_reset", user_id=user_id)

    async def reset_org(self, org_id: str) -> None:
        """Manually reset an org's monthly budget."""
        redis = await self._get_redis()
        await redis.delete(self._org_key(org_id))
        logger.info("org_budget_reset", org_id=org_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _check_user(
        self, user_id: str, estimated_tokens: int
    ) -> BudgetCheckResult:
        redis = await self._get_redis()
        used = int(await redis.get(self._user_key(user_id)) or 0)
        projected = used + estimated_tokens
        utilization = projected / self._user_daily_limit if self._user_daily_limit > 0 else 0
        remaining = max(0, self._user_daily_limit - used)

        if utilization >= BLOCK_THRESHOLD:
            logger.warning(
                "user_budget_exceeded",
                user_id=user_id,
                used=used,
                limit=self._user_daily_limit,
            )
            return BudgetCheckResult(
                allowed=False,
                reason=f"User daily token budget exceeded ({used}/{self._user_daily_limit})",
                remaining=remaining,
                limit=self._user_daily_limit,
                usage=used,
                utilization=utilization,
            )

        if utilization >= WARN_THRESHOLD:
            logger.info(
                "user_budget_warning",
                user_id=user_id,
                utilization=f"{utilization:.0%}",
            )

        return BudgetCheckResult(
            allowed=True,
            reason="Within user daily budget",
            remaining=remaining,
            limit=self._user_daily_limit,
            usage=used,
            utilization=utilization,
        )

    async def _check_org(
        self, org_id: str, estimated_tokens: int
    ) -> BudgetCheckResult:
        redis = await self._get_redis()
        used = int(await redis.get(self._org_key(org_id)) or 0)
        projected = used + estimated_tokens
        utilization = projected / self._org_monthly_limit if self._org_monthly_limit > 0 else 0
        remaining = max(0, self._org_monthly_limit - used)

        if utilization >= BLOCK_THRESHOLD:
            logger.warning(
                "org_budget_exceeded",
                org_id=org_id,
                used=used,
                limit=self._org_monthly_limit,
            )
            return BudgetCheckResult(
                allowed=False,
                reason=f"Organization monthly token budget exceeded ({used}/{self._org_monthly_limit})",
                remaining=remaining,
                limit=self._org_monthly_limit,
                usage=used,
                utilization=utilization,
            )

        if utilization >= WARN_THRESHOLD:
            logger.info(
                "org_budget_warning",
                org_id=org_id,
                utilization=f"{utilization:.0%}",
            )

        return BudgetCheckResult(
            allowed=True,
            reason="Within organization monthly budget",
            remaining=remaining,
            limit=self._org_monthly_limit,
            usage=used,
            utilization=utilization,
        )

    # ------------------------------------------------------------------
    # Key helpers -- embed date so keys auto-partition by period
    # ------------------------------------------------------------------

    @staticmethod
    def _user_key(user_id: str) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"token_budget:user:{user_id}:{today}"

    @staticmethod
    def _org_key(org_id: str) -> str:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return f"token_budget:org:{org_id}:{month}"
