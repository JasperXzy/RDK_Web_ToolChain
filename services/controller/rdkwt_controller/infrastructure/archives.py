from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
import zipfile
from collections.abc import AsyncIterable, Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from rdkwt_controller.settings import Settings


class ArchiveError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise ArchiveError("BACKUP_PATH_INVALID", f"backup root is not a directory: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArchiveError(
                "BACKUP_SYMLINK_REJECTED", f"backup input contains a symlink: {path}"
            )
        if path.is_file():
            yield path


def _validate_member(item: zipfile.ZipInfo) -> PurePosixPath:
    if not item.filename or "\\" in item.filename or "\x00" in item.filename:
        raise ArchiveError("BACKUP_PATH_INVALID", "backup contains an invalid member path")
    logical = PurePosixPath(item.filename)
    if logical.is_absolute() or any(part in {"", ".", ".."} for part in logical.parts):
        raise ArchiveError("BACKUP_PATH_INVALID", f"unsafe backup member: {item.filename}")
    mode = (item.external_attr >> 16) & 0xFFFF
    entry_type = stat.S_IFMT(mode)
    if (
        entry_type not in {0, stat.S_IFREG, stat.S_IFDIR}
        or (item.is_dir() and entry_type not in {0, stat.S_IFDIR})
        or (not item.is_dir() and entry_type not in {0, stat.S_IFREG})
    ):
        raise ArchiveError("BACKUP_ENTRY_INVALID", f"backup member is not a file: {item.filename}")
    if item.flag_bits & 0x1:
        raise ArchiveError("BACKUP_ENCRYPTED_UNSUPPORTED", "encrypted ZIP members are unsupported")
    if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise ArchiveError(
            "BACKUP_COMPRESSION_UNSUPPORTED", f"unsupported compression: {item.filename}"
        )
    return logical


