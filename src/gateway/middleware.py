"""AI Gateway Middleware -- intercepts every request through the control plane.

Applies authentication, rate limiting, PII redaction, token budgets,
guardrails, and audit logging as a single FastAPI middleware.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from src.config import get_settings
from src.gateway.audit_trail import AuditTrail
from src.gateway.guardrails import GuardrailCheckResult, GuardrailsEngine
from src.gateway.pii_redactor import PIIRedactor
from src.gateway.token_budget import TokenBudgetManager

logger = structlog.get_logger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Sliding-window rate limiter (Redis-backed)
# ---------------------------------------------------------------------------

_RATE_LIMIT_WINDOW_SECONDS = settings.rate_limit_window
_RATE_LIMIT_MAX_REQUESTS = settings.rate_limit_requests


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_at: float
    limit: int


class SlidingWindowRateLimiter:
    """Token-bucket-style sliding window backed by Redis sorted sets."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: Any = None

    async def _get_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                self._redis_url, decode_responses=True
            )
        return self._redis

    async def check(
        self,
        key: str,
        max_requests: int = _RATE_LIMIT_MAX_REQUESTS,
        window: int = _RATE_LIMIT_WINDOW_SECONDS,
    ) -> RateLimitResult:
        """Return whether the request is allowed under the sliding window."""
        now = time.time()
        window_start = now - window
        redis = await self._get_redis()

        pipe = redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zadd(key, {str(uuid4()): now})
        pipe.zcard(key)
        pipe.expire(key, window * 2)
        results = await pipe.execute()

        current_count: int = results[2]
        allowed = current_count <= max_requests
        remaining = max(0, max_requests - current_count)
        reset_at = now + window

        if not allowed:
            # Remove the request we just added so it doesn't consume quota
            await redis.zremrangebyscore(key, now, now)

        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            reset_at=reset_at,
            limit=max_requests,
        )


# ---------------------------------------------------------------------------
# JWT / API-key verifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthIdentity:
    user_id: str
    org_id: str
    role: str  # "analyst" | "lead" | "manager" | "admin"
    scopes: tuple[str, ...]


class AuthVerifier:
    """Verify JWT or API-key from the request."""

    def __init__(self, jwt_secret: str, jwt_algorithm: str) -> None:
        self._secret = jwt_secret
        self._algorithm = jwt_algorithm

    async def verify(self, request: Request) -> AuthIdentity | None:
        # Try Authorization header first (Bearer token)
        auth_header = request.headers.get("Authorization", "")
        api_key = request.headers.get("X-API-Key", "")

        if auth_header.startswith("Bearer "):
            return await self._verify_jwt(auth_header[7:])
        if api_key:
            return await self._verify_api_key(api_key)
        return None

    async def _verify_jwt(self, token: str) -> AuthIdentity | None:
        try:
            from jose import jwt as jose_jwt

            payload = jose_jwt.decode(
                token, self._secret, algorithms=[self._algorithm]
            )
            return AuthIdentity(
                user_id=payload.get("sub", "unknown"),
                org_id=payload.get("org_id", "default"),
                role=payload.get("role", "analyst"),
                scopes=tuple(payload.get("scopes", [])),
            )
        except Exception as exc:
            logger.warning("jwt_verification_failed", error=str(exc))
            return None

    async def _verify_api_key(self, api_key: str) -> AuthIdentity | None:
        # In production this would look up the key in a database.
        # For now we accept keys that start with "soc-" as valid.
        if not api_key.startswith("soc-"):
            return None
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:12]
        return AuthIdentity(
            user_id=f"apikey-{key_hash}",
            org_id="default",
            role="analyst",
            scopes=("read", "write"),
        )


# ---------------------------------------------------------------------------
# Request router (determines which agent sub-component to target)
# ---------------------------------------------------------------------------

_ROUTE_MAP: dict[str, str] = {
    "/api/v1/chat": "orchestrator",
    "/api/v1/triage": "triage",
    "/api/v1/enrich": "enrichment",
    "/api/v1/correlate": "correlation",
    "/api/v1/investigate": "investigation",
    "/api/v1/incidents": "incidents",
}


def resolve_route(path: str) -> str:
    """Map a URL path to an internal agent sub-component name."""
    for prefix, component in _ROUTE_MAP.items():
        if path.startswith(prefix):
            return component
    return "default"


# ---------------------------------------------------------------------------
# Gateway Middleware
# ---------------------------------------------------------------------------

# Paths that skip authentication
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {"/health", "/healthz", "/ready", "/metrics", "/docs", "/redoc", "/openapi.json"}
)


