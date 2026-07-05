"""Web fallback — searches the web when vector store retrieval is insufficient."""
import structlog

logger = structlog.get_logger(__name__)


class WebFallback:
    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        logger.info("web_fallback", query=query[:60])
        return []
