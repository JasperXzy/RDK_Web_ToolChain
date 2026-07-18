from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from rdkwt_controller.api.routes import ConversionRunRequest
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


def test_conversion_submission_writes_normalized_s600_request(settings) -> None:
    model = settings.assets_dir / "models" / "resnet18.onnx"
    calibration = settings.assets_dir / "calibration" / "imagenet"
    model.parent.mkdir(parents=True)
    calibration.mkdir(parents=True)
    model.write_bytes(b"model-placeholder")
    app = create_app(settings, docker_client=FakeDockerClient())

    submission = app.state.services.run_service.submit_conversion(
        profile_id="s600-oe-3.7.0",
        model_path="models/resnet18.onnx",
        calibration_path="calibration/imagenet",
        output_prefix="resnet18_s600",
        core_num=2,
        max_l2m_size="auto",
        compile_mode="latency",
        balance_factor=None,
        optimize_level="O2",
        sample_limit=100,
        jobs=8,
    )
    request_path = (
        settings.runs_dir
        / submission.run_id
        / "attempts"
        / str(submission.attempt)
        / "request.json"
    )
    request = json.loads(request_path.read_text())

    assert request["adapter"] == "openexplorer-3.7.0"
    assert request["configuration"]["target_profile"]["profile"]["march"] == "nash-p"
    assert request["configuration"]["compiler"]["core_num"] == 2
    assert request["configuration"]["compiler"]["max_l2m_size"] == "auto"


def test_conversion_submission_rejects_s100_dual_core(settings) -> None:
    model = settings.assets_dir / "models" / "resnet18.onnx"
    calibration = settings.assets_dir / "calibration" / "imagenet"
    model.parent.mkdir(parents=True)
    calibration.mkdir(parents=True)
    model.write_bytes(b"model-placeholder")
    app = create_app(settings, docker_client=FakeDockerClient())

    with pytest.raises(ValueError, match="core_num"):
        app.state.services.run_service.submit_conversion(
            profile_id="s100-oe-3.7.0",
            model_path="models/resnet18.onnx",
            calibration_path="calibration/imagenet",
            output_prefix="resnet18_s100",
            core_num=2,
            max_l2m_size=0,
            compile_mode="latency",
            balance_factor=None,
            optimize_level="O2",
            sample_limit=100,
            jobs=8,
        )


def test_conversion_request_rejects_boolean_integer_fields() -> None:
    with pytest.raises(ValidationError):
        ConversionRunRequest.model_validate(
            {
                "profile_id": "s100-oe-3.7.0",
                "model_path": "models/resnet18.onnx",
                "calibration_path": "calibration/imagenet",
                "core_num": True,
            }
        )
