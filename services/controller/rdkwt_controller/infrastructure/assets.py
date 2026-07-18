from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import uuid
import zipfile
from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np


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
    validation: dict[str, Any] | None = None


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


def _npy_validation(path: Path) -> dict[str, Any]:
    try:
        array = np.load(
            path,
            allow_pickle=False,
            mmap_mode="r",
            max_header_size=16 * 1024,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise AssetStoreError(
            "CALIBRATION_NPY_INVALID", f"NPY sample cannot be read safely: {exc}"
        ) from exc
    if not isinstance(array, np.ndarray):
        raise AssetStoreError("CALIBRATION_NPY_INVALID", "NPY sample must contain one array")
    dtype = array.dtype
    allowed_dtypes = {
        "bool",
        "int8",
        "int16",
        "int32",
        "uint8",
        "uint16",
        "uint32",
        "float16",
        "float32",
        "float64",
    }
    if dtype.hasobject or dtype.fields or dtype.subdtype or dtype.name not in allowed_dtypes:
        raise AssetStoreError(
            "CALIBRATION_NPY_DTYPE_UNSUPPORTED",
            f"NPY dtype {dtype} is not a supported numeric calibration dtype",
        )
    if dtype.byteorder == ">" or (dtype.byteorder == "=" and not np.little_endian):
        raise AssetStoreError(
            "CALIBRATION_NPY_ENDIAN_UNSUPPORTED",
            "big-endian NPY samples are not supported",
        )
    if not 1 <= array.ndim <= 4 or any(int(item) < 1 for item in array.shape):
        raise AssetStoreError(
            "CALIBRATION_NPY_SHAPE_INVALID",
            "NPY shape must contain one to four positive dimensions",
        )
    if not array.flags.c_contiguous:
        raise AssetStoreError(
            "CALIBRATION_NPY_LAYOUT_UNSUPPORTED",
            "Fortran-order NPY samples are not supported",
        )
    offset = int(getattr(array, "offset", 0))
    if offset < 1 or offset + int(array.nbytes) != path.stat().st_size:
        raise AssetStoreError(
            "CALIBRATION_NPY_SIZE_MISMATCH",
            "NPY payload size does not match its header",
        )

    minimum = math.inf
    maximum = -math.inf
    total = 0.0
    total_square = 0.0
    count = int(array.size)
    flattened = array.reshape(-1)
    for start in range(0, count, 1024 * 1024):
        values = np.asarray(flattened[start : start + 1024 * 1024], dtype=np.float64)
        if not np.isfinite(values).all():
            raise AssetStoreError(
                "CALIBRATION_NPY_NON_FINITE",
                "NPY sample contains NaN or infinite values",
            )
        minimum = min(minimum, float(values.min()))
        maximum = max(maximum, float(values.max()))
        with np.errstate(over="ignore", invalid="ignore"):
            total += float(values.sum(dtype=np.float64))
            total_square += float(np.square(values).sum(dtype=np.float64))
        if not math.isfinite(total) or not math.isfinite(total_square):
            raise AssetStoreError(
                "CALIBRATION_NPY_STATISTICS_UNREPRESENTABLE",
                "NPY sample values are too large for safe finite statistics",
            )
    mean = total / count
    variance = max(0.0, total_square / count - mean * mean)
    return {
        "format": "npy",
        "dtype": dtype.name,
        "shape": [int(item) for item in array.shape],
        "fortran_order": False,
        "element_count": count,
        "statistics": {
            "minimum": minimum,
            "maximum": maximum,
            "mean": mean,
            "standard_deviation": math.sqrt(variance),
        },
    }


class AssetStore:
    def __init__(self, root: Path, *, max_upload_bytes: int) -> None:
        self.root = root.resolve(strict=True)
        self.max_upload_bytes = max_upload_bytes
        self.staging_root = self.root / "upload-staging"
        self.blobs_root = self.root / "blobs" / "sha256"
        self.calibration_root = self.root / "calibration-sets"
        for path in (self.staging_root, self.blobs_root, self.calibration_root):
            path.mkdir(parents=True, exist_ok=True)

    def _validate_staged_blob(
        self,
        path: Path,
        *,
        kind: str,
        display_name: str,
        calibration_source_type: str,
        sha256: str,
        size_bytes: int,
        prefix: bytes,
    ) -> StoredBlob:
        display_name = validate_display_filename(display_name)
        if kind not in {"model", "calibration"}:
            raise AssetStoreError("UPLOAD_KIND_INVALID", "unsupported asset kind")
        if calibration_source_type not in {"images", "npy"}:
            raise AssetStoreError(
                "CALIBRATION_SOURCE_TYPE_INVALID", "unsupported calibration source type"
            )
        suffix = Path(display_name).suffix.lower()
        validation: dict[str, Any] | None = None
        if kind == "model":
            if suffix != ".onnx":
                raise AssetStoreError(
                    "MODEL_EXTENSION_INVALID", "model filename must end in .onnx"
                )
            mime_type = "application/onnx"
        elif calibration_source_type == "images":
            detected = _image_type(prefix)
            if detected is None:
                raise AssetStoreError(
                    "CALIBRATION_FORMAT_INVALID",
                    "image calibration sample must be JPEG, PNG, or BMP",
                )
            mime_type, allowed_suffixes = detected
            if suffix not in allowed_suffixes:
                raise AssetStoreError(
                    "CALIBRATION_EXTENSION_MISMATCH",
                    "calibration filename extension does not match its content",
                )
            validation = {"format": "image", "mime_type": mime_type}
        else:
            if suffix != ".npy":
                raise AssetStoreError(
                    "CALIBRATION_EXTENSION_MISMATCH",
                    "direct NPY samples must end in .npy",
                )
            if not prefix.startswith(b"\x93NUMPY"):
                raise AssetStoreError(
                    "CALIBRATION_FORMAT_INVALID", "file does not contain an NPY array"
                )
            mime_type = "application/x-npy"
            validation = _npy_validation(path)
        stored_name = f"{sha256}.onnx" if kind == "model" else sha256
        return StoredBlob(
            blob_key=f"blobs/sha256/{sha256[:2]}/{stored_name}",
            sha256=sha256,
            size_bytes=size_bytes,
            mime_type=mime_type,
            display_name=display_name,
            validation=validation,
        )

    def _promote_staged_blob(self, path: Path, blob: StoredBlob) -> None:
        destination = self.root / blob.blob_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if (
                not destination.is_file()
                or destination.is_symlink()
                or destination.stat().st_size != blob.size_bytes
                or _sha256_file(destination) != blob.sha256
            ):
                raise AssetStoreError(
                    "BLOB_INTEGRITY_FAILED",
                    "existing content-addressed blob failed integrity validation: "
                    f"{blob.sha256}",
                )
        else:
            os.replace(path, destination)

    async def ingest(
        self,
        chunks: AsyncIterable[bytes],
        *,
        kind: str,
        display_name: str,
        content_length: int | None,
        calibration_source_type: str = "images",
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
        if calibration_source_type not in {"images", "npy"}:
            raise AssetStoreError(
                "CALIBRATION_SOURCE_TYPE_INVALID", "unsupported calibration source type"
            )
        suffix = Path(display_name).suffix.lower()
        if kind == "model" and suffix != ".onnx":
            raise AssetStoreError("MODEL_EXTENSION_INVALID", "model filename must end in .onnx")
        if kind == "calibration" and calibration_source_type == "npy" and suffix != ".npy":
            raise AssetStoreError(
                "CALIBRATION_EXTENSION_MISMATCH", "direct NPY samples must end in .npy"
            )

        temporary = self.staging_root / f"{uuid.uuid4()}.part"
        digest = hashlib.sha256()
        size_bytes = 0
        prefix = bytearray()
        try:
            with temporary.open("xb") as handle:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise AssetStoreError(
                            "UPLOAD_INVALID", "upload stream returned non-bytes"
                        )
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
            if content_length is not None and size_bytes != content_length:
                raise AssetStoreError(
                    "UPLOAD_SIZE_MISMATCH", "uploaded byte count does not match Content-Length"
                )

            blob = self._validate_staged_blob(
                temporary,
                kind=kind,
                display_name=display_name,
                calibration_source_type=calibration_source_type,
                sha256=digest.hexdigest(),
                size_bytes=size_bytes,
                prefix=bytes(prefix),
            )
            self._promote_staged_blob(temporary, blob)
            return blob
        finally:
            if temporary.exists():
                temporary.unlink()

    async def ingest_calibration_archive(
        self,
        chunks: AsyncIterable[bytes],
        *,
        display_name: str,
        content_length: int | None,
        source_type: str,
        maximum_entries: int,
    ) -> list[StoredBlob]:
        display_name = validate_display_filename(display_name)
        if Path(display_name).suffix.lower() != ".zip":
            raise AssetStoreError(
                "CALIBRATION_ARCHIVE_EXTENSION_INVALID", "archive filename must end in .zip"
            )
        if source_type not in {"images", "npy"}:
            raise AssetStoreError(
                "CALIBRATION_SOURCE_TYPE_INVALID", "unsupported calibration source type"
            )
        if maximum_entries < 1:
            raise AssetStoreError(
                "CALIBRATION_SAMPLE_LIMIT", "calibration version already contains 100 samples"
            )
        archive_path = self.staging_root / f"{uuid.uuid4()}.zip.part"
        size_bytes = 0
        try:
            with archive_path.open("xb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise AssetStoreError(
                            "UPLOAD_INVALID", "upload stream returned non-bytes"
                        )
                    size_bytes += len(chunk)
                    if size_bytes > self.max_upload_bytes:
                        raise AssetStoreError(
                            "UPLOAD_TOO_LARGE",
                            f"archive exceeds the {self.max_upload_bytes}-byte limit",
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size_bytes == 0:
                raise AssetStoreError("UPLOAD_EMPTY", "uploaded archive is empty")
            if content_length is not None and size_bytes != content_length:
                raise AssetStoreError(
                    "UPLOAD_SIZE_MISMATCH", "uploaded byte count does not match Content-Length"
                )
            try:
                archive = zipfile.ZipFile(archive_path)
            except (OSError, zipfile.BadZipFile) as exc:
                raise AssetStoreError(
                    "CALIBRATION_ARCHIVE_INVALID", f"ZIP archive cannot be read: {exc}"
                ) from exc
            with archive:
                entries: list[tuple[zipfile.ZipInfo, str]] = []
                names: set[str] = set()
                total_uncompressed = 0
                archive_entries = archive.infolist()
                if len(archive_entries) > maximum_entries + 100:
                    raise AssetStoreError(
                        "CALIBRATION_ARCHIVE_ENTRY_LIMIT",
                        "ZIP contains too many file and directory entries",
                    )
                for item in archive_entries:
                    name = item.filename
                    if not name or "\\" in name or "\x00" in name:
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_PATH_INVALID",
                            "ZIP entry contains an invalid path",
                        )
                    logical = PurePosixPath(name)
                    if logical.is_absolute() or any(
                        part in {"", ".", ".."} for part in logical.parts
                    ):
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_PATH_INVALID",
                            f"ZIP entry path is unsafe: {name}",
                        )
                    if item.is_dir():
                        continue
                    mode = (item.external_attr >> 16) & 0xFFFF
                    file_type = stat.S_IFMT(mode)
                    if file_type not in {0, stat.S_IFREG}:
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_ENTRY_INVALID",
                            f"ZIP entry is not a regular file: {name}",
                        )
                    if item.flag_bits & 0x1:
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_ENCRYPTED",
                            f"encrypted ZIP entries are not supported: {name}",
                        )
                    if item.compress_type not in {
                        zipfile.ZIP_STORED,
                        zipfile.ZIP_DEFLATED,
                    }:
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_COMPRESSION_UNSUPPORTED",
                            f"ZIP entry uses an unsupported compression method: {name}",
                        )
                    basename = validate_display_filename(logical.name)
                    suffix = Path(basename).suffix.lower()
                    allowed = (
                        {".npy"}
                        if source_type == "npy"
                        else {".bmp", ".jpeg", ".jpg", ".png"}
                    )
                    if suffix not in allowed:
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_ENTRY_INVALID",
                            f"ZIP entry does not match {source_type} calibration: {name}",
                        )
                    normalized = basename.casefold()
                    if normalized in names:
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_DUPLICATE_NAME",
                            f"ZIP contains duplicate sample filename: {basename}",
                        )
                    names.add(normalized)
                    if item.file_size < 1 or item.file_size > self.max_upload_bytes:
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_ENTRY_TOO_LARGE",
                            f"ZIP entry has an invalid size: {name}",
                        )
                    if (
                        item.file_size > 1024 * 1024
                        and item.file_size > max(1, item.compress_size) * 200
                    ):
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_RATIO_INVALID",
                            f"ZIP entry compression ratio is unsafe: {name}",
                        )
                    total_uncompressed += item.file_size
                    if total_uncompressed > self.max_upload_bytes:
                        raise AssetStoreError(
                            "CALIBRATION_ARCHIVE_TOO_LARGE",
                            "ZIP uncompressed content exceeds the upload limit",
                        )
                    entries.append((item, basename))
                if not entries:
                    raise AssetStoreError(
                        "CALIBRATION_ARCHIVE_EMPTY", "ZIP archive contains no samples"
                    )
                if len(entries) > maximum_entries:
                    raise AssetStoreError(
                        "CALIBRATION_SAMPLE_LIMIT",
                        f"ZIP contains {len(entries)} samples but only "
                        f"{maximum_entries} slots remain",
                    )
                staged: list[tuple[Path, StoredBlob]] = []
                try:
                    for item, basename in sorted(
                        entries, key=lambda value: value[0].filename
                    ):
                        entry_path = self.staging_root / f"{uuid.uuid4()}.entry.part"
                        staged_entry = False
                        try:
                            digest = hashlib.sha256()
                            prefix = bytearray()
                            extracted_size = 0
                            try:
                                with archive.open(item, "r") as source, entry_path.open(
                                    "xb"
                                ) as output:
                                    while True:
                                        chunk = source.read(1024 * 1024)
                                        if not chunk:
                                            break
                                        extracted_size += len(chunk)
                                        if extracted_size > item.file_size:
                                            raise AssetStoreError(
                                                "CALIBRATION_ARCHIVE_SIZE_MISMATCH",
                                                "ZIP entry exceeds its declared size: "
                                                f"{item.filename}",
                                            )
                                        if len(prefix) < 16:
                                            prefix.extend(chunk[: 16 - len(prefix)])
                                        digest.update(chunk)
                                        output.write(chunk)
                                    output.flush()
                                    os.fsync(output.fileno())
                            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                                raise AssetStoreError(
                                    "CALIBRATION_ARCHIVE_INVALID",
                                    "ZIP entry failed integrity validation: "
                                    f"{item.filename}",
                                ) from exc
                            if extracted_size != item.file_size:
                                raise AssetStoreError(
                                    "CALIBRATION_ARCHIVE_SIZE_MISMATCH",
                                    "ZIP entry does not match its declared size: "
                                    f"{item.filename}",
                                )
                            blob = self._validate_staged_blob(
                                entry_path,
                                kind="calibration",
                                display_name=basename,
                                calibration_source_type=source_type,
                                sha256=digest.hexdigest(),
                                size_bytes=extracted_size,
                                prefix=bytes(prefix),
                            )
                            staged.append((entry_path, blob))
                            staged_entry = True
                        finally:
                            if not staged_entry and entry_path.exists():
                                entry_path.unlink()
                    for entry_path, blob in staged:
                        self._promote_staged_blob(entry_path, blob)
                    return [blob for _entry_path, blob in staged]
                finally:
                    for entry_path, _blob in staged:
                        if entry_path.exists():
                            entry_path.unlink()
        finally:
            if archive_path.exists():
                archive_path.unlink()

    def materialize_calibration(
        self,
        version_id: str,
        source_type: str,
        samples: list[dict[str, Any]],
    ) -> MaterializedCalibration:
        if source_type not in {"images", "npy"}:
            raise AssetStoreError(
                "CALIBRATION_SOURCE_TYPE_INVALID", "unsupported calibration source type"
            )
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
        validations: list[dict[str, Any]] = []
        try:
            for sample in samples:
                source = self.resolve_verified_blob(
                    str(sample["blob_key"]),
                    sha256=str(sample["sha256"]),
                    size_bytes=int(sample["size_bytes"]),
                )
                mime_type = str(sample["mime_type"])
                extension = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/bmp": ".bmp",
                    "application/x-npy": ".npy",
                }.get(mime_type)
                if extension is None or (source_type == "npy") != (extension == ".npy"):
                    raise AssetStoreError(
                        "CALIBRATION_FORMAT_INVALID",
                        f"stored sample does not match {source_type} calibration: {mime_type}",
                    )
                if source_type == "npy":
                    validation = _npy_validation(source)
                else:
                    with source.open("rb") as handle:
                        detected = _image_type(handle.read(16))
                    if detected is None or detected[0] != mime_type:
                        raise AssetStoreError(
                            "CALIBRATION_FORMAT_INVALID",
                            "stored image sample no longer matches its registered MIME type",
                        )
                    validation = {"format": "image", "mime_type": mime_type}
                validations.append(validation)
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
                        "validation": validation,
                    }
                )
            if source_type == "npy":
                shapes = {tuple(item["shape"]) for item in validations}
                dtypes = {str(item["dtype"]) for item in validations}
                if len(shapes) != 1 or len(dtypes) != 1:
                    raise AssetStoreError(
                        "CALIBRATION_NPY_INCONSISTENT",
                        "all NPY samples must use the same Shape and dtype",
                    )
            duplicate_count = len(digests) - len(set(digests))
            warnings: list[str] = []
            if len(samples) < 20:
                warnings.append(
                    "fewer than 20 samples; standard conversion will reject this version"
                )
            if duplicate_count:
                warnings.append(f"{duplicate_count} samples duplicate existing content")
            constant_count = sum(
                1
                for item in validations
                if item.get("statistics", {}).get("standard_deviation") == 0
            )
            if constant_count:
                warnings.append(f"{constant_count} NPY samples contain constant values")
            validation_report: dict[str, Any] = {
                "source_type": source_type,
                "sample_count": len(samples),
                "minimum_recommended": 20,
                "maximum_recommended": 100,
                "duplicate_content_count": duplicate_count,
                "warnings": warnings,
            }
            if source_type == "npy":
                validation_report.update(
                    {
                        "shape": validations[0]["shape"],
                        "dtype": validations[0]["dtype"],
                        "first_sample_statistics": validations[0]["statistics"],
                        "constant_sample_count": constant_count,
                    }
                )
            manifest = {
                "schema_version": "1",
                "calibration_version_id": version_id,
                "source_type": source_type,
                "sample_count": len(samples),
                "samples": manifest_samples,
                "validation_report": validation_report,
            }
            encoded = json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            manifest_sha256 = hashlib.sha256(encoded).hexdigest()
            manifest["manifest_sha256"] = manifest_sha256
            manifest_path = staging / "manifest.json"
            with manifest_path.open("w", encoding="utf-8") as handle:
                json.dump(
                    manifest,
                    handle,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
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
