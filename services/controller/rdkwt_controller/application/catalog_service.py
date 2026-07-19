from __future__ import annotations

import logging
import shutil
from collections.abc import AsyncIterable, Callable
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from rdkwt_controller.infrastructure.assets import AssetStore, AssetStoreError
from rdkwt_controller.infrastructure.db import CatalogRepository

T = TypeVar("T")
logger = logging.getLogger(__name__)


class CatalogError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _clean_text(
    value: str,
    name: str,
    *,
    maximum: int,
    allow_empty: bool = False,
    allow_newlines: bool = False,
) -> str:
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise CatalogError("CATALOG_FIELD_INVALID", f"{name} must not be empty")
    if len(cleaned) > maximum:
        raise CatalogError(
            "CATALOG_FIELD_INVALID", f"{name} must contain at most {maximum} characters"
        )
    allowed_controls = "\t\n" if allow_newlines else "\t"
    if any(ord(character) < 32 and character not in allowed_controls for character in cleaned):
        raise CatalogError("CATALOG_FIELD_INVALID", f"{name} contains control characters")
    return cleaned


class CatalogService:
    def __init__(
        self,
        *,
        repository: CatalogRepository,
        asset_store: AssetStore,
        runs_root: Path | None = None,
    ) -> None:
        self.repository = repository
        self.asset_store = asset_store
        self.runs_root = None if runs_root is None else runs_root.resolve(strict=True)

    def create_project(self, *, name: str, description: str) -> dict[str, Any]:
        return self.repository.create_project(
            name=_clean_text(name, "name", maximum=200),
            description=_clean_text(
                description,
                "description",
                maximum=4000,
                allow_empty=True,
                allow_newlines=True,
            ),
        )

    def list_projects(self) -> list[dict[str, Any]]:
        return self.repository.list_projects()

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._project_operation(lambda: self.repository.get_project(project_id))

    def update_project(
        self, project_id: str, *, name: str | None, description: str | None
    ) -> dict[str, Any]:
        if name is None and description is None:
            raise CatalogError("CATALOG_FIELD_INVALID", "at least one project field is required")
        cleaned_name = None if name is None else _clean_text(name, "name", maximum=200)
        cleaned_description = (
            None
            if description is None
            else _clean_text(
                description,
                "description",
                maximum=4000,
                allow_empty=True,
                allow_newlines=True,
            )
        )
        return self._project_operation(
            lambda: self.repository.update_project(
                project_id, name=cleaned_name, description=cleaned_description
            )
        )

    def project_deletion_preview(self, project_id: str) -> dict[str, Any]:
        preview = self._project_operation(
            lambda: self.repository.project_deletion_preview(project_id)
        )
        run_ids = preview.pop("run_ids")
        run_disk_bytes = sum(self._run_directory_size(run_id) for run_id in run_ids)
        preview["run_disk_usage_bytes"] = run_disk_bytes
        preview["disk_usage_bytes"] += run_disk_bytes
        return preview

    def delete_project(self, project_id: str, *, confirmation: str) -> dict[str, Any]:
        if confirmation != project_id:
            raise CatalogError(
                "PROJECT_CONFIRMATION_INVALID",
                "X-Confirm-Project must exactly match the project ID",
                status_code=409,
            )
        try:
            deleted = self.repository.delete_project(project_id)
        except KeyError as exc:
            raise CatalogError("PROJECT_NOT_FOUND", str(exc), status_code=404) from exc
        except ValueError as exc:
            raise CatalogError("PROJECT_DELETE_BLOCKED", str(exc), status_code=409) from exc
        try:
            self.asset_store.delete_unreferenced(
                blob_keys=deleted.pop("blob_keys"),
                version_ids=deleted.pop("calibration_version_ids"),
            )
            for run_id in deleted.pop("run_ids"):
                self._delete_run_directory(run_id)
        except (AssetStoreError, OSError) as exc:
            logger.exception("project metadata was deleted but asset cleanup failed")
            deleted["cleanup_warning"] = str(exc)
        return deleted

    def _run_directory_size(self, run_id: str) -> int:
        if self.runs_root is None:
            return 0
        path = self.runs_root / run_id
        if not path.is_dir() or path.is_symlink():
            return 0
        return sum(
            item.stat().st_size
            for item in path.rglob("*")
            if item.is_file() and not item.is_symlink()
        )

    def _delete_run_directory(self, run_id: str) -> None:
        if self.runs_root is None:
            return
        try:
            parsed = str(UUID(run_id))
        except ValueError as exc:
            raise AssetStoreError("RUN_PATH_INVALID", "run ID is invalid") from exc
        path = self.runs_root / parsed
        if not path.exists():
            return
        if path.is_symlink() or not path.is_dir():
            raise AssetStoreError("RUN_PATH_INVALID", "run path is not a regular directory")
        shutil.rmtree(path)

    async def upload_model(
        self,
        *,
        project_id: str,
        filename: str,
        model_name: str | None,
        content_length: int | None,
        chunks: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        self.get_project(project_id)
        resolved_name = Path(filename).stem if model_name is None else model_name
        resolved_name = _clean_text(resolved_name, "model_name", maximum=200)
        try:
            blob = await self.asset_store.ingest(
                chunks,
                kind="model",
                display_name=filename,
                content_length=content_length,
            )
        except AssetStoreError as exc:
            raise self._asset_error(exc) from exc
        return self._project_operation(
            lambda: self.repository.create_model(
                project_id=project_id,
                model_name=resolved_name,
                original_filename=blob.display_name,
                blob=blob,
            )
        )

    def list_models(self, project_id: str) -> list[dict[str, Any]]:
        return self._project_operation(lambda: self.repository.list_models(project_id))

    def get_model_version(self, version_id: str) -> dict[str, Any]:
        try:
            return self.repository.get_model_version(version_id)
        except KeyError as exc:
            raise CatalogError("MODEL_VERSION_NOT_FOUND", str(exc), status_code=404) from exc

    def create_calibration_set(
        self,
        *,
        project_id: str,
        name: str,
        description: str,
        source_type: str = "images",
    ) -> dict[str, Any]:
        if source_type not in {"images", "npy", "npy_multi"}:
            raise CatalogError(
                "CALIBRATION_SOURCE_TYPE_INVALID",
                "source_type must be images, npy, or npy_multi",
            )
        cleaned_name = _clean_text(name, "name", maximum=200)
        cleaned_description = _clean_text(
            description,
            "description",
            maximum=4000,
            allow_empty=True,
            allow_newlines=True,
        )
        return self._project_operation(
            lambda: self.repository.create_calibration_set(
                project_id=project_id,
                name=cleaned_name,
                description=cleaned_description,
                source_type=source_type,
            )
        )

    def list_calibration_sets(self, project_id: str) -> list[dict[str, Any]]:
        return self._project_operation(lambda: self.repository.list_calibration_sets(project_id))

    async def upload_calibration_sample(
        self,
        *,
        version_id: str,
        filename: str,
        content_length: int | None,
        chunks: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        existing = self.get_calibration_version(version_id)
        if existing["status"] != "DRAFT":
            raise CatalogError(
                "CALIBRATION_VERSION_IMMUTABLE",
                "calibration version is immutable after finalization",
                status_code=409,
            )
        if existing["source_type"] == "npy_multi":
            raise CatalogError(
                "CALIBRATION_MULTI_INPUT_ARCHIVE_REQUIRED",
                "multi-input calibration must be uploaded as one aligned ZIP archive",
            )
        if existing["sample_count"] >= 100:
            raise CatalogError(
                "CALIBRATION_SAMPLE_LIMIT",
                "calibration version cannot contain more than 100 samples",
                status_code=409,
            )
        try:
            blob = await self.asset_store.ingest(
                chunks,
                kind="calibration",
                display_name=filename,
                content_length=content_length,
                calibration_source_type=str(existing["source_type"]),
            )
        except AssetStoreError as exc:
            raise self._asset_error(exc) from exc
        try:
            return self.repository.add_calibration_sample(
                version_id=version_id,
                original_filename=blob.display_name,
                blob=blob,
            )
        except KeyError as exc:
            raise CatalogError("CALIBRATION_VERSION_NOT_FOUND", str(exc), status_code=404) from exc
        except ValueError as exc:
            raise CatalogError("CALIBRATION_VERSION_IMMUTABLE", str(exc), status_code=409) from exc

    async def upload_calibration_archive(
        self,
        *,
        version_id: str,
        filename: str,
        content_length: int | None,
        chunks: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        existing = self.get_calibration_version(version_id)
        if existing["status"] != "DRAFT":
            raise CatalogError(
                "CALIBRATION_VERSION_IMMUTABLE",
                "calibration version is immutable after finalization",
                status_code=409,
            )
        if existing["source_type"] == "npy_multi" and existing["sample_count"]:
            raise CatalogError(
                "CALIBRATION_ARCHIVE_CONFLICT",
                "multi-input calibration accepts exactly one ZIP archive",
                status_code=409,
            )
        maximum_files = 400 if existing["source_type"] == "npy_multi" else 100
        remaining = maximum_files - int(existing["sample_count"])
        try:
            blobs = await self.asset_store.ingest_calibration_archive(
                chunks,
                display_name=filename,
                content_length=content_length,
                source_type=str(existing["source_type"]),
                maximum_entries=remaining,
            )
        except AssetStoreError as exc:
            raise self._asset_error(exc) from exc
        try:
            samples = self.repository.add_calibration_samples(
                version_id=version_id,
                blobs=blobs,
            )
        except KeyError as exc:
            raise CatalogError("CALIBRATION_VERSION_NOT_FOUND", str(exc), status_code=404) from exc
        except ValueError as exc:
            raise CatalogError("CALIBRATION_ARCHIVE_CONFLICT", str(exc), status_code=409) from exc
        imported_count = len(samples)
        if existing["source_type"] == "npy_multi":
            imported_count = len(
                {str((item.get("validation") or {}).get("sample_key")) for item in samples}
            )
        return {
            "version_id": version_id,
            "source_type": existing["source_type"],
            "imported_count": imported_count,
            "imported_file_count": len(samples),
            "samples": samples,
        }

    def get_calibration_version(self, version_id: str) -> dict[str, Any]:
        try:
            return self.repository.get_calibration_version(version_id)
        except KeyError as exc:
            raise CatalogError("CALIBRATION_VERSION_NOT_FOUND", str(exc), status_code=404) from exc

    def calibration_sample_file(self, version_id: str, ordinal: int) -> tuple[Path, dict[str, Any]]:
        if ordinal < 0 or ordinal >= 400:
            raise CatalogError(
                "CALIBRATION_SAMPLE_NOT_FOUND",
                "calibration sample ordinal is outside the supported range",
                status_code=404,
            )
        try:
            metadata = self.repository.calibration_sample_input(version_id, ordinal)
            path = self.asset_store.resolve_verified_blob(
                metadata["blob_key"],
                sha256=metadata["sha256"],
                size_bytes=metadata["size_bytes"],
            )
        except KeyError as exc:
            raise CatalogError("CALIBRATION_SAMPLE_NOT_FOUND", str(exc), status_code=404) from exc
        except AssetStoreError as exc:
            raise self._asset_error(exc) from exc
        return path, metadata

    def finalize_calibration_version(self, version_id: str) -> dict[str, Any]:
        try:
            source = self.repository.calibration_materialization_input(version_id)
        except KeyError as exc:
            raise CatalogError("CALIBRATION_VERSION_NOT_FOUND", str(exc), status_code=404) from exc
        if source["status"] != "DRAFT":
            raise CatalogError(
                "CALIBRATION_VERSION_IMMUTABLE",
                "calibration version is already finalized",
                status_code=409,
            )
        if not source["samples"]:
            raise CatalogError("CALIBRATION_EMPTY", "cannot finalize an empty calibration version")
        try:
            materialized = self.asset_store.materialize_calibration(
                version_id, source["source_type"], source["samples"]
            )
        except AssetStoreError as exc:
            raise self._asset_error(exc) from exc
        try:
            return self.repository.finalize_calibration_version(version_id, materialized)
        except Exception as exc:
            try:
                self.asset_store.delete_unreferenced(blob_keys=[], version_ids=[version_id])
            except (AssetStoreError, OSError):
                logger.exception(
                    "calibration metadata update failed and materialization cleanup also failed"
                )
            if isinstance(exc, KeyError):
                raise CatalogError(
                    "CALIBRATION_VERSION_NOT_FOUND", str(exc), status_code=404
                ) from exc
            if isinstance(exc, ValueError):
                raise CatalogError(
                    "CALIBRATION_FINALIZE_CONFLICT", str(exc), status_code=409
                ) from exc
            raise

    @staticmethod
    def _project_operation(operation: Callable[[], T]) -> T:
        try:
            return operation()
        except KeyError as exc:
            raise CatalogError("PROJECT_NOT_FOUND", str(exc), status_code=404) from exc

    @staticmethod
    def _asset_error(exc: AssetStoreError) -> CatalogError:
        status_code = (
            413
            if exc.code
            in {
                "UPLOAD_TOO_LARGE",
                "CALIBRATION_ARCHIVE_TOO_LARGE",
                "CALIBRATION_ARCHIVE_ENTRY_TOO_LARGE",
            }
            else 422
        )
        if exc.code in {
            "BLOB_INTEGRITY_FAILED",
            "BLOB_PATH_INVALID",
            "CALIBRATION_PATH_INVALID",
            "RUN_PATH_INVALID",
        }:
            status_code = 500
        return CatalogError(exc.code, str(exc), status_code=status_code)
