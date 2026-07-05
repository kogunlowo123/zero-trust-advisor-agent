"""Hybrid retriever combining BM25 and dense search with RRF."""
import structlog
from app.contracts.models import RetrievalRequest, RetrievalResult, Chunk
from app.search.ports import VectorStorePort, EmbeddingPort

logger = structlog.get_logger(__name__)


class HybridRetriever:
    def __init__(self, store: VectorStorePort, embedder: EmbeddingPort, bm25_weight: float = 0.3, dense_weight: float = 0.7):
        self._store = store
        self._embedder = embedder
        self._bm25_weight = bm25_weight
        self._dense_weight = dense_weight

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        logger.info("hybrid_retrieve", query=request.query[:60])
        embedding = await self._embedder.embed([request.query])
        bm25_results = await self._store.keyword_search(request.query, top_k=request.top_k * 2, filters=request.filters)
        dense_results = await self._store.vector_search(embedding[0], top_k=request.top_k * 2, filters=request.filters)
        merged = self._reciprocal_rank_fusion(bm25_results, dense_results, k=60)
        return RetrievalResult(chunks=merged[:request.top_k], total_found=len(merged))

    def _reciprocal_rank_fusion(self, *result_lists: list[Chunk], k: int = 60) -> list[Chunk]:
        scores: dict[str, float] = {}
        chunks: dict[str, Chunk] = {}
        for results in result_lists:
            for rank, chunk in enumerate(results):
                scores[chunk.id] = scores.get(chunk.id, 0) + 1.0 / (k + rank + 1)
                chunks[chunk.id] = chunk
        for cid in chunks:
            chunks[cid].score = scores[cid]
        return sorted(chunks.values(), key=lambda c: c.score, reverse=True)
