from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile
from pathlib import Path

import docker
import pytest

pytestmark = [
    pytest.mark.docker,
    pytest.mark.release,
    pytest.mark.skipif(
        os.environ.get("RDKWT_RUN_OE_RELEASE_TESTS") != "1",
        reason="set RDKWT_RUN_OE_RELEASE_TESTS=1 with local ResNet18 assets",
    ),
]

EXPECTED_RESNET18_SHA256 = "4e8f8653e7a2222b3904cc3fe8e304cd8b339ce1d05fd24688162f86fb6df52c"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}


def _container_http_json(
    container,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object] | list[dict[str, object]]:
    encoded = base64.b64encode(json.dumps(payload).encode()).decode() if payload else ""
    script = """
import base64
import json
import sys
import urllib.request

method, path, encoded = sys.argv[1:]
data = base64.b64decode(encoded) if encoded else None
headers = {"Content-Type": "application/json"}
if method != "GET":
    session = urllib.request.urlopen("http://127.0.0.1:8080/api/v1/session", timeout=30)
    headers["X-RDKWT-CSRF"] = json.loads(session.read())["csrf_token"]
    headers["Cookie"] = session.headers.get("Set-Cookie").split(";", 1)[0]
request = urllib.request.Request(
    "http://127.0.0.1:8080" + path,
    data=data,
    method=method,
    headers=headers,
)
print(urllib.request.urlopen(request, timeout=180).read().decode())
"""
    exit_code, output = container.exec_run(["python", "-c", script, method, path, encoded])
    if exit_code != 0:
        raise RuntimeError(output.decode(errors="replace"))
    return json.loads(output)


def _container_http_file(
    container,
    path: str,
    *,
    container_file: str,
    filename: str,
) -> dict[str, object]:
    script = """
import base64
import json
import pathlib
import sys
import urllib.request

path, source_path, filename = sys.argv[1:]
payload = pathlib.Path(source_path).read_bytes()
session = urllib.request.urlopen("http://127.0.0.1:8080/api/v1/session", timeout=30)
headers = {
    "Content-Type": "application/octet-stream",
    "Content-Length": str(len(payload)),
    "X-Filename-B64": base64.b64encode(filename.encode()).decode(),
    "X-RDKWT-CSRF": json.loads(session.read())["csrf_token"],
    "Cookie": session.headers.get("Set-Cookie").split(";", 1)[0],
}
request = urllib.request.Request(
    "http://127.0.0.1:8080" + path,
    data=payload,
    method="POST",
    headers=headers,
)
print(urllib.request.urlopen(request, timeout=300).read().decode())
"""
    exit_code, output = container.exec_run(["python", "-c", script, path, container_file, filename])
    if exit_code != 0:
        raise RuntimeError(output.decode(errors="replace"))
    result = json.loads(output)
    assert isinstance(result, dict)
    return result


def _container_download_summary(container, path: str, *, inspect_zip: bool) -> dict[str, object]:
    script = """
import hashlib
import io
import json
import sys
import urllib.request
import zipfile

path, inspect_zip = sys.argv[1:]
response = urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=300)
payload = response.read()
result = {
    "size_bytes": len(payload),
    "sha256": hashlib.sha256(payload).hexdigest(),
    "content_type": response.headers.get_content_type(),
}
if inspect_zip == "1":
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        result["names"] = sorted(archive.namelist())
        result["version_manifest"] = json.loads(archive.read("version-manifest.json"))
print(json.dumps(result))
"""
    exit_code, output = container.exec_run(
        ["python", "-c", script, path, "1" if inspect_zip else "0"]
    )
    if exit_code != 0:
        raise RuntimeError(output.decode(errors="replace"))
    result = json.loads(output)
    assert isinstance(result, dict)
    return result


def _wait_for_controller(container) -> None:
    deadline = time.monotonic() + 60
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            health = _container_http_json(container, "/api/v1/health")
            if health["status"] == "ok":  # type: ignore[index]
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.5)
    logs = container.logs().decode(errors="replace")
    raise AssertionError(f"Controller did not become ready: {last_error}\n{logs[-12000:]}")


