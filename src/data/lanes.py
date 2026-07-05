"""Zero Trust Advisor Agent — Data Lane Router."""

import structlog
from enum import Enum
from typing import Any

logger = structlog.get_logger(__name__)


class DataLane(str, Enum):
    INDEXED = "indexed"
    LIVE = "live"
    STRUCTURED = "structured"


class DataLaneRouter:
    """Routes queries to the appropriate data lane for Zero Trust Advisor Agent.

    - INDEXED: Stable documents in the vector index (knowledge base, playbooks, past reports)
    - LIVE: Per-request data queried in real time (current alerts, user data, API calls)
    - STRUCTURED: Tabular data queried via NL2SQL (metrics, statistics, SLA data)
    """

    def __init__(self):
        logger.info("data_lane_router_initialized")

    def classify(self, query: str) -> DataLane:
        """Classify a query into a data lane."""
        q = query.lower()
        live_signals = ["my ", "current ", "right now", "latest ", "active "]
        structured_signals = ["how many", "total", "count", "average", "sum", "trend", "compare"]
        if any(s in q for s in live_signals):
            return DataLane.LIVE
        if any(s in q for s in structured_signals):
            return DataLane.STRUCTURED
        return DataLane.INDEXED

    async def route(self, query: str, **kwargs) -> dict[str, Any]:
        lane = self.classify(query)
        logger.info("query_routed", lane=lane.value, query=query[:60])
        return {"lane": lane.value, "query": query}
