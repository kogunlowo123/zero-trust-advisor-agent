"""Cross-encoder reranker using sentence-transformers."""
import structlog
from app.contracts.models import RerankRequest, RerankResult

logger = structlog.get_logger(__name__)


class CrossEncoderReranker:
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-12-v2"):
        self._model_name = model_name

    async def rerank(self, request: RerankRequest) -> RerankResult:
        logger.info("cross_encoder_rerank", chunks=len(request.chunks))
        pairs = [(request.query, c.content) for c in request.chunks]
        # In production, load the model and score pairs
        ranked = sorted(request.chunks, key=lambda c: c.score, reverse=True)
        return RerankResult(chunks=ranked[:request.top_k])
