import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from rss_surveillance_agent.analyzers import KeywordAnalyzer
from rss_surveillance_agent.lead_storage import LeadStorage
from rss_surveillance_agent.models import FeedEntry, RuleConfig
from rss_surveillance_agent.pipeline import SurveillancePipeline
from rss_surveillance_agent.storage import PostgresEntryStore


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration URL not set"
)
def test_postgres_transaction_cursor_quota_and_outbox_lease():
    fixture = Path(__file__).parent / "fixtures" / "entries.json"
    entry = FeedEntry.model_validate(
        json.loads(fixture.read_text(encoding="utf-8"))[0]
    ).model_copy(update={"id": int(time.time() * 1_000_000)})
    database_url = os.environ["TEST_DATABASE_URL"]
    lead_key = f"postgres-integration-{uuid4().hex}"

    engine = create_engine(database_url)
    try:
        with PostgresEntryStore(database_url) as store:
            result = SurveillancePipeline(KeywordAnalyzer(), store).process(entry)
            assert result.signal is not None
            assert store.save(entry, result, RuleConfig(), "duplicate") is False

            store.advance_cursor("postgres-integration", entry.id)
            assert store.get_cursor("postgres-integration") == entry.id

            first_claim = store.claim_notifications(lease_seconds=120)
            assert [row["entry_id"] for row in first_claim] == [entry.id]
            # A committed lease prevents another worker from claiming the same row.
            assert store.claim_notifications(lease_seconds=120) == []
            store.mark_notification_delivered(entry.id)

            assert store.reserve_daily_model_call(1)
            assert not store.reserve_daily_model_call(1)
            assert store.get_audit(entry.id) is not None
            assert store.outbox_counts()["delivered"] == 1

        lead_store = LeadStorage(database_url, initialize_schema=False)
        try:
            request = {"contact_id": "123", "company_name": "Integration", "website": "https://example.com/"}
            assert lead_store.claim(lead_key, request)["decision"] == "claimed"
            lead_store.save_report(lead_key, {"score": 12}, status="review_required")
            assert lead_store.claim(lead_key, request)["decision"] == "replay"
        finally:
            lead_store.close()
    finally:
        with engine.begin() as connection:
            if "lead_key" in locals():
                connection.execute(
                    text("DELETE FROM lead_assessments WHERE idempotency_key=:key"),
                    {"key": LeadStorage.key_hash(lead_key)},
                )
            connection.execute(
                text("DELETE FROM ingestion_cursors WHERE source=:source"),
                {"source": "postgres-integration"},
            )
            connection.execute(
                text("DELETE FROM model_usage WHERE usage_day=:usage_day"),
                {"usage_day": time.strftime("%Y-%m-%d", time.gmtime())},
            )
            connection.execute(
                text("DELETE FROM notification_outbox WHERE entry_id=:entry_id"),
                {"entry_id": entry.id},
            )
            connection.execute(
                text("DELETE FROM notification_log WHERE entry_id=:entry_id"),
                {"entry_id": entry.id},
            )
            connection.execute(
                text("DELETE FROM processed_entries WHERE entry_id=:entry_id"),
                {"entry_id": entry.id},
            )
        engine.dispose()
