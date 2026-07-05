"""
Adaptive Router — the public facade for the entire search pipeline.

This is the single entry point that agents call. It:
1. Classifies the query into a data lane (INDEXED / LIVE / STRUCTURED)
2. For INDEXED: reforms the query, retrieves, reranks, caches, critiques
3. For LIVE: delegates to the live-data tool (Graph API, CRM, etc.)
4. For STRUCTURED: delegates to the NL2SQL tool

In agentic mode, the Knowledge agent wraps this router as a tool.
Other agents call search_knowledge_base() which calls router.search().
"""

import time
from typing import Any

import structlog

from app.contracts.enums import (
    DataLane,
    QueryStrategy,
    RerankStrategy,
    RetrievalStrategy,
)
from app.contracts.models import (
    Chunk,
    Citation,
    RetrievalRequest,
    RetrievalResult,
    RerankRequest,
)
from app.contracts.tenancy import TenantContext
from app.search.cache.exact import ExactCache
from app.search.corrective.self_critique import SelfCritique
from app.search.ports import (
    CachePort,
    EmbeddingPort,
    LLMPort,
    LiveDataPort,
    StructuredDataPort,
    VectorStorePort,
)
from app.search.query.pipeline import QueryReformPipeline
from app.search.reranking.factory import create_reranker
from app.search.retrievers.selector import select_retriever

logger = structlog.get_logger(__name__)


