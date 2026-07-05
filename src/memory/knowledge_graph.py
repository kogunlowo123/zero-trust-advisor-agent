"""Entity-relationship knowledge graph backed by PostgreSQL.

Uses recursive CTEs for graph traversal — no external graph database required.

SOC Entity Types
----------------
IP, Domain, Hash, User, Host, Campaign, Technique

Relationship Types
------------------
communicated_with, logged_into, downloaded, associated_with, resolved_to,
executed_on, part_of, targets, uses
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
EntityType = Literal[
    "IP", "Domain", "Hash", "User", "Host", "Campaign", "Technique"
]

RelType = Literal[
    "communicated_with",
    "logged_into",
    "downloaded",
    "associated_with",
    "resolved_to",
    "executed_on",
    "part_of",
    "targets",
    "uses",
]

# ---------------------------------------------------------------------------
# SQL DDL
# ---------------------------------------------------------------------------
_CREATE_ENTITIES = """
CREATE TABLE IF NOT EXISTS kg_entities (
    entity_id    TEXT PRIMARY KEY,
    entity_type  TEXT        NOT NULL,
    properties   JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_ENTITY_TYPE_IDX = """
CREATE INDEX IF NOT EXISTS idx_kg_entity_type
    ON kg_entities (entity_type);
"""

_CREATE_RELATIONSHIPS = """
CREATE TABLE IF NOT EXISTS kg_relationships (
    id           BIGSERIAL PRIMARY KEY,
    from_id      TEXT        NOT NULL REFERENCES kg_entities(entity_id) ON DELETE CASCADE,
    to_id        TEXT        NOT NULL REFERENCES kg_entities(entity_id) ON DELETE CASCADE,
    rel_type     TEXT        NOT NULL,
    properties   JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (from_id, to_id, rel_type)
);
"""

_CREATE_REL_FROM_IDX = """
CREATE INDEX IF NOT EXISTS idx_kg_rel_from ON kg_relationships (from_id);
"""
_CREATE_REL_TO_IDX = """
CREATE INDEX IF NOT EXISTS idx_kg_rel_to   ON kg_relationships (to_id);
"""
_CREATE_REL_TYPE_IDX = """
CREATE INDEX IF NOT EXISTS idx_kg_rel_type ON kg_relationships (rel_type);
"""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------
class Entity(BaseModel):
    """A node in the knowledge graph."""

    entity_id: str
    entity_type: str
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Relationship(BaseModel):
    """An edge in the knowledge graph."""

    from_id: str
    to_id: str
    rel_type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphPath(BaseModel):
    """An ordered path of entity IDs returned by find_path."""

    entities: list[str]
    relationships: list[str]
    total_hops: int


# ---------------------------------------------------------------------------
# Knowledge graph store
# ---------------------------------------------------------------------------
class KnowledgeGraph:
    """PostgreSQL entity-relationship graph with recursive CTE traversal."""

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
            await conn.execute(text(_CREATE_ENTITIES))
            await conn.execute(text(_CREATE_ENTITY_TYPE_IDX))
            await conn.execute(text(_CREATE_RELATIONSHIPS))
            await conn.execute(text(_CREATE_REL_FROM_IDX))
            await conn.execute(text(_CREATE_REL_TO_IDX))
            await conn.execute(text(_CREATE_REL_TYPE_IDX))
        logger.info("knowledge_graph.connected")

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            logger.info("knowledge_graph.closed")

    @property
    def _eng(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("KnowledgeGraph not connected — call connect() first")
        return self._engine

    # -- entities ------------------------------------------------------------

    async def add_entity(
        self,
        entity_type: str,
        entity_id: str,
        properties: dict[str, Any] | None = None,
    ) -> Entity:
        """Insert or update an entity node."""
        upsert = text("""
            INSERT INTO kg_entities (entity_id, entity_type, properties)
            VALUES (:eid, :etype, :props::jsonb)
            ON CONFLICT (entity_id) DO UPDATE
                SET properties = kg_entities.properties || EXCLUDED.properties,
                    entity_type = EXCLUDED.entity_type,
                    updated_at  = NOW()
            RETURNING entity_id, entity_type, properties, created_at;
        """)
        async with self._eng.begin() as conn:
            row = (
                await conn.execute(
                    upsert,
                    {
                        "eid": entity_id,
                        "etype": entity_type,
                        "props": json.dumps(properties or {}),
                    },
                )
            ).fetchone()

        entity = Entity(
            entity_id=row.entity_id,  # type: ignore[union-attr]
            entity_type=row.entity_type,  # type: ignore[union-attr]
            properties=row.properties if isinstance(row.properties, dict) else {},  # type: ignore[union-attr]
            created_at=str(row.created_at),  # type: ignore[union-attr]
        )
        logger.debug("knowledge_graph.entity_added", entity_id=entity_id)
        return entity

    async def get_entity(self, entity_id: str) -> Entity | None:
        """Fetch a single entity by ID."""
        sql = text("""
            SELECT entity_id, entity_type, properties, created_at
            FROM kg_entities
            WHERE entity_id = :eid;
        """)
        async with self._eng.connect() as conn:
            row = (await conn.execute(sql, {"eid": entity_id})).fetchone()
        if row is None:
            return None
        return Entity(
            entity_id=row.entity_id,
            entity_type=row.entity_type,
            properties=row.properties if isinstance(row.properties, dict) else {},
            created_at=str(row.created_at),
        )

    # -- relationships -------------------------------------------------------

    async def add_relationship(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> Relationship:
        """Create a directed relationship between two entities."""
        upsert = text("""
            INSERT INTO kg_relationships (from_id, to_id, rel_type, properties)
            VALUES (:fid, :tid, :rtype, :props::jsonb)
            ON CONFLICT (from_id, to_id, rel_type) DO UPDATE
                SET properties = kg_relationships.properties || EXCLUDED.properties
            RETURNING from_id, to_id, rel_type, properties;
        """)
        async with self._eng.begin() as conn:
            row = (
                await conn.execute(
                    upsert,
                    {
                        "fid": from_id,
                        "tid": to_id,
                        "rtype": rel_type,
                        "props": json.dumps(properties or {}),
                    },
                )
            ).fetchone()

        rel = Relationship(
            from_id=row.from_id,  # type: ignore[union-attr]
            to_id=row.to_id,  # type: ignore[union-attr]
            rel_type=row.rel_type,  # type: ignore[union-attr]
            properties=row.properties if isinstance(row.properties, dict) else {},  # type: ignore[union-attr]
        )
        logger.debug(
            "knowledge_graph.relationship_added",
            from_id=from_id,
            to_id=to_id,
            rel_type=rel_type,
        )
        return rel

    # -- traversal -----------------------------------------------------------

    async def get_neighbors(
        self,
        entity_id: str,
        rel_type: str | None = None,
        depth: int = 1,
    ) -> list[Entity]:
        """Return entities reachable within *depth* hops via recursive CTE.

        Traverses relationships in both directions (undirected).
        """
        if depth < 1:
            depth = 1

        rel_filter = "AND r.rel_type = :rtype" if rel_type else ""

        cte_sql = text(f"""
            WITH RECURSIVE reachable(entity_id, lvl) AS (
                SELECT :start_id::text, 0

                UNION ALL

                SELECT
                    CASE
                        WHEN r.from_id = rch.entity_id THEN r.to_id
                        ELSE r.from_id
                    END,
                    rch.lvl + 1
                FROM reachable rch
                JOIN kg_relationships r
                    ON (r.from_id = rch.entity_id OR r.to_id = rch.entity_id)
                    {rel_filter}
                WHERE rch.lvl < :max_depth
            )
            SELECT DISTINCT e.entity_id, e.entity_type, e.properties, e.created_at
            FROM reachable rch
            JOIN kg_entities e ON e.entity_id = rch.entity_id
            WHERE e.entity_id != :start_id;
        """)

        params: dict[str, Any] = {"start_id": entity_id, "max_depth": depth}
        if rel_type:
            params["rtype"] = rel_type

        async with self._eng.connect() as conn:
            rows = (await conn.execute(cte_sql, params)).fetchall()

        return [
            Entity(
                entity_id=r.entity_id,
                entity_type=r.entity_type,
                properties=r.properties if isinstance(r.properties, dict) else {},
                created_at=str(r.created_at),
            )
            for r in rows
        ]

    async def find_path(
        self,
        from_id: str,
        to_id: str,
        max_depth: int = 6,
    ) -> GraphPath | None:
        """Find the shortest path between two entities using BFS via CTE."""
        bfs_sql = text("""
            WITH RECURSIVE search_path(entity_id, path, rel_path, depth) AS (
                SELECT
                    :from_id::text,
                    ARRAY[:from_id::text],
                    ARRAY[]::text[],
                    0

                UNION ALL

                SELECT
                    CASE
                        WHEN r.from_id = sp.entity_id THEN r.to_id
                        ELSE r.from_id
                    END,
                    sp.path || CASE
                        WHEN r.from_id = sp.entity_id THEN r.to_id
                        ELSE r.from_id
                    END,
                    sp.rel_path || r.rel_type,
                    sp.depth + 1
                FROM search_path sp
                JOIN kg_relationships r
                    ON (r.from_id = sp.entity_id OR r.to_id = sp.entity_id)
                WHERE sp.depth < :max_depth
                  AND NOT (
                      CASE
                          WHEN r.from_id = sp.entity_id THEN r.to_id
                          ELSE r.from_id
                      END = ANY(sp.path)
                  )
            )
            SELECT path, rel_path, depth
            FROM search_path
            WHERE entity_id = :to_id
            ORDER BY depth
            LIMIT 1;
        """)

        async with self._eng.connect() as conn:
            row = (
                await conn.execute(
                    bfs_sql,
                    {"from_id": from_id, "to_id": to_id, "max_depth": max_depth},
                )
            ).fetchone()

        if row is None:
            return None

        return GraphPath(
            entities=list(row.path),
            relationships=list(row.rel_path),
            total_hops=row.depth,
        )
