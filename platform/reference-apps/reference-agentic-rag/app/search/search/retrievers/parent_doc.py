"""Parent document retriever — retrieves full parent from child chunk match."""
import structlog
from app.contracts.models import RetrievalRequest, RetrievalResult
from app.search.ports import VectorStorePort, EmbeddingPort

logger = structlog.get_logger(__name__)


class ParentDocRetriever:
    def __init__(self, store: VectorStorePort, embedder: EmbeddingPort):
        self._store = store
        self._embedder = embedder

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        logger.info("parent_doc_retrieve", query=request.query[:60])
        embedding = await self._embedder.embed([request.query])
        child_chunks = await self._store.vector_search(embedding[0], top_k=request.top_k, filters=request.filters)
        parent_ids = list({c.metadata.get("parent_id", c.document_id) for c in child_chunks})
        parents = await self._store.get_by_ids(parent_ids)
        return RetrievalResult(chunks=parents, total_found=len(parents))
