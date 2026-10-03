from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class LeadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: str = Field(pattern=r"^\d{1,80}$")
    company_name: str = Field(min_length=1, max_length=200)
    website: HttpUrl
    email: str | None = Field(default=None, max_length=320)

    @field_validator("contact_id", "company_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class Evidence(BaseModel):
    field: Literal[
        "company_summary", "industry", "employee_count", "relevant_product",
        "recent_growth_signal", "risk_flag",
    ]
    fact: str = Field(min_length=1, max_length=300)
    quote: str = Field(min_length=1, max_length=1000)
    source_url: HttpUrl


class CompanyFacts(BaseModel):
    company_summary: str = Field(min_length=1, max_length=1200)
    industry: str | None = Field(default=None, max_length=200)
    employee_count: int | None = Field(default=None, ge=0, le=10_000_000)
    relevant_product: bool | None = None
    product_summary: str | None = Field(default=None, max_length=1000)
    recent_growth_signals: list[str] = Field(default_factory=list, max_length=10)
    risk_flags: list[str] = Field(default_factory=list, max_length=10)
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)


class Qualification(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class LeadStatus(StrEnum):
    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"


class QualificationReport(BaseModel):
    company_summary: str
    score: int = Field(ge=0, le=100)
    qualification: Qualification
    opportunity_signals: list[str]
    risk_flags: list[str]
    evidence: list[Evidence]
    recommended_action: str
    confidence: float = Field(ge=0, le=1)


class LeadResponse(BaseModel):
    request_id: str
    status: LeadStatus
    report: QualificationReport
    hubspot_note_id: str | None = None
    replayed: bool = False
    error_code: str | None = None
