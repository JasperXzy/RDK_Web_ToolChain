from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    models: Mapped[list[Model]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Model.created_at"
    )
    calibration_sets: Mapped[list[CalibrationSet]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="CalibrationSet.created_at",
    )


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("kind", "sha256", "size_bytes", name="uq_asset_content"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    blob_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Model(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    project: Mapped[Project] = relationship(back_populates="models")
    versions: Mapped[list[ModelVersion]] = relationship(
        back_populates="model",
        cascade="all, delete-orphan",
        order_by="ModelVersion.created_at",
    )


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    compatibility_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING_INSPECTION"
    )
    inspection: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    inspection_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    model: Mapped[Model] = relationship(back_populates="versions")
    asset: Mapped[Asset] = relationship()


class CalibrationSet(Base):
    __tablename__ = "calibration_sets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    project: Mapped[Project] = relationship(back_populates="calibration_sets")
    versions: Mapped[list[CalibrationVersion]] = relationship(
        back_populates="calibration_set",
        cascade="all, delete-orphan",
        order_by="CalibrationVersion.created_at",
    )


class CalibrationVersion(Base):
    __tablename__ = "calibration_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    calibration_set_id: Mapped[str] = mapped_column(
        ForeignKey("calibration_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="images")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT", index=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    manifest_key: Mapped[str | None] = mapped_column(String(512))
    source_path: Mapped[str | None] = mapped_column(String(512))
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    calibration_set: Mapped[CalibrationSet] = relationship(back_populates="versions")
    samples: Mapped[list[CalibrationSample]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="CalibrationSample.ordinal",
    )


class CalibrationSample(Base):
    __tablename__ = "calibration_samples"
    __table_args__ = (
        UniqueConstraint(
            "calibration_version_id", "ordinal", name="uq_calibration_sample_ordinal"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    calibration_version_id: Mapped[str] = mapped_column(
        ForeignKey("calibration_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    version: Mapped[CalibrationVersion] = relationship(back_populates="samples")
    asset: Mapped[Asset] = relationship()


class ConversionRun(Base):
    __tablename__ = "conversion_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    model_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL"), index=True
    )
    calibration_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("calibration_versions.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="CONVERSION", index=True
    )
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_image_reference: Mapped[str | None] = mapped_column(String(512))
    runner_image_id: Mapped[str | None] = mapped_column(String(128))
    contract_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    app_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="0.1.0.dev0"
    )
    generated_yaml: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    request_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Attempt.number"
    )


class Attempt(Base):
    __tablename__ = "run_attempts"
    __table_args__ = (UniqueConstraint("run_id", "number", name="uq_run_attempt_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("conversion_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    container_id: Mapped[str | None] = mapped_column(String(128))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run: Mapped[ConversionRun] = relationship(back_populates="attempts")
