"""Zero Trust Advisor Agent — Tool Registry."""

import time
import structlog
from typing import Any

logger = structlog.get_logger(__name__)


class ToolDefinition:
    """Definition of a registered tool."""
    def __init__(self, name: str, description: str, category: str, handler, permissions: list[str] = None, side_effects: bool = False):
        self.name = name
        self.description = description
        self.category = category
        self.handler = handler
        self.permissions = permissions or []
        self.side_effects = side_effects


class ToolRegistry:
    """Central registry for Zero Trust Advisor Agent domain-specific tools."""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._register_domain_tools()
        logger.info("tool_registry_initialized", tool_count=len(self._tools))

    def _register_domain_tools(self):
        """Register all domain-specific tools from AgentTools."""
        from src.agent.tools import AgentTools
        tools = AgentTools()
        for method_name in dir(tools):
            if method_name.startswith("_") or method_name == "get_tool_definitions":
                continue
            method = getattr(tools, method_name)
            if callable(method):
                self._tools[method_name] = ToolDefinition(
                    name=method_name,
                    description=method.__doc__ or method_name,
                    category="domain",
                    handler=method,
                )

    def register(self, tool: ToolDefinition):
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self, category: str | None = None) -> list[ToolDefinition]:
        if category:
            return [t for t in self._tools.values() if t.category == category]
        return list(self._tools.values())

    async def execute(self, name: str, params: dict[str, Any], user_context: dict | None = None) -> dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Tool '{name}' not found", "available": list(self._tools.keys())}
        start = time.monotonic()
        try:
            result = await tool.handler(**params)
            elapsed = (time.monotonic() - start) * 1000
            logger.info("tool_executed", tool=name, latency_ms=round(elapsed, 1))
            return {"status": "success", "tool": name, "result": result, "latency_ms": round(elapsed, 1)}
        except Exception as e:
            logger.error("tool_execution_failed", tool=name, error=str(e))
            return {"status": "error", "tool": name, "error": str(e)}
