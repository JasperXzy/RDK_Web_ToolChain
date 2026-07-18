from __future__ import annotations

from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from rdkwt_controller import __version__
from rdkwt_controller.api import router
from rdkwt_controller.application import RunService
from rdkwt_controller.infrastructure.db import (
    RunRepository,
    create_database,
    create_session_factory,
)
from rdkwt_controller.infrastructure.docker import DockerGateway
from rdkwt_controller.profiles import ProfileRegistry
from rdkwt_controller.settings import Settings


@dataclass(slots=True)
class AppServices:
    version: str
    profiles: ProfileRegistry
    repository: RunRepository
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
    engine = create_database(settings.database_url)
    repository = RunRepository(create_session_factory(engine))
    docker_gateway = (
        DockerGateway.from_env(settings)
        if docker_client is None
        else DockerGateway(docker_client, settings)
    )
    run_service = RunService(
        settings=settings,
        profiles=profiles,
        repository=repository,
        docker_gateway=docker_gateway,
    )

    app = FastAPI(title="RDK WebToolChain", version=__version__)
    app.state.settings = settings
    app.state.services = AppServices(
        version=__version__,
        profiles=profiles,
        repository=repository,
        docker_gateway=docker_gateway,
        run_service=run_service,
    )
    app.include_router(router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        return """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>RDK WebToolChain</title></head>
<body>
  <main>
    <h1>RDK WebToolChain</h1>
    <p>M0 Controller is running.</p>
    <p><a href="/docs">OpenAPI</a></p>
  </main>
</body>
</html>"""

    return app


def run() -> None:
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port)


if __name__ == "__main__":
    run()
