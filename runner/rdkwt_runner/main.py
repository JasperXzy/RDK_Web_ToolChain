from __future__ import annotations

import json
import os
import platform
import signal
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import AdapterExecutionError, OpenExplorer370Adapter
from .filesystem import atomic_write_json, resolve_within, sha256_file
from .inspection import inspect_onnx
from .probe import check_toolchain, inspect_asset, toolchain_versions

CONTRACT_VERSION = "1.0"
ALLOWED_STEPS = {"inspect", "check", "preprocess", "compile", "verify", "collect"}
_cancel_requested = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _handle_termination(_signum: int, _frame: Any) -> None:
    global _cancel_requested
    _cancel_requested = True


class EventWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sequence = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        *,
        level: str,
        step: str,
        event_type: str,
        code: str,
        message: str,
        progress: int | None = None,
    ) -> dict[str, Any]:
        self.sequence += 1
        event: dict[str, Any] = {
            "sequence": self.sequence,
            "timestamp": utc_now(),
            "level": level,
            "step": step,
            "type": event_type,
            "code": code,
            "message": message,
        }
        if progress is not None:
            event["progress"] = progress
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
        print(encoded, flush=True)
        return event


def _validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    required = {
        "contract_version",
        "run_id",
        "attempt",
        "adapter",
        "runner_mode",
        "pipeline",
        "paths",
        "configuration",
        "limits",
    }
    if set(payload) != required:
        raise ValueError(f"request fields must be exactly {sorted(required)}")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise ValueError(f"unsupported contract version: {payload['contract_version']!r}")
    run_id = str(uuid.UUID(str(payload["run_id"])))
    if (
        isinstance(payload["attempt"], bool)
        or not isinstance(payload["attempt"], int)
        or payload["attempt"] < 1
    ):
        raise ValueError("attempt must be a positive integer")
    if payload["adapter"] not in {
        "contract-probe-1.0",
        "onnx-inspection-1.0",
        "openexplorer-3.7.0",
    }:
        raise ValueError(f"unsupported adapter: {payload['adapter']!r}")
    if payload["runner_mode"] != "cpu":
        raise ValueError("the CPU Runner only accepts runner_mode=cpu")
    pipeline = payload["pipeline"]
    if not isinstance(pipeline, list) or not pipeline or len(pipeline) != len(set(pipeline)):
        raise ValueError("pipeline must be a non-empty list of unique steps")
    if not set(pipeline).issubset(ALLOWED_STEPS):
        raise ValueError("pipeline contains unsupported steps")
    if "collect" not in pipeline:
        raise ValueError("pipeline must include collect")
    if payload["adapter"] == "openexplorer-3.7.0" and pipeline != [
        "inspect",
        "check",
        "preprocess",
        "compile",
        "collect",
    ]:
        raise ValueError("the OpenExplorer 3.7.0 M1 adapter requires the complete ordered pipeline")
    if payload["adapter"] == "onnx-inspection-1.0" and pipeline != [
        "inspect",
        "collect",
    ]:
        raise ValueError("the ONNX inspection adapter requires inspect then collect")
    paths = payload["paths"]
    expected_paths = {"model", "calibration_source", "attempt_root"}
    if not isinstance(paths, dict) or set(paths) != expected_paths:
        raise ValueError(f"paths fields must be exactly {sorted(expected_paths)}")
    expected_attempt_root = f"{run_id}/attempts/{payload['attempt']}"
    if paths["attempt_root"] != expected_attempt_root:
        raise ValueError("attempt_root does not match run_id and attempt")
    if payload["adapter"] == "openexplorer-3.7.0" and paths["calibration_source"] is None:
        raise ValueError("the OpenExplorer adapter requires calibration_source")
    if not isinstance(payload["configuration"], dict):
        raise ValueError("configuration must be an object")
    limits = payload["limits"]
    if not isinstance(limits, dict) or set(limits) != {"timeout_seconds", "max_log_bytes"}:
        raise ValueError("limits must contain timeout_seconds and max_log_bytes")
    if (
        isinstance(limits["timeout_seconds"], bool)
        or not isinstance(limits["timeout_seconds"], int)
        or limits["timeout_seconds"] < 1
    ):
        raise ValueError("timeout_seconds must be a positive integer")
    if (
        isinstance(limits["max_log_bytes"], bool)
        or not isinstance(limits["max_log_bytes"], int)
        or not 1024 <= limits["max_log_bytes"] <= 2**30
    ):
        raise ValueError("max_log_bytes must be between 1024 and 1073741824")
    return payload


