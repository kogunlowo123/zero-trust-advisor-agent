"""Durable decision and action log backed by PostgreSQL.

Records
-------
* Triage decisions (verdict, confidence, reasoning, analyst)
* Containment actions (action, target, approver, result)
* ADR-style records for agent configuration changes

All records are append-only and queryable by time range, severity, technique.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Literal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# SQL DDL
# ---------------------------------------------------------------------------
_CREATE_DECISIONS = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id  TEXT PRIMARY KEY,
    alert_id     TEXT        NOT NULL,
    verdict      TEXT        NOT NULL,
    confidence   FLOAT       NOT NULL,
    reasoning    TEXT        NOT NULL DEFAULT '',
    analyst      TEXT        NOT NULL DEFAULT 'agent',
    severity     TEXT,
    mitre_tactic TEXT,
    mitre_technique TEXT,
    metadata     JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_DECISIONS_IDX = """
CREATE INDEX IF NOT EXISTS idx_decisions_alert   ON decisions (alert_id);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions (created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_severity ON decisions (severity);
CREATE INDEX IF NOT EXISTS idx_decisions_technique ON decisions (mitre_technique);
"""

_CREATE_ACTIONS = """
CREATE TABLE IF NOT EXISTS containment_actions (
    action_id    TEXT PRIMARY KEY,
    action       TEXT        NOT NULL,
    target       TEXT        NOT NULL,
    approver     TEXT,
    result       TEXT,
    alert_id     TEXT,
    metadata     JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_ACTIONS_IDX = """
CREATE INDEX IF NOT EXISTS idx_actions_alert   ON containment_actions (alert_id);
CREATE INDEX IF NOT EXISTS idx_actions_created ON containment_actions (created_at);
"""

_CREATE_ADRS = """
CREATE TABLE IF NOT EXISTS agent_adrs (
    adr_id       TEXT PRIMARY KEY,
    title        TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'accepted',
    context      TEXT        NOT NULL DEFAULT '',
    decision     TEXT        NOT NULL DEFAULT '',
    consequences TEXT        NOT NULL DEFAULT '',
    metadata     JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------
class Decision(BaseModel):
    """A triage decision record."""

    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert_id: str
    verdict: str
    confidence: float
    reasoning: str = ""
    analyst: str = "agent"
    severity: str | None = None
    mitre_tactic: str | None = None
    mitre_technique: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ContainmentAction(BaseModel):
    """A containment/response action record."""

    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action: str
    target: str
    approver: str | None = None
    result: str | None = None
    alert_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class DecisionFilter(BaseModel):
    """Filters for querying decisions."""

    alert_id: str | None = None
    severity: str | None = None
    mitre_technique: str | None = None
    analyst: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    limit: int = 50


# ---------------------------------------------------------------------------
# Decision log store
# ---------------------------------------------------------------------------
class DecisionLog:
    """PostgreSQL-backed durable decision and action ledger."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._engine: AsyncEngine | None = None

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        self._engine = create_async_engine(
            self._database_url,
            pool_size=10,
            max_overflow=5,
            pool_pre_ping=True,
        )
        async with self._engine.begin() as conn:
            await conn.execute(text(_CREATE_DECISIONS))
            await conn.execute(text(_CREATE_DECISIONS_IDX))
            await conn.execute(text(_CREATE_ACTIONS))
            await conn.execute(text(_CREATE_ACTIONS_IDX))
            await conn.execute(text(_CREATE_ADRS))
        logger.info("decision_log.connected")

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            logger.info("decision_log.closed")

    @property
    def _eng(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("DecisionLog not connected — call connect() first")
        return self._engine

    # -- decisions -----------------------------------------------------------

    async def record_decision(self, decision: Decision) -> str:
        """Persist a triage decision. Returns the decision_id."""
        sql = text("""
            INSERT INTO decisions
                (decision_id, alert_id, verdict, confidence, reasoning,
                 analyst, severity, mitre_tactic, mitre_technique, metadata, created_at)
            VALUES
                (:did, :aid, :verdict, :conf, :reasoning,
                 :analyst, :severity, :tactic, :technique, :meta::jsonb,
                 :created_at::timestamptz)
            ON CONFLICT (decision_id) DO NOTHING;
        """)
        async with self._eng.begin() as conn:
            await conn.execute(
                sql,
                {
                    "did": decision.decision_id,
                    "aid": decision.alert_id,
                    "verdict": decision.verdict,
                    "conf": decision.confidence,
                    "reasoning": decision.reasoning,
                    "analyst": decision.analyst,
                    "severity": decision.severity,
                    "tactic": decision.mitre_tactic,
                    "technique": decision.mitre_technique,
                    "meta": json.dumps(decision.metadata),
                    "created_at": decision.created_at,
                },
            )
        logger.info(
            "decision_log.decision_recorded",
            decision_id=decision.decision_id,
            alert_id=decision.alert_id,
        )
        return decision.decision_id

    async def get_decision(self, decision_id: str) -> Decision | None:
        """Fetch a single decision by ID."""
        sql = text("""
            SELECT decision_id, alert_id, verdict, confidence, reasoning,
                   analyst, severity, mitre_tactic, mitre_technique,
                   metadata, created_at
            FROM decisions
            WHERE decision_id = :did;
        """)
        async with self._eng.connect() as conn:
            row = (await conn.execute(sql, {"did": decision_id})).fetchone()
        if row is None:
            return None
        return Decision(
            decision_id=row.decision_id,
            alert_id=row.alert_id,
            verdict=row.verdict,
            confidence=row.confidence,
            reasoning=row.reasoning,
            analyst=row.analyst,
            severity=row.severity,
            mitre_tactic=row.mitre_tactic,
            mitre_technique=row.mitre_technique,
            metadata=row.metadata if isinstance(row.metadata, dict) else {},
            created_at=str(row.created_at),
        )

    async def get_decisions(self, filters: DecisionFilter) -> list[Decision]:
        """Query decisions with optional filters."""
        conditions: list[str] = []
        params: dict[str, Any] = {"lim": filters.limit}

        if filters.alert_id:
            conditions.append("alert_id = :alert_id")
            params["alert_id"] = filters.alert_id
        if filters.severity:
            conditions.append("severity = :severity")
            params["severity"] = filters.severity
        if filters.mitre_technique:
            conditions.append("mitre_technique = :technique")
            params["technique"] = filters.mitre_technique
        if filters.analyst:
            conditions.append("analyst = :analyst")
            params["analyst"] = filters.analyst
        if filters.from_date:
            conditions.append("created_at >= :from_date::timestamptz")
            params["from_date"] = filters.from_date
        if filters.to_date:
            conditions.append("created_at <= :to_date::timestamptz")
            params["to_date"] = filters.to_date

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        sql = text(f"""
            SELECT decision_id, alert_id, verdict, confidence, reasoning,
                   analyst, severity, mitre_tactic, mitre_technique,
                   metadata, created_at
            FROM decisions
            {where}
            ORDER BY created_at DESC
            LIMIT :lim;
        """)

        async with self._eng.connect() as conn:
            rows = (await conn.execute(sql, params)).fetchall()

        return [
            Decision(
                decision_id=r.decision_id,
                alert_id=r.alert_id,
                verdict=r.verdict,
                confidence=r.confidence,
                reasoning=r.reasoning,
                analyst=r.analyst,
                severity=r.severity,
                mitre_tactic=r.mitre_tactic,
                mitre_technique=r.mitre_technique,
                metadata=r.metadata if isinstance(r.metadata, dict) else {},
                created_at=str(r.created_at),
            )
            for r in rows
        ]

    # -- containment actions -------------------------------------------------

    async def record_action(self, action: ContainmentAction) -> str:
        """Persist a containment action. Returns the action_id."""
        sql = text("""
            INSERT INTO containment_actions
                (action_id, action, target, approver, result,
                 alert_id, metadata, created_at)
            VALUES
                (:aid, :action, :target, :approver, :result,
                 :alert_id, :meta::jsonb, :created_at::timestamptz)
            ON CONFLICT (action_id) DO NOTHING;
        """)
        async with self._eng.begin() as conn:
            await conn.execute(
                sql,
                {
                    "aid": action.action_id,
                    "action": action.action,
                    "target": action.target,
                    "approver": action.approver,
                    "result": action.result,
                    "alert_id": action.alert_id,
                    "meta": json.dumps(action.metadata),
                    "created_at": action.created_at,
                },
            )
        logger.info(
            "decision_log.action_recorded",
            action_id=action.action_id,
            action=action.action,
        )
        return action.action_id

    async def get_actions(self, filters: DecisionFilter) -> list[ContainmentAction]:
        """Query containment actions with optional filters."""
        conditions: list[str] = []
        params: dict[str, Any] = {"lim": filters.limit}

        if filters.alert_id:
            conditions.append("alert_id = :alert_id")
            params["alert_id"] = filters.alert_id
        if filters.from_date:
            conditions.append("created_at >= :from_date::timestamptz")
            params["from_date"] = filters.from_date
        if filters.to_date:
            conditions.append("created_at <= :to_date::timestamptz")
            params["to_date"] = filters.to_date

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        sql = text(f"""
            SELECT action_id, action, target, approver, result,
                   alert_id, metadata, created_at
            FROM containment_actions
            {where}
            ORDER BY created_at DESC
            LIMIT :lim;
        """)

        async with self._eng.connect() as conn:
            rows = (await conn.execute(sql, params)).fetchall()

        return [
            ContainmentAction(
                action_id=r.action_id,
                action=r.action,
                target=r.target,
                approver=r.approver,
                result=r.result,
                alert_id=r.alert_id,
                metadata=r.metadata if isinstance(r.metadata, dict) else {},
                created_at=str(r.created_at),
            )
            for r in rows
        ]
