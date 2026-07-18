"""add M2.1 calibration sample validation metadata

Revision ID: 0004_m2_1_calibration_sources
Revises: 0003_m2_task_orchestration
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_m2_1_calibration_sources"
down_revision: str | None = "0003_m2_task_orchestration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("calibration_samples") as batch:
        batch.add_column(sa.Column("validation", sa.JSON()))


def downgrade() -> None:
    with op.batch_alter_table("calibration_samples") as batch:
        batch.drop_column("validation")
