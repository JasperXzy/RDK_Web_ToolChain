"""create M0 run and attempt tables

Revision ID: 0001_m0_runs
Revises:
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_m0_runs"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversion_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_snapshot", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversion_runs_status", "conversion_runs", ["status"])
    op.create_table(
        "run_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("container_id", sa.String(length=128), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["conversion_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "number", name="uq_run_attempt_number"),
    )
    op.create_index("ix_run_attempts_run_id", "run_attempts", ["run_id"])
    op.create_index("ix_run_attempts_status", "run_attempts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_run_attempts_status", table_name="run_attempts")
    op.drop_index("ix_run_attempts_run_id", table_name="run_attempts")
    op.drop_table("run_attempts")
    op.drop_index("ix_conversion_runs_status", table_name="conversion_runs")
    op.drop_table("conversion_runs")
