"""add M4 device and board validation records

Revision ID: 0006_m4_board_validation
Revises: 0005_m3_execution_metadata
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_m4_board_validation"
down_revision: str | None = "0005_m3_execution_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("user", sa.String(length=128), nullable=False),
        sa.Column("auth_type", sa.String(length=16), nullable=False),
        sa.Column("credential_ref", sa.String(length=255), nullable=False),
        sa.Column("host_key_fingerprint", sa.String(length=96)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detected_platform", sa.String(length=16)),
        sa.Column("probe_result", sa.JSON()),
        sa.Column("last_probe_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_probed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_ref"),
    )
    op.create_index("ix_devices_platform", "devices", ["platform"], unique=False)
    op.create_index("ix_devices_status", "devices", ["status"], unique=False)
    op.create_table(
        "board_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36)),
        sa.Column("conversion_run_id", sa.String(length=36)),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("device_snapshot", sa.JSON(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("local_dir", sa.String(length=1024), nullable=False),
        sa.Column("remote_dir", sa.String(length=255), nullable=False),
        sa.Column("hbm_sha256", sa.String(length=64)),
        sa.Column("hbm_size_bytes", sa.BigInteger()),
        sa.Column("result_payload", sa.JSON()),
        sa.Column("error_code", sa.String(length=128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["conversion_run_id"], ["conversion_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_board_runs_conversion_run_id", "board_runs", ["conversion_run_id"])
    op.create_index("ix_board_runs_device_id", "board_runs", ["device_id"])
    op.create_index("ix_board_runs_hbm_sha256", "board_runs", ["hbm_sha256"])
    op.create_index("ix_board_runs_mode", "board_runs", ["mode"])
    op.create_index("ix_board_runs_status", "board_runs", ["status"])


def downgrade() -> None:
    op.drop_table("board_runs")
    op.drop_table("devices")
