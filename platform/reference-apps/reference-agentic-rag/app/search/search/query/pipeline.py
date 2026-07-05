"""Query reform pipeline — orchestrates classify, rewrite, expand, hyde, decompose."""
import structlog
from app.contracts.enums import QueryStrategy
from app.contracts.models import QueryReformResult
from app.search.ports import LLMPort
from app.search.query.classify import classify_query
from app.search.query.rewrite import QueryRewriter
from app.search.query.decompose import QueryDecomposer

logger = structlog.get_logger(__name__)


class QueryReformPipeline:
    def __init__(self, llm: LLMPort | None = None, default_strategy: QueryStrategy = QueryStrategy.REWRITE):
        self._rewriter = QueryRewriter(llm)
        self._decomposer = QueryDecomposer(llm)
        self._default = default_strategy

    async def reform(self, query: str, strategy: QueryStrategy | None = None) -> QueryReformResult:
        s = strategy or self._default
        lane = classify_query(query)
        if s == QueryStrategy.REWRITE:
            reformed = await self._rewriter.rewrite(query)
            return QueryReformResult(reformed_query=reformed, detected_intent=lane.value)
        elif s == QueryStrategy.DECOMPOSE:
            subs = await self._decomposer.decompose(query)
            return QueryReformResult(reformed_query=query, sub_queries=subs, detected_intent=lane.value)
        return QueryReformResult(reformed_query=query, detected_intent=lane.value)
