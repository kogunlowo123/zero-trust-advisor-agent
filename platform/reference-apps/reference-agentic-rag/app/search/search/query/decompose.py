"""Query decomposer — splits complex queries into sub-queries."""
import structlog
from app.search.ports import LLMPort

logger = structlog.get_logger(__name__)


class QueryDecomposer:
    def __init__(self, llm: LLMPort | None = None):
        self._llm = llm

    async def decompose(self, query: str) -> list[str]:
        logger.info("query_decompose", query=query[:60])
        return [query]
