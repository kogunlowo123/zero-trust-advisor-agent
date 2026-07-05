"""Google Gemini / Vertex AI provider.

Supports
--------
* Chat completion (non-streaming and streaming)
* Embedding generation
* Vertex AI endpoint
* Automatic retries with tenacity
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

import structlog
import tiktoken
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
    "gemini-2.0-flash": (0.0001, 0.0004),
    "gemini-2.0-flash-lite": (0.0, 0.0),
    "gemini-1.5-pro": (0.00125, 0.005),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "text-embedding-004": (0.00001, 0.0),
}


def _convert_messages_for_google(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert OpenAI-style messages to Gemini content format."""
    system_instruction: str | None = None
    contents: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        text = msg.get("content", "")

        if role == "system":
            system_instruction = text
            continue

        # Gemini uses "user" and "model" (not "assistant")
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({
            "role": gemini_role,
            "parts": [{"text": text}],
        })

    return system_instruction, contents


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------
class GoogleProvider:
    """Google Gemini provider (direct and Vertex AI)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.0-flash",
        *,
        project_id: str | None = None,
        location: str = "us-central1",
        embedding_model: str = "text-embedding-004",
    ) -> None:
        self._model = model
        self._embedding_model = embedding_model
        self._project_id = project_id
        self._location = location
        self._is_vertex = project_id is not None

        self._api_key = api_key or os.environ.get("GOOGLE_API_KEY", "")

        # Lazy import to avoid hard dep when not using Google
        self._genai_module: Any = None
        self._client: Any = None

        # Token counter (approximation)
        self._encoder: tiktoken.Encoding | None = None

    def _ensure_client(self) -> Any:
        """Lazy-initialise the google.generativeai client."""
        if self._client is not None:
            return self._client

        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise ImportError(
                "google-generativeai is required for GoogleProvider. "
                "Install with: pip install google-generativeai"
            ) from exc

        self._genai_module = genai

        if self._is_vertex:
            genai.configure(
                api_key=self._api_key,
                transport="rest",
            )
        else:
            genai.configure(api_key=self._api_key)

        self._client = genai.GenerativeModel(self._model)
        return self._client

    # -- chat ----------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(Exception),
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
        self._ensure_client()
        genai = self._genai_module
        effective_model = model or self._model

        system_instruction, contents = _convert_messages_for_google(messages)

        # Use a fresh model if different from default
        gen_model = (
            genai.GenerativeModel(
                effective_model,
                system_instruction=system_instruction,
            )
            if effective_model != self._model or system_instruction
            else self._client
        )

        generation_config = genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            stop_sequences=stop or [],
        )

        response = await gen_model.generate_content_async(
            contents,
            generation_config=generation_config,
        )

        text_result = response.text if hasattr(response, "text") else ""

        logger.debug(
            "google.chat",
            model=effective_model,
        )
        return text_result

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
        self._ensure_client()
        genai = self._genai_module
        effective_model = model or self._model

        system_instruction, contents = _convert_messages_for_google(messages)

        gen_model = (
            genai.GenerativeModel(
                effective_model,
                system_instruction=system_instruction,
            )
            if effective_model != self._model or system_instruction
            else self._client
        )

        generation_config = genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )

        response = await gen_model.generate_content_async(
            contents,
            generation_config=generation_config,
            stream=True,
        )

        async for chunk in response:
            if hasattr(chunk, "text") and chunk.text:
                yield chunk.text

    # -- embeddings ----------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(Exception),
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
        self._ensure_client()
        genai = self._genai_module
        effective_model = model or self._embedding_model

        # google.generativeai embed_content is synchronous;
        # wrap in asyncio if needed
        import asyncio

        loop = asyncio.get_event_loop()

        def _embed() -> list[list[float]]:
            results: list[list[float]] = []
            for t in texts:
                resp = genai.embed_content(
                    model=f"models/{effective_model}",
                    content=t,
                    task_type="retrieval_document",
                )
                results.append(resp["embedding"])
            return results

        embeddings = await loop.run_in_executor(None, _embed)

        logger.debug(
            "google.embed",
            model=effective_model,
            count=len(texts),
        )
        return embeddings

    # -- utility -------------------------------------------------------------

    def count_tokens(self, text: str, *, model: str | None = None) -> int:
        if self._encoder is None:
            self._encoder = tiktoken.get_encoding("cl100k_base")
        return len(self._encoder.encode(text))

    def get_model_info(self, model: str | None = None) -> ModelInfo:
        effective_model = model or self._model
        cost = _COST_MAP.get(effective_model, (0.0, 0.0))
        return ModelInfo(
            provider="vertex_ai" if self._is_vertex else "google",
            model_id=effective_model,
            max_context_tokens=1_000_000 if "1.5" in effective_model else 128_000,
            supports_tools=True,
            supports_streaming=True,
            supports_embeddings="embedding" in effective_model,
            cost_per_1k_input=cost[0],
            cost_per_1k_output=cost[1],
        )
