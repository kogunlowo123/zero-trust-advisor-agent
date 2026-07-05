"""Multi-hop synthesizer — combines results from multiple hops."""
import structlog

logger = structlog.get_logger(__name__)


class MultiHopSynthesizer:
    async def synthesize(self, results: list[dict], original_query: str) -> str:
        logger.info("multi_hop_synthesize", results=len(results))
        return ""
