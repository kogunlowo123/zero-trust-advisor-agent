"""Graph retriever for entity-relationship traversal."""
import structlog
from app.contracts.models import RetrievalRequest, RetrievalResult

logger = structlog.get_logger(__name__)


class GraphRetriever:
    def __init__(self, graph_store):
        self._graph = graph_store

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        logger.info("graph_retrieve", query=request.query[:60])
        return RetrievalResult(chunks=[], total_found=0)
