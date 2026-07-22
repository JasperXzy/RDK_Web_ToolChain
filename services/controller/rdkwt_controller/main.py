from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rdkwt_controller import __version__
from rdkwt_controller.api import router
from rdkwt_controller.application import (
    BoardError,
    BoardOrchestrator,
    BoardService,
    CatalogError,
    CatalogService,
    DeviceService,
    MaintenanceError,
    MaintenanceService,
    RunOrchestrator,
    RunService,
    SystemService,
)
from rdkwt_controller.infrastructure.assets import AssetStore
from rdkwt_controller.infrastructure.board import BoardGateway
from rdkwt_controller.infrastructure.credentials import CredentialStore
from rdkwt_controller.infrastructure.db import (
    BoardRepository,
    CatalogRepository,
    RunRepository,
    create_database,
    create_session_factory,
    migrate_database,
)
from rdkwt_controller.infrastructure.docker import DockerGateway
from rdkwt_controller.profiles import ProfileRegistry
from rdkwt_controller.settings import Settings


@dataclass(slots=True)
class AppServices:
    version: str
    profiles: ProfileRegistry
    repository: RunRepository
    catalog_service: CatalogService
    docker_gateway: DockerGateway
    run_service: RunService
    orchestrator: RunOrchestrator
    system_service: SystemService
    device_service: DeviceService
    board_service: BoardService
    board_orchestrator: BoardOrchestrator
    maintenance_service: MaintenanceService


