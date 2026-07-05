"""Score-boost reranker — multiplies retrieval score by metadata signals."""
from app.contracts.models import RerankRequest, RerankResult


class BoostReranker:
    async def rerank(self, request: RerankRequest) -> RerankResult:
        for chunk in request.chunks:
            recency = chunk.metadata.get("recency_boost", 1.0)
            chunk.score *= recency
        ranked = sorted(request.chunks, key=lambda c: c.score, reverse=True)
        return RerankResult(chunks=ranked[:request.top_k])
