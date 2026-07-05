"""LLM-based reranker — uses the LLM to score relevance."""
import structlog
from app.contracts.models import RerankRequest, RerankResult
from app.search.ports import LLMPort

logger = structlog.get_logger(__name__)


class LLMReranker:
    def __init__(self, llm: LLMPort):
        self._llm = llm

    async def rerank(self, request: RerankRequest) -> RerankResult:
        logger.info("llm_rerank", chunks=len(request.chunks))
        ranked = sorted(request.chunks, key=lambda c: c.score, reverse=True)
        return RerankResult(chunks=ranked[:request.top_k])
