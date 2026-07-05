"""Immutable audit trail for the AI Gateway.

Logs every request, tool call, and guardrail trigger to PostgreSQL
in structured JSON format suitable for SIEM ingestion.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import Column, DateTime, Integer, String, Text, Float
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# SQLAlchemy models
# ---------------------------------------------------------------------------


class _Base(DeclarativeBase):
    pass


class AuditRequestLog(_Base):
    """Every API request that passes through the gateway."""

    __tablename__ = "audit_request_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(String(128), nullable=False, index=True)
    action = Column(String(256), nullable=False)
    path = Column(String(512), nullable=False)
    method = Column(String(10), nullable=False)
    status_code = Column(Integer, nullable=False)
    latency_ms = Column(Float, nullable=False)
    model = Column(String(64), nullable=True)
    input_hash = Column(String(64), nullable=False)
    output_hash = Column(String(64), nullable=False)
    tokens_used = Column(Integer, nullable=False, default=0)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    metadata_json = Column(Text, nullable=True)


class AuditToolCallLog(_Base):
    """Every tool invocation."""

    __tablename__ = "audit_tool_call_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(64), nullable=False, index=True)
    tool_call_id = Column(String(64), nullable=False, unique=True)
    tool_name = Column(String(128), nullable=False, index=True)
    parameters_json = Column(Text, nullable=False)
    result_json = Column(Text, nullable=True)
    duration_ms = Column(Float, nullable=False)
    success = Column(Integer, nullable=False, default=1)  # 1=true, 0=false
    error_message = Column(Text, nullable=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)


class AuditGuardrailLog(_Base):
    """Every guardrail check that fired."""

    __tablename__ = "audit_guardrail_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(64), nullable=False, index=True)
    check_name = Column(String(128), nullable=False, index=True)
    result = Column(String(16), nullable=False)  # allow | deny | warn
    reason = Column(Text, nullable=False)
    score = Column(Float, nullable=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)


# ---------------------------------------------------------------------------
# Audit Trail service
# ---------------------------------------------------------------------------


class AuditTrail:
    """Write immutable audit records to PostgreSQL.

    Records are append-only. The schema intentionally has no UPDATE or
    DELETE operations to preserve forensic integrity.
    """

    def __init__(self, database_url: str) -> None:
        self._engine = create_async_engine(
            database_url,
            echo=False,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        self._session_factory = sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        self._initialized = False

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        if self._initialized:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
        self._initialized = True
        logger.info("audit_trail_initialized")

    # ------------------------------------------------------------------
    # Request logging
    # ------------------------------------------------------------------

    async def log_request(
        self,
        *,
        request_id: str,
        user_id: str,
        action: str,
        path: str,
        method: str,
        status_code: int,
        latency_ms: float,
        model: str,
        input_hash: str,
        output_hash: str,
        tokens_used: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append a request audit record."""
        record = AuditRequestLog(
            request_id=request_id,
            user_id=user_id,
            action=action,
            path=path,
            method=method,
            status_code=status_code,
            latency_ms=latency_ms,
            model=model,
            input_hash=input_hash,
            output_hash=output_hash,
            tokens_used=tokens_used,
            timestamp=datetime.now(timezone.utc),
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        await self._persist(record)

        # Also emit structured log for SIEM ingestion
        logger.info(
            "audit_request",
            request_id=request_id,
            user_id=user_id,
            action=action,
            path=path,
            method=method,
            status_code=status_code,
            latency_ms=latency_ms,
            model=model,
            tokens_used=tokens_used,
        )

    # ------------------------------------------------------------------
    # Tool call logging
    # ------------------------------------------------------------------

    async def log_tool_call(
        self,
        *,
        request_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        result: Any = None,
        duration_ms: float = 0.0,
        success: bool = True,
        error_message: str | None = None,
    ) -> None:
        """Append a tool-call audit record."""
        tool_call_id = str(uuid4())
        record = AuditToolCallLog(
            request_id=request_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            parameters_json=self._safe_json(parameters),
            result_json=self._safe_json(result) if result is not None else None,
            duration_ms=duration_ms,
            success=1 if success else 0,
            error_message=error_message,
            timestamp=datetime.now(timezone.utc),
        )
        await self._persist(record)

        logger.info(
            "audit_tool_call",
            request_id=request_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            duration_ms=duration_ms,
            success=success,
        )

    # ------------------------------------------------------------------
    # Guardrail logging
    # ------------------------------------------------------------------

    async def log_guardrail(
        self,
        *,
        request_id: str,
        check_name: str,
        result: str,
        reason: str,
        score: float = 0.0,
    ) -> None:
        """Append a guardrail audit record."""
        record = AuditGuardrailLog(
            request_id=request_id,
            check_name=check_name,
            result=result,
            reason=reason,
            score=score,
            timestamp=datetime.now(timezone.utc),
        )
        await self._persist(record)

        log_fn = logger.warning if result != "allow" else logger.info
        log_fn(
            "audit_guardrail",
            request_id=request_id,
            check_name=check_name,
            result=result,
            reason=reason,
            score=score,
        )

    # ------------------------------------------------------------------
    # SIEM export helpers
    # ------------------------------------------------------------------

    async def export_json_lines(
        self,
        *,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Export recent audit records as JSON objects for SIEM ingestion."""
        async with self._session_factory() as session:
            from sqlalchemy import select

            stmt = select(AuditRequestLog).order_by(
                AuditRequestLog.timestamp.desc()
            )
            if since is not None:
                stmt = stmt.where(AuditRequestLog.timestamp >= since)
            stmt = stmt.limit(limit)

            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [
            {
                "event_type": "gateway_request",
                "request_id": row.request_id,
                "user_id": row.user_id,
                "action": row.action,
                "path": row.path,
                "method": row.method,
                "status_code": row.status_code,
                "latency_ms": row.latency_ms,
                "model": row.model,
                "input_hash": row.input_hash,
                "output_hash": row.output_hash,
                "tokens_used": row.tokens_used,
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                "metadata": json.loads(row.metadata_json) if row.metadata_json else None,
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _persist(self, record: Any) -> None:
        """Write a single record to the database."""
        try:
            await self.initialize()
            async with self._session_factory() as session:
                session.add(record)
                await session.commit()
        except Exception as exc:
            # Audit failures must never break the request pipeline.
            # Log the failure and move on.
            logger.error(
                "audit_persist_failed",
                record_type=type(record).__name__,
                error=str(exc),
            )

    @staticmethod
    def _safe_json(obj: Any) -> str:
        """Serialize to JSON, falling back to str() for non-serialisable objects."""
        try:
            return json.dumps(obj, default=str)
        except (TypeError, ValueError):
            return json.dumps({"raw": str(obj)})
