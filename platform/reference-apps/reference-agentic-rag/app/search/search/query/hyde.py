"""HyDE — Hypothetical Document Embeddings."""
import structlog
from app.search.ports import LLMPort

logger = structlog.get_logger(__name__)


class HyDE:
    def __init__(self, llm: LLMPort | None = None):
        self._llm = llm

    async def generate_hypothetical(self, query: str) -> str:
        logger.info("hyde_generate", query=query[:60])
        return query
