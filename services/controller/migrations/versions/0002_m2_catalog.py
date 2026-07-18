"""create M2 project and asset catalog

Revision ID: 0002_m2_catalog
Revises: 0001_m0_runs
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_m2_catalog"
down_revision: str | None = "0001_m0_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("blob_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "sha256", "size_bytes", name="uq_asset_content"),
    )
    op.create_index("ix_assets_kind", "assets", ["kind"])
    op.create_index("ix_assets_sha256", "assets", ["sha256"])
    op.create_table(
        "models",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_models_project_id", "models", ["project_id"])
    op.create_table(
        "calibration_sets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calibration_sets_project_id", "calibration_sets", ["project_id"]
    )
    op.create_table(
        "model_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("compatibility_status", sa.String(length=32), nullable=False),
        sa.Column("inspection", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_versions_asset_id", "model_versions", ["asset_id"])
    op.create_index("ix_model_versions_model_id", "model_versions", ["model_id"])
    op.create_table(
        "calibration_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("calibration_set_id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("manifest_key", sa.String(length=512), nullable=True),
        sa.Column("source_path", sa.String(length=512), nullable=True),
        sa.Column("validation_report", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["calibration_set_id"], ["calibration_sets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calibration_versions_calibration_set_id",
        "calibration_versions",
        ["calibration_set_id"],
    )
    op.create_index("ix_calibration_versions_status", "calibration_versions", ["status"])
    op.create_table(
        "calibration_samples",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("calibration_version_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["calibration_version_id"], ["calibration_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "calibration_version_id", "ordinal", name="uq_calibration_sample_ordinal"
        ),
    )
    op.create_index("ix_calibration_samples_asset_id", "calibration_samples", ["asset_id"])
    op.create_index(
        "ix_calibration_samples_calibration_version_id",
        "calibration_samples",
        ["calibration_version_id"],
    )

    with op.batch_alter_table("conversion_runs") as batch:
        batch.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("model_version_id", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("calibration_version_id", sa.String(length=36), nullable=True)
        )
        batch.create_index("ix_conversion_runs_project_id", ["project_id"])
        batch.create_index("ix_conversion_runs_model_version_id", ["model_version_id"])
        batch.create_index(
            "ix_conversion_runs_calibration_version_id", ["calibration_version_id"]
        )
        batch.create_foreign_key(
            "fk_conversion_runs_project_id_projects",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_conversion_runs_model_version_id_model_versions",
            "model_versions",
            ["model_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_conversion_runs_calibration_version_id_calibration_versions",
            "calibration_versions",
            ["calibration_version_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("conversion_runs") as batch:
        batch.drop_constraint(
            "fk_conversion_runs_calibration_version_id_calibration_versions",
            type_="foreignkey",
        )
        batch.drop_constraint(
            "fk_conversion_runs_model_version_id_model_versions", type_="foreignkey"
        )
        batch.drop_constraint("fk_conversion_runs_project_id_projects", type_="foreignkey")
        batch.drop_index("ix_conversion_runs_calibration_version_id")
        batch.drop_index("ix_conversion_runs_model_version_id")
        batch.drop_index("ix_conversion_runs_project_id")
        batch.drop_column("calibration_version_id")
        batch.drop_column("model_version_id")
        batch.drop_column("project_id")

    op.drop_index(
        "ix_calibration_samples_calibration_version_id", table_name="calibration_samples"
    )
    op.drop_index("ix_calibration_samples_asset_id", table_name="calibration_samples")
    op.drop_table("calibration_samples")
    op.drop_index("ix_calibration_versions_status", table_name="calibration_versions")
    op.drop_index(
        "ix_calibration_versions_calibration_set_id", table_name="calibration_versions"
    )
    op.drop_table("calibration_versions")
    op.drop_index("ix_model_versions_model_id", table_name="model_versions")
    op.drop_index("ix_model_versions_asset_id", table_name="model_versions")
    op.drop_table("model_versions")
    op.drop_index("ix_calibration_sets_project_id", table_name="calibration_sets")
    op.drop_table("calibration_sets")
    op.drop_index("ix_models_project_id", table_name="models")
    op.drop_table("models")
    op.drop_index("ix_assets_sha256", table_name="assets")
    op.drop_index("ix_assets_kind", table_name="assets")
    op.drop_table("assets")
    op.drop_table("projects")
