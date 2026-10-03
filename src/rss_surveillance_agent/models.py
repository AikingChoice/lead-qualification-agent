from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl, model_validator


class ArticleCategory(StrEnum):
    EARNINGS = "earnings"
    CLINICAL = "clinical"
    REGULATORY = "regulatory"
    MERGER_ACQUISITION = "merger_acquisition"
    OTHER = "other"


class Decision(StrEnum):
    IGNORE = "ignore"
    REVIEW = "review"


class ProcessingStatus(StrEnum):
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    REVIEW_REQUIRED = "review_required"
    DEFERRED = "deferred"


class RuleDisposition(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    REVIEW = "review"


class RuleConfig(BaseModel):
    allowed_categories: set[ArticleCategory] = Field(
        default_factory=lambda: set(ArticleCategory) - {ArticleCategory.OTHER}
    )


class RuleDecision(BaseModel):
    disposition: RuleDisposition
    reason: str


class FeedEntry(BaseModel):
    id: int = Field(gt=0)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    url: HttpUrl
    published_at: datetime


class ArticleAnalysis(BaseModel):
    relevant: bool
    category: ArticleCategory
    summary: list[str]
    confidence: float

    @model_validator(mode="after")
    def validate_analysis(self) -> "ArticleAnalysis":
        if len(self.summary) != 2:
            raise ValueError("summary must contain exactly two items")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        return self


class ReviewSignal(BaseModel):
    decision: Decision
    confidence: float = Field(ge=0, le=1)
    reason: str
    source_entry_id: int
    source_url: HttpUrl


class ProcessingResult(BaseModel):
    status: ProcessingStatus
    analysis: ArticleAnalysis | None = None
    signal: ReviewSignal | None = None
    rule_decision: RuleDecision | None = None
    error_code: str | None = None


class OutboxMessage(BaseModel):
    entry_id: int
    payload: ProcessingResult
    attempts: int = 0
    last_error: str | None = None
