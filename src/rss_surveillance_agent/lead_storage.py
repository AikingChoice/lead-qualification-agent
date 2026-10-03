from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text


class LeadStorage:
    """Idempotency, audit state, and durable daily LLM attempt budget."""

    def __init__(self, database_url: str, *, initialize_schema: bool = True):
        self.engine = create_engine(database_url, pool_pre_ping=True, connect_args={"timeout": 30} if database_url.startswith("sqlite") else {})
        if initialize_schema:
            self.initialize()

    def initialize(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS lead_assessments (
            idempotency_key TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            request_id TEXT NOT NULL,
            status TEXT NOT NULL,
            report_json TEXT,
            note_id TEXT,
            hubspot_write_started BOOLEAN NOT NULL DEFAULT FALSE,
            lease_until TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lead_model_usage (
            usage_day TEXT PRIMARY KEY,
            calls INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lead_assessments_created ON lead_assessments(created_at);
        """
        with self.engine.begin() as conn:
            for statement in ddl.split(";"):
                if statement.strip():
                    conn.exec_driver_sql(statement)

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

    @staticmethod
    def key_hash(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def claim(self, key: str, request_payload: dict[str, Any], *, lease_seconds: int = 180) -> dict[str, Any]:
        key_hash = self.key_hash(key)
        request_json = json.dumps(request_payload, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(request_json.encode()).hexdigest()
        now = datetime.now(UTC)
        now_text = now.isoformat()
        lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.engine.begin() as conn:
            inserted = conn.execute(text("""INSERT INTO lead_assessments
                (idempotency_key, request_hash, request_id, status, lease_until, created_at, updated_at)
                VALUES (:key, :request_hash, :request_id, 'processing', :lease, :now, :now)
                ON CONFLICT(idempotency_key) DO NOTHING"""), {
                "key": key_hash, "request_hash": request_hash,
                "request_id": uuid4().hex, "lease": lease, "now": now_text,
            })
            if inserted.rowcount == 1:
                row = conn.execute(text("SELECT * FROM lead_assessments WHERE idempotency_key=:key"), {"key": key_hash}).mappings().one()
                return {**dict(row), "decision": "claimed"}
            row = conn.execute(text("SELECT * FROM lead_assessments WHERE idempotency_key=:key"), {"key": key_hash}).mappings().one()
            result = dict(row)
            if result["request_hash"] != request_hash:
                return {**result, "decision": "key_conflict"}
            if result["status"] in {"completed", "review_required"}:
                return {**result, "decision": "replay"}
            if result["status"] == "processing" and result["lease_until"] and result["lease_until"] > now_text:
                return {**result, "decision": "in_progress"}
            if result["hubspot_write_started"]:
                conn.execute(text("""UPDATE lead_assessments SET status='review_required',
                    error_code='hubspot_outcome_unknown', updated_at=:now WHERE idempotency_key=:key"""),
                    {"now": now_text, "key": key_hash})
                return {**result, "status": "review_required", "error_code": "hubspot_outcome_unknown", "decision": "replay"}
            conn.execute(text("""UPDATE lead_assessments SET status='processing', lease_until=:lease,
                error_code=NULL, updated_at=:now WHERE idempotency_key=:key"""),
                {"lease": lease, "now": now_text, "key": key_hash})
            return {**result, "status": "processing", "decision": "claimed"}

    def reserve_model_call(self, daily_limit: int) -> bool:
        day = datetime.now(UTC).date().isoformat()
        with self.engine.begin() as conn:
            result = conn.execute(text("""INSERT INTO lead_model_usage(usage_day,calls) VALUES (:day,1)
                ON CONFLICT(usage_day) DO UPDATE SET calls=lead_model_usage.calls+1
                WHERE lead_model_usage.calls < :limit"""), {"day": day, "limit": daily_limit})
        return result.rowcount == 1

    def save_report(self, key: str, report: dict[str, Any], *, status: str, error_code: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        with self.engine.begin() as conn:
            conn.execute(text("""UPDATE lead_assessments SET report_json=:report, status=:status,
                error_code=:error, lease_until=:lease, updated_at=:now WHERE idempotency_key=:key"""), {
                "report": json.dumps(report, ensure_ascii=False), "status": status,
                "error": error_code, "lease": (datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
                "now": now, "key": self.key_hash(key),
            })

    def mark_hubspot_write_started(self, key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE lead_assessments SET hubspot_write_started=TRUE, updated_at=:now WHERE idempotency_key=:key"),
                         {"now": datetime.now(UTC).isoformat(), "key": self.key_hash(key)})

    def complete(self, key: str, note_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("""UPDATE lead_assessments SET status='completed', note_id=:note,
                error_code=NULL, lease_until=NULL, updated_at=:now WHERE idempotency_key=:key"""), {
                "note": note_id, "now": datetime.now(UTC).isoformat(), "key": self.key_hash(key),
            })

    def fail(self, key: str, code: str, *, review: bool = False) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("""UPDATE lead_assessments SET status=:status, error_code=:error,
                lease_until=NULL, updated_at=:now WHERE idempotency_key=:key"""), {
                "status": "review_required" if review else "failed", "error": code,
                "now": datetime.now(UTC).isoformat(), "key": self.key_hash(key),
            })

    def close(self) -> None:
        self.engine.dispose()

