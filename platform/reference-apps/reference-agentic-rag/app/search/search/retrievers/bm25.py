"""BM25 keyword retriever using OpenSearch/Elasticsearch."""
import structlog
from app.contracts.models import RetrievalRequest, RetrievalResult
from app.search.ports import VectorStorePort

logger = structlog.get_logger(__name__)


class BM25Retriever:
    def __init__(self, store: VectorStorePort):
        self._store = store

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        logger.info("bm25_retrieve", query=request.query[:60], top_k=request.top_k)
        results = await self._store.keyword_search(request.query, top_k=request.top_k, filters=request.filters)
        return RetrievalResult(chunks=results, total_found=len(results))
