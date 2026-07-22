from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import uuid
import zipfile
from collections.abc import AsyncIterable, Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar
from uuid import UUID

from rdkwt_controller.infrastructure.assets import (
    AssetStore,
    AssetStoreError,
    StoredBlob,
    validate_display_filename,
)
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
        self.exports_root = self.asset_store.root.parent / "state" / "project-exports"
        # Docker and local development use different sibling layouts. Prefer the explicit
        # state-owned directory when callers provide one through set_exports_root().

    def set_exports_root(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.exports_root = path.resolve(strict=True)

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

    def export_project(self, project_id: str) -> Path:
        project = self.get_project(project_id)
        files: dict[str, dict[str, Any]] = {}
        models: list[dict[str, Any]] = []
        calibration_sets: list[dict[str, Any]] = []
        resolved: dict[str, Path] = {}
        for model in project["models"]:
            versions = []
            for version in model["versions"]:
                source = self.repository.model_inspection_input(version["id"])
                path = self.asset_store.resolve_verified_blob(
                    source.model_path,
                    sha256=source.model_sha256,
                    size_bytes=version["asset"]["size_bytes"],
                )
                key = source.model_sha256
                files.setdefault(
                    key,
                    {
                        "archive_path": f"files/{key}",
                        "sha256": key,
                        "size_bytes": version["asset"]["size_bytes"],
                    },
                )
                resolved[key] = path
                versions.append(
                    {
                        "original_filename": version["original_filename"],
                        "content_sha256": key,
                    }
                )
            models.append({"name": model["name"], "versions": versions})
        for calibration_set in project["calibration_sets"]:
            versions = []
            for version in calibration_set["versions"]:
                source = self.repository.calibration_materialization_input(version["id"])
                samples = []
                for sample in source["samples"]:
                    path = self.asset_store.resolve_verified_blob(
                        sample["blob_key"],
                        sha256=sample["sha256"],
                        size_bytes=sample["size_bytes"],
                    )
                    key = sample["sha256"]
                    files.setdefault(
                        key,
                        {
                            "archive_path": f"files/{key}",
                            "sha256": key,
                            "size_bytes": sample["size_bytes"],
                        },
                    )
                    resolved[key] = path
                    samples.append(
                        {
                            "original_filename": sample["original_filename"],
                            "content_sha256": key,
                        }
                    )
                versions.append(
                    {
                        "source_type": source["source_type"],
                        "status": source["status"],
                        "samples": samples,
                    }
                )
            calibration_sets.append(
                {
                    "name": calibration_set["name"],
                    "description": calibration_set["description"],
                    "versions": versions,
                }
            )
        manifest = {
            "format": "rdkwt-project",
            "schema_version": 1,
            "project": {
                "name": project["name"],
                "description": project["description"],
                "models": models,
                "calibration_sets": calibration_sets,
            },
            "scope": {
                "models": True,
                "calibration_data": True,
                "conversion_runs": False,
                "credentials": False,
            },
            "files": [files[key] for key in sorted(files)],
        }
        self.exports_root.mkdir(parents=True, exist_ok=True)
        destination = self.exports_root / f"rdkwt-project-{project_id}.zip"
        temporary = self.exports_root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with zipfile.ZipFile(
                temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                temporary.chmod(0o600)
                for key in sorted(resolved):
                    archive.write(resolved[key], files[key]["archive_path"])
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                )
            os.replace(temporary, destination)
            destination.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    async def import_project(
        self,
        *,
        content_length: int | None,
        chunks: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        maximum = self.asset_store.max_upload_bytes
        if content_length is not None and content_length > maximum:
            raise CatalogError(
                "PROJECT_PACKAGE_TOO_LARGE", "project package exceeds upload limit", status_code=413
            )
        temporary = self.asset_store.staging_root / f"{uuid.uuid4().hex}.project.zip.part"
        size = 0
        project_id: str | None = None
        try:
            with temporary.open("xb") as output:
                temporary.chmod(0o600)
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise CatalogError(
                            "PROJECT_PACKAGE_INVALID", "upload stream returned non-bytes"
                        )
                    size += len(chunk)
                    if size > maximum:
                        raise CatalogError(
                            "PROJECT_PACKAGE_TOO_LARGE",
                            "project package exceeds upload limit",
                            status_code=413,
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise CatalogError("PROJECT_PACKAGE_EMPTY", "project package is empty")
            if content_length is not None and size != content_length:
                raise CatalogError(
                    "PROJECT_PACKAGE_SIZE_MISMATCH", "uploaded byte count is incorrect"
                )
            manifest, members = self._validate_project_package(temporary)
            project_data = manifest["project"]
            created = self.create_project(
                name=project_data["name"], description=project_data.get("description", "")
            )
            project_id = created["id"]
            model_version_ids: list[str] = []
            with zipfile.ZipFile(temporary) as archive:
                for model in project_data["models"]:
                    model_name = _clean_text(model["name"], "model name", maximum=200)
                    for version in model["versions"]:
                        filename = validate_display_filename(version["original_filename"])
                        record = members[version["content_sha256"]]
                        blob = await self.asset_store.ingest(
                            self._zip_member_chunks(archive, record["info"]),
                            kind="model",
                            display_name=filename,
                            content_length=record["size_bytes"],
                        )
                        imported = self.repository.create_model(
                            project_id=project_id,
                            model_name=model_name,
                            original_filename=filename,
                            blob=blob,
                        )
                        model_version_ids.append(imported["id"])
                for calibration_set in project_data["calibration_sets"]:
                    set_name = _clean_text(calibration_set["name"], "calibration name", maximum=200)
                    description = _clean_text(
                        calibration_set.get("description", ""),
                        "calibration description",
                        maximum=4000,
                        allow_empty=True,
                        allow_newlines=True,
                    )
                    for version in calibration_set["versions"]:
                        source_type = version["source_type"]
                        created_set = self.create_calibration_set(
                            project_id=project_id,
                            name=set_name,
                            description=description,
                            source_type=source_type,
                        )
                        version_id = created_set["versions"][0]["id"]
                        blobs: list[StoredBlob] = []
                        for sample in version["samples"]:
                            original = sample["original_filename"]
                            display_name = original
                            input_name: str | None = None
                            if source_type == "npy_multi":
                                logical = PurePosixPath(original)
                                if len(logical.parts) != 2:
                                    raise CatalogError(
                                        "PROJECT_PACKAGE_INVALID",
                                        "multi-input sample path must use <input>/<sample>.npy",
                                    )
                                input_name = validate_display_filename(logical.parts[0])
                                display_name = validate_display_filename(logical.parts[1])
                            else:
                                validate_display_filename(display_name)
                            record = members[sample["content_sha256"]]
                            blob = await self.asset_store.ingest(
                                self._zip_member_chunks(archive, record["info"]),
                                kind="calibration",
                                display_name=display_name,
                                content_length=record["size_bytes"],
                                calibration_source_type=source_type,
                            )
                            if input_name is not None:
                                blob = replace(
                                    blob,
                                    display_name=original,
                                    validation={
                                        **(blob.validation or {}),
                                        "input_name": input_name,
                                        "sample_key": display_name,
                                    },
                                )
                            blobs.append(blob)
                        if blobs:
                            self.repository.add_calibration_samples(
                                version_id=version_id, blobs=blobs
                            )
                        if version["status"] == "READY":
                            self.finalize_calibration_version(version_id)
            return {
                "project": self.get_project(project_id),
                "model_version_ids": model_version_ids,
                "inspection_required": len(model_version_ids),
            }
        except (AssetStoreError, zipfile.BadZipFile) as exc:
            if project_id is not None:
                self._rollback_import(project_id)
            if isinstance(exc, AssetStoreError):
                raise self._asset_error(exc) from exc
            raise CatalogError("PROJECT_PACKAGE_INVALID", "project ZIP is invalid") from exc
        except Exception:
            if project_id is not None:
                self._rollback_import(project_id)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_project_package(
        self, path: Path
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        try:
            archive = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise CatalogError("PROJECT_PACKAGE_INVALID", "project ZIP cannot be read") from exc
        with archive:
            items = archive.infolist()
            if len(items) > 1002:
                raise CatalogError(
                    "PROJECT_PACKAGE_ENTRY_LIMIT", "project package has too many entries"
                )
            by_name: dict[str, zipfile.ZipInfo] = {}
            total = 0
            for item in items:
                name = item.filename
                logical = PurePosixPath(name)
                mode = (item.external_attr >> 16) & 0xFFFF
                entry_type = stat.S_IFMT(mode)
                if (
                    not name
                    or "\\" in name
                    or "\x00" in name
                    or logical.is_absolute()
                    or any(part in {"", ".", ".."} for part in logical.parts)
                    or entry_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or (item.is_dir() and entry_type not in {0, stat.S_IFDIR})
                    or (not item.is_dir() and entry_type not in {0, stat.S_IFREG})
                    or item.flag_bits & 0x1
                    or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or name in by_name
                ):
                    raise CatalogError(
                        "PROJECT_PACKAGE_PATH_INVALID", "project ZIP has an unsafe entry"
                    )
                by_name[name] = item
                if item.is_dir():
                    continue
                total += item.file_size
                if total > self.asset_store.max_upload_bytes:
                    raise CatalogError(
                        "PROJECT_PACKAGE_TOO_LARGE",
                        "project package expands beyond upload limit",
                        status_code=413,
                    )
                if (
                    item.file_size > 1024 * 1024
                    and item.file_size > max(1, item.compress_size) * 200
                ):
                    raise CatalogError(
                        "PROJECT_PACKAGE_RATIO_INVALID", "project ZIP compression ratio is unsafe"
                    )
            manifest_item = by_name.get("manifest.json")
            if manifest_item is None or manifest_item.file_size > 2 * 1024 * 1024:
                raise CatalogError(
                    "PROJECT_PACKAGE_MANIFEST_INVALID", "manifest is missing or too large"
                )
            try:
                manifest = json.loads(archive.read(manifest_item))
            except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
                raise CatalogError(
                    "PROJECT_PACKAGE_MANIFEST_INVALID", "manifest is invalid"
                ) from exc
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "rdkwt-project"
                or manifest.get("schema_version") != 1
                or not isinstance(manifest.get("project"), dict)
                or not isinstance(manifest.get("files"), list)
            ):
                raise CatalogError(
                    "PROJECT_PACKAGE_UNSUPPORTED", "project package format is unsupported"
                )
            project = manifest["project"]
            if (
                not isinstance(project.get("name"), str)
                or not isinstance(project.get("description", ""), str)
                or not isinstance(project.get("models"), list)
                or not isinstance(project.get("calibration_sets"), list)
                or len(project["models"]) > 100
                or len(project["calibration_sets"]) > 100
            ):
                raise CatalogError(
                    "PROJECT_PACKAGE_MANIFEST_INVALID", "project metadata is invalid"
                )
            files: dict[str, dict[str, Any]] = {}
            expected_names = {"manifest.json"}
            for record in manifest["files"]:
                if not isinstance(record, dict):
                    raise CatalogError("PROJECT_PACKAGE_MANIFEST_INVALID", "file record is invalid")
                digest = record.get("sha256")
                archive_path = record.get("archive_path")
                size_bytes = record.get("size_bytes")
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    or digest in files
                    or archive_path != f"files/{digest}"
                    or not isinstance(size_bytes, int)
                    or size_bytes < 1
                    or archive_path not in by_name
                ):
                    raise CatalogError("PROJECT_PACKAGE_MANIFEST_INVALID", "file record is invalid")
                item = by_name[archive_path]
                if item.file_size != size_bytes:
                    raise CatalogError("PROJECT_PACKAGE_SIZE_MISMATCH", "asset size does not match")
                hash_value = hashlib.sha256()
                with archive.open(item) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        hash_value.update(chunk)
                if hash_value.hexdigest() != digest:
                    raise CatalogError("PROJECT_PACKAGE_HASH_MISMATCH", "asset hash does not match")
                files[digest] = {**record, "info": item}
                expected_names.add(archive_path)
            actual_names = {name for name, item in by_name.items() if not item.is_dir()}
            if actual_names != expected_names:
                raise CatalogError(
                    "PROJECT_PACKAGE_MANIFEST_MISMATCH", "manifest does not match ZIP"
                )
            self._validate_project_topology(project, set(files))
            return manifest, files

    @staticmethod
    def _validate_project_topology(project: dict[str, Any], file_hashes: set[str]) -> None:
        for model in project["models"]:
            if (
                not isinstance(model, dict)
                or not isinstance(model.get("name"), str)
                or not isinstance(model.get("versions"), list)
                or not 1 <= len(model["versions"]) <= 20
            ):
                raise CatalogError("PROJECT_PACKAGE_MANIFEST_INVALID", "model metadata is invalid")
            for version in model["versions"]:
                if (
                    not isinstance(version, dict)
                    or not isinstance(version.get("original_filename"), str)
                    or version.get("content_sha256") not in file_hashes
                ):
                    raise CatalogError(
                        "PROJECT_PACKAGE_MANIFEST_INVALID", "model version is invalid"
                    )
        for calibration_set in project["calibration_sets"]:
            if (
                not isinstance(calibration_set, dict)
                or not isinstance(calibration_set.get("name"), str)
                or not isinstance(calibration_set.get("description", ""), str)
                or not isinstance(calibration_set.get("versions"), list)
                or not 1 <= len(calibration_set["versions"]) <= 20
            ):
                raise CatalogError(
                    "PROJECT_PACKAGE_MANIFEST_INVALID", "calibration metadata is invalid"
                )
            for version in calibration_set["versions"]:
                if (
                    not isinstance(version, dict)
                    or version.get("source_type") not in {"images", "npy", "npy_multi"}
                    or version.get("status") not in {"DRAFT", "READY"}
                    or not isinstance(version.get("samples"), list)
                    or not 0
                    <= len(version["samples"])
                    <= (400 if version.get("source_type") == "npy_multi" else 100)
                    or (version.get("status") == "READY" and not version["samples"])
                ):
                    raise CatalogError(
                        "PROJECT_PACKAGE_MANIFEST_INVALID", "calibration version is invalid"
                    )
                for sample in version["samples"]:
                    if (
                        not isinstance(sample, dict)
                        or not isinstance(sample.get("original_filename"), str)
                        or sample.get("content_sha256") not in file_hashes
                    ):
                        raise CatalogError(
                            "PROJECT_PACKAGE_MANIFEST_INVALID", "sample metadata is invalid"
                        )

    @staticmethod
    async def _zip_member_chunks(archive: zipfile.ZipFile, item: zipfile.ZipInfo):
        with archive.open(item) as source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    def _rollback_import(self, project_id: str) -> None:
        try:
            self.delete_project(project_id, confirmation=project_id)
        except Exception:
            logger.exception("project import rollback failed for %s", project_id)

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
