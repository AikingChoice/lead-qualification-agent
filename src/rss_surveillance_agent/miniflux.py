from collections.abc import Iterator
from datetime import UTC, datetime
from html.parser import HTMLParser

import httpx

from .models import FeedEntry


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._hidden_depth += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)


class MinifluxSource:
    """Read entries from one Miniflux instance in ascending ID order."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        timeout: float = 15,
        page_size: int = 100,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = api_token
        self._timeout = timeout
        self._page_size = page_size

    def fetch_after(
        self, entry_id: int, *, published_after: int | None = None
    ) -> Iterator[FeedEntry]:
        after_id = entry_id
        with httpx.Client(
            base_url=self._base_url,
            headers={"X-Auth-Token": self._token},
            timeout=self._timeout,
        ) as client:
            while True:
                params = {
                    "after_entry_id": after_id,
                    "order": "id",
                    "direction": "asc",
                    "limit": self._page_size,
                }
                if published_after is not None:
                    params["published_after"] = published_after
                response = client.get(
                    "/v1/entries",
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
                raw_entries = payload.get("entries", [])
                if not raw_entries:
                    return
                entries = [self._to_feed_entry(item) for item in raw_entries]
                entries.sort(key=lambda item: item.id)
                for entry in entries:
                    if entry.id <= after_id:
                        continue
                    after_id = entry.id
                    yield entry
                if len(raw_entries) < self._page_size:
                    return

    @staticmethod
    def _to_feed_entry(item: dict) -> FeedEntry:
        published = item.get("published_at") or item.get("created_at")
        if isinstance(published, str):
            timestamp = datetime.fromisoformat(published)
        else:
            timestamp = datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        content = item.get("content") or item.get("summary") or item.get("title")
        extractor = _TextExtractor()
        extractor.feed(str(content))
        plain_text = " ".join(" ".join(extractor.parts).split())
        return FeedEntry(
            id=int(item["id"]),
            title=(item.get("title") or "Untitled article").strip(),
            content=plain_text[:12000] or "[No article content supplied by feed]",
            url=item["url"],
            published_at=timestamp,
        )
