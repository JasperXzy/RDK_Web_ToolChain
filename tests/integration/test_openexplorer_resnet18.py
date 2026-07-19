from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import uuid
from pathlib import Path

import docker
import pytest
from rdkwt_contracts import validate_payload
from rdkwt_controller.profiles import ProfileRegistry

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RDKWT_RUN_OE_GOLDEN_TESTS") != "1",
        reason="set RDKWT_RUN_OE_GOLDEN_TESTS=1 with local ResNet18 assets",
    ),
]


def _put_files(container, destination: str, files: list[tuple[str, bytes]]) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, content in files:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    assert container.put_archive(destination, archive.getvalue())


def _configuration(profile_id: str, output_prefix: str) -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    profile = ProfileRegistry.load(root / "profiles" / "targets").get(profile_id)
    s600 = profile.platform == "s600"
    return {
        "schema_version": "1",
        "target_profile": profile.snapshot(),
        "output_prefix": output_prefix,
        "inputs": [
            {
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
            }
        ],
        "calibration": {
            "source_type": "images",
            "algorithm": "default",
            "sample_limit": 20,
            "recipe": {
                "id": "imagenet-resnet18",
                "version": "1",
                "resize_short": 256,
                "crop_size": [224, 224],
                "mean": [0.485, 0.456, 0.406],
                "std": [0.229, 0.224, 0.225],
            },
        },
        "compiler": {
            "compile_mode": "latency",
            "balance_factor": None,
            "core_num": 2 if s600 else 1,
            "optimize_level": "O0",
            "max_l2m_size": "auto" if s600 else 0,
            "max_time_per_fc": 0,
            "jobs": 4,
            "cache_mode": "disable",
            "cache_key": None,
        },
        "verification": {"mode": "basic", "compare_digits": 5},
    }