def _wait_for_run(container, run_id: str, *, timeout_seconds: int) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    detail: dict[str, object] | None = None
    while time.monotonic() < deadline:
        payload = _container_http_json(container, f"/api/v1/runs/{run_id}")
        assert isinstance(payload, dict)
        detail = payload
        if detail["status"] in TERMINAL_STATUSES:
            break
        time.sleep(2)
    assert detail is not None
    if detail["status"] != "SUCCEEDED":
        logs = container.logs().decode(errors="replace")
        pytest.fail(
            f"run {run_id} ended as {detail['status']}: "
            f"{json.dumps(detail, indent=2, ensure_ascii=False)}\n{logs[-12000:]}"
        )
    return detail


def _conversion_payload(
    *, profile_id: str, model_version_id: str, calibration_version_id: str
) -> dict[str, object]:
    s600 = profile_id.startswith("s600-")
    return {
        "profile_id": profile_id,
        "model_version_id": model_version_id,
        "calibration_version_id": calibration_version_id,
        "output_prefix": "resnet18_release_s600" if s600 else "resnet18_release_s100",
        "input": {
            "name": "data",
            "target_shape": [1, 3, 224, 224],
            "train_type": "rgb",
            "train_layout": "NCHW",
            "runtime_type": "nv12",
            "normalization": {
                "mean": [123.675, 116.28, 103.53],
                "scale": [0.01712475, 0.017507, 0.01742919],
                "std": [],
            },
        },
        "calibration": {
            "algorithm": "default",
            "recipe": {
                "id": "imagenet-resnet18",
                "resize_short": 256,
                "mean": [0.485, 0.456, 0.406],
                "std": [0.229, 0.224, 0.225],
            },
        },
        "core_num": 2 if s600 else 1,
        "max_l2m_size": "auto" if s600 else 0,
        "compile_mode": "latency",
        "balance_factor": None,
        "optimize_level": "O0" if s600 else "O2",
        "sample_limit": 20,
        "jobs": 4,
        "cache_mode": "enable",
        "verification": {"mode": "basic", "compare_digits": 5},
        "runner_mode": "cpu",
    }


