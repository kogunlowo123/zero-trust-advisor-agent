"""Reranker factory — creates the appropriate reranker from strategy."""
from app.contracts.enums import RerankStrategy
from app.search.ports import LLMPort
from app.search.reranking.boost import BoostReranker
from app.search.reranking.cross_encoder import CrossEncoderReranker
from app.search.reranking.llm_rerank import LLMReranker
from app.search.reranking.cascade import CascadeReranker


def create_reranker(strategy: RerankStrategy, llm: LLMPort | None = None):
    if strategy == RerankStrategy.NONE:
        return None
    elif strategy == RerankStrategy.BOOST:
        return BoostReranker()
    elif strategy == RerankStrategy.CROSS_ENCODER:
        return CrossEncoderReranker()
    elif strategy == RerankStrategy.LLM_RERANK and llm:
        return LLMReranker(llm)
    elif strategy == RerankStrategy.CASCADE:
        return CascadeReranker()
    return BoostReranker()
