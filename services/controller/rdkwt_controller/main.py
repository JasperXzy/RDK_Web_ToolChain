from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from rdkwt_controller import __version__
from rdkwt_controller.api import router
from rdkwt_controller.application import CatalogError, CatalogService, RunService
from rdkwt_controller.infrastructure.assets import AssetStore
from rdkwt_controller.infrastructure.db import (
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


def create_app(
    settings: Settings | None = None,
    *,
    docker_client: object | None = None,
) -> FastAPI:
    settings = Settings.from_env() if settings is None else settings
    settings.ensure_directories()
    profiles = ProfileRegistry.load(settings.profile_dir)
    migrate_database(settings.database_url, settings.alembic_config_path)
    engine = create_database(settings.database_url)
    session_factory = create_session_factory(engine)
    repository = RunRepository(session_factory)
    catalog_repository = CatalogRepository(session_factory)
    catalog_service = CatalogService(
        repository=catalog_repository,
        asset_store=AssetStore(
            settings.assets_dir, max_upload_bytes=settings.max_upload_bytes
        ),
    )
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

    app = FastAPI(title="RDK WebToolChain", version=__version__)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.state.settings = settings
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.services = AppServices(
        version=__version__,
        profiles=profiles,
        repository=repository,
        catalog_service=catalog_service,
        docker_gateway=docker_gateway,
        run_service=run_service,
    )
    app.include_router(router)

    @app.exception_handler(CatalogError)
    async def catalog_error(_request: Request, exc: CatalogError) -> JSONResponse:
        return _problem_response(exc.code, str(exc), exc.status_code)

    @app.middleware("http")
    async def protect_mutations(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            supplied = request.headers.get("x-rdkwt-csrf", "")
            if not secrets.compare_digest(supplied, request.app.state.csrf_token):
                return _problem_response(
                    "CSRF_TOKEN_INVALID",
                    "a valid X-RDKWT-CSRF token is required for this local mutation",
                    403,
                )
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).netloc.lower() != request.headers.get(
                "host", ""
            ).lower():
                return _problem_response(
                    "ORIGIN_NOT_ALLOWED",
                    "the request Origin does not match the local application origin",
                    403,
                )
        response = await call_next(request)
        if request.url.path not in {"/docs", "/redoc"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "style-src-attr 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

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