def test_controller_real_resnet18_s100_s600_release_gate(tmp_path: Path) -> None:
    model_path = Path(os.environ["RDKWT_RESNET18_ONNX"]).resolve(strict=True)
    calibration_dir = Path(os.environ["RDKWT_IMAGENET_CALIBRATION_DIR"]).resolve(strict=True)
    assert model_path.is_file()
    model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
    expected_sha256 = os.environ.get("RDKWT_RESNET18_SHA256", EXPECTED_RESNET18_SHA256)
    assert model_sha256 == expected_sha256
    staged_model = tmp_path / "resnet18.onnx"
    shutil.copyfile(model_path, staged_model)
    staged_model.chmod(0o644)
    images = [
        path
        for path in sorted(calibration_dir.iterdir(), key=lambda item: item.name)
        if path.is_file() and path.suffix.lower() in {".bmp", ".jpeg", ".jpg", ".png"}
    ][:20]
    assert len(images) == 20
    calibration_archive = tmp_path / "imagenet-release.zip"
    with zipfile.ZipFile(calibration_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, image in enumerate(images):
            archive.write(image, f"imagenet/{index:04d}-{image.name}")
    calibration_archive.chmod(0o644)

    client = docker.from_env()
    suffix = uuid.uuid4().hex[:12]
    state_name = f"rdkwt-release-state-{suffix}"
    assets_name = f"rdkwt-release-assets-{suffix}"
    runs_name = f"rdkwt-release-runs-{suffix}"
    cache_name = f"rdkwt-release-cache-{suffix}"
    controller_name = f"rdkwt-controller-release-{suffix}"
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
        client.volumes.create(name=cache_name),
    ]
    controller = None
    run_ids: list[str] = []
    try:
        controller = client.containers.run(
            controller_image,
            name=controller_name,
            detach=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            group_add=[os.stat("/var/run/docker.sock").st_gid],
            network_disabled=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=512m"},
            environment={
                "RDKWT_BIND_HOST": "127.0.0.1",
                "RDKWT_PORT": "8080",
                "RDKWT_STATE_DIR": "/state",
                "RDKWT_ASSETS_DIR": "/assets",
                "RDKWT_RUNS_DIR": "/runs",
                "RDKWT_CACHE_DIR": "/cache",
                "RDKWT_PROFILE_DIR": "/app/profiles/targets",
                "RDKWT_ASSETS_VOLUME": assets_name,
                "RDKWT_RUNS_VOLUME": runs_name,
                "RDKWT_CACHE_VOLUME": cache_name,
                "RDKWT_CPU_RUNNER_IMAGE": runner_image,
                "RDKWT_DEFAULT_TIMEOUT_SECONDS": "1200",
                "RDKWT_MAX_UPLOAD_BYTES": str(256 * 1024 * 1024),
                "RDKWT_MIN_FREE_DISK_BYTES": "1",
            },
            volumes={
                "/var/run/docker.sock": {
                    "bind": "/var/run/docker.sock",
                    "mode": "rw",
                },
                state_name: {"bind": "/state", "mode": "rw"},
                assets_name: {"bind": "/assets", "mode": "rw"},
                runs_name: {"bind": "/runs", "mode": "rw"},
                cache_name: {"bind": "/cache", "mode": "rw"},
                str(staged_model): {"bind": "/fixtures/resnet18.onnx", "mode": "ro"},
                str(calibration_archive): {
                    "bind": "/fixtures/imagenet.zip",
                    "mode": "ro",
                },
            },
        )
        _wait_for_controller(controller)
        preflight = _container_http_json(controller, "/api/v1/system/preflight")
        assert preflight["available"] is True  # type: ignore[index]

        project = _container_http_json(
            controller,
            "/api/v1/projects",
            method="POST",
            payload={"name": "M3 release gate", "description": "real ResNet18"},
        )
        assert isinstance(project, dict)
        model = _container_http_file(
            controller,
            f"/api/v1/projects/{project['id']}/models?model_name=ResNet18%20Release",
            container_file="/fixtures/resnet18.onnx",
            filename="resnet18.onnx",
        )
        assert model["asset"]["sha256"] == expected_sha256  # type: ignore[index]
        inspection = _container_http_json(
            controller,
            f"/api/v1/model-versions/{model['id']}/inspect",
            method="POST",
            payload={},
        )
        assert isinstance(inspection, dict)
        inspection_run_id = str(inspection["run_id"])
        run_ids.append(inspection_run_id)
        _wait_for_run(controller, inspection_run_id, timeout_seconds=180)
        inspected_model = _container_http_json(controller, f"/api/v1/model-versions/{model['id']}")
        assert inspected_model["compatibility_status"] == "READY"  # type: ignore[index]
        assert inspected_model["inspection"]["inputs"][0]["shape"] == [  # type: ignore[index]
            "N",
            3,
            224,
            224,
        ]
        assert inspected_model["inspection"]["warnings"][0]["code"] == (  # type: ignore[index]
            "MODEL_DYNAMIC_SHAPE"
        )

        calibration_set = _container_http_json(
            controller,
            f"/api/v1/projects/{project['id']}/calibration-sets",
            method="POST",
            payload={
                "name": "ImageNet release",
                "description": "20 official sample images",
                "source_type": "images",
            },
        )
        assert isinstance(calibration_set, dict)
        calibration_version_id = str(calibration_set["versions"][0]["id"])
        imported = _container_http_file(
            controller,
            f"/api/v1/calibration-versions/{calibration_version_id}/archives",
            container_file="/fixtures/imagenet.zip",
            filename="imagenet.zip",
        )
        assert imported["imported_count"] == 20
        finalized = _container_http_json(
            controller,
            f"/api/v1/calibration-versions/{calibration_version_id}/finalize",
            method="POST",
            payload={},
        )
        assert finalized["status"] == "READY"  # type: ignore[index]
        assert finalized["source_type"] == "images"  # type: ignore[index]
        assert finalized["sample_count"] == 20  # type: ignore[index]

        for profile_id, expected_march, expected_cores in (
            ("s100-oe-3.7.0", "nash-e", 1),
            ("s600-oe-3.7.0", "nash-p", 2),
        ):
            payload = _conversion_payload(
                profile_id=profile_id,
                model_version_id=str(model["id"]),
                calibration_version_id=calibration_version_id,
            )
            preview = _container_http_json(
                controller,
                "/api/v1/conversion-previews",
                method="POST",
                payload=payload,
            )
            assert isinstance(preview, dict)
            assert f"march: {expected_march}" in str(preview["yaml"])
            assert preview["resource_snapshot"]["calibration_source_type"] == "images"  # type: ignore[index]
            submission = _container_http_json(
                controller,
                "/api/v1/conversion-runs",
                method="POST",
                payload=payload,
            )
            assert isinstance(submission, dict)
            run_id = str(submission["run_id"])
            run_ids.append(run_id)
            detail = _wait_for_run(controller, run_id, timeout_seconds=1200)
            assert detail["attempts"][0]["exit_code"] == 0  # type: ignore[index]
            summary = detail["summary"]
            assert summary["static_performance"]["march"] == expected_march  # type: ignore[index]
            assert summary["static_performance"]["core_num"] == expected_cores  # type: ignore[index]
            assert summary["quantization"]["output_cosines"]  # type: ignore[index]
            assert summary["verification"]["enabled"] is True  # type: ignore[index]
            assert summary["verification"]["hbruntime"]["outputs"]  # type: ignore[index]
            assert summary["verification"]["hb_verifier"]["cosines"]  # type: ignore[index]
            hbm = summary["hbm"]  # type: ignore[index]
            assert hbm["size_bytes"] > 1_000_000
            hbm_index = detail["artifacts"].index(hbm)  # type: ignore[union-attr]
            downloaded = _container_download_summary(
                controller,
                f"/api/v1/runs/{run_id}/attempts/1/artifacts/{hbm_index}",
                inspect_zip=False,
            )
            assert downloaded["size_bytes"] == hbm["size_bytes"]
            assert downloaded["sha256"] == hbm["sha256"]
            exported = _container_download_summary(
                controller, f"/api/v1/runs/{run_id}/export", inspect_zip=True
            )
            assert exported["content_type"] == "application/zip"
            assert {
                "metadata.json",
                "version-manifest.json",
                "attempt/request.json",
                "attempt/result.json",
                "attempt/generated.yaml",
                "inputs/model.onnx",
                "inputs/calibration-manifest.json",
            }.issubset(set(exported["names"]))
            assert exported["version_manifest"]["target_profile_id"] == profile_id  # type: ignore[index]

        cached_payload = _conversion_payload(
            profile_id="s100-oe-3.7.0",
            model_version_id=str(model["id"]),
            calibration_version_id=calibration_version_id,
        )
        cached_submission = _container_http_json(
            controller,
            "/api/v1/conversion-runs",
            method="POST",
            payload=cached_payload,
        )
        assert isinstance(cached_submission, dict)
        cached_run_id = str(cached_submission["run_id"])
        run_ids.append(cached_run_id)
        cached_detail = _wait_for_run(controller, cached_run_id, timeout_seconds=1200)
        assert cached_detail["cache"]["hit"] is True  # type: ignore[index]
        assert cached_detail["summary"]["cache"]["warm_before"] is True  # type: ignore[index]
        assert cached_detail["summary"]["cache"]["file_count_after"] > 0  # type: ignore[index]

        for run_id in run_ids:
            assert (
                client.containers.list(
                    all=True,
                    filters={"label": f"io.drobotics.rdkwt.run_id={run_id}"},
                )
                == []
            )
    finally:
        for run_id in run_ids:
            for leftover in client.containers.list(
                all=True,
                filters={"label": f"io.drobotics.rdkwt.run_id={run_id}"},
            ):
                leftover.remove(force=True, v=True)
        if controller is not None:
            controller.remove(force=True, v=True)
        for volume in volumes:
            volume.remove(force=True)
