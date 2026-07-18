from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt

router = APIRouter()


class ContractProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=128)
    asset_path: str = Field(min_length=1, max_length=1024)


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
    recipe_id: Literal["imagenet-resnet18"] = "imagenet-resnet18"
    core_num: StrictInt | None = Field(default=None, ge=1, le=2)
    max_l2m_size: StrictInt | Literal["auto"] | None = Field(default=None)
    compile_mode: Literal["latency", "bandwidth", "balance"] = "latency"
    balance_factor: StrictInt | None = Field(default=None, ge=0, le=100)
    optimize_level: Literal["O0", "O1", "O2"] = "O2"
    sample_limit: StrictInt = Field(default=100, ge=20, le=100)
    jobs: StrictInt = Field(default=8, ge=1, le=128)


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
        raise HTTPException(status_code=400, detail="X-Filename or X-Filename-B64 is required")
    return filename


@router.get("/api/v1/health")
async def health(request: Request) -> dict[str, str]:
    services = _services(request)
    return {"status": "ok", "version": services.version}


@router.get("/api/v1/session")
async def browser_session(request: Request, response: Response) -> dict[str, str | int]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "csrf_token": request.app.state.csrf_token,
        "max_upload_bytes": request.app.state.settings.max_upload_bytes,
    }


@router.get("/api/v1/system/preflight")
async def preflight(request: Request) -> dict[str, object]:
    services = _services(request)
    try:
        details = services.docker_gateway.preflight()
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    return {"available": True, "details": details}


@router.get("/api/v1/profiles")
async def profiles(request: Request) -> list[dict[str, object]]:
    services = _services(request)
    return [profile.model_dump(mode="json") for profile in services.profiles.list()]


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
    result = _services(request).repository.get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    return result


@router.post("/api/v1/system/runner-probes", status_code=status.HTTP_202_ACCEPTED)
async def submit_probe(
    payload: ContractProbeRequest,
    background_tasks: BackgroundTasks,
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
    background_tasks.add_task(services.run_service.execute, submission.run_id, submission.attempt)
    return {
        "run_id": submission.run_id,
        "attempt": submission.attempt,
        "status": submission.status,
    }


@router.post("/api/v1/conversion-runs", status_code=status.HTTP_202_ACCEPTED)
async def submit_conversion(
    payload: ConversionRunRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, object]:
    services = _services(request)
    try:
        submission = services.run_service.submit_conversion(
            profile_id=payload.profile_id,
            model_version_id=str(payload.model_version_id),
            calibration_version_id=str(payload.calibration_version_id),
            output_prefix=payload.output_prefix,
            core_num=payload.core_num,
            max_l2m_size=payload.max_l2m_size,
            compile_mode=payload.compile_mode,
            balance_factor=payload.balance_factor,
            optimize_level=payload.optimize_level,
            sample_limit=payload.sample_limit,
            jobs=payload.jobs,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    background_tasks.add_task(services.run_service.execute, submission.run_id, submission.attempt)
    return {
        "run_id": submission.run_id,
        "attempt": submission.attempt,
        "status": submission.status,
    }
