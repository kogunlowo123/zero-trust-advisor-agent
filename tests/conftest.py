"""Test configuration for Zero Trust Advisor Agent."""

import pytest


@pytest.fixture
def agent_config():
    return {"name": "zero-trust-advisor-agent", "category": "Security AI"}
