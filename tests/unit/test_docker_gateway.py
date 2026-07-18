from __future__ import annotations

import uuid

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
    assert options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges"]
    assert "entrypoint" not in options
    assert "privileged" not in options
    assert "devices" not in options
    assert set(options["volumes"]) == {settings.assets_volume, settings.runs_volume}
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
