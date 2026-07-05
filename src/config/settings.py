"""Application configuration for Zero Trust Advisor Agent."""

from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    app_name: str = Field(default="zero-trust-advisor-agent", description="Application name")
    app_env: str = Field(default="development", description="Environment")
    app_port: int = Field(default=8000, description="API port")
    app_host: str = Field(default="0.0.0.0", description="API host")
    log_level: str = Field(default="INFO", description="Log level")
    secret_key: str = Field(default="change-me", description="Secret key")

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/zero_trust_advisor_agent",
        description="Database connection URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis URL")

    llm_model: str = Field(default="gpt-4o", description="LLM model ID")
    embedding_model: str = Field(default="text-embedding-3-large", description="Embedding model")
    llm_temperature: float = Field(default=0.1, description="LLM temperature")
    llm_max_tokens: int = Field(default=4096, description="Max output tokens")

    vector_store_type: str = Field(default="opensearch", description="Vector store type")
    opensearch_url: str = Field(default="https://localhost:9200", description="OpenSearch URL")
    opensearch_index: str = Field(default="zero_trust_advisor_agent_vectors", description="Index")

    jwt_secret: str = Field(default="change-me", description="JWT secret")
    jwt_algorithm: str = Field(default="HS256", description="JWT algorithm")
    jwt_expiration_hours: int = Field(default=24, description="JWT expiration")

    mcp_server_port: int = Field(default=8001, description="MCP server port")
    mcp_server_name: str = Field(default="zero-trust-advisor-agent-mcp", description="MCP server name")

    a2a_discovery_url: str = Field(default="http://localhost:8500", description="A2A discovery URL")
    a2a_agent_id: str = Field(default="zero-trust-advisor-agent", description="A2A agent ID")

    otel_endpoint: str = Field(default="http://localhost:4317", description="OTLP endpoint")
    enable_tracing: bool = Field(default=True, description="Enable tracing")
    enable_metrics: bool = Field(default=True, description="Enable metrics")

    rate_limit_requests: int = Field(default=100, description="Rate limit")
    rate_limit_window: int = Field(default=60, description="Rate limit window (seconds)")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
