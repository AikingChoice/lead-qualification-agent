
import httpx
from pydantic import ValidationError

from .models import ProcessingResult
from .storage import DatabaseEntryStore


class SlackWebhookNotifier:
    def __init__(self, webhook_url: str, timeout: float = 10) -> None:
        self._webhook_url = webhook_url
        self._timeout = timeout

    def send(self, entry_id: int, result: ProcessingResult) -> None:
        signal = result.signal
        if signal is not None:
            text = (
                f"Review article #{entry_id}: {signal.reason}\n"
                f"{signal.source_url} (confidence {signal.confidence:.0%})"
            )
        else:
            text = f"Article #{entry_id} needs manual review: {result.error_code}"
        response = httpx.post(
            self._webhook_url, json={"text": text}, timeout=self._timeout
        )
        response.raise_for_status()


def deliver_pending(store: DatabaseEntryStore, notifier: SlackWebhookNotifier) -> dict:
    sent = 0
    failed = 0
    for row in store.claim_notifications():
        try:
            result = ProcessingResult.model_validate_json(row["payload_json"])
            notifier.send(row["entry_id"], result)
        except (ValidationError, httpx.HTTPError) as exc:
            store.mark_notification_failed(row["entry_id"], type(exc).__name__)
            failed += 1
            continue
        store.mark_notification_delivered(row["entry_id"])
        sent += 1
    return {"sent": sent, "failed": failed}
