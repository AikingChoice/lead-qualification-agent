"""Add lead qualification workflow persistence."""

import sqlalchemy as sa
from alembic import op

revision = "0002_lead_qualification"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lead_assessments",
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("report_json", sa.Text()),
        sa.Column("note_id", sa.Text()),
        sa.Column("hubspot_write_started", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lease_until", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index("idx_lead_assessments_created", "lead_assessments", ["created_at"])
    op.create_table(
        "lead_model_usage",
        sa.Column("usage_day", sa.Text(), primary_key=True),
        sa.Column("calls", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lead_model_usage")
    op.drop_index("idx_lead_assessments_created", table_name="lead_assessments")
    op.drop_table("lead_assessments")

