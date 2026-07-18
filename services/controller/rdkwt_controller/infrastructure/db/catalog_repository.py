from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rdkwt_controller.infrastructure.assets import MaterializedCalibration, StoredBlob
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .models import (
    Asset,
    CalibrationSample,
    CalibrationSet,
    CalibrationVersion,
    ConversionRun,
    Model,
    ModelVersion,
    Project,
)


@dataclass(frozen=True, slots=True)
class ConversionInputs:
    project_id: str
    model_version_id: str
    model_path: str
    model_sha256: str
    calibration_version_id: str
    calibration_path: str
    calibration_manifest_path: str
    calibration_manifest_sha256: str


class CatalogRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_project(self, *, name: str, description: str) -> dict[str, Any]:
        project = Project(id=str(uuid.uuid4()), name=name, description=description)
        with self._session_factory.begin() as session:
            session.add(project)
        return self.get_project(project.id)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(Project)
                .options(
                    selectinload(Project.models)
                    .selectinload(Model.versions)
                    .selectinload(ModelVersion.asset),
                    selectinload(Project.calibration_sets)
                    .selectinload(CalibrationSet.versions)
                    .selectinload(CalibrationVersion.samples)
                    .selectinload(CalibrationSample.asset),
                )
                .order_by(Project.updated_at.desc(), Project.created_at.desc())
            ).all()
            statuses = self._run_statuses(session)
            return [self._serialize_project(row, statuses.get(row.id, {})) for row in rows]

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.scalar(
                select(Project)
                .where(Project.id == project_id)
                .options(
                    selectinload(Project.models)
                    .selectinload(Model.versions)
                    .selectinload(ModelVersion.asset),
                    selectinload(Project.calibration_sets)
                    .selectinload(CalibrationSet.versions)
                    .selectinload(CalibrationVersion.samples)
                    .selectinload(CalibrationSample.asset),
                )
            )
            if row is None:
                raise KeyError(f"unknown project: {project_id}")
            statuses = self._run_statuses(session).get(project_id, {})
            return self._serialize_project(row, statuses, include_children=True)

    def update_project(
        self, project_id: str, *, name: str | None, description: str | None
    ) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(f"unknown project: {project_id}")
            if name is not None:
                project.name = name
            if description is not None:
                project.description = description
            project.updated_at = datetime.now(UTC)
        return self.get_project(project_id)

    def project_deletion_preview(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        run_count = sum(project["run_statuses"].values())
        return {
            "project_id": project_id,
            "model_count": project["model_count"],
            "calibration_set_count": project["calibration_set_count"],
            "run_count": run_count,
            "disk_usage_bytes": project["disk_usage_bytes"],
            "can_delete": run_count == 0,
            "blocked_reason": (
                None
                if run_count == 0
                else "projects with conversion history cannot be deleted in the current M2 scope"
            ),
        }

    def delete_project(self, project_id: str) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            project = session.scalar(
                select(Project)
                .where(Project.id == project_id)
                .options(
                    selectinload(Project.models)
                    .selectinload(Model.versions)
                    .selectinload(ModelVersion.asset),
                    selectinload(Project.calibration_sets)
                    .selectinload(CalibrationSet.versions)
                    .selectinload(CalibrationVersion.samples)
                    .selectinload(CalibrationSample.asset),
                )
            )
            if project is None:
                raise KeyError(f"unknown project: {project_id}")
            run_count = session.scalar(
                select(func.count(ConversionRun.id)).where(
                    ConversionRun.project_id == project_id
                )
            )
            if run_count:
                raise ValueError(
                    "projects with conversion history cannot be deleted in the current M2 scope"
                )
            assets = {
                version.asset.id: version.asset
                for model in project.models
                for version in model.versions
            }
            assets.update(
                {
                    sample.asset.id: sample.asset
                    for calibration_set in project.calibration_sets
                    for version in calibration_set.versions
                    for sample in version.samples
                }
            )
            version_ids = [
                version.id
                for calibration_set in project.calibration_sets
                for version in calibration_set.versions
            ]
            session.delete(project)
            session.flush()
            candidate_blob_keys: set[str] = set()
            for asset in assets.values():
                model_references = session.scalar(
                    select(func.count(ModelVersion.id)).where(
                        ModelVersion.asset_id == asset.id
                    )
                )
                calibration_references = session.scalar(
                    select(func.count(CalibrationSample.id)).where(
                        CalibrationSample.asset_id == asset.id
                    )
                )
                if not model_references and not calibration_references:
                    candidate_blob_keys.add(asset.blob_key)
                    session.delete(asset)
            session.flush()
            removable_blob_keys = [
                blob_key
                for blob_key in sorted(candidate_blob_keys)
                if not session.scalar(
                    select(func.count(Asset.id)).where(Asset.blob_key == blob_key)
                )
            ]
            return {
                "project_id": project_id,
                "deleted": True,
                "blob_keys": removable_blob_keys,
                "calibration_version_ids": version_ids,
            }

    def create_model(
        self,
        *,
        project_id: str,
        model_name: str,
        original_filename: str,
        blob: StoredBlob,
    ) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(f"unknown project: {project_id}")
            asset, storage_reused = self._get_or_create_asset(session, "model", blob)
            model = Model(id=str(uuid.uuid4()), project_id=project_id, name=model_name)
            version = ModelVersion(
                id=str(uuid.uuid4()),
                model=model,
                asset=asset,
                original_filename=original_filename,
                format="onnx",
                compatibility_status="PENDING_INSPECTION",
            )
            session.add_all((model, version))
            session.flush()
            payload = self._serialize_model_version(version)
            payload["storage_reused"] = storage_reused
            project.updated_at = datetime.now(UTC)
            return payload

    def list_models(self, project_id: str) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            if session.get(Project, project_id) is None:
                raise KeyError(f"unknown project: {project_id}")
            rows = session.scalars(
                select(Model)
                .where(Model.project_id == project_id)
                .options(selectinload(Model.versions).selectinload(ModelVersion.asset))
                .order_by(Model.created_at.desc())
            ).all()
            return [self._serialize_model(row) for row in rows]

    def get_model_version(self, version_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.scalar(
                select(ModelVersion)
                .where(ModelVersion.id == version_id)
                .options(selectinload(ModelVersion.asset), selectinload(ModelVersion.model))
            )
            if row is None:
                raise KeyError(f"unknown model version: {version_id}")
            return self._serialize_model_version(row)

    def create_calibration_set(
        self, *, project_id: str, name: str, description: str
    ) -> dict[str, Any]:
        calibration_set = CalibrationSet(
            id=str(uuid.uuid4()),
            project_id=project_id,
            name=name,
            description=description,
        )
        version = CalibrationVersion(
            id=str(uuid.uuid4()),
            calibration_set=calibration_set,
            source_type="images",
            status="DRAFT",
        )
        with self._session_factory.begin() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(f"unknown project: {project_id}")
            session.add_all((calibration_set, version))
            project.updated_at = datetime.now(UTC)
            session.flush()
            return self._serialize_calibration_set(calibration_set)

    def list_calibration_sets(self, project_id: str) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            if session.get(Project, project_id) is None:
                raise KeyError(f"unknown project: {project_id}")
            rows = session.scalars(
                select(CalibrationSet)
                .where(CalibrationSet.project_id == project_id)
                .options(
                    selectinload(CalibrationSet.versions)
                    .selectinload(CalibrationVersion.samples)
                    .selectinload(CalibrationSample.asset)
                )
                .order_by(CalibrationSet.created_at.desc())
            ).all()
            return [self._serialize_calibration_set(row) for row in rows]

    def add_calibration_sample(
        self,
        *,
        version_id: str,
        original_filename: str,
        blob: StoredBlob,
    ) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            version = session.scalar(
                select(CalibrationVersion)
                .where(CalibrationVersion.id == version_id)
                .options(
                    selectinload(CalibrationVersion.samples),
                    selectinload(CalibrationVersion.calibration_set),
                )
            )
            if version is None:
                raise KeyError(f"unknown calibration version: {version_id}")
            if version.status != "DRAFT":
                raise ValueError("calibration version is immutable after finalization")
            if len(version.samples) >= 100:
                raise ValueError("calibration version cannot contain more than 100 samples")
            asset, storage_reused = self._get_or_create_asset(session, "calibration", blob)
            sample = CalibrationSample(
                id=str(uuid.uuid4()),
                calibration_version_id=version_id,
                asset=asset,
                ordinal=len(version.samples),
                original_filename=original_filename,
            )
            session.add(sample)
            version.sample_count = len(version.samples) + 1
            version.calibration_set.updated_at = datetime.now(UTC)
            session.flush()
            payload = self._serialize_sample(sample)
            payload["storage_reused"] = storage_reused
            return payload

    def get_calibration_version(self, version_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.scalar(
                select(CalibrationVersion)
                .where(CalibrationVersion.id == version_id)
                .options(
                    selectinload(CalibrationVersion.calibration_set),
                    selectinload(CalibrationVersion.samples).selectinload(
                        CalibrationSample.asset
                    ),
                )
            )
            if row is None:
                raise KeyError(f"unknown calibration version: {version_id}")
            return self._serialize_calibration_version(row, include_samples=True)

    def calibration_materialization_input(self, version_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.scalar(
                select(CalibrationVersion)
                .where(CalibrationVersion.id == version_id)
                .options(
                    selectinload(CalibrationVersion.samples).selectinload(
                        CalibrationSample.asset
                    )
                )
            )
            if row is None:
                raise KeyError(f"unknown calibration version: {version_id}")
            return {
                "id": row.id,
                "status": row.status,
                "samples": [
                    {
                        "id": sample.id,
                        "ordinal": sample.ordinal,
                        "original_filename": sample.original_filename,
                        "blob_key": sample.asset.blob_key,
                        "sha256": sample.asset.sha256,
                        "size_bytes": sample.asset.size_bytes,
                        "mime_type": sample.asset.mime_type,
                    }
                    for sample in row.samples
                ],
            }

    def finalize_calibration_version(
        self, version_id: str, materialized: MaterializedCalibration
    ) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            version = session.scalar(
                select(CalibrationVersion)
                .where(CalibrationVersion.id == version_id)
                .options(
                    selectinload(CalibrationVersion.calibration_set),
                    selectinload(CalibrationVersion.samples).selectinload(
                        CalibrationSample.asset
                    ),
                )
            )
            if version is None:
                raise KeyError(f"unknown calibration version: {version_id}")
            if version.status != "DRAFT":
                raise ValueError("calibration version is already finalized")
            version.status = "READY"
            version.sample_count = len(version.samples)
            version.source_path = materialized.source_path
            version.manifest_key = materialized.manifest_key
            version.manifest_sha256 = materialized.manifest_sha256
            version.validation_report = materialized.validation_report
            version.finalized_at = datetime.now(UTC)
            version.calibration_set.updated_at = datetime.now(UTC)
            session.flush()
            return self._serialize_calibration_version(version, include_samples=True)

    def resolve_conversion_inputs(
        self, *, model_version_id: str, calibration_version_id: str
    ) -> ConversionInputs:
        with self._session_factory() as session:
            model_version = session.scalar(
                select(ModelVersion)
                .where(ModelVersion.id == model_version_id)
                .options(selectinload(ModelVersion.asset), selectinload(ModelVersion.model))
            )
            calibration_version = session.scalar(
                select(CalibrationVersion)
                .where(CalibrationVersion.id == calibration_version_id)
                .options(selectinload(CalibrationVersion.calibration_set))
            )
            if model_version is None:
                raise KeyError(f"unknown model version: {model_version_id}")
            if calibration_version is None:
                raise KeyError(f"unknown calibration version: {calibration_version_id}")
            if model_version.model.project_id != calibration_version.calibration_set.project_id:
                raise ValueError("model and calibration versions must belong to the same project")
            if calibration_version.status != "READY" or calibration_version.source_path is None:
                raise ValueError("calibration version must be finalized before conversion")
            if not 20 <= calibration_version.sample_count <= 100:
                raise ValueError("standard conversion requires 20 to 100 calibration samples")
            if (
                calibration_version.manifest_key is None
                or calibration_version.manifest_sha256 is None
            ):
                raise ValueError("finalized calibration version has incomplete manifest metadata")
            return ConversionInputs(
                project_id=model_version.model.project_id,
                model_version_id=model_version.id,
                model_path=model_version.asset.blob_key,
                model_sha256=model_version.asset.sha256,
                calibration_version_id=calibration_version.id,
                calibration_path=calibration_version.source_path,
                calibration_manifest_path=calibration_version.manifest_key,
                calibration_manifest_sha256=calibration_version.manifest_sha256,
            )

    @staticmethod
    def _get_or_create_asset(
        session: Session, kind: str, blob: StoredBlob
    ) -> tuple[Asset, bool]:
        asset = session.scalar(
            select(Asset).where(
                Asset.kind == kind,
                Asset.sha256 == blob.sha256,
                Asset.size_bytes == blob.size_bytes,
            )
        )
        if asset is not None:
            return asset, True
        candidate_id = str(uuid.uuid4())
        session.execute(
            sqlite_insert(Asset)
            .values(
                id=candidate_id,
                kind=kind,
                display_name=blob.display_name,
                blob_key=blob.blob_key,
                sha256=blob.sha256,
                size_bytes=blob.size_bytes,
                mime_type=blob.mime_type,
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["kind", "sha256", "size_bytes"])
        )
        asset = session.scalar(
            select(Asset).where(
                Asset.kind == kind,
                Asset.sha256 == blob.sha256,
                Asset.size_bytes == blob.size_bytes,
            )
        )
        if asset is None:
            raise RuntimeError("failed to create or resolve content-addressed asset")
        return asset, asset.id != candidate_id

    @staticmethod
    def _run_statuses(session: Session) -> dict[str, dict[str, int]]:
        rows = session.execute(
            select(ConversionRun.project_id, ConversionRun.status, func.count(ConversionRun.id))
            .where(ConversionRun.project_id.is_not(None))
            .group_by(ConversionRun.project_id, ConversionRun.status)
        ).all()
        result: dict[str, dict[str, int]] = {}
        for project_id, status, count in rows:
            assert project_id is not None
            result.setdefault(project_id, {})[status] = count
        return result

    @classmethod
    def _serialize_project(
        cls,
        row: Project,
        run_statuses: dict[str, int],
        *,
        include_children: bool = False,
    ) -> dict[str, Any]:
        assets: dict[str, int] = {}
        for model in row.models:
            for version in model.versions:
                assets[version.asset.id] = version.asset.size_bytes
        for calibration_set in row.calibration_sets:
            for version in calibration_set.versions:
                for sample in version.samples:
                    assets[sample.asset.id] = sample.asset.size_bytes
        payload: dict[str, Any] = {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "model_count": len(row.models),
            "calibration_set_count": len(row.calibration_sets),
            "run_statuses": run_statuses,
            "disk_usage_bytes": sum(assets.values()),
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
        if include_children:
            payload["models"] = [cls._serialize_model(model) for model in row.models]
            payload["calibration_sets"] = [
                cls._serialize_calibration_set(item) for item in row.calibration_sets
            ]
        return payload

    @classmethod
    def _serialize_model(cls, row: Model) -> dict[str, Any]:
        return {
            "id": row.id,
            "project_id": row.project_id,
            "name": row.name,
            "versions": [cls._serialize_model_version(version) for version in row.versions],
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    @staticmethod
    def _serialize_model_version(row: ModelVersion) -> dict[str, Any]:
        return {
            "id": row.id,
            "model_id": row.model_id,
            "original_filename": row.original_filename,
            "format": row.format,
            "compatibility_status": row.compatibility_status,
            "inspection": row.inspection,
            "asset": {
                "id": row.asset.id,
                "sha256": row.asset.sha256,
                "size_bytes": row.asset.size_bytes,
                "mime_type": row.asset.mime_type,
            },
            "created_at": row.created_at.isoformat(),
        }

    @classmethod
    def _serialize_calibration_set(cls, row: CalibrationSet) -> dict[str, Any]:
        return {
            "id": row.id,
            "project_id": row.project_id,
            "name": row.name,
            "description": row.description,
            "versions": [
                cls._serialize_calibration_version(version) for version in row.versions
            ],
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    @classmethod
    def _serialize_calibration_version(
        cls, row: CalibrationVersion, *, include_samples: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": row.id,
            "calibration_set_id": row.calibration_set_id,
            "source_type": row.source_type,
            "status": row.status,
            "sample_count": row.sample_count,
            "manifest_sha256": row.manifest_sha256,
            "validation_report": row.validation_report,
            "created_at": row.created_at.isoformat(),
            "finalized_at": (
                None if row.finalized_at is None else row.finalized_at.isoformat()
            ),
        }
        if include_samples:
            payload["samples"] = [cls._serialize_sample(sample) for sample in row.samples]
        return payload

    @staticmethod
    def _serialize_sample(row: CalibrationSample) -> dict[str, Any]:
        return {
            "id": row.id,
            "ordinal": row.ordinal,
            "original_filename": row.original_filename,
            "asset": {
                "id": row.asset.id,
                "sha256": row.asset.sha256,
                "size_bytes": row.asset.size_bytes,
                "mime_type": row.asset.mime_type,
            },
            "created_at": row.created_at.isoformat(),
        }
