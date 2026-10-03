from collections.abc import Iterable

from .models import FeedEntry, ProcessingStatus
from .pipeline import SurveillancePipeline
from .storage import DatabaseEntryStore


def process_batch(
    source_name: str,
    entries: Iterable[FeedEntry],
    pipeline: SurveillancePipeline,
    store: DatabaseEntryStore,
    *,
    max_entries: int = 20,
) -> dict[str, int]:
    """Process in source order and checkpoint only after a durable terminal outcome."""
    counts = {status.value: 0 for status in ProcessingStatus}
    seen = 0
    cursor = store.get_cursor(source_name)
    for entry in entries:
        if seen >= max_entries:
            break
        if entry.id <= cursor:
            continue
        result = pipeline.process(entry)
        if result.status is ProcessingStatus.DEFERRED:
            counts[result.status.value] += 1
            break
        # process() returns only after its audit/outbox transaction is committed.
        store.advance_cursor(source_name, entry.id)
        cursor = entry.id
        counts[result.status.value] += 1
        seen += 1
    counts["cursor"] = cursor
    counts["processed_this_run"] = seen
    return counts
