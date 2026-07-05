"""Shared data models for the search pipeline."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    id: str
    content: str
    title: str = ""
    source: str = ""
    score: float = 0.0
    document_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Citation:
    index: int
    title: str
    source: str
    score: float
    snippet: str


@dataclass
class RetrievalRequest:
    query: str
    top_k: int = 10
    filters: dict[str, Any] = field(default_factory=dict)
    strategy: str = "hybrid"


@dataclass
class RetrievalResult:
    chunks: list[Chunk] = field(default_factory=list)
    total_found: int = 0
    latency_ms: float = 0.0


@dataclass
class RerankRequest:
    query: str
    chunks: list[Chunk] = field(default_factory=list)
    strategy: str = "cascade"
    top_k: int = 5


@dataclass
class RerankResult:
    chunks: list[Chunk] = field(default_factory=list)
    latency_ms: float = 0.0


@dataclass
class QueryReformResult:
    reformed_query: str
    sub_queries: list[str] = field(default_factory=list)
    detected_intent: str | None = None