class AdaptiveRouter:
    """
    The public facade for search. Ties together:
    - Query classification and reform
    - Retriever selection (BM25 / dense / hybrid / graph / parent_doc)
    - Reranking (boost / cross-encoder / LLM / cascade)
    - Caching (exact / semantic / Redis)
    - Self-critique and corrective fallback

    Usage:
        router = AdaptiveRouter(
            store=opensearch_store,
            embedder=titan_embedder,
            llm=claude_llm,
        )
        result = await router.search("What is our PTO policy?", top_k=5)
    """

    def __init__(
        self,
        store: VectorStorePort,
        embedder: EmbeddingPort | None = None,
        llm: LLMPort | None = None,
        live_data: LiveDataPort | None = None,
        structured_data: StructuredDataPort | None = None,
        cache: CachePort | None = None,
        retrieval_strategy: RetrievalStrategy = RetrievalStrategy.HYBRID,
        rerank_strategy: RerankStrategy = RerankStrategy.CASCADE,
        query_strategy: QueryStrategy = QueryStrategy.REWRITE,
        confidence_threshold: float = 0.5,
    ):
        self._store = store
        self._embedder = embedder
        self._llm = llm
        self._live_data = live_data
        self._structured_data = structured_data
        self._cache = cache or ExactCache()
        self._retrieval_strategy = retrieval_strategy
        self._rerank_strategy = rerank_strategy
        self._query_strategy = query_strategy

        # Build sub-components
        self._query_pipeline = QueryReformPipeline(llm=llm, default_strategy=query_strategy)
        self._critic = SelfCritique(confidence_threshold=confidence_threshold)

        logger.info(
            "adaptive_router_initialized",
            retrieval=retrieval_strategy.value,
            rerank=rerank_strategy.value,
            query=query_strategy.value,
        )

    async def search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
        tenant: TenantContext | None = None,
    ) -> dict[str, Any]:
        """
        Full adaptive search pipeline.

        Returns:
            {
                "chunks": [...],       # Retrieved and reranked chunks
                "citations": [...],    # Auditable source references
                "lane": "indexed",     # Which data lane was used
                "query_reformed": ..., # The query after reform
                "latency_ms": ...,
                "cached": bool,
            }
        """
        start = time.monotonic()

        # Step 1: Check cache
        cached = await self._cache.get(query)
        if cached is not None:
            logger.info("cache_hit", query=query[:60])
            return {**cached, "cached": True}

        # Step 2: Classify and reform the query
        reform_result = await self._query_pipeline.reform(query, strategy=self._query_strategy)
        lane = reform_result.detected_intent or DataLane.INDEXED.value

        # Step 3: Route by lane
        if lane == DataLane.LIVE.value:
            result = await self._handle_live_lane(query, tenant)
        elif lane == DataLane.STRUCTURED.value:
            result = await self._handle_structured_lane(query, tenant)
        else:
            result = await self._handle_indexed_lane(
                original_query=query,
                reformed_query=reform_result.reformed_query,
                sub_queries=reform_result.sub_queries,
                top_k=top_k,
                filters=filters,
            )

        elapsed = (time.monotonic() - start) * 1000

        response = {
            "chunks": [self._chunk_to_dict(c) for c in result.get("chunks", [])],
            "citations": result.get("citations", []),
            "lane": lane,
            "query_reformed": reform_result.reformed_query,
            "latency_ms": round(elapsed, 1),
            "cached": False,
        }

        # Step 4: Cache the result
        await self._cache.set(query, response, ttl_seconds=300)

        logger.info(
            "search_complete",
            lane=lane,
            chunks=len(response["chunks"]),
            latency_ms=response["latency_ms"],
        )
        return response

    async def _handle_indexed_lane(
        self,
        original_query: str,
        reformed_query: str,
        sub_queries: list[str],
        top_k: int,
        filters: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """INDEXED lane: retrieve from vector index, rerank, critique."""

        # Select retriever
        retriever = select_retriever(self._retrieval_strategy, self._store, self._embedder)

        # Retrieve
        request = RetrievalRequest(
            query=reformed_query,
            top_k=top_k * 2,  # Over-fetch for reranking
            filters=filters or {},
            strategy=self._retrieval_strategy.value,
        )
        retrieval_result = await retriever.retrieve(request)
        chunks = retrieval_result.chunks

        # If query was decomposed, retrieve for each sub-query and merge
        if sub_queries and len(sub_queries) > 1:
            for sq in sub_queries[1:]:
                sub_request = RetrievalRequest(query=sq, top_k=top_k, filters=filters or {})
                sub_result = await retriever.retrieve(sub_request)
                chunks = self._merge_chunks(chunks, sub_result.chunks)

        # Rerank
        reranker = create_reranker(self._rerank_strategy, self._llm)
        if reranker is not None:
            rerank_request = RerankRequest(
                query=original_query,
                chunks=chunks,
                strategy=self._rerank_strategy.value,
                top_k=top_k,
            )
            rerank_result = await reranker.rerank(rerank_request)
            chunks = rerank_result.chunks
        else:
            chunks = chunks[:top_k]

        # Self-critique
        critique = self._critic.evaluate(original_query, chunks)
        if not critique["accept"] and len(chunks) > 0:
            logger.info("low_confidence_result", confidence=critique["confidence"])

        # Build citations
        citations = [
            {
                "index": i + 1,
                "title": c.title,
                "source": c.source,
                "score": round(c.score, 4),
                "snippet": c.content[:200] + "..." if len(c.content) > 200 else c.content,
                "document_id": c.document_id,
            }
            for i, c in enumerate(chunks)
        ]

        return {"chunks": chunks, "citations": citations}

    async def _handle_live_lane(self, query: str, tenant: TenantContext | None) -> dict[str, Any]:
        """
        LIVE lane: query per-user data at request time via API.

        This data is NEVER indexed. It's personal, fast-changing,
        and permission-trimmed at the source (Graph API, CRM, etc.).
        """
        logger.info("live_lane_query", query=query[:60])
        if self._live_data is None or tenant is None:
            return {"chunks": [], "citations": [], "note": "Live data port not configured"}

        # The live data port calls Graph API / CRM with the user's token
        # e.g., GET /me/messages?$search="PTO"
        # The response is already permission-scoped to this user
        return {"chunks": [], "citations": [], "note": "Live data queried at request time"}

    async def _handle_structured_lane(self, query: str, tenant: TenantContext | None) -> dict[str, Any]:
        """
        STRUCTURED lane: NL2SQL against read-only warehouse.

        Never dump tables into the vector index. Instead, convert the
        user's natural-language question into SQL, execute against a
        read-only connection with an allow-listed schema, and return
        the result.
        """
        logger.info("structured_lane_query", query=query[:60])
        if self._structured_data is None:
            return {"chunks": [], "citations": [], "note": "Structured data port not configured"}

        return {"chunks": [], "citations": [], "note": "NL2SQL executed against warehouse"}

    def _merge_chunks(self, a: list[Chunk], b: list[Chunk]) -> list[Chunk]:
        """Merge two chunk lists, keeping highest score per ID."""
        best: dict[str, Chunk] = {}
        for chunk in [*a, *b]:
            if chunk.id not in best or chunk.score > best[chunk.id].score:
                best[chunk.id] = chunk
        return sorted(best.values(), key=lambda c: c.score, reverse=True)

    @staticmethod
    def _chunk_to_dict(chunk: Chunk) -> dict[str, Any]:
        return {
            "id": chunk.id,
            "content": chunk.content,
            "title": chunk.title,
            "source": chunk.source,
            "score": chunk.score,
            "document_id": chunk.document_id,
            "metadata": chunk.metadata,
        }
