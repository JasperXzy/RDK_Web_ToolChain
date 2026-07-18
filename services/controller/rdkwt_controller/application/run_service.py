from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from rdkwt_contracts import validate_payload

from rdkwt_controller import __version__
from rdkwt_controller.application.configuration import (
    normalize_configuration,
    render_configuration_preview,
)
from rdkwt_controller.infrastructure.db import (
    Attempt,
    CatalogRepository,
    ConversionRun,
    ExecutionRecord,
    RunRepository,
)
from rdkwt_controller.infrastructure.docker import DockerGateway
from rdkwt_controller.profiles import ProfileRegistry
from rdkwt_controller.settings import Settings

OUTPUT_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
STEP_STATUS = {
    "inspect": "INSPECTING",
    "check": "CHECKING",
    "preprocess": "PREPROCESSING",
    "compile": "COMPILING",
    "verify": "VERIFYING",
    "collect": "COLLECTING",
}
ERROR_ADVICE = {
    "MODEL_PARSE_FAILED": "确认文件是完整的 ONNX，并在导出时关闭 external data。",
    "MODEL_EXTERNAL_DATA_UNSUPPORTED": "将模型重新导出为单个自包含 ONNX 文件。",
    "MODEL_IR_UNSUPPORTED": "使用受支持的 ONNX IR 版本重新导出模型。",
    "MODEL_OPSET_UNSUPPORTED": "将模型转换到 opset 10～19。",
    "TOOL_CHECK_FAILED": "查看 check.log 中的不支持算子和 Shape 约束。",
    "TOOL_COMPILE_FAILED": "查看 compile.log，并核对输入预处理和平台参数。",
    "CALIBRATION_INVALID_SAMPLE": "检查校准集数量、图片格式和 Recipe。",
    "CALIBRATION_PREPROCESS_FAILED": "检查损坏图片、裁剪尺寸及归一化参数。",
    "RUN_TIMEOUT": "提高任务超时或减少校准样本后重试。",
    "RUN_RECOVERY_FAILED": "确认 Docker 可用后，以相同快照重试任务。",
    "RUNNER_EXECUTION_FAILED": "检查 Docker、Runner 镜像和原始日志后重试。",
}


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
    calibration: Path | None = None,
    manifest_path: Path | None = None,
    expected_manifest_sha256: str | None = None,
) -> None:
    if not model.is_file() or model.is_symlink():
        raise ValueError("catalog model asset must reference a regular file")
    if _sha256_file(model) != expected_model_sha256:
        raise ValueError("catalog model asset hash does not match its registered version")
    if calibration is None:
        return
    if not calibration.is_dir() or calibration.is_symlink():
        raise ValueError("calibration source must reference a regular directory")
    if manifest_path is None or expected_manifest_sha256 is None:
        raise ValueError("calibration manifest metadata is incomplete")
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
        request = self._request(
            adapter="contract-probe-1.0",
            pipeline=["inspect", "check", "collect"],
            model_path=asset_path,
            calibration_path=None,
            configuration={"target_profile": profile_snapshot},
        )
        return self._persist_submission(
            kind="PROBE",
            request=request,
            profile_id=profile.profile_id,
            profile_sha256=profile_snapshot["sha256"],
            project_id=None,
            model_version_id=None,
            calibration_version_id=None,
            generated_yaml=None,
        )

    def submit_model_inspection(self, *, model_version_id: str) -> RunSubmission:
        source = self._catalog.model_inspection_input(model_version_id)
        if source.compatibility_status == "INSPECTING":
            raise ValueError(
                f"model inspection is already queued: {source.inspection_run_id}"
            )
        try:
            model = _resolve_within(
                self._settings.assets_dir, source.model_path, must_exist=True
            )
        except FileNotFoundError as exc:
            raise ValueError("registered model asset does not exist") from exc
        _verify_catalog_assets(
            model=model, expected_model_sha256=source.model_sha256
        )
        request = self._request(
            adapter="onnx-inspection-1.0",
            pipeline=["inspect", "collect"],
            model_path=source.model_path,
            calibration_path=None,
            configuration={},
        )
        submission = self._persist_submission(
            kind="MODEL_INSPECTION",
            request=request,
            profile_id="model-inspection",
            profile_sha256=hashlib.sha256(b"onnx-inspection-1.0").hexdigest(),
            project_id=source.project_id,
            model_version_id=source.model_version_id,
            calibration_version_id=None,
            generated_yaml=None,
        )
        self._catalog.mark_model_inspection_pending(
            model_version_id, run_id=submission.run_id
        )
        return submission

    def preview_conversion(
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
        input_options: dict[str, Any] | None = None,
        calibration_options: dict[str, Any] | None = None,
        max_time_per_fc: int = 0,
        cache_mode: str = "disable",
    ) -> dict[str, Any]:
        inputs, configuration = self._prepare_conversion(
            profile_id=profile_id,
            model_version_id=model_version_id,
            calibration_version_id=calibration_version_id,
            output_prefix=output_prefix,
            core_num=core_num,
            max_l2m_size=max_l2m_size,
            compile_mode=compile_mode,
            balance_factor=balance_factor,
            optimize_level=optimize_level,
            sample_limit=sample_limit,
            jobs=jobs,
            input_options=input_options,
            calibration_options=calibration_options,
            max_time_per_fc=max_time_per_fc,
            cache_mode=cache_mode,
        )
        preview = render_configuration_preview(
            configuration, model_logical_path=inputs.model_path
        )
        preview["model_inspection"] = inputs.model_inspection
        preview["resource_snapshot"] = {
            "model_sha256": inputs.model_sha256,
            "calibration_manifest_sha256": inputs.calibration_manifest_sha256,
            "calibration_sample_count": inputs.calibration_sample_count,
        }
        return preview

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
        input_options: dict[str, Any] | None = None,
        calibration_options: dict[str, Any] | None = None,
        max_time_per_fc: int = 0,
        cache_mode: str = "disable",
    ) -> RunSubmission:
        inputs, configuration = self._prepare_conversion(
            profile_id=profile_id,
            model_version_id=model_version_id,
            calibration_version_id=calibration_version_id,
            output_prefix=output_prefix,
            core_num=core_num,
            max_l2m_size=max_l2m_size,
            compile_mode=compile_mode,
            balance_factor=balance_factor,
            optimize_level=optimize_level,
            sample_limit=sample_limit,
            jobs=jobs,
            input_options=input_options,
            calibration_options=calibration_options,
            max_time_per_fc=max_time_per_fc,
            cache_mode=cache_mode,
        )
        preview = render_configuration_preview(
            configuration, model_logical_path=inputs.model_path
        )
        profile = self._profiles.get(profile_id)
        request = self._request(
            adapter="openexplorer-3.7.0",
            pipeline=["inspect", "check", "preprocess", "compile", "collect"],
            model_path=inputs.model_path,
            calibration_path=inputs.calibration_path,
            configuration=configuration,
        )
        return self._persist_submission(
            kind="CONVERSION",
            request=request,
            profile_id=profile.profile_id,
            profile_sha256=profile.snapshot()["sha256"],
            project_id=inputs.project_id,
            model_version_id=inputs.model_version_id,
            calibration_version_id=inputs.calibration_version_id,
            generated_yaml=str(preview["yaml"]),
        )

    def _prepare_conversion(self, **options: Any) -> tuple[Any, dict[str, Any]]:
        profile = self._profiles.get(str(options["profile_id"]))
        inputs = self._catalog.resolve_conversion_inputs(
            model_version_id=str(options["model_version_id"]),
            calibration_version_id=str(options["calibration_version_id"]),
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
        _verify_catalog_assets(
            model=model,
            expected_model_sha256=inputs.model_sha256,
            calibration=calibration,
            manifest_path=calibration_manifest,
            expected_manifest_sha256=inputs.calibration_manifest_sha256,
        )
        output_prefix = str(options["output_prefix"])
        if not OUTPUT_PREFIX.fullmatch(output_prefix):
            raise ValueError("output_prefix contains unsupported characters")
        calibration_options = copy.deepcopy(options.get("calibration_options") or {})
        calibration_options["sample_limit"] = options["sample_limit"]
        configuration = normalize_configuration(
            profile=profile,
            inspection=inputs.model_inspection,
            output_prefix=output_prefix,
            input_options=options.get("input_options"),
            calibration_options=calibration_options,
            compiler_options={
                "core_num": options["core_num"],
                "max_l2m_size": options["max_l2m_size"],
                "compile_mode": options["compile_mode"],
                "balance_factor": options["balance_factor"],
                "optimize_level": options["optimize_level"],
                "jobs": options["jobs"],
                "max_time_per_fc": options.get("max_time_per_fc", 0),
                "cache_mode": options.get("cache_mode", "disable"),
            },
            sample_count=inputs.calibration_sample_count,
        )
        return inputs, configuration

    def _request(
        self,
        *,
        adapter: str,
        pipeline: list[str],
        model_path: str,
        calibration_path: str | None,
        configuration: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        attempt = 1
        return {
            "contract_version": "1.0",
            "run_id": run_id,
            "attempt": attempt,
            "adapter": adapter,
            "runner_mode": "cpu",
            "pipeline": pipeline,
            "paths": {
                "model": model_path,
                "calibration_source": calibration_path,
                "attempt_root": f"{run_id}/attempts/{attempt}",
            },
            "configuration": configuration,
            "limits": {
                "timeout_seconds": self._settings.default_timeout_seconds,
                "max_log_bytes": self._settings.max_log_bytes,
            },
        }

    def _persist_submission(
        self,
        *,
        kind: str,
        request: dict[str, Any],
        profile_id: str,
        profile_sha256: str,
        project_id: str | None,
        model_version_id: str | None,
        calibration_version_id: str | None,
        generated_yaml: str | None,
    ) -> RunSubmission:
        validate_payload("request", request)
        image = self._docker.resolve_cpu_image()
        run_id = str(request["run_id"])
        attempt = int(request["attempt"])
        request_path = (
            self._settings.runs_dir / request["paths"]["attempt_root"] / "request.json"
        )
        _atomic_json(request_path, request)
        run = ConversionRun(
            id=run_id,
            kind=kind,
            project_id=project_id,
            model_version_id=model_version_id,
            calibration_version_id=calibration_version_id,
            profile_id=profile_id,
            profile_sha256=profile_sha256,
            runner_image_reference=image.configured_reference,
            runner_image_id=image.immutable_id,
            contract_version="1.0",
            app_version=__version__,
            generated_yaml=generated_yaml,
            status="QUEUED",
            request_snapshot=request,
        )
        attempt_row = Attempt(
            run_id=run_id,
            number=attempt,
            status="QUEUED",
            stage="QUEUED",
        )
        self._repository.create(run=run, attempt=attempt_row)
        return RunSubmission(run_id=run_id, attempt=attempt, status="QUEUED")

    def execute(self, run_id: str, attempt: int = 1, *, recover: bool = False) -> None:
        record = self._repository.execution(run_id, attempt)
        container = None
        exit_code: int | None = None
        result: dict[str, Any] | None = None
        try:
            if recover:
                if record.container_id is None:
                    raise RuntimeError("running attempt has no recorded container")
                container = self._docker.recover_attempt(
                    record.container_id, run_id=run_id, attempt=attempt
                )
                self._repository.mark_recovered(run_id, attempt)
            else:
                if not self._repository.claim_queued(run_id, attempt):
                    return
                container = self._docker.create_frozen_attempt(
                    run_id=run_id,
                    attempt=attempt,
                    image_reference=record.runner_image_reference,
                    image_id=record.runner_image_id,
                )
                if not self._repository.set_running(run_id, attempt, container.id):
                    self._docker.remove_managed(
                        container.id, run_id=run_id, attempt=attempt
                    )
                    container = None
                    self._finish_cancelled(record, result=None, exit_code=None)
                    return
                self._docker.start(container)
            self._collect_logs(run_id, attempt, container)
            exit_code = self._docker.wait(container)
            result = self._collect_result(run_id, attempt)
            if result["status"] == "cancelled" or self._repository.is_cancel_requested(
                run_id, attempt
            ):
                self._docker.remove_managed(container.id, run_id=run_id, attempt=attempt)
                container = None
                self._finish_cancelled(record, result=result, exit_code=exit_code)
                return
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
            self._record_model_inspection(record, "SUCCEEDED", result, None)
        except BaseException as exc:
            cancelled = self._cancel_requested_safely(run_id, attempt)
            cleanup_error = self._cleanup_failed_container(container, run_id, attempt)
            message = str(exc)
            if cleanup_error is not None:
                message = f"{message}; cleanup failed: {cleanup_error}"
            if result is None:
                with suppress(Exception):
                    result = self._collect_result(run_id, attempt)
            runner_error = None if result is None else result.get("error")
            error_code = (
                "RUNNER_EXECUTION_FAILED"
                if not isinstance(runner_error, dict)
                else str(runner_error.get("code") or "RUNNER_EXECUTION_FAILED")
            )
            terminal_status = "CANCELLED" if cancelled else "FAILED"
            self._repository.finish(
                run_id,
                attempt,
                status=terminal_status,
                exit_code=exit_code,
                result_payload=result,
                error_code="RUN_CANCELLED" if cancelled else error_code,
                error_message="task cancelled by user" if cancelled else message,
            )
            self._record_model_inspection(
                record, terminal_status, result, None if cancelled else message
            )
            if not cancelled:
                raise

    def _finish_cancelled(
        self,
        record: ExecutionRecord,
        *,
        result: dict[str, Any] | None,
        exit_code: int | None,
    ) -> None:
        self._repository.finish(
            record.run_id,
            record.attempt,
            status="CANCELLED",
            exit_code=exit_code,
            result_payload=result,
            error_code="RUN_CANCELLED",
            error_message="task cancelled by user",
        )
        self._record_model_inspection(record, "CANCELLED", result, None)

    def _record_model_inspection(
        self,
        record: ExecutionRecord,
        terminal_status: str,
        result: dict[str, Any] | None,
        error_message: str | None,
    ) -> None:
        if record.kind != "MODEL_INSPECTION" or record.model_version_id is None:
            return
        inspection = None
        if isinstance(result, dict):
            metrics = result.get("metrics")
            if isinstance(metrics, dict) and isinstance(metrics.get("inspect"), dict):
                inspection = metrics["inspect"]
        if inspection is None:
            runner_error = result.get("error") if isinstance(result, dict) else None
            error_code = (
                str(runner_error.get("code") or "MODEL_INSPECTION_FAILED")
                if isinstance(runner_error, dict)
                else "MODEL_INSPECTION_FAILED"
            )
            message = (
                str(
                    runner_error.get("message")
                    or error_message
                    or "model inspection did not complete"
                )
                if isinstance(runner_error, dict)
                else error_message or "model inspection did not complete"
            )
            compatibility_status = (
                "PENDING_INSPECTION"
                if terminal_status in {"CANCELLED", "INTERRUPTED"}
                else "BLOCKED"
            )
            inspection = {
                "schema_version": "1",
                "compatibility_status": compatibility_status,
                "blockers": [
                    {
                        "code": error_code,
                        "message": message,
                    }
                ],
                "warnings": [],
            }
        status = str(inspection.get("compatibility_status") or terminal_status)
        self._catalog.set_model_inspection(
            record.model_version_id,
            run_id=record.run_id,
            status=status,
            inspection=inspection,
        )

    def mark_recovery_interrupted(
        self, record: ExecutionRecord, message: str
    ) -> None:
        self._repository.mark_interrupted(
            record.run_id, record.attempt, message
        )
        self._record_model_inspection(
            record, "INTERRUPTED", None, message
        )

    def cancel(self, run_id: str) -> dict[str, Any]:
        detail = self._repository.get(run_id)
        if detail is None:
            raise KeyError(f"unknown run: {run_id}")
        result = self._repository.request_cancel(run_id)
        container_id = result["container_id"]
        if isinstance(container_id, str):
            try:
                self._docker.stop_managed(
                    container_id,
                    run_id=run_id,
                    attempt=int(result["attempt"]),
                )
            except Exception as exc:
                result["stop_warning"] = str(exc)
        if result["terminal"] and detail["kind"] == "MODEL_INSPECTION":
            record = self._repository.execution(run_id, int(result["attempt"]))
            self._record_model_inspection(record, "CANCELLED", None, None)
        return result

    def retry(self, run_id: str) -> RunSubmission:
        detail = self._repository.get(run_id)
        if detail is None:
            raise KeyError(f"unknown run: {run_id}")
        attempts = detail["attempts"]
        attempt_number = len(attempts) + 1
        request = copy.deepcopy(detail["request"])
        request["attempt"] = attempt_number
        request["paths"]["attempt_root"] = f"{run_id}/attempts/{attempt_number}"
        validate_payload("request", request)
        request_path = (
            self._settings.runs_dir / request["paths"]["attempt_root"] / "request.json"
        )
        _atomic_json(request_path, request)
        self._repository.retry(
            run_id,
            attempt=Attempt(
                run_id=run_id,
                number=attempt_number,
                status="QUEUED",
                stage="QUEUED",
            ),
        )
        if detail["kind"] == "MODEL_INSPECTION" and detail["model_version_id"]:
            self._catalog.mark_model_inspection_pending(
                detail["model_version_id"], run_id=run_id
            )
        return RunSubmission(run_id=run_id, attempt=attempt_number, status="QUEUED")

    def result_detail(self, run_id: str) -> dict[str, Any]:
        detail = self._repository.get(run_id)
        if detail is None:
            raise KeyError(f"unknown run: {run_id}")
        latest = detail["attempts"][-1]
        result = latest.get("result")
        artifacts: list[dict[str, Any]] = []
        if isinstance(result, dict):
            with suppress(Exception):
                artifacts = self._load_artifact_manifest(
                    run_id, int(latest["number"]), result
                )["artifacts"]
        detail["artifacts"] = artifacts
        detail["summary"] = self._result_summary(detail, result, artifacts)
        if detail["error"] is not None:
            code = str(detail["error"]["code"])
            detail["error"]["advice"] = ERROR_ADVICE.get(
                code, "下载原始日志，确认环境与输入后再重试。"
            )
        return detail

    def artifact_file(
        self, run_id: str, attempt: int, artifact_index: int
    ) -> tuple[Path, dict[str, Any]]:
        detail = self._repository.get(run_id)
        if detail is None:
            raise KeyError(f"unknown run: {run_id}")
        selected = next(
            (item for item in detail["attempts"] if item["number"] == attempt), None
        )
        if selected is None or not isinstance(selected.get("result"), dict):
            raise KeyError(f"run attempt has no result: {run_id}/{attempt}")
        manifest = self._load_artifact_manifest(run_id, attempt, selected["result"])
        artifacts = manifest["artifacts"]
        if artifact_index < 0 or artifact_index >= len(artifacts):
            raise KeyError(f"unknown artifact index: {artifact_index}")
        metadata = artifacts[artifact_index]
        root = self._attempt_root(run_id, attempt)
        path = _resolve_within(root, metadata["relative_path"], must_exist=True)
        if not path.is_file() or path.is_symlink():
            raise ValueError("artifact is not a regular file")
        if path.stat().st_size != metadata["size_bytes"]:
            raise ValueError("artifact size no longer matches its manifest")
        if _sha256_file(path) != metadata["sha256"]:
            raise ValueError("artifact hash no longer matches its manifest")
        return path, metadata

    def log_file(self, run_id: str, attempt: int) -> Path:
        self._repository.execution(run_id, attempt)
        path = self._attempt_root(run_id, attempt) / "logs" / "runner.log"
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("runner log is not available")
        return path

    def events_file(self, run_id: str, attempt: int) -> Path:
        self._repository.execution(run_id, attempt)
        return self._settings.runs_dir / run_id / "attempts" / str(attempt) / "events.jsonl"

    def log_stream_file(self, run_id: str, attempt: int) -> Path:
        self._repository.execution(run_id, attempt)
        return (
            self._settings.runs_dir
            / run_id
            / "attempts"
            / str(attempt)
            / "logs"
            / "stream.jsonl"
        )

    def export_run(self, run_id: str) -> Path:
        detail = self.result_detail(run_id)
        latest_attempt = int(detail["attempts"][-1]["number"])
        attempt_root = self._attempt_root(run_id, latest_attempt)
        exports_root = attempt_root / "exports"
        exports_root.mkdir(parents=True, exist_ok=True)
        destination = exports_root / f"rdkwt-{run_id}-reproducible.zip"
        temporary = exports_root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                metadata = {
                    "schema_version": "1",
                    "run": detail,
                    "export_scope": {
                        "includes_model": bool(detail["model_version_id"]),
                        "includes_calibration_manifest": bool(
                            detail["calibration_version_id"]
                        ),
                        "includes_raw_calibration_samples": False,
                    },
                }
                archive.writestr(
                    "metadata.json",
                    json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
                )
                version_manifest = {
                    "schema_version": "1",
                    "app_version": detail["app_version"],
                    "contract_version": detail["contract_version"],
                    "runner_image": detail["runner_image"],
                    "target_profile_id": detail["profile_id"],
                    "target_profile_sha256": detail["profile_sha256"],
                    "toolchain_versions": (detail.get("summary") or {}).get(
                        "toolchain_versions", {}
                    ),
                }
                archive.writestr(
                    "version-manifest.json",
                    json.dumps(
                        version_manifest,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                )
                for relative in (
                    "request.json",
                    "result.json",
                    "artifact-manifest.json",
                    "events.jsonl",
                    "generated.yaml",
                ):
                    path = attempt_root / relative
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, f"attempt/{relative}")
                for directory in (attempt_root / "logs", attempt_root / "artifacts"):
                    if directory.is_dir() and not directory.is_symlink():
                        for path in sorted(directory.rglob("*")):
                            if path.is_file() and not path.is_symlink():
                                archive.write(
                                    path,
                                    f"attempt/{path.relative_to(attempt_root).as_posix()}",
                                )
                if detail["model_version_id"]:
                    model = self._catalog.model_inspection_input(
                        detail["model_version_id"]
                    )
                    model_path = _resolve_within(
                        self._settings.assets_dir, model.model_path, must_exist=True
                    )
                    archive.write(model_path, "inputs/model.onnx")
                if detail["calibration_version_id"] and detail["model_version_id"]:
                    inputs = self._catalog.resolve_conversion_inputs(
                        model_version_id=detail["model_version_id"],
                        calibration_version_id=detail["calibration_version_id"],
                    )
                    manifest_path = _resolve_within(
                        self._settings.assets_dir,
                        inputs.calibration_manifest_path,
                        must_exist=True,
                    )
                    archive.write(manifest_path, "inputs/calibration-manifest.json")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    def _attempt_root(self, run_id: str, attempt: int) -> Path:
        parsed = str(uuid.UUID(run_id))
        if attempt < 1:
            raise ValueError("attempt must be positive")
        root = self._settings.runs_dir.resolve(strict=True)
        path = (root / parsed / "attempts" / str(attempt)).resolve(strict=True)
        if root not in path.parents or path.is_symlink():
            raise ValueError("attempt path escaped the runs root")
        return path

    def _load_artifact_manifest(
        self, run_id: str, attempt: int, result: dict[str, Any]
    ) -> dict[str, Any]:
        root = self._attempt_root(run_id, attempt)
        path = _resolve_within(root, result["artifact_manifest"], must_exist=True)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        validate_payload("artifact-manifest", manifest)
        return manifest

    @staticmethod
    def _result_summary(
        detail: dict[str, Any],
        result: dict[str, Any] | None,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not isinstance(result, dict):
            return None
        hbm = next((item for item in artifacts if item["kind"] == "hbm"), None)
        compile_metrics = result.get("metrics", {}).get("compile", {})
        static_performance = (
            compile_metrics.get("static_performance", {})
            if isinstance(compile_metrics, dict)
            else {}
        )
        quantization = (
            compile_metrics.get("quantization", {})
            if isinstance(compile_metrics, dict)
            else {}
        )
        warnings = result.get("warnings", [])
        advice_artifact_count = sum(
            1 for item in artifacts if item["kind"] in {"advice_csv", "advice_json"}
        )
        return {
            "profile_id": detail["profile_id"],
            "status": detail["status"],
            "hbm": hbm,
            "toolchain_versions": result.get("toolchain_versions", {}),
            "total_duration_ms": sum(
                int(step.get("duration_ms", 0)) for step in result.get("steps", [])
            ),
            "steps": result.get("steps", []),
            "static_performance": static_performance,
            "quantization": quantization,
            "warnings": warnings,
            "warning_count": len(warnings),
            "advice_artifact_count": advice_artifact_count,
        }

    def _collect_logs(self, run_id: str, attempt: int, container: Any) -> None:
        logs_root = self._attempt_root(run_id, attempt) / "logs"
        log_path = logs_root / "runner.log"
        stream_path = logs_root / "stream.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        sequence = 0
        pending = {"stdout": b"", "stderr": b"", "combined": b""}
        log_iterator = (
            self._docker.logs_demux(container)
            if hasattr(self._docker, "logs_demux")
            else self._docker.logs(container)
        )
        with log_path.open("wb") as handle, stream_path.open(
            "w", encoding="utf-8"
        ) as stream:
            for entry in log_iterator:
                chunks: list[tuple[str, bytes]]
                if isinstance(entry, tuple) and len(entry) == 2:
                    chunks = [
                        (name, bytes(value))
                        for name, value in zip(("stdout", "stderr"), entry, strict=True)
                        if value
                    ]
                else:
                    chunks = [("combined", bytes(entry))]
                for stream_name, chunk in chunks:
                    if written >= self._settings.max_log_bytes:
                        break
                    remaining = self._settings.max_log_bytes - written
                    data = chunk[:remaining]
                    if not data:
                        continue
                    handle.write(data)
                    handle.flush()
                    written += len(data)
                    sequence += 1
                    stream.write(
                        json.dumps(
                            {
                                "sequence": sequence,
                                "timestamp": datetime.now(UTC).isoformat(),
                                "stream": stream_name,
                                "text": data.decode("utf-8", errors="replace"),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    stream.flush()
                    pending[stream_name] += data
                    while b"\n" in pending[stream_name]:
                        line, pending[stream_name] = pending[stream_name].split(
                            b"\n", 1
                        )
                        self._apply_event_stage(run_id, attempt, line)
                if written >= self._settings.max_log_bytes:
                    break
            for buffered in pending.values():
                if buffered:
                    self._apply_event_stage(run_id, attempt, buffered)
            handle.flush()
            stream.flush()
            os.fsync(handle.fileno())
            os.fsync(stream.fileno())

    def _apply_event_stage(self, run_id: str, attempt: int, line: bytes) -> None:
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if event.get("type") == "step_started" and event.get("step") in STEP_STATUS:
            self._repository.set_stage(
                run_id, attempt, STEP_STATUS[str(event["step"])]
            )

    def _collect_result(self, run_id: str, attempt: int) -> dict[str, Any]:
        attempt_root = self._attempt_root(run_id, attempt)
        result_path = _resolve_within(attempt_root, "result.json", must_exist=True)
        if not result_path.is_file():
            raise RuntimeError("Runner result.json is missing")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        validate_payload("result", result)
        if result["run_id"] != run_id or result["attempt"] != attempt:
            raise RuntimeError("Runner result identity does not match the database attempt")
        manifest = self._load_artifact_manifest(run_id, attempt, result)
        for artifact in manifest["artifacts"]:
            path = _resolve_within(
                attempt_root, artifact["relative_path"], must_exist=True
            )
            if not path.is_file() or path.is_symlink():
                raise RuntimeError("artifact must be a regular non-symlink file")
            if path.stat().st_size != artifact["size_bytes"]:
                raise RuntimeError(f"artifact size mismatch: {artifact['relative_path']}")
            if _sha256_file(path) != artifact["sha256"]:
                raise RuntimeError(f"artifact hash mismatch: {artifact['relative_path']}")
        return result

    def _cancel_requested_safely(self, run_id: str, attempt: int) -> bool:
        try:
            return self._repository.is_cancel_requested(run_id, attempt)
        except Exception:
            return False

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
