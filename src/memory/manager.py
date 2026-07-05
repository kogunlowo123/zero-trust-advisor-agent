"""Memory Manager — unified facade over all five memory stores.

The agent runtime calls the manager, never the individual stores directly.
The manager decides *which* store(s) to query based on the request, and
cross-references results when appropriate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

from src.memory.decisions import (
    ContainmentAction,
    Decision,
    DecisionFilter,
    DecisionLog,
)
from src.memory.knowledge_graph import Entity, KnowledgeGraph
from src.memory.lexical import LexicalMemory
from src.memory.long_term import Document, LongTermMemory, SearchResult
from src.memory.short_term import ShortTermMemory

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class MemoryType(str, Enum):
    """Selectable memory backends."""

    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    LEXICAL = "lexical"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    DECISIONS = "decisions"


# ---------------------------------------------------------------------------
# Recall result
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecallResult:
    """Aggregated recall response from one or more memory stores."""

    short_term: list[dict[str, Any]] = field(default_factory=list)
    long_term: list[SearchResult] = field(default_factory=list)
    lexical: list[dict[str, Any]] = field(default_factory=list)
    graph_entities: list[Entity] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Memory Manager
# ---------------------------------------------------------------------------
class MemoryManager:
    """Orchestrates all five memory stores behind a single interface."""

    def __init__(
        self,
        short_term: ShortTermMemory,
        long_term: LongTermMemory,
        lexical: LexicalMemory,
        knowledge_graph: KnowledgeGraph,
        decision_log: DecisionLog,
    ) -> None:
        self._stm = short_term
        self._ltm = long_term
        self._lex = lexical
        self._kg = knowledge_graph
        self._dec = decision_log

    # -- lifecycle -----------------------------------------------------------

    async def connect_all(self) -> None:
        """Connect every backing store in parallel."""
        await asyncio.gather(
            self._stm.connect(),
            self._ltm.connect(),
            self._lex.connect(),
            self._kg.connect(),
            self._dec.connect(),
        )
        logger.info("memory_manager.all_connected")

    async def close_all(self) -> None:
        """Gracefully shut down every backing store."""
        await asyncio.gather(
            self._stm.close(),
            self._ltm.close(),
            self._lex.close(),
            self._kg.close(),
            self._dec.close(),
        )
        logger.info("memory_manager.all_closed")

    # -- write ---------------------------------------------------------------

    async def remember(
        self,
        session_id: str,
        key: str,
        value: Any,
    ) -> None:
        """Store a value in session scratch-pad (short-term)."""
        await self._stm.set_scratch(session_id, key, value)

    async def store_document(self, doc: Document) -> str:
        """Persist a document in both vector and lexical stores."""
        doc_id = await self._ltm.store_document(doc)
        await self._lex.index_document(doc_id, doc.text, doc.metadata)
        logger.info("memory_manager.document_stored", doc_id=doc_id)
        return doc_id

    async def record_decision(self, decision: Decision) -> str:
        """Record a triage decision."""
        return await self._dec.record_decision(decision)

    async def record_action(self, action: ContainmentAction) -> str:
        """Record a containment action."""
        return await self._dec.record_action(action)

    async def add_entity(
        self,
        entity_type: str,
        entity_id: str,
        properties: dict[str, Any] | None = None,
    ) -> Entity:
        """Add an entity to the knowledge graph."""
        return await self._kg.add_entity(entity_type, entity_id, properties)

    async def add_relationship(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Add a relationship to the knowledge graph."""
        await self._kg.add_relationship(from_id, to_id, rel_type, properties)

    # -- read (unified recall) -----------------------------------------------

    async def recall(
        self,
        session_id: str,
        query: str,
        memory_types: list[MemoryType] | None = None,
        query_embedding: list[float] | None = None,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> RecallResult:
        """Query one or more memory stores and return aggregated results.

        Parameters
        ----------
        session_id : str
            Active session for short-term context.
        query : str
            Natural-language or keyword query string.
        memory_types : list[MemoryType] | None
            Which stores to query.  ``None`` means all.
        query_embedding : list[float] | None
            Pre-computed embedding for vector search.
        top_k : int
            Max results per store.
        filters : dict | None
            Metadata filters forwarded to vector / decision stores.
        """
        types = set(memory_types or MemoryType)
        tasks: dict[str, Any] = {}

        if MemoryType.SHORT_TERM in types:
            tasks["short_term"] = self._stm.get_context(session_id)

        if MemoryType.LONG_TERM in types:
            tasks["long_term"] = self._ltm.search_similar(
                query_embedding=query_embedding,
                query_text=query,
                top_k=top_k,
                filters=filters,
            )

        if MemoryType.LEXICAL in types:
            tasks["lexical"] = self._lex.search(query, top_k=top_k)

        if MemoryType.KNOWLEDGE_GRAPH in types:
            # Try to find a matching entity and return neighbours
            tasks["graph"] = self._kg.get_neighbors(query, depth=2)

        if MemoryType.DECISIONS in types:
            dec_filter = DecisionFilter(
                alert_id=query if filters is None else filters.get("alert_id"),
                severity=filters.get("severity") if filters else None,
                mitre_technique=filters.get("mitre_technique") if filters else None,
                limit=top_k,
            )
            tasks["decisions"] = self._dec.get_decisions(dec_filter)

        # Run all selected backends in parallel
        keys = list(tasks.keys())
        results = await asyncio.gather(
            *tasks.values(),
            return_exceptions=True,
        )

        resolved: dict[str, Any] = {}
        for key, result in zip(keys, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "memory_manager.recall_partial_failure",
                    store=key,
                    error=str(result),
                )
                resolved[key] = []
            else:
                resolved[key] = result

        return RecallResult(
            short_term=resolved.get("short_term", []),
            long_term=resolved.get("long_term", []),
            lexical=[
                {"doc_id": h.doc_id, "snippet": h.snippet, "rank": h.rank}
                for h in resolved.get("lexical", [])
            ]
            if resolved.get("lexical")
            else [],
            graph_entities=resolved.get("graph", []),
            decisions=resolved.get("decisions", []),
        )

    # -- delete --------------------------------------------------------------

    async def forget(self, session_id: str, key: str) -> None:
        """Remove a scratch-pad key from short-term memory."""
        redis = self._stm._redis  # noqa: SLF001 — intentional internal access
        await redis.hdel(self._stm._scratch_key(session_id), key)  # noqa: SLF001

    # -- investigation helper ------------------------------------------------

    async def get_investigation_context(
        self,
        alert_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Build a rich context bundle for an alert investigation.

        Pulls data from every relevant store: prior decisions, related
        entities in the knowledge graph, similar past incidents, and
        optionally the current session context.
        """
        tasks: dict[str, Any] = {
            "decisions": self._dec.get_decisions(
                DecisionFilter(alert_id=alert_id, limit=20)
            ),
            "actions": self._dec.get_actions(
                DecisionFilter(alert_id=alert_id, limit=20)
            ),
            "similar_incidents": self._ltm.search_similar(
                query_text=alert_id,
                top_k=5,
            ),
            "lexical_hits": self._lex.search(alert_id, top_k=5),
        }
        if session_id:
            tasks["session_context"] = self._stm.get_context(session_id)

        keys = list(tasks.keys())
        results = await asyncio.gather(
            *tasks.values(),
            return_exceptions=True,
        )

        resolved: dict[str, Any] = {}
        for key, result in zip(keys, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "memory_manager.investigation_context_error",
                    store=key,
                    error=str(result),
                )
                resolved[key] = []
            else:
                resolved[key] = result

        return {
            "alert_id": alert_id,
            "prior_decisions": [
                d.model_dump() for d in resolved.get("decisions", [])
            ],
            "prior_actions": [
                a.model_dump() for a in resolved.get("actions", [])
            ],
            "similar_incidents": [
                s.model_dump() for s in resolved.get("similar_incidents", [])
            ],
            "lexical_hits": [
                {"doc_id": h.doc_id, "snippet": h.snippet, "rank": h.rank}
                for h in resolved.get("lexical_hits", [])
            ],
            "session_context": resolved.get("session_context", []),
        }
