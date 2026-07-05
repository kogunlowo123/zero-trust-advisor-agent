"""Dense vector retriever using embedding similarity."""
import structlog
from app.contracts.models import RetrievalRequest, RetrievalResult
from app.search.ports import VectorStorePort, EmbeddingPort

logger = structlog.get_logger(__name__)


class DenseRetriever:
    def __init__(self, store: VectorStorePort, embedder: EmbeddingPort):
        self._store = store
        self._embedder = embedder

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        logger.info("dense_retrieve", query=request.query[:60], top_k=request.top_k)
        embedding = await self._embedder.embed([request.query])
        results = await self._store.vector_search(embedding[0], top_k=request.top_k, filters=request.filters)
        return RetrievalResult(chunks=results, total_found=len(results))
