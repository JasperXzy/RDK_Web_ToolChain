from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from rdkwt_contracts import validate_payload

from rdkwt_controller.infrastructure.db import (
    Attempt,
    CatalogRepository,
    ConversionRun,
    RunRepository,
)
from rdkwt_controller.infrastructure.docker import DockerGateway
from rdkwt_controller.profiles import ProfileRegistry
from rdkwt_controller.settings import Settings

OUTPUT_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_catalog_assets(
    *,
    model: Path,
    expected_model_sha256: str,
    calibration: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
) -> None:
    if _sha256_file(model) != expected_model_sha256:
        raise ValueError("catalog model asset hash does not match its registered version")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("calibration manifest must be a regular file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded_digest = manifest.pop("manifest_sha256")
        encoded = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"calibration manifest is invalid: {exc}") from exc
    actual_manifest_sha256 = hashlib.sha256(encoded).hexdigest()
    if (
        recorded_digest != expected_manifest_sha256
        or actual_manifest_sha256 != expected_manifest_sha256
    ):
        raise ValueError("calibration manifest hash does not match its registered version")
    samples = manifest.get("samples")
    if not isinstance(samples, list) or manifest.get("sample_count") != len(samples):
        raise ValueError("calibration manifest sample count is invalid")
    expected_files: dict[str, dict[str, Any]] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("calibration manifest contains an invalid sample")
        name = sample.get("materialized_name")
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or "\\" in name
            or name in expected_files
        ):
            raise ValueError("calibration manifest contains an invalid materialized name")
        expected_files[name] = sample
    actual_files: dict[str, Path] = {}
    for path in calibration.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ValueError("calibration source contains a non-regular sample")
        actual_files[path.name] = path
    if set(actual_files) != set(expected_files):
        raise ValueError("calibration source file list does not match its frozen manifest")
    for name, sample in expected_files.items():
        path = actual_files[name]
        if path.stat().st_size != sample.get("size_bytes"):
            raise ValueError(f"calibration sample size changed after finalization: {name}")
        if _sha256_file(path) != sample.get("sha256"):
            raise ValueError(f"calibration sample hash changed after finalization: {name}")


