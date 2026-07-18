from __future__ import annotations

import base64
import json
import os
import time
import uuid

import docker
import pytest

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RDKWT_RUN_DOCKER_TESTS") != "1",
        reason="set RDKWT_RUN_DOCKER_TESTS=1 to run Docker integration tests",
    ),
]


def _container_http_json(
    container,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object] | list[dict[str, object]]:
    encoded_payload = (
        "" if payload is None else base64.b64encode(json.dumps(payload).encode()).decode()
    )
    script = (
        "import base64,json,urllib.request;"
        f"data=base64.b64decode('{encoded_payload}') if '{encoded_payload}' else None;"
        f"request=urllib.request.Request('http://127.0.0.1:8080{path}',data=data,method='{method}');"
        "request.add_header('Content-Type','application/json');"
        "print(urllib.request.urlopen(request,timeout=10).read().decode())"
    )
    exit_code, output = container.exec_run(["python", "-c", script])
    if exit_code != 0:
        raise RuntimeError(output.decode(errors="replace"))
    return json.loads(output)


def _wait_for_controller(container) -> None:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = _container_http_json(container, "/api/v1/health")
            if response["status"] == "ok":  # type: ignore[index]
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.25)
    container.reload()
    logs = container.logs().decode(errors="replace")
    raise AssertionError(f"Controller did not become ready: {last_error}\n{logs}")


def test_controller_creates_collects_and_cleans_restricted_runner() -> None:
    client = docker.from_env()
    suffix = uuid.uuid4().hex[:12]
    state_name = f"rdkwt-e2e-state-{suffix}"
    assets_name = f"rdkwt-e2e-assets-{suffix}"
    runs_name = f"rdkwt-e2e-runs-{suffix}"
    controller_name = f"rdkwt-controller-e2e-{suffix}"
    controller_image = os.environ.get(
        "RDKWT_CONTROLLER_IMAGE", "rdk-webtoolchain/controller:0.1-dev"
    )
    runner_image = os.environ.get(
        "RDKWT_CPU_RUNNER_IMAGE", "rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1"
    )
    volumes = [
        client.volumes.create(name=state_name),
        client.volumes.create(name=assets_name),
        client.volumes.create(name=runs_name),
    ]
    controller = None
    run_id = None
    try:
        populate_script = (
            "import pathlib;"
            "asset=pathlib.Path('/assets/probe/model.bin');"
            "asset.parent.mkdir(parents=True);"
            "asset.write_bytes(b'controller-e2e-probe')"
        )
        client.containers.run(
            runner_image,
            command=["-c", populate_script],
            entrypoint="python3",
            network_disabled=True,
            remove=True,
            volumes={assets_name: {"bind": "/assets", "mode": "rw"}},
        )
        controller = client.containers.run(
            controller_image,
            name=controller_name,
            detach=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            group_add=[65534],
            network_disabled=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=256m"},
            environment={
                "RDKWT_BIND_HOST": "127.0.0.1",
                "RDKWT_PORT": "8080",
                "RDKWT_STATE_DIR": "/state",
                "RDKWT_ASSETS_DIR": "/assets",
                "RDKWT_RUNS_DIR": "/runs",
                "RDKWT_PROFILE_DIR": "/app/profiles/targets",
                "RDKWT_ASSETS_VOLUME": assets_name,
                "RDKWT_RUNS_VOLUME": runs_name,
                "RDKWT_CPU_RUNNER_IMAGE": runner_image,
            },
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                state_name: {"bind": "/state", "mode": "rw"},
                assets_name: {"bind": "/assets", "mode": "rw"},
                runs_name: {"bind": "/runs", "mode": "rw"},
            },
        )
        _wait_for_controller(controller)
        preflight = _container_http_json(controller, "/api/v1/system/preflight")
        assert preflight["available"] is True  # type: ignore[index]

        submission = _container_http_json(
            controller,
            "/api/v1/system/runner-probes",
            method="POST",
            payload={"profile_id": "s100-oe-3.7.0", "asset_path": "probe/model.bin"},
        )
        run_id = str(submission["run_id"])  # type: ignore[index]
        deadline = time.monotonic() + 30
        result = None
        while time.monotonic() < deadline:
            result = _container_http_json(controller, f"/api/v1/runs/{run_id}")
            if result["status"] in {"SUCCEEDED", "FAILED"}:  # type: ignore[index]
                break
            time.sleep(0.25)

        assert result is not None
        assert result["status"] == "SUCCEEDED"  # type: ignore[index]
        assert result["attempts"][0]["exit_code"] == 0  # type: ignore[index]
        leftovers = client.containers.list(
            all=True,
            filters={"label": f"io.drobotics.rdkwt.run_id={run_id}"},
        )
        assert leftovers == []
    finally:
        if run_id is not None:
            for leftover in client.containers.list(
                all=True,
                filters={"label": f"io.drobotics.rdkwt.run_id={run_id}"},
            ):
                leftover.remove(force=True, v=True)
        if controller is not None:
            controller.remove(force=True, v=True)
        for volume in volumes:
            volume.remove(force=True)
