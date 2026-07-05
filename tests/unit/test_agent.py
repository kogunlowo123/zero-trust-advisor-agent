"""Zero Trust Advisor Agent - Unit Tests."""

import pytest
from src.agent.tools import AgentTools


@pytest.mark.asyncio
async def test_analyze():
    """Test Primary analysis function for Zero Trust Advisor Agent."""
    tools = AgentTools()
    result = await tools.analyze(target="test", scope="test")
    assert result is not None
    assert "status" in result or "tool" in result


@pytest.mark.asyncio
async def test_scan():
    """Test Scan target for issues relevant to Zero Trust Advisor Agent."""
    tools = AgentTools()
    result = await tools.scan(target="test", policy="test")
    assert result is not None
    assert "status" in result or "tool" in result


@pytest.mark.asyncio
async def test_report():
    """Test Generate report for Zero Trust Advisor Agent."""
    tools = AgentTools()
    result = await tools.report(scope="test", period="test")
    assert result is not None
    assert "status" in result or "tool" in result


@pytest.mark.asyncio
async def test_remediate():
    """Test Execute remediation action."""
    tools = AgentTools()
    result = await tools.remediate(finding_id="test", action="test")
    assert result is not None
    assert "status" in result or "tool" in result


@pytest.mark.asyncio
async def test_agent_initialization():
    """Test that the agent initializes correctly."""
    from src.agent.zero_trust_advisor_agent_agent import ZeroTrustAdvisorAgentAgent
    agent = ZeroTrustAdvisorAgentAgent()
    assert agent.agent_id is not None
    assert agent._system_prompt is not None
    assert len(agent._tool_dispatch) > 0
