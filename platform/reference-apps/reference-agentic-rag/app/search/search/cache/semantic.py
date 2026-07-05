"""Semantic cache — finds similar past queries."""
import structlog

logger = structlog.get_logger(__name__)


class SemanticCache:
    async def get(self, query: str, threshold: float = 0.95):
        return None

    async def set(self, query: str, result, embedding=None, ttl_seconds: int = 300):
        pass