def _run_step(
    step: str,
    operation: Callable[[], dict[str, Any]],
    events: EventWriter,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if _cancel_requested:
        raise InterruptedError("runner cancellation requested")
    started_at = utc_now()
    started_ns = time.monotonic_ns()
    events.emit(
        level="info",
        step=step,
        event_type="step_started",
        code=f"{step.upper()}_STARTED",
        message=f"{step} started",
    )
    details = operation()
    finished_at = utc_now()
    duration_ms = max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
    events.emit(
        level="info",
        step=step,
        event_type="step_completed",
        code=f"{step.upper()}_SUCCEEDED",
        message=f"{step} completed",
    )
    return details, {
        "step": step,
        "status": "succeeded",
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "details": details,
    }


def execute_request(payload: dict[str, Any], assets_root: Path, runs_root: Path) -> dict[str, Any]:
    request = _validate_request(payload)
    attempt_root = resolve_within(runs_root, request["paths"]["attempt_root"])
    attempt_root.mkdir(parents=True, exist_ok=True)
    model_path = resolve_within(assets_root, request["paths"]["model"], must_exist=True)
    calibration_path = None
    if request["paths"]["calibration_source"] is not None:
        calibration_path = resolve_within(
            assets_root,
            request["paths"]["calibration_source"],
            must_exist=True,
        )
    events = EventWriter(attempt_root / "events.jsonl")
    started_at = utc_now()
    steps: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    openexplorer_adapter: OpenExplorer370Adapter | None = None

    events.emit(
        level="info",
        step="runner",
        event_type="runner_started",
        code="RUNNER_STARTED",
        message=f"Runner {__version__} accepted contract {CONTRACT_VERSION}",
        progress=0,
    )

    try:
        if request["adapter"] == "openexplorer-3.7.0":
            assert calibration_path is not None
            openexplorer_adapter = OpenExplorer370Adapter(
                request=request,
                model_path=model_path,
                calibration_source=calibration_path,
                attempt_root=attempt_root,
                is_cancel_requested=lambda: _cancel_requested,
            )
            operations = {
                step: lambda step=step: openexplorer_adapter.run_step(step)
                for step in openexplorer_adapter.implemented_steps
            }
        elif request["adapter"] == "onnx-inspection-1.0":
            inspection_path = attempt_root / "artifacts" / "model-inspection.json"

            def inspect_model() -> dict[str, Any]:
                try:
                    details = inspect_onnx(model_path)
                except ValueError as exc:
                    raise AdapterExecutionError(
                        "MODEL_PARSE_FAILED", str(exc), step="inspect"
                    ) from exc
                atomic_write_json(inspection_path, details)
                return details

            operations = {"inspect": inspect_model}
        else:
            operations = {
                "inspect": lambda: inspect_asset(model_path),
                "check": lambda: check_toolchain(request["limits"]["timeout_seconds"]),
            }
        for step in request["pipeline"]:
            if step not in operations and step != "collect":
                now = utc_now()
                events.emit(
                    level="info",
                    step=step,
                    event_type="step_skipped",
                    code=f"{step.upper()}_SKIPPED",
                    message=f"{step} is not implemented by the contract probe adapter",
                )
                steps.append(
                    {
                        "step": step,
                        "status": "skipped",
                        "started_at": now,
                        "finished_at": now,
                        "duration_ms": 0,
                        "details": {},
                    }
                )
                continue
            if step == "collect":
                continue
            details, step_result = _run_step(step, operations[step], events)
            metrics[step] = details
            steps.append(step_result)

        collect_started = utc_now()
        collect_started_ns = time.monotonic_ns()
        events.emit(
            level="info",
            step="collect",
            event_type="step_started",
            code="COLLECT_STARTED",
            message="collect started",
        )
        if request["adapter"] == "onnx-inspection-1.0":
            artifact_path = attempt_root / "artifacts" / "model-inspection.json"
            manifest = {
                "schema_version": "1",
                "artifacts": [
                    {
                        "kind": "model_inspection",
                        "relative_path": "artifacts/model-inspection.json",
                        "size_bytes": artifact_path.stat().st_size,
                        "sha256": sha256_file(artifact_path),
                        "mime_type": "application/json",
                        "required": True,
                    }
                ],
            }
            collect_details = {"artifact_count": 1}
        elif openexplorer_adapter is None:
            artifact_path = attempt_root / "artifacts" / "probe.json"
            artifact_payload = {
                "adapter": request["adapter"],
                "python": platform.python_version(),
                "runner_version": __version__,
                "toolchain_versions": toolchain_versions(),
                "metrics": metrics,
            }
            atomic_write_json(artifact_path, artifact_payload)
            manifest = {
                "schema_version": "1",
                "artifacts": [
                    {
                        "kind": "runner_probe",
                        "relative_path": "artifacts/probe.json",
                        "size_bytes": artifact_path.stat().st_size,
                        "sha256": sha256_file(artifact_path),
                        "mime_type": "application/json",
                        "required": True,
                    }
                ],
            }
            collect_details = {"artifact_count": 1}
        else:
            manifest, collect_details = openexplorer_adapter.collect(require_hbm=True)
        atomic_write_json(attempt_root / "artifact-manifest.json", manifest)
        collect_finished = utc_now()
        steps.append(
            {
                "step": "collect",
                "status": "succeeded",
                "started_at": collect_started,
                "finished_at": collect_finished,
                "duration_ms": max(0, (time.monotonic_ns() - collect_started_ns) // 1_000_000),
                "details": collect_details,
            }
        )
        events.emit(
            level="info",
            step="collect",
            event_type="step_completed",
            code="COLLECT_SUCCEEDED",
            message="collect completed",
        )
        result = {
            "contract_version": CONTRACT_VERSION,
            "run_id": request["run_id"],
            "attempt": request["attempt"],
            "status": "succeeded",
            "started_at": started_at,
            "finished_at": utc_now(),
            "steps": steps,
            "toolchain_versions": toolchain_versions(),
            "metrics": metrics,
            "warnings": [],
            "error": None,
            "artifact_manifest": "artifact-manifest.json",
        }
        atomic_write_json(attempt_root / "result.json", result)
        events.emit(
            level="info",
            step="runner",
            event_type="runner_completed",
            code="RUNNER_SUCCEEDED",
            message="runner completed successfully",
            progress=100,
        )
        return result
    except BaseException as exc:
        cancelled = isinstance(exc, InterruptedError)
        status = "cancelled" if cancelled else "failed"
        adapter_error = exc if isinstance(exc, AdapterExecutionError) else None
        error_code = (
            "RUNNER_CANCELLED"
            if cancelled
            else adapter_error.code
            if adapter_error is not None
            else "RUNNER_FAILED"
        )
        error_step = None if adapter_error is None else adapter_error.step
        error_details = {} if adapter_error is None else adapter_error.details
        result = {
            "contract_version": CONTRACT_VERSION,
            "run_id": request["run_id"],
            "attempt": request["attempt"],
            "status": status,
            "started_at": started_at,
            "finished_at": utc_now(),
            "steps": steps,
            "toolchain_versions": toolchain_versions(),
            "metrics": metrics,
            "warnings": [],
            "error": {
                "code": error_code,
                "message": str(exc),
                "step": error_step,
                "details": error_details,
            },
            "artifact_manifest": "artifact-manifest.json",
        }
        manifest_path = attempt_root / "artifact-manifest.json"
        if not manifest_path.exists():
            manifest = {"schema_version": "1", "artifacts": []}
            if openexplorer_adapter is not None:
                with suppress(BaseException):
                    manifest, _details = openexplorer_adapter.collect(require_hbm=False)
            atomic_write_json(manifest_path, manifest)
        atomic_write_json(attempt_root / "result.json", result)
        events.emit(
            level="warning" if cancelled else "error",
            step="runner",
            event_type="cancel_requested" if cancelled else "error",
            code=error_code,
            message=str(exc),
        )
        raise


def run_request_file(request_path: Path, assets_root: Path, runs_root: Path) -> dict[str, Any]:
    absolute_request = request_path.absolute()
    absolute_runs = runs_root.absolute()
    try:
        logical_request = absolute_request.relative_to(absolute_runs).as_posix()
    except ValueError as exc:
        raise ValueError("request path escapes runs root") from exc
    try:
        resolved_request = resolve_within(runs_root, logical_request, must_exist=True)
    except ValueError as exc:
        raise ValueError("request path escapes runs root") from exc
    if not resolved_request.is_file():
        raise ValueError("request path must be a regular non-symlink file")
    with resolved_request.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return execute_request(payload, assets_root, runs_root)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_termination)
    signal.signal(signal.SIGINT, _handle_termination)


def configured_roots() -> tuple[Path, Path]:
    return (
        Path(os.environ.get("RDKWT_ASSETS_ROOT", "/assets")),
        Path(os.environ.get("RDKWT_RUNS_ROOT", "/runs")),
    )
