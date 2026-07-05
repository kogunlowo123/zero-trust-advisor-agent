"""Cascade reranker — applies boost then cross-encoder for cost efficiency."""
from app.contracts.models import RerankRequest, RerankResult
from app.search.reranking.boost import BoostReranker
from app.search.reranking.cross_encoder import CrossEncoderReranker


class CascadeReranker:
    def __init__(self, top_k_first_stage: int = 20):
        self._boost = BoostReranker()
        self._cross_encoder = CrossEncoderReranker()
        self._first_stage_k = top_k_first_stage

    async def rerank(self, request: RerankRequest) -> RerankResult:
        first = await self._boost.rerank(RerankRequest(query=request.query, chunks=request.chunks, top_k=self._first_stage_k))
        return await self._cross_encoder.rerank(RerankRequest(query=request.query, chunks=first.chunks, top_k=request.top_k))
