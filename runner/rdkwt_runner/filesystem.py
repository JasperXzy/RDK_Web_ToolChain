from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


class UnsafePathError(ValueError):
    pass


def validate_logical_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise UnsafePathError("logical path must be a non-empty string")
    if "\\" in value:
        raise UnsafePathError("backslashes are not allowed in logical paths")
    logical = PurePosixPath(value)
    if logical.is_absolute() or ".." in logical.parts or "." in logical.parts:
        raise UnsafePathError(f"unsafe logical path: {value!r}")
    return logical


def resolve_within(root: Path, logical_path: str, *, must_exist: bool = False) -> Path:
    root = root.resolve(strict=True)
    logical = validate_logical_path(logical_path)
    candidate = root.joinpath(*logical.parts)

    current = root
    for part in logical.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise UnsafePathError(f"symbolic links are not allowed: {logical_path!r}")

    resolved = candidate.resolve(strict=must_exist)
    if resolved != root and root not in resolved.parents:
        raise UnsafePathError(f"path escapes configured root: {logical_path!r}")
    return resolved


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
