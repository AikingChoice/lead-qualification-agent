from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.engine import URL


class ScorePolicy(BaseModel):
    """Editable demo rules. Replace with the customer's approved ICP before use."""

    target_industries: list[str] = Field(default_factory=lambda: ["logistics", "warehouse", "manufacturing", "robotics"])
    min_employee_count: int = Field(default=50, ge=1)
    industry_points: int = Field(default=30, ge=0, le=100)
    company_size_points: int = Field(default=20, ge=0, le=100)
    relevant_product_points: int = Field(default=25, ge=0, le=100)
    growth_signal_points: int = Field(default=15, ge=0, le=100)
    evidence_points: int = Field(default=10, ge=0, le=100)
    risk_penalty: int = Field(default=10, ge=0, le=100)
    hot_threshold: int = Field(default=70, ge=0, le=100)
    warm_threshold: int = Field(default=40, ge=0, le=100)


class Settings(BaseModel):
    database_url: str = "sqlite+pysqlite:///./lead-agent.sqlite3"
    api_bearer_token: SecretStr
    openai_api_key: SecretStr
    openai_model: str = "gpt-4o-mini"
    hubspot_access_token: SecretStr
    hubspot_api_version: str = "2026-09"
    daily_model_call_limit: int = Field(default=100, ge=1)
    max_website_bytes: int = Field(default=1_500_000, ge=1024)
    website_timeout_seconds: float = Field(default=12.0, ge=1, le=60)
    score_policy_path: Path = Path("config/qualification_policy.json")
    environment: str = "development"

    @classmethod
    def from_env(cls) -> Settings:
        database_url = os.getenv("DATABASE_URL")
        if not database_url and os.getenv("DB_HOST"):
            database_url = URL.create(
                "postgresql+psycopg",
                username=os.getenv("DB_USER", "lead_agent"),
                password=os.getenv("DB_PASSWORD", ""),
                host=os.environ["DB_HOST"],
                port=int(os.getenv("DB_PORT", "5432")),
                database=os.getenv("DB_NAME", "lead_agent"),
            ).render_as_string(hide_password=False)
        values: dict[str, object] = {
            "database_url": database_url or cls.model_fields["database_url"].default,
            "api_bearer_token": os.getenv("API_BEARER_TOKEN", "local-development-token-change-me"),
            "openai_api_key": os.getenv("OPENAI_API_KEY", "replace_me"),
            "openai_model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "hubspot_access_token": os.getenv("HUBSPOT_ACCESS_TOKEN", "replace_me"),
            "hubspot_api_version": os.getenv("HUBSPOT_API_VERSION", "2026-09"),
            "daily_model_call_limit": int(os.getenv("DAILY_MODEL_CALL_LIMIT", "100")),
            "max_website_bytes": int(os.getenv("MAX_WEBSITE_BYTES", "1500000")),
            "website_timeout_seconds": float(os.getenv("WEBSITE_TIMEOUT_SECONDS", "12")),
            "score_policy_path": Path(os.getenv("SCORE_POLICY_PATH", "config/qualification_policy.json")),
            "environment": os.getenv("APP_ENV", "development"),
        }
        return cls.model_validate(values)

    def load_policy(self) -> ScorePolicy:
        if not self.score_policy_path.exists():
            return ScorePolicy()
        return ScorePolicy.model_validate_json(self.score_policy_path.read_text(encoding="utf-8"))

