"""Multi-hop executor — runs planned retrieval steps."""
import structlog

logger = structlog.get_logger(__name__)


class MultiHopExecutor:
    async def execute(self, steps: list[str]) -> list[dict]:
        logger.info("multi_hop_execute", steps=len(steps))
        return []
