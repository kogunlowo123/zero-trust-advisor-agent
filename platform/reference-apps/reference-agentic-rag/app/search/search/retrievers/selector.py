"""Retriever selector — picks the right retriever based on strategy."""
from app.contracts.enums import RetrievalStrategy
from app.search.ports import VectorStorePort, EmbeddingPort
from app.search.retrievers.bm25 import BM25Retriever
from app.search.retrievers.dense import DenseRetriever
from app.search.retrievers.hybrid import HybridRetriever


def select_retriever(strategy: RetrievalStrategy, store: VectorStorePort, embedder: EmbeddingPort | None = None):
    if strategy == RetrievalStrategy.BM25:
        return BM25Retriever(store)
    elif strategy == RetrievalStrategy.DENSE and embedder:
        return DenseRetriever(store, embedder)
    elif strategy == RetrievalStrategy.HYBRID and embedder:
        return HybridRetriever(store, embedder)
    else:
        return BM25Retriever(store)
