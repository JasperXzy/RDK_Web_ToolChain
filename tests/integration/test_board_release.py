from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from rdkwt_controller.infrastructure.board import BoardGateway


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} is required for the board release gate")
    return value


@pytest.mark.release
def test_real_board_model_info_perf_and_optional_infer(tmp_path: Path) -> None:
    if os.environ.get("RDKWT_RUN_BOARD_RELEASE_TESTS") != "1":
        pytest.skip("set RDKWT_RUN_BOARD_RELEASE_TESTS=1 to exercise a real RDK board")

    platform = _required("RDKWT_BOARD_PLATFORM")
    if platform not in {"s100", "s600"}:
        pytest.fail("RDKWT_BOARD_PLATFORM must be s100 or s600")
    auth_type = os.environ.get("RDKWT_BOARD_AUTH_TYPE", "password").strip()
    if auth_type == "private_key":
        key_path = Path(_required("RDKWT_BOARD_PRIVATE_KEY_FILE"))
        credential: dict[str, Any] = {"private_key": key_path.read_text(encoding="utf-8")}
        if passphrase := os.environ.get("RDKWT_BOARD_PRIVATE_KEY_PASSPHRASE"):
            credential["passphrase"] = passphrase
    elif auth_type == "password":
        credential = {"password": _required("RDKWT_BOARD_PASSWORD")}
    else:
        pytest.fail("RDKWT_BOARD_AUTH_TYPE must be password or private_key")

    hbm = Path(_required("RDKWT_BOARD_HBM")).resolve(strict=True)
    if not hbm.is_file() or hbm.is_symlink():
        pytest.fail("RDKWT_BOARD_HBM must be a regular file")
    device = {
        "host": _required("RDKWT_BOARD_HOST"),
        "port": int(os.environ.get("RDKWT_BOARD_PORT", "22")),
        "user": _required("RDKWT_BOARD_USER"),
        "auth_type": auth_type,
        "host_key_fingerprint": _required("RDKWT_BOARD_HOST_KEY"),
    }
    gateway = BoardGateway(
        connect_timeout=10,
        command_timeout=1_800,
        max_output_bytes=100 * 1024 * 1024,
        max_download_bytes=2 * 1024 * 1024 * 1024,
    )

    probe = gateway.probe(device, credential)
    assert probe["detected_platform"] == platform
    assert probe["ssh"]["sftp"] is True
    assert probe["hrt_model_exec_version"]

    cases: list[tuple[str, dict[str, Any]]] = [
        ("model_info", {}),
        ("perf", {"core_id": 1, "thread_num": 1, "frame_count": 50}),
    ]
    if input_value := os.environ.get("RDKWT_BOARD_INPUT"):
        input_path = Path(input_value).resolve(strict=True)
        infer_root = tmp_path / "infer"
        (infer_root / "input").mkdir(parents=True)
        shutil.copyfile(input_path, infer_root / "input" / "input.bin")
        cases.append(("infer", {"core_id": 1, "input_filename": "input.bin"}))

    for mode, options in cases:
        local_dir = tmp_path / mode
        local_dir.mkdir(exist_ok=True)
        if mode == "infer":
            source = tmp_path / "infer" / "input" / "input.bin"
            target = local_dir / "input" / "input.bin"
            if source != target:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        phases: list[str] = []
        run_id = str(uuid.uuid4())
        result = gateway.execute(
            run_id=run_id,
            device=device,
            credential=credential,
            local_dir=local_dir,
            remote_dir=f"/tmp/rdkwt/{run_id}",
            hbm_path=hbm,
            mode=mode,
            options=options,
            on_phase=phases.append,
            keep_remote=False,
        )
        assert result["exit_code"] == 0
        assert (local_dir / "board.log").is_file()
        assert {"UPLOADING", "RUNNING", "COLLECTING", "CLEANING"}.issubset(phases)
        if mode == "model_info":
            assert result["metrics"]["models"]
        elif mode == "perf":
            assert result["metrics"].get("latency_avg_ms") is not None
            assert result["metrics"].get("fps") is not None
            assert result["artifacts"]
        else:
            assert result["metrics"].get("latency_ms") is not None
            assert result["artifacts"]
