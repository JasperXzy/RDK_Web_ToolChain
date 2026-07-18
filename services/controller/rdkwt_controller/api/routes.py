from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt

router = APIRouter()
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}


class ContractProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=128)
    asset_path: str = Field(min_length=1, max_length=1024)


class RunnerSmokeTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(default="s100-oe-3.7.0", min_length=1, max_length=128)


Number = StrictInt | StrictFloat


class NormalizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mean: list[Number] | None = Field(default=None, max_length=4)
    scale: list[Number] | None = Field(default=None, max_length=4)
    std: list[Number] | None = Field(default=None, max_length=4)


class InputOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=256)
    target_shape: list[StrictInt] | None = Field(
        default=None, min_length=4, max_length=4
    )
    train_type: Literal["rgb", "bgr", "gray"] = "rgb"
    train_layout: Literal["NCHW", "NHWC"] = "NCHW"
    runtime_type: Literal["nv12", "rgb", "bgr", "yuv444", "gray", "featuremap"] = (
        "nv12"
    )
    normalization: NormalizationRequest = Field(default_factory=NormalizationRequest)


class RecipeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Literal["image-center-crop", "imagenet-resnet18"] = "image-center-crop"
    resize_short: StrictInt | None = Field(default=None, ge=1, le=16384)
    mean: list[Number] | None = Field(default=None, min_length=1, max_length=4)
    std: list[Number] | None = Field(default=None, min_length=1, max_length=4)


class CalibrationOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["default", "mix", "kl", "max"] = "default"
    recipe: RecipeRequest = Field(default_factory=RecipeRequest)


class ConversionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=128)
    model_version_id: UUID
    calibration_version_id: UUID
    output_prefix: str = Field(
        default="resnet18_224x224_nv12",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    input: InputOptionsRequest | None = None
    calibration: CalibrationOptionsRequest = Field(
        default_factory=CalibrationOptionsRequest
    )
    core_num: StrictInt | None = Field(default=None, ge=1, le=2)
    max_l2m_size: StrictInt | Literal["auto"] | None = Field(default=None)
    compile_mode: Literal["latency", "bandwidth", "balance"] = "latency"
    balance_factor: StrictInt | None = Field(default=None, ge=0, le=100)
    optimize_level: Literal["O0", "O1", "O2"] = "O2"
    sample_limit: StrictInt = Field(default=100, ge=20, le=100)
    jobs: StrictInt = Field(default=8, ge=1, le=128)
    max_time_per_fc: StrictInt = Field(default=0, ge=0, le=2**31 - 1)
    cache_mode: Literal["disable", "enable", "force_overwrite"] = "disable"


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class CalibrationSetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


def _services(request: Request) -> object:
    return request.app.state.services


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    if value < 0:
        raise HTTPException(status_code=400, detail="invalid Content-Length")
    return value


def _upload_filename(filename: str | None, filename_b64: str | None) -> str:
    if filename_b64 is not None:
        try:
            return base64.b64decode(filename_b64, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid X-Filename-B64") from exc
    if filename is None:
        raise HTTPException(
            status_code=400, detail="X-Filename or X-Filename-B64 is required"
        )
    return filename


def _conversion_kwargs(payload: ConversionRunRequest) -> dict[str, Any]:
    return {
        "profile_id": payload.profile_id,
        "model_version_id": str(payload.model_version_id),
        "calibration_version_id": str(payload.calibration_version_id),
        "output_prefix": payload.output_prefix,
        "input_options": (
            None
            if payload.input is None
            else payload.input.model_dump(mode="json", exclude_none=True)
        ),
        "calibration_options": payload.calibration.model_dump(
            mode="json", exclude_none=True
        ),
        "core_num": payload.core_num,
        "max_l2m_size": payload.max_l2m_size,
        "compile_mode": payload.compile_mode,
        "balance_factor": payload.balance_factor,
        "optimize_level": payload.optimize_level,
        "sample_limit": payload.sample_limit,
        "jobs": payload.jobs,
        "max_time_per_fc": payload.max_time_per_fc,
        "cache_mode": payload.cache_mode,
    }


def _submission_payload(submission: object) -> dict[str, object]:
    return {
        "run_id": submission.run_id,
        "attempt": submission.attempt,
        "status": submission.status,
    }


@router.get("/api/v1/health")
async def health(request: Request) -> dict[str, str]:
    services = _services(request)
    return {"status": "ok", "version": services.version}


@router.get("/api/v1/session")
async def browser_session(request: Request, response: Response) -> dict[str, str | int]:
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        "rdkwt_session",
        request.app.state.session_key,
        httponly=True,
        secure=False,
        samesite="strict",
        path="/",
    )
    return {
        "csrf_token": request.app.state.csrf_token,
        "max_upload_bytes": request.app.state.settings.max_upload_bytes,
    }


@router.get("/api/v1/system/preflight")
async def preflight(request: Request) -> dict[str, object]:
    return _services(request).system_service.preflight()


@router.post(
    "/api/v1/system/preflight/runner-smoke-test",
    status_code=status.HTTP_202_ACCEPTED,
)
async def runner_smoke_test(
    payload: RunnerSmokeTestRequest, request: Request
) -> dict[str, object]:
    services = _services(request)
    try:
        asset_path = services.system_service.ensure_runner_probe_asset()
        submission = services.run_service.submit_contract_probe(
            profile_id=payload.profile_id, asset_path=asset_path
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    services.orchestrator.enqueue(submission.run_id, submission.attempt)
    return _submission_payload(submission)


@router.get("/api/v1/profiles")
async def profiles(request: Request) -> list[dict[str, object]]:
    services = _services(request)
    return [
        {
            **profile.model_dump(mode="json"),
            "sha256": profile.snapshot()["sha256"],
        }
        for profile in services.profiles.list()
    ]


@router.get("/api/v1/projects")
async def projects(request: Request) -> list[dict[str, object]]:
    return _services(request).catalog_service.list_projects()


@router.post("/api/v1/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreateRequest, request: Request
) -> dict[str, object]:
    return _services(request).catalog_service.create_project(
        name=payload.name, description=payload.description
    )


@router.get("/api/v1/projects/{project_id}")
async def project_detail(project_id: str, request: Request) -> dict[str, object]:
    return _services(request).catalog_service.get_project(project_id)


@router.patch("/api/v1/projects/{project_id}")
async def update_project(
    project_id: str, payload: ProjectUpdateRequest, request: Request
) -> dict[str, object]:
    return _services(request).catalog_service.update_project(
        project_id, name=payload.name, description=payload.description
    )


@router.get("/api/v1/projects/{project_id}/deletion-preview")
async def project_deletion_preview(
    project_id: str, request: Request
) -> dict[str, object]:
    return _services(request).catalog_service.project_deletion_preview(project_id)


@router.delete("/api/v1/projects/{project_id}")
async def delete_project(
    project_id: str,
    request: Request,
    confirmation: Annotated[str, Header(alias="X-Confirm-Project")],
) -> dict[str, object]:
    return _services(request).catalog_service.delete_project(
        project_id, confirmation=confirmation
    )


@router.get("/api/v1/projects/{project_id}/models")
async def project_models(project_id: str, request: Request) -> list[dict[str, object]]:
    return _services(request).catalog_service.list_models(project_id)


@router.post(
    "/api/v1/projects/{project_id}/models", status_code=status.HTTP_201_CREATED
)
async def upload_model(
    project_id: str,
    request: Request,
    filename: Annotated[str | None, Header(alias="X-Filename")] = None,
    filename_b64: Annotated[str | None, Header(alias="X-Filename-B64")] = None,
    model_name: str | None = None,
) -> dict[str, object]:
    return await _services(request).catalog_service.upload_model(
        project_id=project_id,
        filename=_upload_filename(filename, filename_b64),
        model_name=model_name,
        content_length=_content_length(request),
        chunks=request.stream(),
    )


@router.get("/api/v1/model-versions/{version_id}")
async def model_version(version_id: str, request: Request) -> dict[str, object]:
    return _services(request).catalog_service.get_model_version(version_id)


@router.post(
    "/api/v1/model-versions/{version_id}/inspect",
    status_code=status.HTTP_202_ACCEPTED,
)
async def inspect_model_version(version_id: str, request: Request) -> dict[str, object]:
    services = _services(request)
    try:
        submission = services.run_service.submit_model_inspection(
            model_version_id=version_id
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    services.orchestrator.enqueue(submission.run_id, submission.attempt)
    return _submission_payload(submission)


@router.get("/api/v1/projects/{project_id}/calibration-sets")
async def project_calibration_sets(
    project_id: str, request: Request
) -> list[dict[str, object]]:
    return _services(request).catalog_service.list_calibration_sets(project_id)


@router.post(
    "/api/v1/projects/{project_id}/calibration-sets",
    status_code=status.HTTP_201_CREATED,
)
async def create_calibration_set(
    project_id: str,
    payload: CalibrationSetCreateRequest,
    request: Request,
) -> dict[str, object]:
    return _services(request).catalog_service.create_calibration_set(
        project_id=project_id,
        name=payload.name,
        description=payload.description,
    )


@router.get("/api/v1/calibration-versions/{version_id}")
async def calibration_version(version_id: str, request: Request) -> dict[str, object]:
    return _services(request).catalog_service.get_calibration_version(version_id)


@router.post(
    "/api/v1/calibration-versions/{version_id}/samples",
    status_code=status.HTTP_201_CREATED,
)
async def upload_calibration_sample(
    version_id: str,
    request: Request,
    filename: Annotated[str | None, Header(alias="X-Filename")] = None,
    filename_b64: Annotated[str | None, Header(alias="X-Filename-B64")] = None,
) -> dict[str, object]:
    return await _services(request).catalog_service.upload_calibration_sample(
        version_id=version_id,
        filename=_upload_filename(filename, filename_b64),
        content_length=_content_length(request),
        chunks=request.stream(),
    )


@router.get(
    "/api/v1/calibration-versions/{version_id}/samples/{ordinal}/content"
)
async def calibration_sample_content(
    version_id: str, ordinal: int, request: Request
) -> FileResponse:
    path, metadata = _services(request).catalog_service.calibration_sample_file(
        version_id, ordinal
    )
    return FileResponse(
        path,
        media_type=metadata["mime_type"],
        filename=metadata["original_filename"],
        content_disposition_type="inline",
    )


@router.post("/api/v1/calibration-versions/{version_id}/finalize")
async def finalize_calibration_version(
    version_id: str, request: Request
) -> dict[str, object]:
    return _services(request).catalog_service.finalize_calibration_version(version_id)


@router.get("/api/v1/runs")
async def runs(request: Request) -> list[dict[str, object]]:
    return _services(request).repository.list()


@router.get("/api/v1/runs/{run_id}")
async def run_detail(run_id: str, request: Request) -> dict[str, object]:
    try:
        return _services(request).run_service.result_detail(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/v1/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(run_id: str, request: Request) -> dict[str, object]:
    try:
        return _services(request).run_service.cancel(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/v1/runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_run(run_id: str, request: Request) -> dict[str, object]:
    services = _services(request)
    try:
        submission = services.run_service.retry(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    services.orchestrator.enqueue(submission.run_id, submission.attempt)
    return _submission_payload(submission)


async def _event_stream(
    request: Request,
    *,
    run_id: str,
    attempt: int,
    after_sequence: int,
) -> AsyncIterator[str]:
    services = _services(request)
    path = services.run_service.events_file(run_id, attempt)
    seen = after_sequence
    terminal_idle_rounds = 0
    while True:
        emitted = False
        if path.is_file() and not path.is_symlink():
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(raw)
                    sequence = int(event["sequence"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if sequence <= seen:
                    continue
                seen = sequence
                emitted = True
                encoded = json.dumps(event, ensure_ascii=False)
                yield f"id: {sequence}\nevent: runner\ndata: {encoded}\n\n"
        detail = services.repository.get(run_id)
        if detail is None:
            yield "event: error\ndata: {\"detail\":\"run not found\"}\n\n"
            return
        if detail["status"] in TERMINAL_STATUSES:
            terminal_idle_rounds = 0 if emitted else terminal_idle_rounds + 1
            if terminal_idle_rounds >= 2:
                yield (
                    "event: terminal\ndata: "
                    + json.dumps({"status": detail["status"]})
                    + "\n\n"
                )
                return
        if await request.is_disconnected():
            return
        await asyncio.sleep(0.25)


async def _log_stream(
    request: Request,
    *,
    run_id: str,
    attempt: int,
    after_sequence: int,
) -> AsyncIterator[str]:
    services = _services(request)
    path = services.run_service.log_stream_file(run_id, attempt)
    seen = after_sequence
    terminal_idle_rounds = 0
    while True:
        emitted = False
        if path.is_file() and not path.is_symlink():
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(raw)
                    sequence = int(item["sequence"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if sequence <= seen:
                    continue
                seen = sequence
                emitted = True
                yield (
                    f"id: {sequence}\nevent: log\ndata: "
                    + json.dumps(item, ensure_ascii=False)
                    + "\n\n"
                )
        detail = services.repository.get(run_id)
        if detail is None:
            yield "event: error\ndata: {\"detail\":\"run not found\"}\n\n"
            return
        if detail["status"] in TERMINAL_STATUSES:
            terminal_idle_rounds = 0 if emitted else terminal_idle_rounds + 1
            if terminal_idle_rounds >= 2:
                yield (
                    "event: terminal\ndata: "
                    + json.dumps({"status": detail["status"]})
                    + "\n\n"
                )
                return
        if await request.is_disconnected():
            return
        await asyncio.sleep(0.25)


@router.get("/api/v1/runs/{run_id}/attempts/{attempt}/events")
async def run_events(
    run_id: str,
    attempt: int,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    try:
        _services(request).repository.execution(run_id, attempt)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if last_event_id is not None:
        try:
            after = max(after, int(last_event_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Last-Event-ID") from exc
    return StreamingResponse(
        _event_stream(
            request, run_id=run_id, attempt=attempt, after_sequence=after
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/v1/runs/{run_id}/attempts/{attempt}/logs")
async def download_run_log(run_id: str, attempt: int, request: Request) -> FileResponse:
    try:
        path = _services(request).run_service.log_file(run_id, attempt)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="text/plain", filename=f"{run_id}-a{attempt}.log")


@router.get("/api/v1/runs/{run_id}/attempts/{attempt}/log-stream")
async def stream_run_log(
    run_id: str,
    attempt: int,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    try:
        _services(request).repository.execution(run_id, attempt)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if last_event_id is not None:
        try:
            after = max(after, int(last_event_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Last-Event-ID") from exc
    return StreamingResponse(
        _log_stream(
            request, run_id=run_id, attempt=attempt, after_sequence=after
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/runs/{run_id}/attempts/{attempt}/artifacts/{artifact_index}")
async def download_artifact(
    run_id: str,
    attempt: int,
    artifact_index: int,
    request: Request,
    inline: bool = False,
) -> FileResponse:
    try:
        path, metadata = _services(request).run_service.artifact_file(
            run_id, attempt, artifact_index
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    headers = {}
    if inline and metadata["mime_type"] == "text/html":
        headers["X-RDKWT-Sandbox-Report"] = "true"
    return FileResponse(
        path,
        media_type=metadata["mime_type"],
        filename=path.name,
        content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )


@router.get("/api/v1/runs/{run_id}/export")
async def export_run(run_id: str, request: Request) -> FileResponse:
    try:
        path = _services(request).run_service.export_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.post("/api/v1/system/runner-probes", status_code=status.HTTP_202_ACCEPTED)
async def submit_probe(
    payload: ContractProbeRequest,
    request: Request,
) -> dict[str, object]:
    services = _services(request)
    try:
        submission = services.run_service.submit_contract_probe(
            profile_id=payload.profile_id,
            asset_path=payload.asset_path,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    services.orchestrator.enqueue(submission.run_id, submission.attempt)
    return _submission_payload(submission)


@router.post("/api/v1/conversion-previews")
async def preview_conversion(
    payload: ConversionRunRequest, request: Request
) -> dict[str, object]:
    try:
        return _services(request).run_service.preview_conversion(
            **_conversion_kwargs(payload)
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/v1/conversion-runs", status_code=status.HTTP_202_ACCEPTED)
async def submit_conversion(
    payload: ConversionRunRequest,
    request: Request,
) -> dict[str, object]:
    services = _services(request)
    try:
        submission = services.run_service.submit_conversion(
            **_conversion_kwargs(payload)
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    services.orchestrator.enqueue(submission.run_id, submission.attempt)
    return _submission_payload(submission)
