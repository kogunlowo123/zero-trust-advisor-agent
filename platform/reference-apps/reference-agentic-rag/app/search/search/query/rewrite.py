"""Query rewriter — improves query clarity for retrieval."""
import structlog
from app.search.ports import LLMPort

logger = structlog.get_logger(__name__)


class QueryRewriter:
    def __init__(self, llm: LLMPort | None = None):
        self._llm = llm

    async def rewrite(self, query: str) -> str:
        if self._llm is None:
            return query
        logger.info("query_rewrite", original=query[:60])
        return query
