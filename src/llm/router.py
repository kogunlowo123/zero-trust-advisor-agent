"""Multi-model router with task-based routing, fallback chains, and cost tracking.

Routing Strategy
----------------
* Complex reasoning  (triage, investigation)  -> Claude Sonnet or GPT-4o
* Simple classification (alert priority)      -> Claude Haiku  or GPT-4o-mini
* Embedding generation                       -> text-embedding-3-large
* Reranking                                  -> cross-encoder (sentence-transformers)
* Code generation (SIEM queries)             -> GPT-4o or Claude Sonnet

Each task type has a primary and secondary model; if the primary fails the
router transparently falls through to the secondary.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator

import structlog
from pydantic import BaseModel, Field

from src.llm.providers.anthropic_provider import AnthropicProvider
from src.llm.providers.base import LLMProvider, ModelInfo
from src.llm.providers.google_provider import GoogleProvider
from src.llm.providers.openai_provider import OpenAIProvider

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Task taxonomy
# ---------------------------------------------------------------------------
class TaskType(str, Enum):
    """High-level task categories that influence model selection."""

    COMPLEX_REASONING = "complex_reasoning"
    SIMPLE_CLASSIFICATION = "simple_classification"
    EMBEDDING = "embedding"
    RERANKING = "reranking"
    CODE_GENERATION = "code_generation"
    GENERAL = "general"


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RouteEntry:
    """A primary + fallback model for a given task type."""

    provider: str
    model: str
    fallback_provider: str | None = None
    fallback_model: str | None = None


_DEFAULT_ROUTES: dict[TaskType, RouteEntry] = {
    TaskType.COMPLEX_REASONING: RouteEntry(
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        fallback_provider="openai",
        fallback_model="gpt-4o",
    ),
    TaskType.SIMPLE_CLASSIFICATION: RouteEntry(
        provider="anthropic",
        model="claude-3-5-haiku-20241022",
        fallback_provider="openai",
        fallback_model="gpt-4o-mini",
    ),
    TaskType.EMBEDDING: RouteEntry(
        provider="openai",
        model="text-embedding-3-large",
        fallback_provider="google",
        fallback_model="text-embedding-004",
    ),
    TaskType.RERANKING: RouteEntry(
        provider="reranker",
        model="cross-encoder/ms-marco-MiniLM-L-12-v2",
    ),
    TaskType.CODE_GENERATION: RouteEntry(
        provider="openai",
        model="gpt-4o",
        fallback_provider="anthropic",
        fallback_model="claude-sonnet-4-20250514",
    ),
    TaskType.GENERAL: RouteEntry(
        provider="openai",
        model="gpt-4o",
        fallback_provider="anthropic",
        fallback_model="claude-sonnet-4-20250514",
    ),
}


# ---------------------------------------------------------------------------
# Usage stats
# ---------------------------------------------------------------------------
class UsageRecord(BaseModel):
    """Accumulated usage for a single model."""

    model: str
    provider: str
    total_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0
    errors: int = 0


# ---------------------------------------------------------------------------
# Multi-model router
# ---------------------------------------------------------------------------
class MultiModelRouter:
    """Routes LLM requests to the best provider/model for each task type."""

    def __init__(
        self,
        *,
        openai_api_key: str | None = None,
        anthropic_api_key: str | None = None,
        google_api_key: str | None = None,
        azure_endpoint: str | None = None,
        azure_api_version: str = "2024-10-21",
        bedrock_region: str | None = None,
        google_project_id: str | None = None,
        routes: dict[TaskType, RouteEntry] | None = None,
    ) -> None:
        # Build provider registry
        self._providers: dict[str, LLMProvider] = {}

        self._providers["openai"] = OpenAIProvider(
            api_key=openai_api_key,
            azure_endpoint=azure_endpoint,
            azure_api_version=azure_api_version,
        )

        self._providers["anthropic"] = AnthropicProvider(
            api_key=anthropic_api_key,
            bedrock_region=bedrock_region,
        )

        self._providers["google"] = GoogleProvider(
            api_key=google_api_key,
            project_id=google_project_id,
        )

        # Route overrides from env
        self._routes = routes or dict(_DEFAULT_ROUTES)
        self._override_routes_from_env()

        # Usage tracking
        self._usage: dict[str, UsageRecord] = {}

        # Optional cross-encoder for reranking (lazy loaded)
        self._reranker: Any = None

    # -- env overrides -------------------------------------------------------

    def _override_routes_from_env(self) -> None:
        """Allow route overrides via env vars, e.g.

        ROUTE_COMPLEX_REASONING_PROVIDER=openai
        ROUTE_COMPLEX_REASONING_MODEL=gpt-4o
        """
        for task_type in TaskType:
            prefix = f"ROUTE_{task_type.name}"
            provider = os.environ.get(f"{prefix}_PROVIDER")
            model = os.environ.get(f"{prefix}_MODEL")
            if provider and model:
                existing = self._routes.get(task_type, _DEFAULT_ROUTES[TaskType.GENERAL])
                self._routes[task_type] = RouteEntry(
                    provider=provider,
                    model=model,
                    fallback_provider=existing.fallback_provider,
                    fallback_model=existing.fallback_model,
                )
                logger.info(
                    "router.route_override",
                    task=task_type.value,
                    provider=provider,
                    model=model,
                )

    # -- internal helpers ----------------------------------------------------

    def _get_provider(self, provider_name: str) -> LLMProvider | None:
        return self._providers.get(provider_name)

    def _record_usage(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        error: bool = False,
    ) -> None:
        key = f"{provider}:{model}"
        if key not in self._usage:
            self._usage[key] = UsageRecord(model=model, provider=provider)
        rec = self._usage[key]
        rec.total_requests += 1
        rec.total_input_tokens += input_tokens
        rec.total_output_tokens += output_tokens
        rec.total_latency_ms += latency_ms
        if error:
            rec.errors += 1

        # Estimate cost
        prov = self._get_provider(provider)
        if prov is not None:
            info = prov.get_model_info(model)
            rec.total_cost_usd += (
                input_tokens / 1000.0 * info.cost_per_1k_input
                + output_tokens / 1000.0 * info.cost_per_1k_output
            )

    # -- public API: chat ----------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        task_type: TaskType = TaskType.GENERAL,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        """Route a chat request through the primary model, falling back if it fails."""
        route = self._routes.get(task_type, _DEFAULT_ROUTES[TaskType.GENERAL])

        # --- primary attempt ---
        primary_provider = self._get_provider(route.provider)
        if primary_provider is not None:
            try:
                start = time.monotonic()
                result = await primary_provider.chat(
                    messages,
                    model=route.model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    **kwargs,
                )
                elapsed = (time.monotonic() - start) * 1000
                input_toks = primary_provider.count_tokens(
                    " ".join(m.get("content", "") for m in messages),
                    model=route.model,
                )
                output_toks = primary_provider.count_tokens(result, model=route.model)
                self._record_usage(
                    route.provider, route.model, input_toks, output_toks, elapsed
                )
                logger.info(
                    "router.chat",
                    task=task_type.value,
                    provider=route.provider,
                    model=route.model,
                    latency_ms=round(elapsed, 1),
                )
                return result
            except Exception as exc:
                logger.warning(
                    "router.primary_failed",
                    task=task_type.value,
                    provider=route.provider,
                    model=route.model,
                    error=str(exc),
                )
                self._record_usage(route.provider, route.model, 0, 0, 0, error=True)

        # --- fallback attempt ---
        if route.fallback_provider and route.fallback_model:
            fallback = self._get_provider(route.fallback_provider)
            if fallback is not None:
                start = time.monotonic()
                result = await fallback.chat(
                    messages,
                    model=route.fallback_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    **kwargs,
                )
                elapsed = (time.monotonic() - start) * 1000
                input_toks = fallback.count_tokens(
                    " ".join(m.get("content", "") for m in messages),
                    model=route.fallback_model,
                )
                output_toks = fallback.count_tokens(result, model=route.fallback_model)
                self._record_usage(
                    route.fallback_provider,
                    route.fallback_model,
                    input_toks,
                    output_toks,
                    elapsed,
                )
                logger.info(
                    "router.chat_fallback",
                    task=task_type.value,
                    provider=route.fallback_provider,
                    model=route.fallback_model,
                    latency_ms=round(elapsed, 1),
                )
                return result

        raise RuntimeError(
            f"No available provider for task_type={task_type.value}"
        )

    # -- public API: chat_stream ---------------------------------------------

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        task_type: TaskType = TaskType.GENERAL,
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream a chat response from the primary provider (no fallback for streams)."""
        route = self._routes.get(task_type, _DEFAULT_ROUTES[TaskType.GENERAL])
        provider = self._get_provider(route.provider)
        if provider is None:
            raise RuntimeError(f"Provider '{route.provider}' not available")

        start = time.monotonic()
        collected: list[str] = []

        async for chunk in provider.chat_stream(
            messages,
            model=route.model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            **kwargs,
        ):
            collected.append(chunk)
            yield chunk

        elapsed = (time.monotonic() - start) * 1000
        full_text = "".join(collected)
        input_toks = provider.count_tokens(
            " ".join(m.get("content", "") for m in messages), model=route.model
        )
        output_toks = provider.count_tokens(full_text, model=route.model)
        self._record_usage(
            route.provider, route.model, input_toks, output_toks, elapsed
        )

    # -- public API: embed ---------------------------------------------------

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        **kwargs: Any,
    ) -> list[list[float]]:
        """Generate embeddings using the EMBEDDING route."""
        route = self._routes[TaskType.EMBEDDING]
        provider = self._get_provider(route.provider)
        if provider is None:
            raise RuntimeError(f"Embedding provider '{route.provider}' not available")

        effective_model = model or route.model

        try:
            start = time.monotonic()
            embeddings = await provider.embed(texts, model=effective_model, **kwargs)
            elapsed = (time.monotonic() - start) * 1000
            total_tokens = sum(
                provider.count_tokens(t, model=effective_model) for t in texts
            )
            self._record_usage(
                route.provider, effective_model, total_tokens, 0, elapsed
            )
            return embeddings
        except (NotImplementedError, Exception) as exc:
            logger.warning(
                "router.embed_primary_failed",
                provider=route.provider,
                error=str(exc),
            )
            if route.fallback_provider and route.fallback_model:
                fallback = self._get_provider(route.fallback_provider)
                if fallback is not None:
                    return await fallback.embed(
                        texts, model=route.fallback_model, **kwargs
                    )
            raise

    # -- public API: rerank --------------------------------------------------

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """Rerank documents using a cross-encoder model.

        Returns a list of ``{"index": int, "score": float, "text": str}``
        sorted by descending score.
        """
        if self._reranker is None:
            import asyncio

            loop = asyncio.get_event_loop()

            def _load_reranker() -> Any:
                from sentence_transformers import CrossEncoder

                route = self._routes[TaskType.RERANKING]
                return CrossEncoder(route.model)

            self._reranker = await loop.run_in_executor(None, _load_reranker)

        import asyncio

        loop = asyncio.get_event_loop()
        pairs = [(query, doc) for doc in documents]

        scores: list[float] = await loop.run_in_executor(
            None, lambda: self._reranker.predict(pairs).tolist()
        )

        ranked = sorted(
            [
                {"index": i, "score": s, "text": documents[i]}
                for i, s in enumerate(scores)
            ],
            key=lambda x: x["score"],
            reverse=True,
        )

        if top_k:
            ranked = ranked[:top_k]

        logger.debug(
            "router.rerank",
            query_len=len(query),
            documents=len(documents),
            top_k=top_k,
        )
        return ranked

    # -- public API: usage stats ---------------------------------------------

    def get_usage_stats(self) -> dict[str, Any]:
        """Return accumulated usage statistics for all models."""
        stats: dict[str, Any] = {}
        for key, rec in self._usage.items():
            stats[key] = rec.model_dump()
        return stats

    def reset_usage_stats(self) -> None:
        """Clear all usage counters."""
        self._usage.clear()

    # -- provider access (for advanced scenarios) ----------------------------

    def get_provider(self, name: str) -> LLMProvider | None:
        """Get a raw provider by name for direct access."""
        return self._providers.get(name)
