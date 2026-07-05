"""Anthropic Claude provider (direct API and Amazon Bedrock).

Supports
--------
* Chat completion (streaming and non-streaming)
* Tool use via Claude's native tool format
* Token counting (approximation via tiktoken cl100k_base)
* Automatic retries with tenacity
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

import structlog
import tiktoken
from anthropic import (
    AsyncAnthropic,
    APIError,
    RateLimitError,
)
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
    "claude-sonnet-4-20250514": (0.003, 0.015),
    "claude-3-5-sonnet-20241022": (0.003, 0.015),
    "claude-3-5-haiku-20241022": (0.0008, 0.004),
    "claude-3-opus-20240229": (0.015, 0.075),
    "claude-3-haiku-20240307": (0.00025, 0.00125),
}


def _convert_messages_for_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split an OpenAI-style message list into (system_prompt, messages).

    Anthropic requires the system prompt as a separate parameter, not as
    a message with role ``system``.
    """
    system_prompt: str | None = None
    converted: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            system_prompt = msg.get("content", "")
        else:
            converted.append({"role": msg["role"], "content": msg["content"]})
    return system_prompt, converted


def _convert_tools_for_anthropic(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Convert OpenAI-style tool definitions to Anthropic format."""
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "function":
            fn = tool["function"]
            converted.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        else:
            # Already in Anthropic format
            converted.append(tool)
    return converted


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------
class AnthropicProvider:
    """Anthropic Claude provider (direct and Bedrock)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-20250514",
        *,
        bedrock_region: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._is_bedrock = bedrock_region is not None

        if self._is_bedrock:
            from anthropic import AsyncAnthropicBedrock

            self._client: AsyncAnthropic = AsyncAnthropicBedrock(  # type: ignore[assignment]
                aws_region=bedrock_region,
            )
        else:
            self._client = AsyncAnthropic(
                api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
            )

        # token counter (approximation)
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
        system_prompt, converted_msgs = _convert_messages_for_anthropic(messages)
        anthropic_tools = _convert_tools_for_anthropic(tools)

        params: dict[str, Any] = {
            "model": effective_model,
            "messages": converted_msgs,
            "temperature": temperature,
            "max_tokens": max_tokens or self._max_tokens,
        }
        if system_prompt:
            params["system"] = system_prompt
        if anthropic_tools:
            params["tools"] = anthropic_tools
        if tool_choice:
            params["tool_choice"] = {"type": tool_choice}
        if stop:
            params["stop_sequences"] = stop

        response = await self._client.messages.create(**params)

        # Extract text from content blocks
        text_parts: list[str] = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)

        logger.debug(
            "anthropic.chat",
            model=effective_model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return "".join(text_parts)

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
        system_prompt, converted_msgs = _convert_messages_for_anthropic(messages)
        anthropic_tools = _convert_tools_for_anthropic(tools)

        params: dict[str, Any] = {
            "model": effective_model,
            "messages": converted_msgs,
            "temperature": temperature,
            "max_tokens": max_tokens or self._max_tokens,
        }
        if system_prompt:
            params["system"] = system_prompt
        if anthropic_tools:
            params["tools"] = anthropic_tools

        async with self._client.messages.stream(**params) as stream:
            async for text_chunk in stream.text_stream:
                yield text_chunk

    # -- embeddings ----------------------------------------------------------

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        **kwargs: Any,
    ) -> list[list[float]]:
        """Anthropic does not natively provide embeddings.

        Raises NotImplementedError — the router should delegate to another
        provider for embedding tasks.
        """
        raise NotImplementedError(
            "Anthropic Claude does not provide an embedding API. "
            "Use OpenAI or Google for embeddings."
        )

    # -- utility -------------------------------------------------------------

    def count_tokens(self, text: str, *, model: str | None = None) -> int:
        """Approximate token count using cl100k_base encoding.

        Claude uses its own tokenizer, but cl100k_base provides a reasonable
        approximation for planning purposes.
        """
        if self._encoder is None:
            self._encoder = tiktoken.get_encoding("cl100k_base")
        return len(self._encoder.encode(text))

    def get_model_info(self, model: str | None = None) -> ModelInfo:
        effective_model = model or self._model
        cost = _COST_MAP.get(effective_model, (0.0, 0.0))
        return ModelInfo(
            provider="bedrock" if self._is_bedrock else "anthropic",
            model_id=effective_model,
            max_context_tokens=200_000,
            supports_tools=True,
            supports_streaming=True,
            supports_embeddings=False,
            cost_per_1k_input=cost[0],
            cost_per_1k_output=cost[1],
        )
