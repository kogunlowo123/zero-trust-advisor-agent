"""Multi-hop planner — decomposes into retrieval steps."""
import structlog

logger = structlog.get_logger(__name__)


class MultiHopPlanner:
    async def plan(self, query: str, max_hops: int = 3) -> list[str]:
        logger.info("multi_hop_plan", query=query[:60], max_hops=max_hops)
        return [query]
