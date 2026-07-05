# Vector Store Contract (ADR-0002)

## Promise
Every cloud implementation provides a vector-capable store for Zero Trust Advisor Agent:
- Dense vector storage (1536 or 3072 dimensions)
- Keyword / BM25 search
- Hybrid search (vector + keyword combined via RRF)
- Metadata filtering on ingestion-time fields
- Index lifecycle management (create, update, delete)

## Interface
| Operation              | Input                        | Output                    |
|------------------------|------------------------------|---------------------------|
| `create_index`         | index_name, dimension, schema| index_id                  |
| `upsert_documents`     | index_id, documents[]        | upserted_count            |
| `search`               | index_id, query, vector, top_k, filters | results[]    |
| `delete_documents`     | index_id, document_ids[]     | deleted_count             |
| `health_check`         | -                            | {status, doc_count}     |

## Document Schema
```json
{
  "id": "sha256_chunkIndex",
  "content": "The paragraph text...",
  "embedding": [0.012, -0.034, ...],
  "metadata": {
    "document_title": "Knowledge Base Article",
    "source": "internal://knowledge-base/article.md",
    "chunk_index": 3,
    "total_chunks": 47,
    "connector_type": "internal",
    "ingested_at": "2025-01-15T10:30:00Z"
  }
}
```

## Implementors
- `modules/appops/vectorstore/aws-opensearch-serverless/`
- `modules/appops/vectorstore/pgvector/`
- `modules/appops/vectorstore/azure-ai-search/`
- `modules/appops/vectorstore/vertex-ai-search/`

## Lane Ownership
This contract serves the **INDEXED lane** only.
