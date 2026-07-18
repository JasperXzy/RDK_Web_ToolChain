from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient
from rdkwt_controller.main import create_app


class FakeImage:
    id = "sha256:" + "a" * 64
    attrs = {"RepoDigests": ["example.invalid/runner@sha256:" + "a" * 64]}


class FakeImages:
    def get(self, _reference: str) -> FakeImage:
        return FakeImage()


class FakeDockerClient:
    images = FakeImages()

    @staticmethod
    def ping() -> bool:
        return True

    @staticmethod
    def version() -> dict[str, str]:
        return {"Version": "test", "ApiVersion": "test", "Os": "linux", "Arch": "amd64"}

    @staticmethod
    def info() -> dict[str, str]:
        return {"OperatingSystem": "test", "DockerRootDir": "/test"}


def test_health_profiles_and_preflight(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())

    async def exercise_app():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await asyncio.gather(
                client.get("/api/v1/health"),
                client.get("/api/v1/profiles"),
                client.get("/api/v1/system/preflight"),
            )

    health, profiles, preflight = asyncio.run(exercise_app())

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert {item["platform"] for item in profiles.json()} == {"s100", "s600"}
    assert preflight.json()["available"] is True
    assert preflight.json()["details"]["runner_image"]["immutable_id"].startswith("sha256:")


def test_probe_rejects_missing_asset_before_docker(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())

    async def exercise_app():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                "/api/v1/system/runner-probes",
                json={"profile_id": "s100-oe-3.7.0", "asset_path": "missing.onnx"},
            )

    response = asyncio.run(exercise_app())
    assert response.status_code == 422
