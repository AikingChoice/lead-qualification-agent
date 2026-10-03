import json
import sqlite3
from pathlib import Path

from rss_surveillance_agent.analyzers import (
    KeywordAnalyzer,
    OpenAIAnalyzer,
    RetryableAnalyzerFailure,
)
from rss_surveillance_agent.miniflux import MinifluxSource
from rss_surveillance_agent.models import FeedEntry, ProcessingStatus
from rss_surveillance_agent.pipeline import SurveillancePipeline
from rss_surveillance_agent.storage import SQLiteEntryStore
from rss_surveillance_agent.worker import process_batch


def load_entry() -> FeedEntry:
    path = Path(__file__).parent / "fixtures" / "entries.json"
    return FeedEntry.model_validate(json.loads(path.read_text())[0])


def test_miniflux_mapping_strips_html_and_normalizes_timestamp():
    entry = MinifluxSource._to_feed_entry({
        "id": 12,
        "title": "News",
        "content": "<p>Revenue rose</p><script>ignore()</script>",
        "url": "https://example.com/news",
        "published_at": "2026-09-19T08:00:00Z",
    })

    assert entry.id == 12
    assert entry.content == "Revenue rose"
    assert entry.published_at.tzinfo is not None


def test_miniflux_paginates_in_id_order_and_sends_first_run_filter(monkeypatch):
    requests = []

    def raw_entry(entry_id):
        return {
            "id": entry_id,
            "title": f"News {entry_id}",
            "content": "Quarterly revenue update",
            "url": f"https://example.com/{entry_id}",
            "published_at": "2026-09-19T08:00:00Z",
        }

    pages = [
        {"entries": [raw_entry(10), raw_entry(11)]},
        {"entries": [raw_entry(12)]},
    ]

    class Response:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _path, *, params):
            requests.append((self.kwargs, params.copy()))
            return Response(pages.pop(0))

    monkeypatch.setattr("rss_surveillance_agent.miniflux.httpx.Client", Client)
    entries = list(MinifluxSource("https://feed.example", "secret", page_size=2).fetch_after(
        0, published_after=123456
    ))

    assert [entry.id for entry in entries] == [10, 11, 12]
    assert requests[0][0]["headers"]["X-Auth-Token"] == "secret"
    assert requests[0][1]["published_after"] == 123456
    assert requests[1][1]["after_entry_id"] == 11


def test_openai_analyzer_bounds_request_retries_and_output_tokens(monkeypatch):
    from types import SimpleNamespace

    from rss_surveillance_agent.models import ArticleAnalysis, ArticleCategory

    captured = {}
    expected = ArticleAnalysis(
        relevant=True,
        category=ArticleCategory.EARNINGS,
        summary=["Revenue grew", "Company reports higher profit"],
        confidence=0.8,
    )

    class Completions:
        def parse(self, **kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(refusal=None, parsed=expected)
                    )
                ]
            )

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr("openai.OpenAI", Client)
    analyzer = OpenAIAnalyzer("test-key")

    assert analyzer.analyze(load_entry()) == expected
    assert captured["client"]["max_retries"] == 0
    assert captured["request"]["max_completion_tokens"] == 300
    assert captured["request"]["temperature"] == 0


def test_worker_advances_durable_cursor_and_outbox_is_deliverable(tmp_path):
    entry = load_entry()
    database = tmp_path / "worker.sqlite3"
    with SQLiteEntryStore(database) as store:
        pipeline = SurveillancePipeline(KeywordAnalyzer(), store)
        result = process_batch("miniflux", [entry], pipeline, store)
        assert result["processed_this_run"] == 1
        assert store.get_cursor("miniflux") == entry.id
        pending = store.pending_notifications()
        assert len(pending) == 1
        assert json.loads(pending[0]["payload_json"])["signal"]["source_entry_id"] == entry.id

    # Simulates a crash after the audit transaction but before cursor advancement.
    with SQLiteEntryStore(tmp_path / "crash.sqlite3") as store:
        pipeline = SurveillancePipeline(KeywordAnalyzer(), store)
        pipeline.process(entry)
        result = process_batch("miniflux", [entry], pipeline, store)
        assert result["duplicate"] == 1
        assert store.get_cursor("miniflux") == entry.id
        assert len(store.pending_notifications()) == 1


def test_outbox_lease_recovers_abandoned_delivery(tmp_path):
    entry = load_entry()
    with SQLiteEntryStore(tmp_path / "outbox.sqlite3") as store:
        SurveillancePipeline(KeywordAnalyzer(), store).process(entry)

        first_claim = store.claim_notifications(lease_seconds=0)
        assert len(first_claim) == 1
        assert first_claim[0]["attempts"] == 1

        recovered_claim = store.claim_notifications(lease_seconds=60)
        assert len(recovered_claim) == 1
        assert recovered_claim[0]["attempts"] == 2

        store.mark_notification_failed(entry.id, "temporary timeout")
        assert store.claim_notifications() == []
        store.mark_notification_delivered(entry.id)
        assert store.pending_notifications() == []


def test_daily_model_quota_defers_without_advancing_article(tmp_path):
    entries = [load_entry(), load_entry().model_copy(update={"id": 102})]

    class ExternalAnalyzer(KeywordAnalyzer):
        uses_external_api = True

    with SQLiteEntryStore(tmp_path / "quota.sqlite3") as store:
        pipeline = SurveillancePipeline(
            ExternalAnalyzer(), store, daily_model_limit=1
        )
        first = pipeline.process(entries[0])
        second = pipeline.process(entries[1])

        assert first.status is ProcessingStatus.PROCESSED
        assert second.status is ProcessingStatus.DEFERRED
        assert not store.contains(entries[1].id)


def test_operator_can_requeue_dead_letter(tmp_path):
    entry = load_entry()
    database = tmp_path / "dead-letter.sqlite3"
    with SQLiteEntryStore(database) as store:
        SurveillancePipeline(KeywordAnalyzer(), store).process(entry)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE notification_outbox SET status='dead_letter' WHERE entry_id=?",
                (entry.id,),
            )
        assert store.outbox_counts()["dead_letter"] == 1
        assert store.retry_dead_letters([entry.id]) == 1
        assert store.outbox_counts()["pending"] == 1
        assert store.outbox_counts()["dead_letter"] == 0


def test_transient_model_failures_back_off_then_become_manual_review(tmp_path):
    entry = load_entry()

    class UnavailableAnalyzer:
        uses_external_api = True

        def analyze(self, _entry):
            raise RetryableAnalyzerFailure("upstream unavailable")

    database = tmp_path / "retry.sqlite3"
    with SQLiteEntryStore(database) as store:
        pipeline = SurveillancePipeline(UnavailableAnalyzer(), store, daily_model_limit=20)
        first = pipeline.process(entry)
        assert first.status is ProcessingStatus.DEFERRED
        assert pipeline.process(entry).status is ProcessingStatus.DEFERRED

        for _ in range(7):
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE analysis_failures SET next_attempt_at='2000-01-01T00:00:00+00:00'"
                )
            result = pipeline.process(entry)

        assert result.status is ProcessingStatus.REVIEW_REQUIRED
        assert result.error_code == "analysis_retry_limit_reached"
        assert result.signal is None
        assert store.outbox_counts()["pending"] == 1