@pytest.mark.parametrize(
    ("profile_id", "expected_march", "expected_cores"),
    [
        ("s100-oe-3.7.0", "nash-e", 1),
        ("s600-oe-3.7.0", "nash-p", 2),
    ],
)
def test_resnet18_real_calibration_compile_golden(
    profile_id: str, expected_march: str, expected_cores: int
) -> None:
    model_path = Path(os.environ["RDKWT_RESNET18_ONNX"])
    calibration_dir = Path(os.environ["RDKWT_IMAGENET_CALIBRATION_DIR"])
    assert model_path.is_file()
    images = [
        path
        for path in sorted(calibration_dir.iterdir(), key=lambda item: item.name)
        if path.is_file() and path.suffix.lower() in {".bmp", ".jpeg", ".jpg", ".png"}
    ][:20]
    assert len(images) == 20
    expected_sha256 = os.environ.get("RDKWT_RESNET18_SHA256")
    if expected_sha256 is not None:
        assert hashlib.sha256(model_path.read_bytes()).hexdigest() == expected_sha256

    client = docker.from_env()
    suffix = uuid.uuid4().hex[:12]
    assets_name = f"rdkwt-oe-golden-assets-{suffix}"
    runs_name = f"rdkwt-oe-golden-runs-{suffix}"
    image = os.environ.get(
        "RDKWT_CPU_RUNNER_IMAGE", "rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1"
    )
    volumes = [client.volumes.create(name=assets_name), client.volumes.create(name=runs_name)]
    loader = None
    runner = None
    try:
        model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
        model_asset_directory = f"/assets/blobs/sha256/{model_sha256[:2]}"
        model_logical_path = f"blobs/sha256/{model_sha256[:2]}/{model_sha256}.onnx"
        calibration_version = str(uuid.uuid4())
        calibration_asset_directory = f"/assets/calibration-sets/{calibration_version}/source"
        calibration_logical_path = f"calibration-sets/{calibration_version}/source"
        loader = client.containers.run(
            image,
            command=["-c", "import time; time.sleep(900)"],
            entrypoint="python3",
            detach=True,
            network_disabled=True,
            volumes={
                assets_name: {"bind": "/assets", "mode": "rw"},
                runs_name: {"bind": "/runs", "mode": "rw"},
            },
        )
        exit_code, output = loader.exec_run(
            [
                "python3",
                "-c",
                "import pathlib;"
                f"pathlib.Path('{model_asset_directory}').mkdir(parents=True);"
                f"pathlib.Path('{calibration_asset_directory}').mkdir(parents=True);",
            ]
        )
        assert exit_code == 0, output.decode(errors="replace")
        _put_files(
            loader,
            model_asset_directory,
            [(f"{model_sha256}.onnx", model_path.read_bytes())],
        )
        _put_files(
            loader,
            calibration_asset_directory,
            [(path.name, path.read_bytes()) for path in images],
        )

        run_id = str(uuid.uuid4())
        attempt_root = f"{run_id}/attempts/1"
        output_prefix = f"resnet18_{expected_march.replace('-', '_')}"
        request = {
            "contract_version": "1.0",
            "run_id": run_id,
            "attempt": 1,
            "adapter": "openexplorer-3.7.0",
            "runner_mode": "cpu",
            "pipeline": [
                "inspect",
                "check",
                "preprocess",
                "compile",
                "verify",
                "collect",
            ],
            "paths": {
                "model": model_logical_path,
                "calibration_source": calibration_logical_path,
                "attempt_root": attempt_root,
            },
            "configuration": _configuration(profile_id, output_prefix),
            "limits": {"timeout_seconds": 600, "max_log_bytes": 50 * 1024 * 1024},
        }
        validate_payload("request", request)
        request_directory = f"/runs/{attempt_root}"
        exit_code, output = loader.exec_run(
            [
                "python3",
                "-c",
                f"import pathlib; pathlib.Path('{request_directory}').mkdir(parents=True)",
            ]
        )
        assert exit_code == 0, output.decode(errors="replace")
        _put_files(
            loader,
            request_directory,
            [("request.json", json.dumps(request, separators=(",", ":")).encode())],
        )

        runner = client.containers.create(
            image,
            command=["--request", f"{request_directory}/request.json"],
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=512,
            mem_limit="8g",
            nano_cpus=4_000_000_000,
            init=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=512m"},
            volumes={
                assets_name: {"bind": "/assets", "mode": "ro"},
                runs_name: {"bind": "/runs", "mode": "rw"},
            },
        )
        runner.start()
        response = runner.wait(timeout=650)
        logs = runner.logs().decode(errors="replace")
        assert response["StatusCode"] == 0, logs[-8000:]

        read_script = (
            "import hashlib,json,pathlib;"
            f"root=pathlib.Path('/runs/{attempt_root}');"
            "result=json.loads((root/'result.json').read_text());"
            "manifest=json.loads((root/'artifact-manifest.json').read_text());"
            "valid=all((root/a['relative_path']).is_file() and not "
            "(root/a['relative_path']).is_symlink() and "
            "(root/a['relative_path']).stat().st_size==a['size_bytes'] and "
            "hashlib.sha256((root/a['relative_path']).read_bytes()).hexdigest()==a['sha256'] "
            "for a in manifest['artifacts']);"
            "print(json.dumps({'result':result,'manifest':manifest,'valid':valid}))"
        )
        exit_code, output = loader.exec_run(["python3", "-c", read_script])
        assert exit_code == 0, output.decode(errors="replace")
        payload = json.loads(output)
        result = payload["result"]
        manifest = payload["manifest"]
        validate_payload("result", result)
        validate_payload("artifact-manifest", manifest)
        assert payload["valid"] is True
        assert result["status"] == "succeeded"
        assert result["metrics"]["compile"]["static_performance"]["march"] == expected_march
        assert result["metrics"]["compile"]["static_performance"]["core_num"] == expected_cores
        assert any(item["kind"] == "hbm" and item["required"] for item in manifest["artifacts"])
    finally:
        if runner is not None:
            runner.remove(force=True, v=True)
        if loader is not None:
            loader.remove(force=True, v=True)
        for volume in volumes:
            volume.remove(force=True)