class RunService:
    def __init__(
        self,
        *,
        settings: Settings,
        profiles: ProfileRegistry,
        repository: RunRepository,
        catalog_repository: CatalogRepository,
        docker_gateway: DockerGateway,
    ) -> None:
        self._settings = settings
        self._profiles = profiles
        self._repository = repository
        self._catalog = catalog_repository
        self._docker = docker_gateway

    def submit_contract_probe(self, *, profile_id: str, asset_path: str) -> RunSubmission:
        profile = self._profiles.get(profile_id)
        try:
            asset = _resolve_within(self._settings.assets_dir, asset_path, must_exist=True)
        except FileNotFoundError as exc:
            raise ValueError(f"probe asset does not exist: {asset_path}") from exc
        if not asset.is_file():
            raise ValueError("probe asset must be a regular file")

        profile_snapshot = profile.snapshot()
        run_id = str(uuid.uuid4())
        attempt_number = 1
        attempt_root = f"{run_id}/attempts/{attempt_number}"
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
        return self._persist_submission(
            run_id=run_id,
            attempt=attempt_number,
            profile_id=profile.profile_id,
            profile_sha256=profile_snapshot["sha256"],
            request=request,
            project_id=None,
            model_version_id=None,
            calibration_version_id=None,
        )

    def submit_conversion(
        self,
        *,
        profile_id: str,
        model_version_id: str,
        calibration_version_id: str,
        output_prefix: str,
        core_num: int | None,
        max_l2m_size: int | str | None,
        compile_mode: str,
        balance_factor: int | None,
        optimize_level: str,
        sample_limit: int,
        jobs: int,
    ) -> RunSubmission:
        profile = self._profiles.get(profile_id)
        inputs = self._catalog.resolve_conversion_inputs(
            model_version_id=model_version_id,
            calibration_version_id=calibration_version_id,
        )
        try:
            model = _resolve_within(
                self._settings.assets_dir, inputs.model_path, must_exist=True
            )
            calibration = _resolve_within(
                self._settings.assets_dir, inputs.calibration_path, must_exist=True
            )
            calibration_manifest = _resolve_within(
                self._settings.assets_dir,
                inputs.calibration_manifest_path,
                must_exist=True,
            )
        except FileNotFoundError as exc:
            raise ValueError("model or calibration asset does not exist") from exc
        if not model.is_file():
            raise ValueError("catalog model asset must reference a regular file")
        if not calibration.is_dir():
            raise ValueError("calibration_path must reference a directory")
        _verify_catalog_assets(
            model=model,
            expected_model_sha256=inputs.model_sha256,
            calibration=calibration,
            manifest_path=calibration_manifest,
            expected_manifest_sha256=inputs.calibration_manifest_sha256,
        )
        if not OUTPUT_PREFIX.fullmatch(output_prefix):
            raise ValueError("output_prefix contains unsupported characters")
        if isinstance(sample_limit, bool) or not 20 <= sample_limit <= 100:
            raise ValueError("sample_limit must be between 20 and 100")
        if isinstance(jobs, bool) or not 1 <= jobs <= 128:
            raise ValueError("jobs must be between 1 and 128")
        resolved_core_num = (
            int(profile.capabilities.core_num.default) if core_num is None else core_num
        )
        resolved_l2m = profile.capabilities.max_l2m_size.default
        if max_l2m_size is not None:
            resolved_l2m = max_l2m_size
        if isinstance(resolved_core_num, bool) or isinstance(resolved_l2m, bool):
            raise ValueError("core_num and max_l2m_size must not be booleans")
        profile.validate_compile_options(
            core_num=resolved_core_num,
            max_l2m_size=resolved_l2m,
        )
        if compile_mode not in profile.capabilities.compile_mode.allowed:
            raise ValueError(f"compile_mode={compile_mode!r} is not supported by {profile_id}")
        if optimize_level not in profile.capabilities.optimize_level.allowed:
            raise ValueError(f"optimize_level={optimize_level!r} is not supported by {profile_id}")
        if compile_mode == "balance":
            if (
                isinstance(balance_factor, bool)
                or not isinstance(balance_factor, int)
                or not 0 <= balance_factor <= 100
            ):
                raise ValueError("balance compile mode requires balance_factor from 0 to 100")
        elif balance_factor is not None:
            raise ValueError("balance_factor is only valid for balance compile mode")

        profile_snapshot = profile.snapshot()
        configuration = {
            "schema_version": "1",
            "target_profile": profile_snapshot,
            "output_prefix": output_prefix,
            "inputs": [
                {
                    "name": "data",
                    "target_shape": [1, 3, 224, 224],
                    "train_type": "rgb",
                    "train_layout": "NCHW",
                    "runtime_type": "nv12",
                    "normalization": {
                        "mean": [123.675, 116.28, 103.53],
                        "scale": [0.01712475, 0.017507, 0.01742919],
                        "std": [],
                    },
                }
            ],
            "calibration": {
                "algorithm": "default",
                "sample_limit": sample_limit,
                "recipe": {
                    "id": "imagenet-resnet18",
                    "version": "1",
                    "resize_short": 256,
                    "crop_size": [224, 224],
                    "mean": [0.485, 0.456, 0.406],
                    "std": [0.229, 0.224, 0.225],
                },
            },
            "compiler": {
                "compile_mode": compile_mode,
                "balance_factor": balance_factor,
                "core_num": resolved_core_num,
                "optimize_level": optimize_level,
                "max_l2m_size": resolved_l2m,
                "max_time_per_fc": 0,
                "jobs": jobs,
                "cache_mode": "disable",
            },
        }
        run_id = str(uuid.uuid4())
        attempt_number = 1
        attempt_root = f"{run_id}/attempts/{attempt_number}"
        request = {
            "contract_version": "1.0",
            "run_id": run_id,
            "attempt": attempt_number,
            "adapter": "openexplorer-3.7.0",
            "runner_mode": "cpu",
            "pipeline": ["inspect", "check", "preprocess", "compile", "collect"],
            "paths": {
                "model": inputs.model_path,
                "calibration_source": inputs.calibration_path,
                "attempt_root": attempt_root,
            },
            "configuration": configuration,
            "limits": {
                "timeout_seconds": self._settings.default_timeout_seconds,
                "max_log_bytes": self._settings.max_log_bytes,
            },
        }
        return self._persist_submission(
            run_id=run_id,
            attempt=attempt_number,
            profile_id=profile.profile_id,
            profile_sha256=profile_snapshot["sha256"],
            request=request,
            project_id=inputs.project_id,
            model_version_id=inputs.model_version_id,
            calibration_version_id=inputs.calibration_version_id,
        )

    def _persist_submission(
        self,
        *,
        run_id: str,
        attempt: int,
        profile_id: str,
        profile_sha256: str,
        request: dict[str, Any],
        project_id: str | None,
        model_version_id: str | None,
        calibration_version_id: str | None,
    ) -> RunSubmission:
        validate_payload("request", request)
        attempt_root = request["paths"]["attempt_root"]
        request_path = self._settings.runs_dir / attempt_root / "request.json"
        _atomic_json(request_path, request)

        run = ConversionRun(
            id=run_id,
            project_id=project_id,
            model_version_id=model_version_id,
            calibration_version_id=calibration_version_id,
            profile_id=profile_id,
            profile_sha256=profile_sha256,
            status="QUEUED",
            request_snapshot=request,
        )
        attempt_row = Attempt(run_id=run_id, number=attempt, status="QUEUED")
        self._repository.create(run=run, attempt=attempt_row)
        return RunSubmission(run_id=run_id, attempt=attempt, status="QUEUED")

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
