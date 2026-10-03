"""Create production PostgreSQL schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processed_entries",
        sa.Column("entry_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("article_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("rules_json", sa.Text(), nullable=False),
        sa.Column("analyzer_name", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "notification_log",
        sa.Column("entry_id", sa.BigInteger(), sa.ForeignKey("processed_entries.entry_id"), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "ingestion_cursors",
        sa.Column("source", sa.Text(), primary_key=True),
        sa.Column("last_entry_id", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("entry_id", sa.BigInteger(), sa.ForeignKey("processed_entries.entry_id"), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("next_attempt_at", sa.Text()),
        sa.Column("lease_until", sa.Text()),
    )
    op.create_index(
        "idx_outbox_ready", "notification_outbox",
        ["status", "next_attempt_at", "entry_id"],
    )
    op.create_table(
        "poll_runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text()),
    )
    op.create_index("idx_poll_runs_started", "poll_runs", ["started_at"])
    op.create_table(
        "model_usage",
        sa.Column("usage_day", sa.Text(), primary_key=True),
        sa.Column("calls", sa.Integer(), nullable=False),
    )
    op.create_table(
        "analysis_failures",
        sa.Column("entry_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("next_attempt_at", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("analysis_failures")
    op.drop_table("model_usage")
    op.drop_index("idx_poll_runs_started", table_name="poll_runs")
    op.drop_table("poll_runs")
    op.drop_index("idx_outbox_ready", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_table("ingestion_cursors")
    op.drop_table("notification_log")
    op.drop_table("processed_entries")
