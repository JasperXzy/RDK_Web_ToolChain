from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from rdkwt_contracts import validate_payload

from rdkwt_controller.infrastructure.db import Attempt, ConversionRun, RunRepository
from rdkwt_controller.infrastructure.docker import DockerGateway
from rdkwt_controller.profiles import ProfileRegistry
from rdkwt_controller.settings import Settings


@dataclass(frozen=True, slots=True)
class RunSubmission:
    run_id: str
    attempt: int
    status: str


def _logical_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ValueError("asset path must be a non-empty POSIX logical path")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise ValueError("asset path must remain within the assets root")
    return path


def _resolve_within(root: Path, logical: str, *, must_exist: bool) -> Path:
    path = _logical_path(logical)
    root = root.resolve(strict=True)
    current = root
    for part in path.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError("symbolic links are not allowed")
    resolved = current.resolve(strict=must_exist)
    if resolved != root and root not in resolved.parents:
        raise ValueError("path escaped its configured root")
    return resolved


def _atomic_json(path: Path, payload: Any) -> None:
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


class RunService:
    def __init__(
        self,
        *,
        settings: Settings,
        profiles: ProfileRegistry,
        repository: RunRepository,
        docker_gateway: DockerGateway,
    ) -> None:
        self._settings = settings
        self._profiles = profiles
        self._repository = repository
        self._docker = docker_gateway

    def submit_contract_probe(self, *, profile_id: str, asset_path: str) -> RunSubmission:
        profile = self._profiles.get(profile_id)
        try:
            asset = _resolve_within(self._settings.assets_dir, asset_path, must_exist=True)
        except FileNotFoundError as exc:
            raise ValueError(f"probe asset does not exist: {asset_path}") from exc
        if not asset.is_file():
            raise ValueError("probe asset must be a regular file")

        run_id = str(uuid.uuid4())
        attempt_number = 1
        attempt_root = f"{run_id}/attempts/{attempt_number}"
        profile_snapshot = profile.snapshot()
        request = {
            "contract_version": "1.0",
            "run_id": run_id,
            "attempt": attempt_number,
            "adapter": "contract-probe-1.0",
            "runner_mode": "cpu",
            "pipeline": ["inspect", "check", "collect"],
            "paths": {
                "model": asset_path,
                "calibration_source": None,
                "attempt_root": attempt_root,
            },
            "configuration": {"target_profile": profile_snapshot},
            "limits": {
                "timeout_seconds": self._settings.default_timeout_seconds,
                "max_log_bytes": self._settings.max_log_bytes,
            },
        }
        validate_payload("request", request)
        request_path = self._settings.runs_dir / attempt_root / "request.json"
        _atomic_json(request_path, request)

        run = ConversionRun(
            id=run_id,
            profile_id=profile.profile_id,
            profile_sha256=profile_snapshot["sha256"],
            status="QUEUED",
            request_snapshot=request,
        )
        attempt = Attempt(run_id=run_id, number=attempt_number, status="QUEUED")
        self._repository.create(run=run, attempt=attempt)
        return RunSubmission(run_id=run_id, attempt=attempt_number, status="QUEUED")

    def execute(self, run_id: str, attempt: int = 1) -> None:
        container = None
        exit_code: int | None = None
        result: dict[str, Any] | None = None
        try:
            container = self._docker.create_attempt(run_id=run_id, attempt=attempt)
            self._repository.set_running(run_id, attempt, container.id)
            self._docker.start(container)
            self._collect_logs(run_id, attempt, container)
            exit_code = self._docker.wait(container)
            result = self._collect_result(run_id, attempt)
            if exit_code != 0:
                raise RuntimeError(f"Runner container exited with code {exit_code}")
            if result["status"] != "succeeded":
                raise RuntimeError(f"Runner reported terminal status {result['status']}")
            self._docker.remove_managed(container.id, run_id=run_id, attempt=attempt)
            container = None
            self._repository.finish(
                run_id,
                attempt,
                status="SUCCEEDED",
                exit_code=exit_code,
                result_payload=result,
            )
        except BaseException as exc:
            cleanup_error = self._cleanup_failed_container(container, run_id, attempt)
            if cleanup_error is not None:
                message = f"{exc}; cleanup failed: {cleanup_error}"
            else:
                message = str(exc)
            runner_error = None if result is None else result.get("error")
            error_code = (
                "RUNNER_EXECUTION_FAILED"
                if not isinstance(runner_error, dict)
                else str(runner_error.get("code") or "RUNNER_EXECUTION_FAILED")
            )
            self._repository.finish(
                run_id,
                attempt,
                status="FAILED",
                exit_code=exit_code,
                result_payload=result,
                error_code=error_code,
                error_message=message,
            )
            raise

    def _cleanup_failed_container(
        self, container: Any | None, run_id: str, attempt: int
    ) -> Exception | None:
        if container is None:
            return None
        with suppress(Exception):
            self._docker.stop_managed(container.id, run_id=run_id, attempt=attempt)
        try:
            self._docker.remove_managed(container.id, run_id=run_id, attempt=attempt)
        except Exception as exc:
            return exc
        return None

    def _collect_logs(self, run_id: str, attempt: int, container: Any) -> None:
        log_path = (
            self._settings.runs_dir / run_id / "attempts" / str(attempt) / "logs" / "runner.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with log_path.open("wb") as handle:
            for chunk in self._docker.logs(container):
                if written >= self._settings.max_log_bytes:
                    break
                remaining = self._settings.max_log_bytes - written
                data = bytes(chunk)[:remaining]
                handle.write(data)
                written += len(data)
            handle.flush()
            os.fsync(handle.fileno())

    def _collect_result(self, run_id: str, attempt: int) -> dict[str, Any]:
        attempt_root = self._settings.runs_dir / run_id / "attempts" / str(attempt)
        result_path = _resolve_within(attempt_root, "result.json", must_exist=True)
        if not result_path.is_file():
            raise RuntimeError("Runner result.json is missing")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        validate_payload("result", result)
        if result["run_id"] != run_id or result["attempt"] != attempt:
            raise RuntimeError("Runner result identity does not match the database attempt")

        manifest_path = _resolve_within(attempt_root, result["artifact_manifest"], must_exist=True)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_payload("artifact-manifest", manifest)
        for artifact in manifest["artifacts"]:
            path = _resolve_within(attempt_root, artifact["relative_path"], must_exist=True)
            if not path.is_file() or path.is_symlink():
                raise RuntimeError("artifact must be a regular non-symlink file")
            if path.stat().st_size != artifact["size_bytes"]:
                raise RuntimeError(f"artifact size mismatch: {artifact['relative_path']}")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != artifact["sha256"]:
                raise RuntimeError(f"artifact hash mismatch: {artifact['relative_path']}")
        return result
