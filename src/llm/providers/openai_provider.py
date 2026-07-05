"""OpenAI / Azure OpenAI provider.

Supports
--------
* Chat completion (streaming and non-streaming)
* Embedding generation (text-embedding-3-large, ada-002)
* Tool / function calling
* Token counting via tiktoken
* Automatic retries with tenacity
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

import structlog
import tiktoken
from openai import AsyncAzureOpenAI, AsyncOpenAI, APIError, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.llm.providers.base import LLMProvider, ModelInfo

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Cost table (per 1 000 tokens, USD, as of 2025-Q2)
# ---------------------------------------------------------------------------
_COST_MAP: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.0025, 0.010),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.01, 0.03),
    "gpt-4": (0.03, 0.06),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "text-embedding-3-large": (0.00013, 0.0),
    "text-embedding-3-small": (0.00002, 0.0),
    "text-embedding-ada-002": (0.0001, 0.0),
}


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------
class OpenAIProvider:
    """OpenAI and Azure OpenAI provider."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o",
        *,
        azure_endpoint: str | None = None,
        azure_api_version: str = "2024-10-21",
        azure_deployment: str | None = None,
        embedding_model: str = "text-embedding-3-large",
    ) -> None:
        self._model = model
        self._embedding_model = embedding_model
        self._is_azure = azure_endpoint is not None

        if self._is_azure:
            self._client: AsyncOpenAI = AsyncAzureOpenAI(
                api_key=api_key or os.environ.get("AZURE_OPENAI_API_KEY", ""),
                azure_endpoint=azure_endpoint,  # type: ignore[arg-type]
                api_version=azure_api_version,
                azure_deployment=azure_deployment,
            )
        else:
            self._client = AsyncOpenAI(
                api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            )

        # tiktoken encoder — lazily loaded
        self._encoder: tiktoken.Encoding | None = None

    # -- chat ----------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitError, APIError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
        reraise=True,
    )
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        effective_model = model or self._model
        params: dict[str, Any] = {
            "model": effective_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            params["tools"] = tools
        if tool_choice:
            params["tool_choice"] = tool_choice
        if stop:
            params["stop"] = stop

        response = await self._client.chat.completions.create(**params)
        content = response.choices[0].message.content or ""

        logger.debug(
            "openai.chat",
            model=effective_model,
            input_tokens=getattr(response.usage, "prompt_tokens", 0),
            output_tokens=getattr(response.usage, "completion_tokens", 0),
        )
        return content

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        effective_model = model or self._model
        params: dict[str, Any] = {
            "model": effective_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            params["tools"] = tools

        stream = await self._client.chat.completions.create(**params)
        async for chunk in stream:  # type: ignore[union-attr]
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    # -- embeddings ----------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((RateLimitError, APIError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=30),
        reraise=True,
    )
    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        **kwargs: Any,
    ) -> list[list[float]]:
        effective_model = model or self._embedding_model
        response = await self._client.embeddings.create(
            model=effective_model,
            input=texts,
        )
        logger.debug(
            "openai.embed",
            model=effective_model,
            count=len(texts),
            tokens=getattr(response.usage, "total_tokens", 0),
        )
        return [item.embedding for item in response.data]

    # -- utility -------------------------------------------------------------

    def count_tokens(self, text: str, *, model: str | None = None) -> int:
        effective_model = model or self._model
        if self._encoder is None:
            try:
                self._encoder = tiktoken.encoding_for_model(effective_model)
            except KeyError:
                self._encoder = tiktoken.get_encoding("cl100k_base")
        return len(self._encoder.encode(text))

    def get_model_info(self, model: str | None = None) -> ModelInfo:
        effective_model = model or self._model
        cost = _COST_MAP.get(effective_model, (0.0, 0.0))
        return ModelInfo(
            provider="azure_openai" if self._is_azure else "openai",
            model_id=effective_model,
            max_context_tokens=128_000,
            supports_tools=True,
            supports_streaming=True,
            supports_embeddings="embedding" in effective_model,
            cost_per_1k_input=cost[0],
            cost_per_1k_output=cost[1],
        )
