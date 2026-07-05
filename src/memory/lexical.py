"""Lexical (full-text) memory backed by PostgreSQL tsvector/tsquery.

Responsibilities
----------------
* BM25-style full-text search across all stored documents.
* Phrase search and boolean operators via PostgreSQL tsquery.
* Ranked results using ts_rank.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# SQL DDL
# ---------------------------------------------------------------------------
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS lexical_documents (
    doc_id       TEXT PRIMARY KEY,
    body         TEXT        NOT NULL,
    metadata     JSONB       NOT NULL DEFAULT '{}',
    tsv          tsvector    GENERATED ALWAYS AS (
                     to_tsvector('english', body)
                 ) STORED,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_lexical_tsv
    ON lexical_documents USING gin(tsv);
"""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------
class LexicalHit(BaseModel):
    """A single full-text search result."""

    doc_id: str
    snippet: str
    rank: float
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Lexical memory store
# ---------------------------------------------------------------------------
class LexicalMemory:
    """PostgreSQL-backed inverted index using tsvector/tsquery."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._engine: AsyncEngine | None = None

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Create the engine and ensure the table exists."""
        self._engine = create_async_engine(
            self._database_url,
            pool_size=10,
            max_overflow=5,
            pool_pre_ping=True,
        )
        async with self._engine.begin() as conn:
            await conn.execute(text(_CREATE_TABLE))
            await conn.execute(text(_CREATE_INDEX))
        logger.info("lexical_memory.connected")

    async def close(self) -> None:
        """Dispose of the engine."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            logger.info("lexical_memory.closed")

    @property
    def _eng(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("LexicalMemory not connected — call connect() first")
        return self._engine

    # -- write ---------------------------------------------------------------

    async def index_document(
        self,
        doc_id: str,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert or update a document in the full-text index."""
        upsert = text("""
            INSERT INTO lexical_documents (doc_id, body, metadata)
            VALUES (:doc_id, :body, :metadata::jsonb)
            ON CONFLICT (doc_id) DO UPDATE
                SET body     = EXCLUDED.body,
                    metadata = EXCLUDED.metadata;
        """)
        import json

        async with self._eng.begin() as conn:
            await conn.execute(
                upsert,
                {
                    "doc_id": doc_id,
                    "body": body,
                    "metadata": json.dumps(metadata or {}),
                },
            )
        logger.debug("lexical_memory.indexed", doc_id=doc_id)

    # -- read ----------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 10,
    ) -> list[LexicalHit]:
        """Full-text search with ts_rank scoring.

        *query* supports PostgreSQL tsquery syntax:
        - Simple words: ``malware``
        - Phrase:       ``'lateral movement'`` (use websearch_to_tsquery)
        - Boolean:      ``malware & !benign``
        """
        search_sql = text("""
            SELECT
                doc_id,
                ts_headline('english', body, websearch_to_tsquery('english', :q),
                            'MaxWords=60, MinWords=20, StartSel=**, StopSel=**')
                    AS snippet,
                ts_rank(tsv, websearch_to_tsquery('english', :q)) AS rank,
                metadata
            FROM lexical_documents
            WHERE tsv @@ websearch_to_tsquery('english', :q)
            ORDER BY rank DESC
            LIMIT :k;
        """)

        async with self._eng.connect() as conn:
            result = await conn.execute(search_sql, {"q": query, "k": top_k})
            rows = result.fetchall()

        hits: list[LexicalHit] = []
        for row in rows:
            hits.append(
                LexicalHit(
                    doc_id=row.doc_id,
                    snippet=row.snippet,
                    rank=float(row.rank),
                    metadata=row.metadata if isinstance(row.metadata, dict) else {},
                )
            )
        return hits

    # -- delete --------------------------------------------------------------

    async def delete(self, doc_id: str) -> bool:
        """Remove a document from the index. Returns True if found."""
        delete_sql = text("""
            DELETE FROM lexical_documents WHERE doc_id = :doc_id;
        """)
        async with self._eng.begin() as conn:
            result = await conn.execute(delete_sql, {"doc_id": doc_id})
        deleted = result.rowcount > 0  # type: ignore[union-attr]
        if deleted:
            logger.info("lexical_memory.deleted", doc_id=doc_id)
        return deleted
