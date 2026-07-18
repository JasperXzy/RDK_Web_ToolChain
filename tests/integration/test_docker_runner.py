from __future__ import annotations

import base64
import json
import os
import uuid

import docker
import pytest
from rdkwt_contracts import validate_payload
from rdkwt_controller.infrastructure.docker import DockerGateway
from rdkwt_controller.settings import Settings

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RDKWT_RUN_DOCKER_TESTS") != "1",
        reason="set RDKWT_RUN_DOCKER_TESTS=1 to run Docker integration tests",
    ),
]


def test_restricted_openexplorer_runner_contract(tmp_path) -> None:
    client = docker.from_env()
    suffix = uuid.uuid4().hex[:12]
    assets_volume_name = f"rdkwt-it-assets-{suffix}"
    runs_volume_name = f"rdkwt-it-runs-{suffix}"
    image_ref = os.environ.get(
        "RDKWT_CPU_RUNNER_IMAGE",
        "rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1",
    )
    assets_volume = client.volumes.create(name=assets_volume_name)
    runs_volume = client.volumes.create(name=runs_volume_name)
    run_id = str(uuid.uuid4())
    request = {
        "contract_version": "1.0",
        "run_id": run_id,
        "attempt": 1,
        "adapter": "contract-probe-1.0",
        "runner_mode": "cpu",
        "pipeline": ["inspect", "check", "collect"],
        "paths": {
            "model": "probe/model.bin",
            "calibration_source": None,
            "attempt_root": f"{run_id}/attempts/1",
        },
        "configuration": {},
        "limits": {"timeout_seconds": 60, "max_log_bytes": 1_048_576},
    }
    validate_payload("request", request)
    encoded_request = base64.b64encode(json.dumps(request).encode()).decode()
    populate_script = (
        "import base64,json,os,pathlib;"
        "asset=pathlib.Path('/assets/probe/model.bin');asset.parent.mkdir(parents=True);"
        "asset.write_bytes(b'rdkwt-integration-probe');"
        f"request=pathlib.Path('/runs/{run_id}/attempts/1/request.json');"
        "request.parent.mkdir(parents=True);"
        "request.write_bytes(base64.b64decode(os.environ['REQUEST_B64']))"
    )
    settings = Settings(
        state_dir=tmp_path / "state",
        assets_dir=tmp_path / "assets",
        runs_dir=tmp_path / "runs",
        profile_dir=tmp_path,
        assets_volume=assets_volume_name,
        runs_volume=runs_volume_name,
        cpu_runner_image=image_ref,
    )
    gateway = DockerGateway(client, settings)
    runner = None
    try:
        client.containers.run(
            image_ref,
            command=["-c", populate_script],
            entrypoint="python3",
            environment={"REQUEST_B64": encoded_request},
            network_disabled=True,
            remove=True,
            volumes={
                assets_volume_name: {"bind": "/assets", "mode": "rw"},
                runs_volume_name: {"bind": "/runs", "mode": "rw"},
            },
        )
        runner = gateway.create_attempt(run_id=run_id, attempt=1)
        gateway.start(runner)
        logs = b"".join(gateway.logs(runner))
        assert gateway.wait(runner) == 0, logs.decode(errors="replace")

        read_script = (
            "import pathlib;"
            f"print(pathlib.Path('/runs/{run_id}/attempts/1/result.json').read_text())"
        )
        output = client.containers.run(
            image_ref,
            command=["-c", read_script],
            entrypoint="python3",
            network_disabled=True,
            remove=True,
            read_only=True,
            volumes={runs_volume_name: {"bind": "/runs", "mode": "ro"}},
        )
        result = json.loads(output)
        validate_payload("result", result)
        assert result["status"] == "succeeded"
        assert b"RUNNER_SUCCEEDED" in logs
        gateway.remove_managed(runner.id, run_id=run_id, attempt=1)
        runner = None
    finally:
        if runner is not None:
            runner.reload()
            if runner.status == "running":
                runner.kill()
            runner.remove(force=True, v=True)
        assets_volume.remove(force=True)
        runs_volume.remove(force=True)
