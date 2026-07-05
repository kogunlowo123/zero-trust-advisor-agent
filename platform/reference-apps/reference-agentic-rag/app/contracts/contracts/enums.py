"""Data lane and strategy enums."""
from enum import Enum


class DataLane(str, Enum):
    INDEXED = "indexed"
    LIVE = "live"
    STRUCTURED = "structured"


class RetrievalStrategy(str, Enum):
    BM25 = "bm25"
    DENSE = "dense"
    HYBRID = "hybrid"
    GRAPH = "graph"
    PARENT_DOC = "parent_doc"


class RerankStrategy(str, Enum):
    NONE = "none"
    BOOST = "boost"
    CROSS_ENCODER = "cross_encoder"
    LLM_RERANK = "llm_rerank"
    CASCADE = "cascade"


class QueryStrategy(str, Enum):
    PASSTHROUGH = "passthrough"
    REWRITE = "rewrite"
    EXPAND = "expand"
    HYDE = "hyde"
    DECOMPOSE = "decompose"
