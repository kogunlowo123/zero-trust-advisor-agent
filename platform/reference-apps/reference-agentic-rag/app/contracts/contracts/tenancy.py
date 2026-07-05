"""Multi-tenant context for permission-scoped queries."""
from dataclasses import dataclass


@dataclass
class TenantContext:
    tenant_id: str
    user_id: str
    user_email: str
    roles: list[str]
    access_token: str | None = None
