from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()


class ContractProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=128)
    asset_path: str = Field(min_length=1, max_length=1024)


def _services(request: Request) -> object:
    return request.app.state.services


@router.get("/api/v1/health")
async def health(request: Request) -> dict[str, str]:
    services = _services(request)
    return {"status": "ok", "version": services.version}


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
