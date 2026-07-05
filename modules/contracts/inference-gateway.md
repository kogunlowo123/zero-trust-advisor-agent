# Inference Gateway Contract (ADR-0004)

## Promise
Every cloud implementation provides access to LLM and embedding models for Zero Trust Advisor Agent:
- Chat completion (streaming and non-streaming)
- Text embedding generation
- Cross-encoder reranking (optional)
- Token counting and cost tracking

## Interface
| Operation          | Input                      | Output              |
|-------------------|----------------------------|----------------------|
| `chat`            | messages[], model, params  | response, usage      |
| `chat_stream`     | messages[], model, params  | async_iterator       |
| `embed`           | texts[], model             | embeddings[]         |
| `rerank`          | query, documents[], model  | scored_documents[]   |
| `count_tokens`    | text, model                | token_count          |

## Implementors
- `modules/appops/inference-gateway/aws-bedrock/` — Claude, Titan
- `modules/appops/inference-gateway/azure-openai/` — GPT-4o, text-embedding-3
- `modules/appops/inference-gateway/vertex-ai/` — Gemini, text-embedding
- `modules/appops/inference-gateway/openai/` — Direct OpenAI API
