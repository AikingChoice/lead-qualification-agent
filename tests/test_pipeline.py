import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from rss_surveillance_agent.analyzers import KeywordAnalyzer
from rss_surveillance_agent.models import (
    ArticleCategory,
    Decision,
    FeedEntry,
    ProcessingStatus,
    RuleConfig,
    RuleDisposition,
)
from rss_surveillance_agent.pipeline import (
    InMemoryProcessedEntryStore,
    SurveillancePipeline,
)
from rss_surveillance_agent.storage import SQLiteEntryStore


def load_entries() -> list[FeedEntry]:
    path = Path(__file__).parent / "fixtures" / "entries.json"
    return [
        FeedEntry.model_validate(item)
        for item in json.loads(path.read_text(encoding="utf-8"))
    ]


def build_pipeline() -> SurveillancePipeline:
    return SurveillancePipeline(
        analyzer=KeywordAnalyzer(),
        store=InMemoryProcessedEntryStore(),
    )


def test_relevant_entry_creates_review_signal() -> None:
    relevant_entry = load_entries()[0]

    result = build_pipeline().process(relevant_entry)

    assert result.status is ProcessingStatus.PROCESSED
    assert result.analysis is not None
    assert result.analysis.relevant is True
    assert result.signal is not None
    assert result.signal.decision is Decision.REVIEW
    assert result.signal.source_entry_id == relevant_entry.id


def test_irrelevant_entry_does_not_create_signal() -> None:
    irrelevant_entry = load_entries()[1]

    result = build_pipeline().process(irrelevant_entry)

    assert result.analysis is not None
    assert result.analysis.relevant is False
    assert result.signal is None


def test_duplicate_entry_is_not_processed_twice() -> None:
    entry = load_entries()[0]
    pipeline = build_pipeline()

    first_result = pipeline.process(entry)
    second_result = pipeline.process(entry)

    assert first_result.status is ProcessingStatus.PROCESSED
    assert second_result.status is ProcessingStatus.DUPLICATE
    assert second_result.analysis is None
    assert second_result.signal is None


class CountingAnalyzer(KeywordAnalyzer):
    def __init__(self):
        self.calls = 0

    def analyze(self, entry):
        self.calls += 1
        return super().analyze(entry)


class BrokenAnalyzer:
    def __init__(self, failure):
        self.failure = failure

    def analyze(self, entry):
        if self.failure == "timeout":
            raise TimeoutError("Test timeout")
        return {"relevant": True, "summary": ["incomplete"]}


def test_restart_preserves_audit_and_prevents_reanalysis_and_duplicate_alert(tmp_path):
    db = tmp_path / "state.sqlite3"
    entry = load_entries()[0]
    analyzer = CountingAnalyzer()
    with SQLiteEntryStore(db) as store:
        SurveillancePipeline(analyzer, store).process(entry)
    with SQLiteEntryStore(db) as store:
        result = SurveillancePipeline(analyzer, store).process(entry)
        assert result.status is ProcessingStatus.DUPLICATE
        assert analyzer.calls == 1
        assert store.counts() == {"articles": 1, "notifications": 1}
        audit = store.get_audit(entry.id)
        assert json.loads(audit["article_json"]) == entry.model_dump(mode="json")
        saved = json.loads(audit["result_json"])
        assert saved["analysis"]["category"] == "earnings"
        assert saved["signal"]["source_url"] == str(entry.url)
        assert saved["rule_decision"]["disposition"] == "allow"
        assert "earnings" in json.loads(audit["rules_json"])["allowed_categories"]
        assert audit["analyzer_name"] == "CountingAnalyzer"
        assert audit["recorded_at"].endswith("+00:00")


@pytest.mark.parametrize("categories", [{ArticleCategory.CLINICAL}, set()])
def test_category_filter_overrides_relevant_analysis_and_is_audited(tmp_path, categories):
    entry = load_entries()[0]
    with SQLiteEntryStore(tmp_path / "state.sqlite3") as store:
        result = SurveillancePipeline(
            KeywordAnalyzer(), store, RuleConfig(allowed_categories=categories)
        ).process(entry)
        assert result.analysis.relevant is True
        assert result.signal is None
        assert result.rule_decision.disposition is RuleDisposition.REJECT
        assert "not allowed" in result.rule_decision.reason
        assert store.get_audit(entry.id) is not None
        assert store.counts() == {"articles": 1, "notifications": 0}


def test_irrelevant_article_is_audited_without_alert(tmp_path):
    entry = load_entries()[1]
    with SQLiteEntryStore(tmp_path / "state.sqlite3") as store:
        result = SurveillancePipeline(KeywordAnalyzer(), store).process(entry)
        assert result.rule_decision.disposition is RuleDisposition.REJECT
        assert result.analysis.relevant is False
        assert store.get_audit(entry.id) is not None
        assert store.counts() == {"articles": 1, "notifications": 0}


@pytest.mark.parametrize("failure,code", [
    ("timeout", "analysis_timeout"), ("invalid", "invalid_analysis"),
])
def test_analysis_failure_remains_unknown_and_creates_one_review_alert(tmp_path, failure, code):
    db = tmp_path / "state.sqlite3"
    entry = load_entries()[0]
    with SQLiteEntryStore(db) as store:
        result = SurveillancePipeline(BrokenAnalyzer(failure), store).process(entry)
        assert result.status is ProcessingStatus.REVIEW_REQUIRED
        assert result.analysis is None
        assert result.signal is None
        assert result.error_code == code
        assert result.rule_decision.disposition is RuleDisposition.REVIEW
        assert json.loads(store.get_audit(entry.id)["result_json"])["error_code"] == code
    with SQLiteEntryStore(db) as store:
        result = SurveillancePipeline(KeywordAnalyzer(), store).process(entry)
        assert result.status is ProcessingStatus.DUPLICATE
        assert store.counts() == {"articles": 1, "notifications": 1}


def test_notification_write_failure_rolls_back_audit_and_allows_retry(tmp_path):
    db = tmp_path / "state.sqlite3"
    entry = load_entries()[0]
    with SQLiteEntryStore(db) as store:
        with sqlite3.connect(db) as connection:
            connection.execute("""CREATE TRIGGER fail_alert BEFORE INSERT ON notification_log
                BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
        pipeline = SurveillancePipeline(KeywordAnalyzer(), store)
        with pytest.raises(IntegrityError, match="simulated write failure"):
            pipeline.process(entry)
        assert store.counts() == {"articles": 0, "notifications": 0}
        with sqlite3.connect(db) as connection:
            connection.execute("DROP TRIGGER fail_alert")
        assert pipeline.process(entry).signal is not None
        assert store.counts() == {"articles": 1, "notifications": 1}


def test_database_conflict_cannot_create_second_result_or_notification(tmp_path):
    db = tmp_path / "state.sqlite3"
    entry = load_entries()[0]
    with SQLiteEntryStore(db) as first, SQLiteEntryStore(db) as second:
        assert not second.contains(entry.id)
        result = SurveillancePipeline(KeywordAnalyzer(), first).process(entry)
        assert second.save(entry, result, RuleConfig(), "OtherAnalyzer") is False
        assert first.counts() == {"articles": 1, "notifications": 1}
        assert first.get_audit(entry.id)["analyzer_name"] == "KeywordAnalyzer"
