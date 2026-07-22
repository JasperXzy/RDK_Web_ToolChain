from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from rdkwt_controller.infrastructure.docker.gateway import (
    ATTEMPT_LABEL,
    MANAGED_LABEL,
    RUN_ID_LABEL,
    DockerGateway,
    ResolvedRunnerImage,
)
from rdkwt_controller.settings import Settings


class UnusedClient:
    pass


class PingClient:
    @staticmethod
    def ping() -> bool:
        return True


class TestImage:
    id = "sha256:" + "b" * 64
    attrs = {"RepoDigests": []}


class TestImages:
    @staticmethod
    def get(_reference: str) -> TestImage:
        return TestImage()


class GpuClient:
    images = TestImages()

    @staticmethod
    def info() -> dict[str, object]:
        return {"Runtimes": {"runc": {}, "nvidia": {}}}


def test_environment_client_is_connected_lazily(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        "rdkwt_controller.infrastructure.docker.gateway.docker.from_env",
        lambda: calls.append("connected") or PingClient(),
    )

    gateway = DockerGateway.from_env(settings)

    assert calls == []
    assert gateway.ping() is True
    assert calls == ["connected"]


def test_container_spec_has_fixed_security_boundary(settings: Settings) -> None:
    gateway = DockerGateway(UnusedClient(), settings)
    image = ResolvedRunnerImage(
        logical_id="openexplorer-3.7.0-cpu",
        configured_reference=settings.cpu_runner_image,
        immutable_id="sha256:" + "a" * 64,
        repo_digests=(),
    )
    run_id = str(uuid.uuid4())

    options = gateway.container_create_kwargs(run_id=run_id, attempt=1, image=image)

    assert options["image"] == image.immutable_id
    assert options["command"] == ["--request", f"/runs/{run_id}/attempts/1/request.json"]
    assert options["network_disabled"] is True
    assert options["read_only"] is True
    assert options["user"] == f"{settings.runner_uid}:{settings.runner_gid}"
    assert options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges"]
    assert "entrypoint" not in options
    assert "privileged" not in options
    assert "devices" not in options
    assert set(options["volumes"]) == {
        settings.assets_volume,
        settings.runs_volume,
        settings.cache_volume,
    }
    assert options["volumes"][settings.cache_volume]["bind"] == "/cache"
    assert options["labels"][MANAGED_LABEL] == "true"
    assert options["labels"][RUN_ID_LABEL] == run_id
    assert options["labels"][ATTEMPT_LABEL] == "1"


def test_deployment_rejects_host_paths_as_volume_names(settings: Settings) -> None:
    invalid = Settings(
        state_dir=settings.state_dir,
        assets_dir=settings.assets_dir,
        runs_dir=settings.runs_dir,
        profile_dir=settings.profile_dir,
        assets_volume="/tmp/assets",
        runs_volume="rdkwt-runs",
        cpu_runner_image=settings.cpu_runner_image,
    )
    with pytest.raises(ValueError, match="volume name"):
        DockerGateway(UnusedClient(), invalid)


def test_gpu_runner_is_optional_and_uses_an_explicit_device_request(
    settings: Settings,
) -> None:
    disabled = DockerGateway(UnusedClient(), settings).gpu_capability()
    assert disabled["status"] == "DISABLED"
    assert disabled["available"] is False

    configured = replace(
        settings,
        gpu_enabled=True,
        gpu_runner_image="rdk-webtoolchain/oe-runner-gpu:oe3.7.0-app0.1",
        gpu_device_ids=("0",),
    )
    gateway = DockerGateway(GpuClient(), configured)
    capability = gateway.gpu_capability()
    image = gateway.resolve_runner_image("gpu")
    options = gateway.container_create_kwargs(
        run_id=str(uuid.uuid4()), attempt=1, image=image, runner_mode="gpu"
    )

    assert capability["status"] == "READY"
    assert capability["available"] is True
    assert options["shm_size"] == "15g"
    assert options["device_requests"][0]["Driver"] == "nvidia"
    assert options["device_requests"][0]["DeviceIDs"] == ["0"]
