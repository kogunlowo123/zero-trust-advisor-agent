"""Long-term memory backed by OpenSearch (vector + keyword search).

Responsibilities
----------------
* Persist embedded documents: past investigations, resolved incidents, playbooks.
* Provide semantic (dense-vector) and keyword (BM25) retrieval.
* Support metadata filtering by date, severity, MITRE technique, analyst.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from opensearchpy import AsyncOpenSearch, NotFoundError
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
DEFAULT_INDEX = "soc_analyst_agent_vectors"
VECTOR_DIM = 3072  # text-embedding-3-large default


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------
class Document(BaseModel):
    """A document destined for long-term storage."""

    doc_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    text: str
    embedding: list[float] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class SearchResult(BaseModel):
    """A single search hit."""

    doc_id: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Index mapping
# ---------------------------------------------------------------------------
_INDEX_BODY: dict[str, Any] = {
    "settings": {
        "index": {
            "knn": True,
            "knn.algo_param.ef_search": 512,
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
    },
    "mappings": {
        "properties": {
            "doc_id": {"type": "keyword"},
            "text": {"type": "text", "analyzer": "standard"},
            "embedding": {
                "type": "knn_vector",
                "dimension": VECTOR_DIM,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "nmslib",
                    "parameters": {"ef_construction": 512, "m": 16},
                },
            },
            "metadata": {"type": "object", "enabled": True},
            "created_at": {"type": "date"},
        },
    },
}


# ---------------------------------------------------------------------------
# Long-term memory store
# ---------------------------------------------------------------------------
class LongTermMemory:
    """OpenSearch-backed vector store for persistent knowledge."""

    def __init__(
        self,
        opensearch_url: str = "https://localhost:9200",
        index_name: str = DEFAULT_INDEX,
        verify_certs: bool = False,
        http_auth: tuple[str, str] | None = None,
    ) -> None:
        self._url = opensearch_url
        self._index = index_name
        self._client: AsyncOpenSearch | None = None
        self._verify_certs = verify_certs
        self._http_auth = http_auth

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Create the async client and ensure the index exists."""
        self._client = AsyncOpenSearch(
            hosts=[self._url],
            verify_certs=self._verify_certs,
            http_auth=self._http_auth,
            use_ssl=self._url.startswith("https"),
        )
        await self._ensure_index()
        logger.info("long_term_memory.connected", url=self._url, index=self._index)

    async def close(self) -> None:
        """Shut down the client transport."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("long_term_memory.closed")

    @property
    def _os(self) -> AsyncOpenSearch:
        if self._client is None:
            raise RuntimeError("LongTermMemory not connected — call connect() first")
        return self._client

    async def _ensure_index(self) -> None:
        exists = await self._os.indices.exists(index=self._index)
        if not exists:
            await self._os.indices.create(index=self._index, body=_INDEX_BODY)
            logger.info("long_term_memory.index_created", index=self._index)

    # -- write ---------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(ConnectionError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=5),
        reraise=True,
    )
    async def store_document(self, doc: Document) -> str:
        """Index a document. Returns the doc_id."""
        body: dict[str, Any] = {
            "doc_id": doc.doc_id,
            "text": doc.text,
            "metadata": doc.metadata,
            "created_at": doc.created_at,
        }
        if doc.embedding:
            body["embedding"] = doc.embedding

        await self._os.index(
            index=self._index,
            id=doc.doc_id,
            body=body,
            refresh="wait_for",
        )
        logger.info("long_term_memory.stored", doc_id=doc.doc_id)
        return doc.doc_id

    # -- read ----------------------------------------------------------------

    async def search_similar(
        self,
        query_embedding: list[float] | None = None,
        query_text: str | None = None,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Hybrid search: dense vector + optional keyword + metadata filters.

        Supply *query_embedding* for vector search, *query_text* for keyword
        search, or both.
        """
        must_clauses: list[dict[str, Any]] = []
        filter_clauses: list[dict[str, Any]] = []

        # keyword leg
        if query_text:
            must_clauses.append({"match": {"text": {"query": query_text}}})

        # metadata filters
        if filters:
            for key, value in filters.items():
                if isinstance(value, dict) and ("gte" in value or "lte" in value):
                    filter_clauses.append({"range": {f"metadata.{key}": value}})
                else:
                    filter_clauses.append({"term": {f"metadata.{key}": value}})

        # vector leg (KNN)
        if query_embedding:
            knn_body: dict[str, Any] = {
                "size": top_k,
                "query": {
                    "bool": {
                        "must": must_clauses,
                        "filter": [
                            {
                                "knn": {
                                    "embedding": {
                                        "vector": query_embedding,
                                        "k": top_k,
                                    }
                                }
                            },
                            *filter_clauses,
                        ],
                    }
                },
            }
        else:
            # pure keyword + filter search
            knn_body = {
                "size": top_k,
                "query": {
                    "bool": {
                        "must": must_clauses or [{"match_all": {}}],
                        "filter": filter_clauses,
                    }
                },
            }

        resp = await self._os.search(index=self._index, body=knn_body)
        hits = resp.get("hits", {}).get("hits", [])

        results: list[SearchResult] = []
        for hit in hits:
            src = hit["_source"]
            results.append(
                SearchResult(
                    doc_id=src.get("doc_id", hit["_id"]),
                    text=src.get("text", ""),
                    score=hit.get("_score", 0.0),
                    metadata=src.get("metadata", {}),
                )
            )
        return results

    async def get_by_id(self, doc_id: str) -> Document | None:
        """Fetch a document by its ID."""
        try:
            resp = await self._os.get(index=self._index, id=doc_id)
            src = resp["_source"]
            return Document(
                doc_id=src.get("doc_id", doc_id),
                text=src.get("text", ""),
                embedding=src.get("embedding", []),
                metadata=src.get("metadata", {}),
                created_at=src.get("created_at", ""),
            )
        except NotFoundError:
            return None

    async def delete_by_id(self, doc_id: str) -> bool:
        """Delete a document. Returns True if found and deleted."""
        try:
            await self._os.delete(
                index=self._index,
                id=doc_id,
                refresh="wait_for",
            )
            logger.info("long_term_memory.deleted", doc_id=doc_id)
            return True
        except NotFoundError:
            return False
