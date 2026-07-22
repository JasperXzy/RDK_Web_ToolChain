from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

_REFERENCE = re.compile(r"^[0-9a-f]{32}$")


class CredentialStoreError(RuntimeError):
    pass


class CredentialStore:
    """Small encrypted-at-rest store; SQLite only ever sees an opaque reference."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._root.is_symlink() or not self._root.is_dir():
            raise CredentialStoreError("credential store must be a regular directory")
        self._root.chmod(0o700)
        self._key_path = self._root / "master.key"
        self._fernet = Fernet(self._load_or_create_key())

    def put(self, credential: dict[str, Any], *, reference: str | None = None) -> str:
        reference = uuid.uuid4().hex if reference is None else self._validate_reference(reference)
        encoded = json.dumps(
            credential, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        encrypted = self._fernet.encrypt(encoded)
        target = self._path(reference)
        temporary = self._root / f".{reference}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            target.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
        return reference

    def get(self, reference: str) -> dict[str, Any]:
        path = self._path(reference)
        if not path.is_file() or path.is_symlink():
            raise CredentialStoreError("device credential is missing")
        try:
            decrypted = self._fernet.decrypt(path.read_bytes())
            payload = json.loads(decrypted)
        except (InvalidToken, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("device credential cannot be decrypted") from exc
        if not isinstance(payload, dict):
            raise CredentialStoreError("device credential has an invalid payload")
        return payload

    def delete(self, reference: str) -> None:
        path = self._path(reference)
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise CredentialStoreError("refusing to delete an irregular credential path")
            path.unlink()

    def _path(self, reference: str) -> Path:
        return self._root / f"{self._validate_reference(reference)}.secret"

    @staticmethod
    def _validate_reference(reference: str) -> str:
        if not _REFERENCE.fullmatch(reference):
            raise CredentialStoreError("invalid credential reference")
        return reference

    def _load_or_create_key(self) -> bytes:
        if self._key_path.exists():
            if self._key_path.is_symlink() or not self._key_path.is_file():
                raise CredentialStoreError("credential master key must be a regular file")
            self._key_path.chmod(0o600)
            key = self._key_path.read_bytes().strip()
            try:
                Fernet(key)
            except (TypeError, ValueError) as exc:
                raise CredentialStoreError("credential master key is invalid") from exc
            return key
        key = Fernet.generate_key()
        try:
            descriptor = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self._load_or_create_key()
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._key_path.chmod(0o600)
        return key
