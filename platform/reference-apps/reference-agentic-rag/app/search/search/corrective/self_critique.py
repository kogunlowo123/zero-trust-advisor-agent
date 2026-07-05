"""Self-critique — evaluates retrieval confidence."""
from app.contracts.models import Chunk


class SelfCritique:
    def __init__(self, confidence_threshold: float = 0.5):
        self._threshold = confidence_threshold

    def evaluate(self, query: str, chunks: list[Chunk]) -> dict:
        if not chunks:
            return {"accept": False, "confidence": 0.0, "reason": "No chunks retrieved"}
        avg_score = sum(c.score for c in chunks) / len(chunks)
        return {
            "accept": avg_score >= self._threshold,
            "confidence": round(avg_score, 4),
            "reason": "Sufficient confidence" if avg_score >= self._threshold else "Below threshold",
        }
