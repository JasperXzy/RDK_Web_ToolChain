from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from rdkwt_controller.infrastructure.archives import ArchiveError, BackupArchive
from rdkwt_controller.infrastructure.db import BoardRepository, CatalogRepository, RunRepository
from rdkwt_controller.settings import Settings


class MaintenanceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _directory_size(path: Path) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            continue
        if item.is_file():
            try:
                total += item.stat().st_size
            except FileNotFoundError:
                continue
    return total


class MaintenanceService:
    CLEANUP_CATEGORIES = {
        "stale_uploads",
        "generated_exports",
        "orphan_run_directories",
        "cache",
    }

    def __init__(
        self,
        *,
        settings: Settings,
        app_version: str,
        run_repository: RunRepository,
        board_repository: BoardRepository,
        catalog_repository: CatalogRepository,
        system_service: object,
    ) -> None:
        self.settings = settings
        self.app_version = app_version
        self.run_repository = run_repository
        self.board_repository = board_repository
        self.catalog_repository = catalog_repository
        self.system_service = system_service
        self.backups = BackupArchive(settings, app_version=app_version)
        self._plans: dict[str, dict[str, Any]] = {}

    def storage(self) -> dict[str, Any]:
        roots = {
            "state": self.settings.state_dir,
            "assets": self.settings.assets_dir,
            "runs": self.settings.runs_dir,
            "cache": self.settings.effective_cache_dir,
        }
        categories = {
            "database": self.settings.state_dir / "db",
            "credentials": self.settings.secrets_dir,
            "backups": self.settings.backups_dir,
            "project_exports": self.settings.project_exports_dir,
            "model_and_calibration_assets": self.settings.assets_dir,
            "conversion_and_board_runs": self.settings.runs_dir,
            "compile_cache": self.settings.effective_cache_dir,
        }
        root_records = []
        for name, path in roots.items():
            usage = shutil.disk_usage(path)
            root_records.append(
                {
                    "name": name,
                    "path": str(path),
                    "used_bytes": _directory_size(path),
                    "filesystem_free_bytes": usage.free,
                    "filesystem_total_bytes": usage.total,
                }
            )
        active = self._active_summary()
        cleanable = {
            category: self._candidate_summary(self._candidates(category))
            for category in sorted(self.CLEANUP_CATEGORIES)
        }
        return {
            "captured_at": datetime.now(UTC).isoformat(),
            "roots": root_records,
            "categories": [
                {"name": name, "used_bytes": _directory_size(path)}
                for name, path in categories.items()
            ],
            "cleanable": cleanable,
            "active_tasks": active,
            "cleanup_requires_no_active_tasks": True,
        }

    def cleanup_preview(self, categories: list[str]) -> dict[str, Any]:
        selected = self._validate_categories(categories)
        candidates = []
        for category in selected:
            candidates.extend(self._candidates(category))
        candidates.sort(key=lambda item: (item["category"], item["path"]))
        active = self._active_summary()
        token = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + 300
        self._plans[token] = {
            "categories": selected,
            "candidates": candidates,
            "expires_at": expires_at,
        }
        self._expire_plans()
        summary = self._candidate_summary(candidates)
        return {
            "categories": selected,
            **summary,
            "active_tasks": active,
            "can_execute": active["total"] == 0,
            "blocked_reason": (
                None
                if active["total"] == 0
                else "cleanup is blocked while conversion or board tasks are active"
            ),
            "confirmation_token": token,
            "expires_in_seconds": 300,
        }

    def cleanup(self, *, token: str, categories: list[str]) -> dict[str, Any]:
        self._expire_plans()
        plan = self._plans.pop(token, None)
        if plan is None:
            raise MaintenanceError(
                "CLEANUP_CONFIRMATION_INVALID",
                "cleanup preview token is missing, expired, or already used",
                status_code=409,
            )
        selected = self._validate_categories(categories)
        if selected != plan["categories"]:
            raise MaintenanceError(
                "CLEANUP_PLAN_CHANGED",
                "cleanup categories no longer match the preview",
                status_code=409,
            )
        if self._active_summary()["total"]:
            raise MaintenanceError(
                "CLEANUP_ACTIVE_TASKS",
                "cleanup is blocked while conversion or board tasks are active",
                status_code=409,
            )
        current: list[dict[str, Any]] = []
        for category in selected:
            current.extend(self._candidates(category))
        current.sort(key=lambda item: (item["category"], item["path"]))
        if current != plan["candidates"]:
            raise MaintenanceError(
                "CLEANUP_PLAN_STALE",
                "cleanable files changed; create a new preview",
                status_code=409,
            )
        deleted_bytes = 0
        deleted_count = 0
        for candidate in current:
            path = Path(candidate["path"])
            self._assert_cleanup_target(path, category=candidate["category"])
            if not path.exists():
                raise MaintenanceError(
                    "CLEANUP_PLAN_STALE", "a cleanable path disappeared", status_code=409
                )
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted_bytes += candidate["size_bytes"]
            deleted_count += 1
        return {
            "categories": selected,
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    def create_backup(self, *, include_runs: bool, include_credentials: bool) -> dict[str, Any]:
        self.require_idle_backup()
        try:
            return self.backups.create(
                include_runs=include_runs,
                include_credentials=include_credentials,
            )
        except ArchiveError as exc:
            raise MaintenanceError(exc.code, str(exc)) from exc

    def require_idle_backup(self) -> None:
        active = self._active_summary()
        if active["total"]:
            raise MaintenanceError(
                "BACKUP_ACTIVE_TASKS",
                "a consistent backup requires all conversion and board tasks to be idle",
                status_code=409,
            )

    async def import_backup(self, chunks: Any, *, content_length: int | None) -> dict[str, Any]:
        try:
            return await self.backups.ingest(chunks, content_length=content_length)
        except ArchiveError as exc:
            raise MaintenanceError(exc.code, str(exc)) from exc

    def list_backups(self) -> list[dict[str, Any]]:
        return self.backups.list()

    def backup_file(self, filename: str) -> Path:
        try:
            return self.backups.resolve(filename)
        except FileNotFoundError as exc:
            raise MaintenanceError(
                "BACKUP_NOT_FOUND", "backup was not found", status_code=404
            ) from exc
        except ArchiveError as exc:
            status_code = 404 if exc.code == "BACKUP_NOT_FOUND" else 422
            raise MaintenanceError(exc.code, str(exc), status_code=status_code) from exc

    def diagnostics(self) -> dict[str, Any]:
        preflight = self.system_service.preflight()  # type: ignore[attr-defined]
        storage = self.storage()
        return {
            "schema_version": "1",
            "generated_at": datetime.now(UTC).isoformat(),
            "application": {"name": "RDK WebToolChain", "version": self.app_version},
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "uid": os.getuid(),
                "gid": os.getgid(),
                "container_non_root": os.getuid() != 0,
            },
            "configuration": {
                "gpu_enabled": self.settings.gpu_enabled,
                "bind_host": self.settings.bind_host,
                "port": self.settings.port,
                "max_upload_bytes": self.settings.max_upload_bytes,
                "min_free_bytes": self.settings.min_free_bytes,
                "board_keep_remote": self.settings.board_keep_remote,
            },
            "preflight": preflight,
            "storage": storage,
            "redaction": {
                "credentials_included": False,
                "environment_variables_included": False,
                "logs_included": False,
                "host_paths": "container-local paths only",
            },
        }

    def _active_summary(self) -> dict[str, int]:
        conversions = len(self.run_repository.active())
        board_runs = len(self.board_repository.active_board_runs())
        return {
            "conversions": conversions,
            "board_runs": board_runs,
            "total": conversions + board_runs,
        }

    @classmethod
    def _validate_categories(cls, categories: list[str]) -> list[str]:
        selected = sorted(set(categories))
        if not selected:
            raise MaintenanceError("CLEANUP_CATEGORY_REQUIRED", "select a cleanup category")
        unsupported = set(selected) - cls.CLEANUP_CATEGORIES
        if unsupported:
            raise MaintenanceError(
                "CLEANUP_CATEGORY_INVALID",
                f"unsupported cleanup category: {sorted(unsupported)[0]}",
            )
        return selected

    @staticmethod
    def _candidate_summary(candidates: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "candidate_count": len(candidates),
            "reclaimable_bytes": sum(item["size_bytes"] for item in candidates),
        }

    def _candidates(self, category: str) -> list[dict[str, Any]]:
        now = time.time()
        paths: list[Path] = []
        if category == "stale_uploads":
            for root in (
                self.settings.assets_dir / "upload-staging",
                self.settings.backups_dir / ".incoming",
            ):
                if root.is_dir() and not root.is_symlink():
                    paths.extend(
                        path
                        for path in root.iterdir()
                        if not path.is_symlink()
                        and (path.is_file() or path.is_dir())
                        and now - path.stat().st_mtime >= 24 * 60 * 60
                    )
        elif category == "generated_exports":
            export_root = self.settings.project_exports_dir
            if export_root.is_dir() and not export_root.is_symlink():
                paths.extend(
                    path
                    for path in export_root.iterdir()
                    if path.is_file() and not path.is_symlink()
                )
            runs_root = self.settings.runs_dir
            if runs_root.is_dir() and not runs_root.is_symlink():
                paths.extend(
                    path
                    for path in runs_root.glob("*/attempts/*/exports/*.zip")
                    if path.is_file() and not path.is_symlink()
                )
        elif category == "cache":
            root = self.settings.effective_cache_dir
            if root.is_dir() and not root.is_symlink():
                paths.extend(
                    path
                    for path in root.iterdir()
                    if not path.is_symlink() and path.name != ".keep"
                )
        elif category == "orphan_run_directories":
            known_runs = {str(item["id"]) for item in self.run_repository.list()}
            for path in self.settings.runs_dir.iterdir():
                if path.name == "board-runs" or not path.is_dir() or path.is_symlink():
                    continue
                try:
                    parsed = str(UUID(path.name))
                except ValueError:
                    continue
                if parsed not in known_runs:
                    paths.append(path)
            known_board_runs = {str(item["id"]) for item in self.board_repository.list_board_runs()}
            if (
                self.settings.board_runs_dir.is_dir()
                and not self.settings.board_runs_dir.is_symlink()
            ):
                for path in self.settings.board_runs_dir.iterdir():
                    if not path.is_dir() or path.is_symlink():
                        continue
                    try:
                        parsed = str(UUID(path.name))
                    except ValueError:
                        continue
                    if parsed not in known_board_runs:
                        paths.append(path)
        records = []
        for path in paths:
            try:
                records.append(
                    {
                        "category": category,
                        "path": str(path.resolve(strict=True)),
                        "size_bytes": _directory_size(path),
                        "mtime_ns": path.stat().st_mtime_ns,
                    }
                )
            except FileNotFoundError:
                continue
        return records

    def _assert_cleanup_target(self, path: Path, *, category: str) -> None:
        allowed_roots = {
            "stale_uploads": (
                self.settings.assets_dir / "upload-staging",
                self.settings.backups_dir / ".incoming",
            ),
            "generated_exports": (self.settings.project_exports_dir, self.settings.runs_dir),
            "cache": (self.settings.effective_cache_dir,),
            "orphan_run_directories": (self.settings.runs_dir,),
        }[category]
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not any(
            root.resolve(strict=True) in resolved.parents for root in allowed_roots
        ):
            raise MaintenanceError(
                "CLEANUP_PATH_INVALID", "cleanup target escaped its managed root"
            )

    def _expire_plans(self) -> None:
        now = time.monotonic()
        self._plans = {
            token: plan for token, plan in self._plans.items() if plan["expires_at"] > now
        }

    def diagnostics_json(self) -> bytes:
        return json.dumps(self.diagnostics(), ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
