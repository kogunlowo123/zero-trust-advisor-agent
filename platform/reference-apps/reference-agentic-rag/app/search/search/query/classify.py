"""Query classifier — detects data lane intent."""
from app.contracts.enums import DataLane


def classify_query(query: str) -> DataLane:
    q = query.lower()
    live_signals = ["my ", "my calendar", "my email", "my tasks"]
    struct_signals = ["how many", "total", "count", "sum", "average", "group by"]
    if any(s in q for s in live_signals):
        return DataLane.LIVE
    if any(s in q for s in struct_signals):
        return DataLane.STRUCTURED
    return DataLane.INDEXED
