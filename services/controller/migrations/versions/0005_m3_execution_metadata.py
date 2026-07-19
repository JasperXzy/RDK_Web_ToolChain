"""add M3 runner mode and cache metadata

Revision ID: 0005_m3_execution_metadata
Revises: 0004_m2_1_calibration_sources
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_m3_execution_metadata"
down_revision: str | None = "0004_m2_1_calibration_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversion_runs") as batch:
        batch.add_column(
            sa.Column(
                "runner_mode",
                sa.String(length=16),
                nullable=False,
                server_default="cpu",
            )
        )
        batch.add_column(sa.Column("cache_key", sa.String(length=64)))
        batch.add_column(
            sa.Column(
                "cache_hit",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.create_index("ix_conversion_runs_cache_key", ["cache_key"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("conversion_runs") as batch:
        batch.drop_index("ix_conversion_runs_cache_key")
        batch.drop_column("cache_hit")
        batch.drop_column("cache_key")
        batch.drop_column("runner_mode")
