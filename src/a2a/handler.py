"""Zero Trust Advisor Agent - A2A Handler."""

import structlog

logger = structlog.get_logger(__name__)


class A2AHandler:
    """Agent-to-agent communication handler for Zero Trust Advisor Agent."""

    def __init__(self):
        logger.info("a2a_handler_initialized")