class BackupArchive:
    FORMAT = "rdkwt-system-backup"
    SCHEMA_VERSION = 1

    def __init__(self, settings: Settings, *, app_version: str) -> None:
        self.settings = settings
        self.app_version = app_version
        self.backups_root = settings.state_dir / "backups"
        self.upload_root = self.backups_root / ".incoming"
        self.backups_root.mkdir(parents=True, exist_ok=True)
        self.upload_root.mkdir(parents=True, exist_ok=True)

    @property
    def database_path(self) -> Path:
        return self.settings.state_dir / "db" / "rdkwt.sqlite3"

    def create(
        self,
        *,
        include_runs: bool,
        include_credentials: bool,
        label: str = "backup",
    ) -> dict[str, Any]:
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ArchiveError("BACKUP_DATABASE_MISSING", "SQLite database is unavailable")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_id = f"{label}-{timestamp}-{uuid.uuid4().hex[:8]}"
        destination = self.backups_root / f"{backup_id}.rdkwt-backup.zip"
        temporary = self.backups_root / f".{destination.name}.tmp"
        database_snapshot = self.backups_root / f".{backup_id}.sqlite3"
        files: list[dict[str, Any]] = []
        published = False
        try:
            source = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
            target = sqlite3.connect(database_snapshot)
            database_snapshot.chmod(0o600)
            try:
                # A bounded page step yields between WAL lock checks and works both from
                # the request worker pool and the offline maintenance process.
                source.backup(target, pages=16, sleep=0.01)
            finally:
                target.close()
                source.close()

            inputs: list[tuple[Path, str]] = [(database_snapshot, "state/db/rdkwt.sqlite3")]
            if include_credentials:
                inputs.extend(
                    (
                        path,
                        f"state/secrets/{path.relative_to(self.settings.secrets_dir).as_posix()}",
                    )
                    for path in _safe_files(self.settings.secrets_dir)
                )
            inputs.extend(
                (path, f"assets/{path.relative_to(self.settings.assets_dir).as_posix()}")
                for path in _safe_files(self.settings.assets_dir)
                if self.settings.assets_dir / "upload-staging" not in path.parents
            )
            if include_runs:
                inputs.extend(
                    (path, f"runs/{path.relative_to(self.settings.runs_dir).as_posix()}")
                    for path in _safe_files(self.settings.runs_dir)
                )

            with zipfile.ZipFile(
                temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                temporary.chmod(0o600)
                for source_path, archive_path in inputs:
                    size_bytes = source_path.stat().st_size
                    digest = sha256_file(source_path)
                    archive.write(source_path, archive_path)
                    files.append({"path": archive_path, "sha256": digest, "size_bytes": size_bytes})
                manifest = {
                    "format": self.FORMAT,
                    "schema_version": self.SCHEMA_VERSION,
                    "backup_id": backup_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "app_version": self.app_version,
                    "scope": {
                        "database": True,
                        "assets": True,
                        "runs": include_runs,
                        "credentials": include_credentials,
                        "cache": False,
                    },
                    "files": files,
                }
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                )
            os.replace(temporary, destination)
            published = True
            destination.chmod(0o600)
            verified = self.verify(destination)
            return {
                **verified,
                "filename": destination.name,
                "size_bytes": destination.stat().st_size,
            }
        except Exception:
            if published:
                destination.unlink(missing_ok=True)
            raise
        finally:
            database_snapshot.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)

    def verify(self, path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise ArchiveError("BACKUP_FILE_INVALID", "backup must not be a symlink")
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or resolved.is_symlink():
            raise ArchiveError("BACKUP_FILE_INVALID", "backup is not a regular file")
        if resolved.stat().st_size > self.settings.max_backup_bytes:
            raise ArchiveError("BACKUP_TOO_LARGE", "backup exceeds the configured size limit")
        try:
            archive = zipfile.ZipFile(resolved)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ArchiveError("BACKUP_INVALID", f"backup ZIP cannot be read: {exc}") from exc
        with archive:
            members = archive.infolist()
            if len(members) > self.settings.max_backup_entries:
                raise ArchiveError("BACKUP_ENTRY_LIMIT", "backup contains too many entries")
            member_map: dict[str, zipfile.ZipInfo] = {}
            total = 0
            for item in members:
                logical = _validate_member(item)
                name = logical.as_posix()
                if name in member_map:
                    raise ArchiveError("BACKUP_DUPLICATE_ENTRY", f"duplicate member: {name}")
                member_map[name] = item
                if item.is_dir():
                    continue
                total += item.file_size
                if total > self.settings.max_backup_uncompressed_bytes:
                    raise ArchiveError(
                        "BACKUP_UNCOMPRESSED_TOO_LARGE",
                        "backup expands beyond the configured size limit",
                    )
                if (
                    item.file_size > 1024 * 1024
                    and item.file_size > max(1, item.compress_size) * 200
                ):
                    raise ArchiveError(
                        "BACKUP_COMPRESSION_RATIO_INVALID",
                        f"unsafe compression ratio: {name}",
                    )
                if not (
                    name == "manifest.json"
                    or name == "state/db/rdkwt.sqlite3"
                    or name.startswith("state/secrets/")
                    or name.startswith("assets/")
                    or name.startswith("runs/")
                ):
                    raise ArchiveError("BACKUP_SCOPE_INVALID", f"unexpected backup member: {name}")
            manifest_item = member_map.get("manifest.json")
            if manifest_item is None or manifest_item.file_size > 2 * 1024 * 1024:
                raise ArchiveError(
                    "BACKUP_MANIFEST_INVALID", "backup manifest is missing or too large"
                )
            try:
                manifest = json.loads(archive.read(manifest_item))
            except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
                raise ArchiveError("BACKUP_MANIFEST_INVALID", "backup manifest is invalid") from exc
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != self.FORMAT
                or manifest.get("schema_version") != self.SCHEMA_VERSION
                or not isinstance(manifest.get("files"), list)
            ):
                raise ArchiveError("BACKUP_MANIFEST_UNSUPPORTED", "unsupported backup manifest")
            scope = manifest.get("scope")
            if (
                not isinstance(scope, dict)
                or scope.get("database") is not True
                or scope.get("assets") is not True
                or type(scope.get("runs")) is not bool
                or type(scope.get("credentials")) is not bool
                or scope.get("cache") is not False
            ):
                raise ArchiveError("BACKUP_MANIFEST_INVALID", "backup scope is invalid")
            expected: dict[str, dict[str, Any]] = {}
            for record in manifest["files"]:
                if not isinstance(record, dict):
                    raise ArchiveError("BACKUP_MANIFEST_INVALID", "invalid file record")
                name = record.get("path")
                digest = record.get("sha256")
                size = record.get("size_bytes")
                if (
                    not isinstance(name, str)
                    or name == "manifest.json"
                    or name in expected
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or not isinstance(size, int)
                    or size < 0
                ):
                    raise ArchiveError("BACKUP_MANIFEST_INVALID", "invalid file record")
                expected[name] = record
            actual_names = {name for name, item in member_map.items() if not item.is_dir()} - {
                "manifest.json"
            }
            if actual_names != set(expected):
                raise ArchiveError(
                    "BACKUP_MANIFEST_MISMATCH", "manifest does not match ZIP members"
                )
            if not scope["runs"] and any(name.startswith("runs/") for name in expected):
                raise ArchiveError("BACKUP_MANIFEST_INVALID", "backup scope is invalid")
            if not scope["credentials"] and any(
                name.startswith("state/secrets/") for name in expected
            ):
                raise ArchiveError("BACKUP_MANIFEST_INVALID", "backup scope is invalid")
            for name, record in expected.items():
                item = member_map[name]
                if item.file_size != record["size_bytes"]:
                    raise ArchiveError("BACKUP_SIZE_MISMATCH", f"size mismatch: {name}")
                digest = hashlib.sha256()
                try:
                    with archive.open(item) as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise ArchiveError("BACKUP_INVALID", f"cannot read member: {name}") from exc
                if digest.hexdigest() != record["sha256"]:
                    raise ArchiveError("BACKUP_HASH_MISMATCH", f"hash mismatch: {name}")
            database = expected.get("state/db/rdkwt.sqlite3")
            if database is None:
                raise ArchiveError("BACKUP_DATABASE_MISSING", "backup does not contain SQLite")
            database_item = member_map["state/db/rdkwt.sqlite3"]
            temporary_database = self.upload_root / f".verify-{uuid.uuid4().hex}.sqlite3"
            try:
                with archive.open(database_item) as source, temporary_database.open("xb") as output:
                    temporary_database.chmod(0o600)
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                self._verify_database(temporary_database)
            finally:
                temporary_database.unlink(missing_ok=True)
            return {
                "backup_id": str(manifest.get("backup_id", "")),
                "created_at": manifest.get("created_at"),
                "app_version": manifest.get("app_version"),
                "scope": scope,
                "file_count": len(expected),
                "uncompressed_bytes": sum(item["size_bytes"] for item in expected.values()),
                "verified": True,
            }

    async def ingest(
        self,
        chunks: AsyncIterable[bytes],
        *,
        content_length: int | None,
    ) -> dict[str, Any]:
        if content_length is not None and content_length > self.settings.max_backup_bytes:
            raise ArchiveError("BACKUP_TOO_LARGE", "backup exceeds the configured size limit")
        temporary = self.upload_root / f"{uuid.uuid4().hex}.part"
        size = 0
        try:
            with temporary.open("xb") as output:
                temporary.chmod(0o600)
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise ArchiveError(
                            "BACKUP_UPLOAD_INVALID", "backup stream returned non-bytes"
                        )
                    size += len(chunk)
                    if size > self.settings.max_backup_bytes:
                        raise ArchiveError(
                            "BACKUP_TOO_LARGE", "backup exceeds the configured size limit"
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise ArchiveError("BACKUP_EMPTY", "backup upload is empty")
            if content_length is not None and content_length != size:
                raise ArchiveError("BACKUP_SIZE_MISMATCH", "uploaded byte count is incorrect")
            verified = self.verify(temporary)
            backup_id = verified["backup_id"] or f"uploaded-{uuid.uuid4().hex[:12]}"
            safe_id = "".join(
                character for character in backup_id if character.isalnum() or character in "-_"
            )
            if not safe_id:
                safe_id = f"uploaded-{uuid.uuid4().hex[:12]}"
            destination = self.backups_root / f"{safe_id}.rdkwt-backup.zip"
            if destination.exists():
                destination = (
                    self.backups_root / f"{safe_id}-{uuid.uuid4().hex[:8]}.rdkwt-backup.zip"
                )
            os.replace(temporary, destination)
            destination.chmod(0o600)
            return {**verified, "filename": destination.name, "size_bytes": size}
        finally:
            temporary.unlink(missing_ok=True)

    def list(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.backups_root.glob("*.rdkwt-backup.zip"), reverse=True):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                verified = self.verify(path)
                records.append(
                    {**verified, "filename": path.name, "size_bytes": path.stat().st_size}
                )
            except ArchiveError as exc:
                records.append(
                    {
                        "backup_id": path.stem,
                        "filename": path.name,
                        "size_bytes": path.stat().st_size,
                        "verified": False,
                        "error_code": exc.code,
                    }
                )
        return records

    def resolve(self, filename: str) -> Path:
        if (
            not filename
            or Path(filename).name != filename
            or not filename.endswith(".rdkwt-backup.zip")
        ):
            raise ArchiveError("BACKUP_NAME_INVALID", "backup filename is invalid")
        path = self.backups_root / filename
        if path.is_symlink():
            raise ArchiveError("BACKUP_FILE_INVALID", "backup must not be a symlink")
        resolved = path.resolve(strict=True)
        if resolved.parent != self.backups_root.resolve(strict=True) or not resolved.is_file():
            raise ArchiveError("BACKUP_NOT_FOUND", "backup was not found")
        return resolved

    def restore(self, path: Path, *, confirmation: str) -> dict[str, Any]:
        if confirmation != "RESTORE":
            raise ArchiveError("RESTORE_CONFIRMATION_INVALID", "confirmation must equal RESTORE")
        verified = self.verify(path)
        safety = self.create(
            include_runs=True,
            include_credentials=True,
            label="pre-restore",
        )
        restore_id = uuid.uuid4().hex
        state_stage = self.settings.state_dir / f".restore-{restore_id}"
        assets_stage = self.settings.assets_dir / f".restore-{restore_id}"
        runs_stage = self.settings.runs_dir / f".restore-{restore_id}"
        stages = (state_stage, assets_stage, runs_stage)
        for stage in stages:
            stage.mkdir(mode=0o700, parents=True, exist_ok=False)
        moved: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        rollback_roots: list[Path] = []
        try:
            with zipfile.ZipFile(path) as archive:
                for item in archive.infolist():
                    logical = _validate_member(item)
                    if item.is_dir() or logical.as_posix() == "manifest.json":
                        continue
                    top = logical.parts[0]
                    if top == "state":
                        relative = Path(*logical.parts[1:])
                        target = state_stage / relative
                    elif top == "assets":
                        target = assets_stage / Path(*logical.parts[1:])
                    elif top == "runs":
                        target = runs_stage / Path(*logical.parts[1:])
                    else:
                        raise ArchiveError("BACKUP_SCOPE_INVALID", "unexpected restore scope")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)

            database_stage = state_stage / "db" / "rdkwt.sqlite3"
            self._verify_database(database_stage)
            if verified["scope"].get("credentials"):
                (state_stage / "secrets").mkdir(parents=True, exist_ok=True)

            for suffix in ("-wal", "-shm"):
                self._move_existing(
                    Path(f"{self.database_path}{suffix}"), restore_id, moved
                )
            self._swap_file(database_stage, self.database_path, restore_id, moved, installed)
            self.database_path.chmod(0o600)
            if verified["scope"].get("credentials"):
                self._swap_directory(
                    state_stage / "secrets",
                    self.settings.secrets_dir,
                    restore_id,
                    moved,
                    installed,
                )
                self.settings.secrets_dir.chmod(0o700)
                for secret in self.settings.secrets_dir.rglob("*"):
                    if secret.is_file():
                        secret.chmod(0o600)
            self._replace_root_children(
                assets_stage,
                self.settings.assets_dir,
                restore_id,
                moved,
                installed,
                rollback_roots,
            )
            if verified["scope"].get("runs"):
                self._replace_root_children(
                    runs_stage,
                    self.settings.runs_dir,
                    restore_id,
                    moved,
                    installed,
                    rollback_roots,
                )
            for _original, rollback in moved:
                try:
                    if rollback.is_symlink() or rollback.is_file():
                        rollback.unlink(missing_ok=True)
                    elif rollback.is_dir():
                        shutil.rmtree(rollback, ignore_errors=True)
                except OSError:
                    pass
            for rollback_root in rollback_roots:
                shutil.rmtree(rollback_root, ignore_errors=True)
            return {
                **verified,
                "restored": True,
                "safety_backup": safety["filename"],
            }
        except Exception:
            for target in reversed(installed):
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            for original, rollback in reversed(moved):
                if rollback.exists() and not original.exists():
                    os.replace(rollback, original)
            for rollback_root in reversed(rollback_roots):
                with suppress(OSError):
                    rollback_root.rmdir()
            raise
        finally:
            for stage in stages:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)

    @staticmethod
    def _verify_database(path: Path) -> None:
        try:
            # Snapshots are self-contained and must not consult adjacent WAL/SHM files.
            # immutable=1 also avoids WAL lock negotiation in worker threads.
            connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchone()
                required = {"projects", "conversion_runs", "alembic_version"}
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise ArchiveError(
                "RESTORE_DATABASE_INVALID", "backup SQLite cannot be opened"
            ) from exc
        if result != ("ok",) or not required.issubset(tables):
            raise ArchiveError("RESTORE_DATABASE_INVALID", "backup SQLite integrity failed")

    @staticmethod
    def _swap_file(
        stage: Path,
        target: Path,
        restore_id: str,
        moved: list[tuple[Path, Path]],
        installed: list[Path],
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        BackupArchive._move_existing(target, restore_id, moved)
        os.replace(stage, target)
        installed.append(target)

    @staticmethod
    def _move_existing(
        target: Path,
        restore_id: str,
        moved: list[tuple[Path, Path]],
    ) -> None:
        if target.exists() or target.is_symlink():
            rollback = target.with_name(f".{target.name}.rollback-{restore_id}")
            os.replace(target, rollback)
            moved.append((target, rollback))

    @staticmethod
    def _swap_directory(
        stage: Path,
        target: Path,
        restore_id: str,
        moved: list[tuple[Path, Path]],
        installed: list[Path],
    ) -> None:
        if not stage.is_dir() or stage.is_symlink():
            raise ArchiveError("RESTORE_SCOPE_MISSING", f"restore scope is missing: {target.name}")
        rollback = target.with_name(f".{target.name}.rollback-{restore_id}")
        if target.exists():
            os.replace(target, rollback)
            moved.append((target, rollback))
        os.replace(stage, target)
        installed.append(target)

    @staticmethod
    def _replace_root_children(
        stage: Path,
        target: Path,
        restore_id: str,
        moved: list[tuple[Path, Path]],
        installed: list[Path],
        rollback_roots: list[Path],
    ) -> None:
        if not stage.is_dir() or stage.is_symlink():
            raise ArchiveError("RESTORE_SCOPE_MISSING", f"restore scope is missing: {target}")
        rollback_root = target / f".rollback-{restore_id}"
        rollback_root.mkdir()
        rollback_roots.append(rollback_root)
        for child in list(target.iterdir()):
            if child in {stage, rollback_root}:
                continue
            rollback = rollback_root / child.name
            os.replace(child, rollback)
            moved.append((child, rollback))
        for child in list(stage.iterdir()):
            destination = target / child.name
            os.replace(child, destination)
            installed.append(destination)
