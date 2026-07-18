"""add M2 task orchestration metadata

Revision ID: 0003_m2_task_orchestration
Revises: 0002_m2_catalog
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_m2_task_orchestration"
down_revision: str | None = "0002_m2_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_versions") as batch:
        batch.add_column(sa.Column("inspection_run_id", sa.String(length=36)))
        batch.add_column(sa.Column("inspected_at", sa.DateTime(timezone=True)))
        batch.create_index("ix_model_versions_inspection_run_id", ["inspection_run_id"])

    with op.batch_alter_table("conversion_runs") as batch:
        batch.add_column(
            sa.Column(
                "kind",
                sa.String(length=32),
                nullable=False,
                server_default="CONVERSION",
            )
        )
        batch.add_column(sa.Column("runner_image_reference", sa.String(length=512)))
        batch.add_column(sa.Column("runner_image_id", sa.String(length=128)))
        batch.add_column(
            sa.Column(
                "contract_version",
                sa.String(length=16),
                nullable=False,
                server_default="1.0",
            )
        )
        batch.add_column(
            sa.Column(
                "app_version",
                sa.String(length=64),
                nullable=False,
                server_default="0.1.0.dev0",
            )
        )
        batch.add_column(sa.Column("generated_yaml", sa.Text()))
        batch.create_index("ix_conversion_runs_kind", ["kind"])

    with op.batch_alter_table("run_attempts") as batch:
        batch.add_column(
            sa.Column(
                "stage",
                sa.String(length=32),
                nullable=False,
                server_default="QUEUED",
            )
        )
        batch.add_column(sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column(
                "recovered",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("run_attempts") as batch:
        batch.drop_column("recovered")
        batch.drop_column("cancel_requested_at")
        batch.drop_column("stage")

    with op.batch_alter_table("conversion_runs") as batch:
        batch.drop_index("ix_conversion_runs_kind")
        batch.drop_column("generated_yaml")
        batch.drop_column("app_version")
        batch.drop_column("contract_version")
        batch.drop_column("runner_image_id")
        batch.drop_column("runner_image_reference")
        batch.drop_column("kind")

    with op.batch_alter_table("model_versions") as batch:
        batch.drop_index("ix_model_versions_inspection_run_id")
        batch.drop_column("inspected_at")
        batch.drop_column("inspection_run_id")
