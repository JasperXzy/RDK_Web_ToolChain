from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class AssetStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredBlob:
    blob_key: str
    sha256: str
    size_bytes: int
    mime_type: str
    display_name: str


@dataclass(frozen=True, slots=True)
class MaterializedCalibration:
    source_path: str
    manifest_key: str
    manifest_sha256: str
    validation_report: dict[str, Any]


def validate_display_filename(value: str) -> str:
    if not value or len(value) > 255:
        raise AssetStoreError(
            "UPLOAD_FILENAME_INVALID", "filename must contain 1 to 255 characters"
        )
    if value in {".", ".."} or any(character in value for character in "/\\\x00\r\n"):
        raise AssetStoreError("UPLOAD_FILENAME_INVALID", "filename contains unsupported characters")
    if any(ord(character) < 32 for character in value):
        raise AssetStoreError("UPLOAD_FILENAME_INVALID", "filename contains control characters")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_type(prefix: bytes) -> tuple[str, set[str]] | None:
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", {".jpeg", ".jpg"}
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", {".png"}
    if prefix.startswith(b"BM"):
        return "image/bmp", {".bmp"}
    return None


class AssetStore:
    def __init__(self, root: Path, *, max_upload_bytes: int) -> None:
        self.root = root.resolve(strict=True)
        self.max_upload_bytes = max_upload_bytes
        self.staging_root = self.root / "upload-staging"
        self.blobs_root = self.root / "blobs" / "sha256"
        self.calibration_root = self.root / "calibration-sets"
        for path in (self.staging_root, self.blobs_root, self.calibration_root):
            path.mkdir(parents=True, exist_ok=True)

    async def ingest(
        self,
        chunks: AsyncIterable[bytes],
        *,
        kind: str,
        display_name: str,
        content_length: int | None,
    ) -> StoredBlob:
        display_name = validate_display_filename(display_name)
        if content_length is not None and content_length < 0:
            raise AssetStoreError("UPLOAD_INVALID", "Content-Length must not be negative")
        if content_length is not None and content_length > self.max_upload_bytes:
            raise AssetStoreError(
                "UPLOAD_TOO_LARGE",
                f"upload exceeds the {self.max_upload_bytes}-byte limit",
            )
        if kind not in {"model", "calibration"}:
            raise AssetStoreError("UPLOAD_KIND_INVALID", "unsupported asset kind")
        suffix = Path(display_name).suffix.lower()
        if kind == "model" and suffix != ".onnx":
            raise AssetStoreError("MODEL_EXTENSION_INVALID", "model filename must end in .onnx")

        temporary = self.staging_root / f"{uuid.uuid4()}.part"
        digest = hashlib.sha256()
        size_bytes = 0
        prefix = bytearray()
        try:
            with temporary.open("xb") as handle:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise AssetStoreError("UPLOAD_INVALID", "upload stream returned non-bytes")
                    if not chunk:
                        continue
                    size_bytes += len(chunk)
                    if size_bytes > self.max_upload_bytes:
                        raise AssetStoreError(
                            "UPLOAD_TOO_LARGE",
                            f"upload exceeds the {self.max_upload_bytes}-byte limit",
                        )
                    if len(prefix) < 16:
                        prefix.extend(chunk[: 16 - len(prefix)])

                    digest.update(chunk)
                    handle.write(chunk)

                handle.flush()
                os.fsync(handle.fileno())
            if size_bytes == 0:
                raise AssetStoreError("UPLOAD_EMPTY", "uploaded file is empty")

            if kind == "calibration":
                detected = _image_type(bytes(prefix))
                if detected is None:
                    raise AssetStoreError(
                        "CALIBRATION_FORMAT_INVALID",
                        "calibration sample must be a JPEG, PNG, or BMP image",
                    )
                mime_type, allowed_suffixes = detected
                if suffix not in allowed_suffixes:
                    raise AssetStoreError(
                        "CALIBRATION_EXTENSION_MISMATCH",
                        "calibration filename extension does not match its content",
                    )
            else:
                mime_type = "application/onnx"

            sha256 = digest.hexdigest()
            stored_name = f"{sha256}.onnx" if kind == "model" else sha256
            blob_key = f"blobs/sha256/{sha256[:2]}/{stored_name}"
            destination = self.root / blob_key
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if (
                    not destination.is_file()
                    or destination.is_symlink()
                    or destination.stat().st_size != size_bytes
                    or _sha256_file(destination) != sha256
                ):
                    raise AssetStoreError(
                        "BLOB_INTEGRITY_FAILED",
                        f"existing content-addressed blob failed integrity validation: {sha256}",
                    )
            else:
                os.replace(temporary, destination)
            return StoredBlob(
                blob_key=blob_key,
                sha256=sha256,
                size_bytes=size_bytes,
                mime_type=mime_type,
                display_name=display_name,
            )
        finally:
            if temporary.exists():
                temporary.unlink()

    def materialize_calibration(
        self,
        version_id: str,
        samples: list[dict[str, Any]],
    ) -> MaterializedCalibration:
        final_root = self.calibration_root / version_id
        if final_root.exists():
            raise AssetStoreError(
                "CALIBRATION_ALREADY_MATERIALIZED",
                "calibration version already has materialized source files",
            )
        staging = self.calibration_root / f".{version_id}.{uuid.uuid4().hex}.tmp"
        source_root = staging / "source"
        source_root.mkdir(parents=True)
        manifest_samples: list[dict[str, Any]] = []
        digests: list[str] = []
        try:
            for sample in samples:
                source = self.resolve_blob(str(sample["blob_key"]))
                mime_type = str(sample["mime_type"])
                extension = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/bmp": ".bmp",
                }.get(mime_type)
                if extension is None:
                    raise AssetStoreError(
                        "CALIBRATION_FORMAT_INVALID",
                        f"unsupported stored calibration MIME type: {mime_type}",
                    )
                materialized_name = (
                    f"{int(sample['ordinal']):04d}_{str(sample['sha256'])[:12]}{extension}"
                )
                destination = source_root / materialized_name
                try:
                    os.link(source, destination, follow_symlinks=False)
                except OSError:
                    shutil.copyfile(source, destination, follow_symlinks=False)
                digest = _sha256_file(destination)
                if digest != sample["sha256"]:
                    raise AssetStoreError(
                        "BLOB_INTEGRITY_FAILED",
                        f"materialized sample failed hash validation: {materialized_name}",
                    )
                digests.append(digest)
                manifest_samples.append(
                    {
                        "id": sample["id"],
                        "ordinal": sample["ordinal"],
                        "original_filename": sample["original_filename"],
                        "materialized_name": materialized_name,
                        "sha256": digest,
                        "size_bytes": sample["size_bytes"],
                        "mime_type": mime_type,
                    }
                )
            duplicate_count = len(digests) - len(set(digests))
            validation_report = {
                "sample_count": len(samples),
                "minimum_recommended": 20,
                "maximum_recommended": 100,
                "duplicate_content_count": duplicate_count,
                "warnings": (
                    ["fewer than 20 samples; standard M1 conversion will reject this version"]
                    if len(samples) < 20
                    else []
                ),
            }
            manifest = {
                "schema_version": "1",
                "calibration_version_id": version_id,
                "source_type": "images",
                "sample_count": len(samples),
                "samples": manifest_samples,
                "validation_report": validation_report,
            }
            encoded = json.dumps(
                manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()
            manifest_sha256 = hashlib.sha256(encoded).hexdigest()
            manifest["manifest_sha256"] = manifest_sha256
            manifest_path = staging / "manifest.json"
            with manifest_path.open("w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, final_root)
            return MaterializedCalibration(
                source_path=f"calibration-sets/{version_id}/source",
                manifest_key=f"calibration-sets/{version_id}/manifest.json",
                manifest_sha256=manifest_sha256,
                validation_report=validation_report,
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def resolve_blob(self, blob_key: str) -> Path:
        logical = PurePosixPath(blob_key)
        if logical.is_absolute() or "." in logical.parts or ".." in logical.parts:
            raise AssetStoreError("BLOB_PATH_INVALID", "stored blob path is invalid")
        current = self.root
        for part in logical.parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise AssetStoreError("BLOB_PATH_INVALID", "stored blob path contains a symlink")
        resolved = current.resolve(strict=True)
        if self.root not in resolved.parents or not resolved.is_file() or resolved.is_symlink():
            raise AssetStoreError("BLOB_PATH_INVALID", "stored blob is not a regular file")
        return resolved

    def resolve_verified_blob(
        self, blob_key: str, *, sha256: str, size_bytes: int
    ) -> Path:
        path = self.resolve_blob(blob_key)
        if path.stat().st_size != size_bytes or _sha256_file(path) != sha256:
            raise AssetStoreError(
                "BLOB_INTEGRITY_FAILED",
                "stored content no longer matches its catalog metadata",
            )
        return path

    def delete_unreferenced(self, *, blob_keys: list[str], version_ids: list[str]) -> None:
        for blob_key in blob_keys:
            try:
                path = self.resolve_blob(blob_key)
            except FileNotFoundError:
                continue
            path.unlink()
            parent = path.parent
            if parent != self.blobs_root and not any(parent.iterdir()):
                parent.rmdir()
        for version_id in version_ids:
            if not version_id or any(
                character not in "0123456789abcdef-" for character in version_id
            ):
                raise AssetStoreError(
                    "CALIBRATION_PATH_INVALID", "calibration version ID is invalid"
                )
            path = self.calibration_root / version_id
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_dir():
                raise AssetStoreError(
                    "CALIBRATION_PATH_INVALID",
                    "materialized calibration path is not a regular directory",
                )
            shutil.rmtree(path)
