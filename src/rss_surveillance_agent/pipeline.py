from datetime import UTC, datetime

from pydantic import ValidationError

from .analyzers import AnalyzerFailure, ArticleAnalyzer, RetryableAnalyzerFailure
from .models import (
    ArticleAnalysis,
    Decision,
    FeedEntry,
    ProcessingResult,
    ProcessingStatus,
    ReviewSignal,
    RuleConfig,
    RuleDecision,
    RuleDisposition,
)
from .storage import DatabaseEntryStore, SQLiteEntryStore
from .workflow import build_analysis_graph


class InMemoryProcessedEntryStore(SQLiteEntryStore):
    def __init__(self) -> None:
        super().__init__(":memory:")


class SurveillancePipeline:
    def __init__(
        self,
        analyzer: ArticleAnalyzer,
        store: DatabaseEntryStore,
        config: RuleConfig | None = None,
        daily_model_limit: int = 200,
    ) -> None:
        self._analyzer = analyzer
        self._store = store
        self._config = (config or RuleConfig()).model_copy(deep=True)
        self._daily_model_limit = daily_model_limit
        self._workflow = build_analysis_graph(self._analyzer, self._config)

    def process(self, entry: FeedEntry) -> ProcessingResult:
        if self._store.contains(entry.id):
            return ProcessingResult(status=ProcessingStatus.DUPLICATE)

        prior_failure = self._store.get_analysis_failure(entry.id)
        if prior_failure is not None:
            if prior_failure["attempts"] >= 8:
                result = self._review_required("analysis_retry_limit_reached")
                return self._save(entry, result)
            next_attempt = prior_failure.get("next_attempt_at")
            if next_attempt and datetime.fromisoformat(next_attempt) > datetime.now(UTC):
                return ProcessingResult(
                    status=ProcessingStatus.DEFERRED,
                    error_code="analysis_retry_backoff",
                )

        if (
            getattr(self._analyzer, "uses_external_api", False)
            and not self._store.reserve_daily_model_call(self._daily_model_limit)
        ):
            return ProcessingResult(
                status=ProcessingStatus.DEFERRED,
                error_code="daily_model_limit_reached",
            )

        try:
            workflow_state = self._workflow.invoke({"entry": entry})
            analysis = ArticleAnalysis.model_validate(workflow_state["analysis"])
            rule_decision = RuleDecision.model_validate(
                workflow_state["rule_decision"]
            )
        except RetryableAnalyzerFailure as exc:
            attempts = self._store.record_analysis_failure(entry.id, type(exc).__name__)
            if attempts >= 8:
                result = self._review_required("analysis_retry_limit_reached")
                return self._save(entry, result)
            return ProcessingResult(
                status=ProcessingStatus.DEFERRED,
                error_code="transient_provider_failure",
            )
        except (TimeoutError, ValidationError, AnalyzerFailure) as exc:
            error_code = "analysis_timeout" if isinstance(exc, TimeoutError) else (
                "invalid_analysis" if isinstance(exc, ValidationError)
                else "analysis_provider_error"
            )
            result = ProcessingResult(
                status=ProcessingStatus.REVIEW_REQUIRED,
                error_code=error_code,
                rule_decision=RuleDecision(
                    disposition=RuleDisposition.REVIEW,
                    reason="Analysis unavailable; manual review required.",
                ),
            )
            return self._save(entry, result)

        signal = None

        if rule_decision.disposition is RuleDisposition.ALLOW:
            signal = ReviewSignal(
                decision=Decision.REVIEW,
                confidence=analysis.confidence,
                reason=f"Relevant {analysis.category.value} article requires review.",
                source_entry_id=entry.id,
                source_url=entry.url,
            )

        result = ProcessingResult(
            status=ProcessingStatus.PROCESSED,
            analysis=analysis,
            signal=signal,
            rule_decision=rule_decision,
        )
        return self._save(entry, result)

    @staticmethod
    def _review_required(error_code: str) -> ProcessingResult:
        return ProcessingResult(
            status=ProcessingStatus.REVIEW_REQUIRED,
            error_code=error_code,
            rule_decision=RuleDecision(
                disposition=RuleDisposition.REVIEW,
                reason="Analysis retries exhausted; manual review required.",
            ),
        )

    def _save(self, entry: FeedEntry, result: ProcessingResult) -> ProcessingResult:
        saved = self._store.save(
            entry, result, self._config, type(self._analyzer).__name__
        )
        if not saved:
            return ProcessingResult(status=ProcessingStatus.DUPLICATE)
        return result