class LocalSecurityMiddleware:
    """Pure ASGI middleware so streaming files and SSE are never buffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)

        async def send_with_security(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if headers.get("X-RDKWT-Sandbox-Report") is not None:
                    del headers["X-RDKWT-Sandbox-Report"]
                    headers["Content-Security-Policy"] = (
                        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
                    )
                elif request.url.path not in {"/docs", "/redoc"}:
                    headers["Content-Security-Policy"] = (
                        "default-src 'self'; script-src 'self'; style-src 'self'; "
                        "style-src-attr 'unsafe-inline'; "
                        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                        "base-uri 'none'; frame-ancestors 'none'"
                    )
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        if request.url.path.startswith("/api/") and request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            supplied = request.headers.get("x-rdkwt-csrf", "")
            if not secrets.compare_digest(supplied, request.app.state.csrf_token):
                response = _problem_response(
                    "CSRF_TOKEN_INVALID",
                    "a valid X-RDKWT-CSRF token is required for this local mutation",
                    403,
                )
                await response(scope, receive, send_with_security)
                return
            session_key = request.cookies.get("rdkwt_session", "")
            if not secrets.compare_digest(session_key, request.app.state.session_key):
                response = _problem_response(
                    "SESSION_COOKIE_INVALID",
                    "a valid SameSite local session cookie is required",
                    403,
                )
                await response(scope, receive, send_with_security)
                return
            origin = request.headers.get("origin")
            if (
                origin is not None
                and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()
            ):
                response = _problem_response(
                    "ORIGIN_NOT_ALLOWED",
                    "the request Origin does not match the local application origin",
                    403,
                )
                await response(scope, receive, send_with_security)
                return

        await self.app(scope, receive, send_with_security)


def create_app(
    settings: Settings | None = None,
    *,
    docker_client: object | None = None,
    board_gateway: BoardGateway | None = None,
) -> FastAPI:
    settings = Settings.from_env() if settings is None else settings
    settings.ensure_directories()
    profiles = ProfileRegistry.load(settings.profile_dir)
    migrate_database(settings.database_url, settings.alembic_config_path)
    engine = create_database(settings.database_url)
    session_factory = create_session_factory(engine)
    repository = RunRepository(session_factory)
    catalog_repository = CatalogRepository(session_factory)
    board_repository = BoardRepository(session_factory)
    catalog_service = CatalogService(
        repository=catalog_repository,
        asset_store=AssetStore(settings.assets_dir, max_upload_bytes=settings.max_upload_bytes),
        runs_root=settings.runs_dir,
    )
    catalog_service.set_exports_root(settings.project_exports_dir)
    docker_gateway = (
        DockerGateway.from_env(settings)
        if docker_client is None
        else DockerGateway(docker_client, settings)
    )
    run_service = RunService(
        settings=settings,
        profiles=profiles,
        repository=repository,
        catalog_repository=catalog_repository,
        docker_gateway=docker_gateway,
    )
    orchestrator = RunOrchestrator(
        run_service=run_service,
        repository=repository,
        docker_gateway=docker_gateway,
    )
    system_service = SystemService(
        settings=settings,
        docker_gateway=docker_gateway,
        repository=repository,
    )
    maintenance_service = MaintenanceService(
        settings=settings,
        app_version=__version__,
        run_repository=repository,
        board_repository=board_repository,
        catalog_repository=catalog_repository,
        system_service=system_service,
    )
    credential_store = CredentialStore(settings.secrets_dir)
    board_gateway = board_gateway or BoardGateway(
        connect_timeout=settings.board_connect_timeout_seconds,
        command_timeout=settings.board_command_timeout_seconds,
        max_output_bytes=settings.max_log_bytes,
        max_download_bytes=settings.board_max_upload_bytes,
    )
    device_service = DeviceService(
        repository=board_repository,
        credential_store=credential_store,
        gateway=board_gateway,
    )
    board_service = BoardService(
        settings=settings,
        repository=board_repository,
        credential_store=credential_store,
        gateway=board_gateway,
        run_service=run_service,
        profiles=profiles,
    )
    board_orchestrator = BoardOrchestrator(
        service=board_service,
        repository=board_repository,
        gateway=board_gateway,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        orchestrator.start()
        board_orchestrator.start()
        try:
            yield
        finally:
            board_orchestrator.stop()
            orchestrator.stop()

    app = FastAPI(title="RDK WebToolChain", version=__version__, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(LocalSecurityMiddleware)
    app.state.settings = settings
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.session_key = secrets.token_urlsafe(32)
    app.state.services = AppServices(
        version=__version__,
        profiles=profiles,
        repository=repository,
        catalog_service=catalog_service,
        docker_gateway=docker_gateway,
        run_service=run_service,
        orchestrator=orchestrator,
        system_service=system_service,
        device_service=device_service,
        board_service=board_service,
        board_orchestrator=board_orchestrator,
        maintenance_service=maintenance_service,
    )
    app.include_router(router)

    @app.exception_handler(CatalogError)
    async def catalog_error(_request: Request, exc: CatalogError) -> JSONResponse:
        return _problem_response(exc.code, str(exc), exc.status_code)

    @app.exception_handler(BoardError)
    async def board_error(_request: Request, exc: BoardError) -> JSONResponse:
        response = _problem_response(exc.code, str(exc), exc.status_code)
        if exc.observed_fingerprint is not None:
            content = json.loads(bytes(response.body))
            content["observed_fingerprint"] = exc.observed_fingerprint
            return JSONResponse(
                status_code=exc.status_code,
                media_type="application/problem+json",
                content=content,
            )
        return response

    @app.exception_handler(MaintenanceError)
    async def maintenance_error(_request: Request, exc: MaintenanceError) -> JSONResponse:
        return _problem_response(exc.code, str(exc), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        device_suffix = request.url.path.removeprefix("/api/v1/devices/")
        credential_request = request.url.path == "/api/v1/devices" or (
            request.method == "PATCH" and device_suffix and "/" not in device_suffix
        )
        if credential_request:
            return _problem_response(
                "DEVICE_REQUEST_INVALID",
                "device request validation failed; credential values were omitted",
                422,
            )
        return await request_validation_exception_handler(request, exc)

    web_root = Path(__file__).with_name("web")
    app.mount("/static", StaticFiles(directory=web_root), name="static")
    index_html = (web_root / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        return index_html

    return app


def _problem_response(code: str, detail: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        content={
            "type": f"https://local.rdkwt/errors/{code.lower().replace('_', '-')}",
            "title": code.replace("_", " ").title(),
            "status": status_code,
            "code": code,
            "detail": detail,
        },
    )


def run() -> None:
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port)


if __name__ == "__main__":
    run()
