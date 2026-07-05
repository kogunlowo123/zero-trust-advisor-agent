"""Base LLM provider protocol.

Every provider must satisfy this interface.  The router dispatches to
providers via these methods and never touches vendor SDKs directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class ModelInfo:
    """Metadata about a model exposed by a provider."""

    provider: str
    model_id: str
    max_context_tokens: int = 128_000
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_embeddings: bool = False
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol that all LLM providers must implement."""

    # -- chat ----------------------------------------------------------------

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
        """Synchronous (non-streaming) chat completion.

        Returns the assistant's text reply.
        """
        ...

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
        """Streaming chat completion.

        Yields text deltas as they arrive.
        """
        ...

    # -- embeddings ----------------------------------------------------------

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        **kwargs: Any,
    ) -> list[list[float]]:
        """Generate embedding vectors for the given texts."""
        ...

    # -- utility -------------------------------------------------------------

    def count_tokens(self, text: str, *, model: str | None = None) -> int:
        """Estimate the token count of *text* for the given model."""
        ...

    def get_model_info(self, model: str | None = None) -> ModelInfo:
        """Return metadata for the active or specified model."""
        ...
