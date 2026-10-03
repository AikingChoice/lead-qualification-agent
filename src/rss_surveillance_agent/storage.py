import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.engine import Engine

from .models import FeedEntry, ProcessingResult, RuleConfig


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DatabaseEntryStore:
    """Durable audit, cursor, and leased notification outbox storage."""

    max_notification_attempts = 8

    def __init__(self, database_url: str, *, initialize_schema: bool = True) -> None:
        if database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix(
                "postgresql://"
            )
        connect_args = {"timeout": 30} if database_url.startswith("sqlite") else {}
        self._engine: Engine = create_engine(
            database_url, pool_pre_ping=True, connect_args=connect_args
        )
        if initialize_schema:
            self._initialize_schema()

    @classmethod
    def from_sqlite(cls, path: str | Path) -> "DatabaseEntryStore":
        if str(path) == ":memory:":
            url = "sqlite+pysqlite:///:memory:"
        else:
            absolute = Path(path).resolve().as_posix()
            url = f"sqlite+pysqlite:///{absolute}"
        return cls(url)

    def _initialize_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS processed_entries (
            entry_id BIGINT PRIMARY KEY,
            article_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            rules_json TEXT NOT NULL,
            analyzer_name TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_log (
            entry_id BIGINT PRIMARY KEY REFERENCES processed_entries(entry_id),
            payload_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingestion_cursors (
            source TEXT PRIMARY KEY,
            last_entry_id BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
            entry_id BIGINT PRIMARY KEY REFERENCES processed_entries(entry_id),
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            next_attempt_at TEXT,
            lease_until TEXT
        );
        CREATE TABLE IF NOT EXISTS poll_runs (
            run_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            processed_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT
        );
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_usage (
            usage_day TEXT PRIMARY KEY,
            calls INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analysis_failures (
            entry_id BIGINT PRIMARY KEY,
            attempts INTEGER NOT NULL,
            status TEXT NOT NULL,
            next_attempt_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_ready
            ON notification_outbox(status, next_attempt_at, entry_id);
        CREATE INDEX IF NOT EXISTS idx_poll_runs_started
            ON poll_runs(started_at);
        """
        with self._engine.begin() as connection:
            for statement in schema.split(";"):
                if statement.strip():
                    connection.exec_driver_sql(statement)
            # Additive migration for databases created by the earlier POC schema.
            outbox_columns = {
                column["name"]
                for column in inspect(connection).get_columns("notification_outbox")
            }
            for column, sql_type in (
                ("next_attempt_at", "TEXT"),
                ("lease_until", "TEXT"),
            ):
                if column not in outbox_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE notification_outbox ADD COLUMN {column} {sql_type}"
                    )
            connection.execute(
                text("INSERT INTO schema_version(version, applied_at) "
                     "VALUES (:version, :applied_at) ON CONFLICT(version) DO NOTHING"),
                {"version": 2, "applied_at": _utcnow().isoformat()},
            )
        if self._engine.dialect.name == "sqlite":
            with self._engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                connection.exec_driver_sql("PRAGMA synchronous=NORMAL")
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.exec_driver_sql("PRAGMA busy_timeout=30000")

    def contains(self, entry_id: int) -> bool:
        with self._engine.connect() as connection:
            return connection.execute(
                text("SELECT 1 FROM processed_entries WHERE entry_id=:entry_id"),
                {"entry_id": entry_id},
            ).first() is not None

    def save(
        self,
        entry: FeedEntry,
        result: ProcessingResult,
        config: RuleConfig,
        analyzer_name: str,
    ) -> bool:
        now = _utcnow().isoformat()
        with self._engine.begin() as connection:
            inserted = connection.execute(
                text("""INSERT INTO processed_entries
                    (entry_id, article_json, result_json, rules_json, analyzer_name, recorded_at)
                    VALUES (:entry_id, :article_json, :result_json, :rules_json, :analyzer_name, :recorded_at)
                    ON CONFLICT(entry_id) DO NOTHING"""),
                {
                    "entry_id": entry.id,
                    "article_json": entry.model_dump_json(),
                    "result_json": result.model_dump_json(),
                    "rules_json": config.model_dump_json(),
                    "analyzer_name": analyzer_name,
                    "recorded_at": now,
                },
            )
            if inserted.rowcount == 0:
                return False
            if result.signal is not None or result.error_code is not None:
                payload = result.model_dump_json()
                connection.execute(
                    text("INSERT INTO notification_log VALUES (:entry_id, :payload, :now)"),
                    {"entry_id": entry.id, "payload": payload, "now": now},
                )
                connection.execute(
                    text("""INSERT INTO notification_outbox
                        (entry_id, payload_json, status, attempts, updated_at)
                        VALUES (:entry_id, :payload, 'pending', 0, :now)"""),
                    {"entry_id": entry.id, "payload": payload, "now": now},
                )
            connection.execute(
                text("DELETE FROM analysis_failures WHERE entry_id=:entry_id"),
                {"entry_id": entry.id},
            )
        return True

    def get_cursor(self, source: str) -> int:
        with self._engine.connect() as connection:
            value = connection.execute(
                text("SELECT last_entry_id FROM ingestion_cursors WHERE source=:source"),
                {"source": source},
            ).scalar_one_or_none()
        return int(value) if value is not None else 0

    def advance_cursor(self, source: str, entry_id: int) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO ingestion_cursors(source, last_entry_id)
                    VALUES (:source, :entry_id)
                    ON CONFLICT(source) DO UPDATE SET last_entry_id =
                    CASE WHEN ingestion_cursors.last_entry_id < excluded.last_entry_id
                    THEN excluded.last_entry_id ELSE ingestion_cursors.last_entry_id END"""),
                {"source": source, "entry_id": entry_id},
            )

    def pending_notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        now = _utcnow().isoformat()
        with self._engine.connect() as connection:
            rows = connection.execute(
                text("""SELECT entry_id, payload_json, attempts, last_error
                    FROM notification_outbox WHERE status='pending'
                    AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
                    ORDER BY entry_id LIMIT :limit"""),
                {"now": now, "limit": limit},
            ).mappings().all()
        return [dict(row) for row in rows]

    def outbox_counts(self) -> dict[str, int]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                text("SELECT status, COUNT(*) AS total FROM notification_outbox GROUP BY status")
            ).all()
        counts = {"pending": 0, "processing": 0, "delivered": 0, "dead_letter": 0}
        counts.update({str(row[0]): int(row[1]) for row in rows})
        return counts

    def retry_dead_letters(self, entry_ids: list[int] | None = None) -> int:
        with self._engine.begin() as connection:
            if entry_ids:
                result = connection.execute(
                    text("""UPDATE notification_outbox SET status='pending', attempts=0,
                        next_attempt_at=NULL, lease_until=NULL, last_error=NULL, updated_at=:now
                        WHERE status='dead_letter' AND entry_id IN :entry_ids""").bindparams(
                        bindparam("entry_ids", expanding=True)
                    ),
                    {"now": _utcnow().isoformat(), "entry_ids": entry_ids},
                )
            else:
                result = connection.execute(
                    text("""UPDATE notification_outbox SET status='pending', attempts=0,
                        next_attempt_at=NULL, lease_until=NULL, last_error=NULL, updated_at=:now
                        WHERE status='dead_letter'"""),
                    {"now": _utcnow().isoformat()},
                )
        return int(result.rowcount)

    def claim_notifications(
        self, limit: int = 50, lease_seconds: int = 120
    ) -> list[dict[str, Any]]:
        now = _utcnow()
        now_text = now.isoformat()
        lease_text = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._engine.begin() as connection:
            query = """SELECT entry_id, payload_json, attempts, last_error
                FROM notification_outbox
                WHERE (status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= :now))
                   OR (status='processing' AND lease_until <= :now)
                ORDER BY entry_id LIMIT :limit"""
            if self._engine.dialect.name == "postgresql":
                query += " FOR UPDATE SKIP LOCKED"
            rows = connection.execute(
                text(query), {"now": now_text, "limit": limit}
            ).mappings().all()
            claimed = []
            for row in rows:
                attempts = int(row["attempts"]) + 1
                connection.execute(
                    text("""UPDATE notification_outbox SET status='processing',
                        attempts=:attempts, lease_until=:lease, updated_at=:now
                        WHERE entry_id=:entry_id"""),
                    {
                        "attempts": attempts,
                        "lease": lease_text,
                        "now": now_text,
                        "entry_id": row["entry_id"],
                    },
                )
                claimed.append({**dict(row), "attempts": attempts})
        return claimed

    def mark_notification_delivered(self, entry_id: int) -> None:
        now = _utcnow().isoformat()
        with self._engine.begin() as connection:
            connection.execute(
                text("""UPDATE notification_outbox SET status='delivered',
                    last_error=NULL, lease_until=NULL, updated_at=:now
                    WHERE entry_id=:entry_id"""),
                {"now": now, "entry_id": entry_id},
            )

    def mark_notification_failed(self, entry_id: int, error: str) -> None:
        now = _utcnow()
        now_text = now.isoformat()
        with self._engine.begin() as connection:
            row = connection.execute(
                text("SELECT attempts FROM notification_outbox WHERE entry_id=:entry_id"),
                {"entry_id": entry_id},
            ).first()
            if row is None:
                return
            attempts = int(row[0])
            dead = attempts >= self.max_notification_attempts
            delay = min(21600, 30 * (2 ** max(0, attempts - 1)))
            delay = int(delay * random.uniform(0.8, 1.2))
            connection.execute(
                text("""UPDATE notification_outbox SET status=:status,
                    next_attempt_at=:next_attempt, lease_until=NULL,
                    last_error=:error, updated_at=:now WHERE entry_id=:entry_id"""),
                {
                    "status": "dead_letter" if dead else "pending",
                    "next_attempt": None if dead else (now + timedelta(seconds=delay)).isoformat(),
                    "error": error[:1000],
                    "now": now_text,
                    "entry_id": entry_id,
                },
            )

    def get_audit(self, entry_id: int) -> dict | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM processed_entries WHERE entry_id=:entry_id"),
                {"entry_id": entry_id},
            ).mappings().first()
        return dict(row) if row is not None else None

    def counts(self) -> dict[str, int]:
        with self._engine.connect() as connection:
            articles = connection.execute(
                text("SELECT COUNT(*) FROM processed_entries")
            ).scalar_one()
            notifications = connection.execute(
                text("SELECT COUNT(*) FROM notification_log")
            ).scalar_one()
        return {"articles": int(articles), "notifications": int(notifications)}

    def start_poll(self, run_id: str, source: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO poll_runs(run_id, source, started_at, status)
                    VALUES (:run_id, :source, :started_at, 'running')"""),
                {"run_id": run_id, "source": source, "started_at": _utcnow().isoformat()},
            )

    def finish_poll(
        self,
        run_id: str,
        *,
        status: str,
        processed_count: int,
        error_code: str | None = None,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text("""UPDATE poll_runs SET finished_at=:finished_at,
                    status=:status, processed_count=:processed_count,
                    error_code=:error_code WHERE run_id=:run_id"""),
                {
                    "finished_at": _utcnow().isoformat(),
                    "status": status,
                    "processed_count": processed_count,
                    "error_code": error_code,
                    "run_id": run_id,
                },
            )

    def latest_poll(self) -> dict[str, Any] | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM poll_runs ORDER BY started_at DESC LIMIT 1")
            ).mappings().first()
        return dict(row) if row is not None else None

    def reserve_daily_model_call(self, daily_limit: int) -> bool:
        usage_day = _utcnow().date().isoformat()
        with self._engine.begin() as connection:
            inserted = connection.execute(
                text("""INSERT INTO model_usage(usage_day, calls)
                    VALUES (:usage_day, 1)
                    ON CONFLICT(usage_day) DO UPDATE SET calls=model_usage.calls + 1
                    WHERE model_usage.calls < :daily_limit"""),
                {"usage_day": usage_day, "daily_limit": daily_limit},
            )
        return inserted.rowcount == 1

    def get_analysis_failure(self, entry_id: int) -> dict[str, Any] | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM analysis_failures WHERE entry_id=:entry_id"),
                {"entry_id": entry_id},
            ).mappings().first()
        return dict(row) if row is not None else None

    def record_analysis_failure(
        self, entry_id: int, error_code: str, *, max_attempts: int = 8
    ) -> int:
        now = _utcnow()
        now_text = now.isoformat()
        with self._engine.begin() as connection:
            row = connection.execute(
                text("SELECT attempts FROM analysis_failures WHERE entry_id=:entry_id"),
                {"entry_id": entry_id},
            ).first()
            attempts = 1 if row is None else int(row[0]) + 1
            dead = attempts >= max_attempts
            delay = min(21600, 30 * (2 ** max(0, attempts - 1)))
            delay = int(delay * random.uniform(0.8, 1.2))
            connection.execute(
                text("""INSERT INTO analysis_failures
                    (entry_id, attempts, status, next_attempt_at, last_error, updated_at)
                    VALUES (:entry_id, :attempts, :status, :next_attempt, :error, :now)
                    ON CONFLICT(entry_id) DO UPDATE SET
                    attempts=excluded.attempts, status=excluded.status,
                    next_attempt_at=excluded.next_attempt_at,
                    last_error=excluded.last_error, updated_at=excluded.updated_at"""),
                {
                    "entry_id": entry_id,
                    "attempts": attempts,
                    "status": "dead_letter" if dead else "waiting",
                    "next_attempt": None if dead else (now + timedelta(seconds=delay)).isoformat(),
                    "error": error_code[:1000],
                    "now": now_text,
                },
            )
        return attempts

    def close(self) -> None:
        self._engine.dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class SQLiteEntryStore(DatabaseEntryStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__(
            "sqlite+pysqlite:///:memory:"
            if str(path) == ":memory:"
            else f"sqlite+pysqlite:///{Path(path).resolve().as_posix()}"
        )


class PostgresEntryStore(DatabaseEntryStore):
    def __init__(self, database_url: str, *, initialize_schema: bool = False) -> None:
        if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("PostgresEntryStore requires a PostgreSQL URL")
        super().__init__(database_url, initialize_schema=initialize_schema)


def create_store(sqlite_path: str | Path) -> DatabaseEntryStore:
    """Prefer PostgreSQL when configured; retain SQLite for local/offline runs."""
    import os

    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return PostgresEntryStore(database_url)
    if os.getenv("DB_HOST"):
        from sqlalchemy.engine import URL

        url = URL.create(
            "postgresql+psycopg",
            username=os.getenv("DB_USER", "rss_agent"),
            password=os.getenv("DB_PASSWORD", ""),
            host=os.environ["DB_HOST"],
            port=int(os.getenv("DB_PORT", "5432")),
            database=os.getenv("DB_NAME", "rss_agent"),
        )
        return PostgresEntryStore(url.render_as_string(hide_password=False))
    return SQLiteEntryStore(sqlite_path)