class GatewayMiddleware(BaseHTTPMiddleware):
    """Unified control-plane middleware for the SOC Analyst Agent.

    Pipeline order:
    1. Authentication verification
    2. Rate limiting
    3. PII redaction on request body
    4. Token budget enforcement
    5. Guardrail checks
    6. Route to sub-component
    7. PII redaction on response body
    8. Audit trail logging
    """

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)
        self._auth = AuthVerifier(settings.jwt_secret, settings.jwt_algorithm)
        self._rate_limiter = SlidingWindowRateLimiter(settings.redis_url)
        self._pii = PIIRedactor()
        self._budget = TokenBudgetManager(settings.redis_url)
        self._guardrails = GuardrailsEngine()
        self._audit = AuditTrail(settings.database_url)

    # ------------------------------------------------------------------
    # Main dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = str(uuid4())
        start_time = time.time()

        # Attach request_id for downstream access
        request.state.request_id = request_id

        # Allow public endpoints through without checks
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        # 1 -- Authentication
        identity = await self._auth.verify(request)
        if identity is None:
            return self._error(401, "Authentication required", request_id)
        request.state.identity = identity

        # 2 -- Rate limiting
        rate_key = f"rate:{identity.user_id}"
        rate_result = await self._rate_limiter.check(rate_key)
        if not rate_result.allowed:
            await self._audit.log_request(
                request_id=request_id,
                user_id=identity.user_id,
                action="rate_limited",
                path=request.url.path,
                method=request.method,
                status_code=429,
                latency_ms=0,
                model="",
                input_hash="",
                output_hash="",
                tokens_used=0,
            )
            return self._error(
                429,
                "Rate limit exceeded",
                request_id,
                headers={
                    "X-RateLimit-Limit": str(rate_result.limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(rate_result.reset_at)),
                },
            )

        # 3 -- Read and redact request body PII
        body_bytes = await request.body()
        body_text = body_bytes.decode("utf-8", errors="replace")
        redacted_input, input_redactions = self._pii.redact(body_text)
        if input_redactions:
            logger.info(
                "pii_redacted_input",
                request_id=request_id,
                redaction_count=len(input_redactions),
            )

        input_hash = hashlib.sha256(redacted_input.encode()).hexdigest()

        # 4 -- Token budget enforcement
        estimated_tokens = self._budget.estimate_tokens(redacted_input)
        budget_ok = await self._budget.check_budget(
            user_id=identity.user_id,
            org_id=identity.org_id,
            estimated_tokens=estimated_tokens,
        )
        if not budget_ok.allowed:
            return self._error(
                429,
                f"Token budget exceeded: {budget_ok.reason}",
                request_id,
                headers={
                    "X-Token-Budget-Remaining": str(budget_ok.remaining),
                    "X-Token-Budget-Limit": str(budget_ok.limit),
                },
            )

        # 5 -- Guardrail checks on input
        guardrail_result = await self._guardrails.check_input(redacted_input)
        if guardrail_result.decision == "deny":
            await self._audit.log_guardrail(
                request_id=request_id,
                check_name=guardrail_result.check_name,
                result="denied",
                reason=guardrail_result.reason,
            )
            return self._error(400, f"Blocked by guardrail: {guardrail_result.reason}", request_id)
        if guardrail_result.decision == "warn":
            logger.warning(
                "guardrail_warning",
                request_id=request_id,
                check=guardrail_result.check_name,
                reason=guardrail_result.reason,
            )

        # 6 -- Resolve route
        component = resolve_route(request.url.path)
        request.state.target_component = component

        # Forward to the actual handler
        response = await call_next(request)

        # 7 -- Redact PII in response body
        response_body = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            if isinstance(chunk, str):
                response_body += chunk.encode("utf-8")
            else:
                response_body += chunk

        response_text = response_body.decode("utf-8", errors="replace")
        redacted_output, output_redactions = self._pii.redact(response_text)
        output_hash = hashlib.sha256(redacted_output.encode()).hexdigest()

        # 8 -- Record token usage
        response_tokens = self._budget.estimate_tokens(redacted_output)
        total_tokens = estimated_tokens + response_tokens
        await self._budget.record_usage(
            user_id=identity.user_id,
            org_id=identity.org_id,
            tokens=total_tokens,
        )

        # 9 -- Audit trail
        latency_ms = (time.time() - start_time) * 1000
        await self._audit.log_request(
            request_id=request_id,
            user_id=identity.user_id,
            action=f"{request.method} {request.url.path}",
            path=request.url.path,
            method=request.method,
            status_code=response.status_code,
            latency_ms=round(latency_ms, 2),
            model=settings.llm_model,
            input_hash=input_hash,
            output_hash=output_hash,
            tokens_used=total_tokens,
        )

        # Build final response with budget headers
        budget_remaining = await self._budget.get_remaining(
            identity.user_id, identity.org_id
        )
        final_headers = dict(response.headers)
        final_headers["X-Request-Id"] = request_id
        final_headers["X-Token-Budget-Remaining-User"] = str(
            budget_remaining.get("user_remaining", 0)
        )
        final_headers["X-Token-Budget-Remaining-Org"] = str(
            budget_remaining.get("org_remaining", 0)
        )
        final_headers["X-RateLimit-Remaining"] = str(rate_result.remaining)

        return Response(
            content=redacted_output.encode("utf-8"),
            status_code=response.status_code,
            headers=final_headers,
            media_type=response.media_type,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _error(
        status: int,
        detail: str,
        request_id: str,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        resp_headers = {"X-Request-Id": request_id}
        if headers:
            resp_headers.update(headers)
        return JSONResponse(
            status_code=status,
            content={"error": detail, "request_id": request_id},
            headers=resp_headers,
        )
