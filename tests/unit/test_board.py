from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rdkwt_controller.application import BoardError, BoardService
from rdkwt_controller.infrastructure.board import (
    BoardGateway,
    BoardGatewayError,
    PinnedHostKeyPolicy,
    build_hrt_model_exec_args,
    detect_platform,
    parse_infer_metrics,
    parse_model_info,
    parse_perf_metrics,
    ssh_fingerprint,
)
from rdkwt_controller.infrastructure.credentials import CredentialStore
from rdkwt_controller.infrastructure.db import Attempt, ConversionRun
from rdkwt_controller.main import create_app
from test_controller import FakeDockerClient


async def _chunks(payload: bytes):
    yield payload


class FakeProfiles:
    @staticmethod
    def get(profile_id: str) -> SimpleNamespace:
        return SimpleNamespace(platform="s600" if profile_id.startswith("s600") else "s100")


class FakeConversionRuns:
    def __init__(self, root: Path, *, platform: str = "s100") -> None:
        self.path = root / "model.hbm"
        self.path.write_bytes(b"fake-hbm")
        self.platform = platform
        self.run_id = "11111111-1111-1111-1111-111111111111"
        self.metadata = {
            "kind": "hbm",
            "relative_path": "artifacts/model.hbm",
            "name": "model.hbm",
            "size_bytes": self.path.stat().st_size,
            "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
            "mime_type": "application/octet-stream",
        }

    def result_detail(self, run_id: str) -> dict[str, Any]:
        if run_id != self.run_id:
            raise KeyError(run_id)
        return {
            "id": run_id,
            "kind": "CONVERSION",
            "status": "SUCCEEDED",
            "profile_id": f"{self.platform}-oe-3.7.0",
            "attempts": [{"number": 1}],
            "artifacts": [self.metadata],
        }

    def artifact_file(self, run_id: str, attempt: int, index: int):
        assert run_id == self.run_id
        assert attempt == 1
        assert index == 0
        return self.path, self.metadata


class FakeBoardGateway:
    def __init__(self, *, platform: str = "s100") -> None:
        self.platform = platform
        self.cancelled: list[str] = []

    def probe(self, device: dict[str, Any], credential: dict[str, Any]) -> dict[str, Any]:
        assert credential == {"password": "board-secret"}
        return {
            "detected_platform": self.platform,
            "os_release": {"NAME": "RDK Linux"},
            "board_model": self.platform,
            "uname": "Linux rdk",
            "hrt_model_exec_version": "1.0.0",
            "disk": {"path": "/tmp", "free_bytes": 10**9, "total_bytes": 2 * 10**9},
            "ssh": {
                "sftp": True,
                "host_key_fingerprint": device["host_key_fingerprint"],
            },
        }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        for phase in ("UPLOADING", "RUNNING", "COLLECTING", "CLEANING"):
            kwargs["on_phase"](phase)
        local_dir: Path = kwargs["local_dir"]
        mode = kwargs["mode"]
        (local_dir / "board.log").write_text(f"{mode} ok\n")
        metrics = (
            {"models": [{"name": "demo", "inputs": [], "outputs": []}]}
            if mode == "model_info"
            else {"latency_ms": 2.5}
            if mode == "infer"
            else {"latency_avg_ms": 4.1, "latency_min_ms": 3.0, "latency_max_ms": 6.0, "fps": 240.0}
        )
        return {
            "command": f"hrt_model_exec {mode} --model_file=<managed>",
            "exit_code": 0,
            "metrics": metrics,
            "artifacts": [],
        }

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class UntrustedBoardGateway(FakeBoardGateway):
    def probe(self, device: dict[str, Any], credential: dict[str, Any]) -> dict[str, Any]:
        del device, credential
        raise BoardGatewayError(
            "HOST_KEY_UNTRUSTED",
            "host key requires trust",
            observed_fingerprint="SHA256:" + "A" * 43,
        )


class ToolMissingBoardGateway(FakeBoardGateway):
    def probe(self, device: dict[str, Any], credential: dict[str, Any]) -> dict[str, Any]:
        del device, credential
        raise BoardGatewayError(
            "BOARD_TOOL_UNAVAILABLE", "hrt_model_exec --version failed on the device"
        )


def _ready_device(app, *, fingerprint: str = "SHA256:" + "A" * 43) -> dict[str, Any]:
    service = app.state.services.device_service
    device = service.create(
        name="RDK S100",
        platform="s100",
        host="192.0.2.10",
        port=22,
        user="root",
        auth_type="password",
        credential={"password": "board-secret"},
        host_key_fingerprint=fingerprint,
    )
    return service.probe(device["id"])


def _configure_fake_conversion(app, root: Path) -> FakeConversionRuns:
    conversion = FakeConversionRuns(root)
    app.state.services.repository.create(
        run=ConversionRun(
            id=conversion.run_id,
            kind="CONVERSION",
            profile_id="s100-oe-3.7.0",
            profile_sha256="a" * 64,
            status="SUCCEEDED",
            request_snapshot={},
        ),
        attempt=Attempt(
            run_id=conversion.run_id,
            number=1,
            status="SUCCEEDED",
            stage="SUCCEEDED",
        ),
    )
    app.state.services.board_service._runs = conversion
    app.state.services.board_service._profiles = FakeProfiles()
    return conversion


def test_credential_store_encrypts_payload_and_uses_private_permissions(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    store = CredentialStore(root)
    reference = store.put({"password": "not-in-plaintext"})

    assert store.get(reference) == {"password": "not-in-plaintext"}
    assert b"not-in-plaintext" not in (root / f"{reference}.secret").read_bytes()
    assert (root.stat().st_mode & 0o777) == 0o700
    assert ((root / "master.key").stat().st_mode & 0o777) == 0o600
    assert ((root / f"{reference}.secret").stat().st_mode & 0o777) == 0o600


def test_board_output_parsers_extract_runtime_metrics() -> None:
    model_info = parse_model_info(
        "[model name]: demo\ninput[0]:\nname: data\nvalid shape: 1x3x224x224\n"
        "output[0]:\nname: logits\n"
    )
    infer = parse_infer_metrics("Infer     time: 3.25 ms\nInfer time: 2.75 ms")
    perf = parse_perf_metrics(
        "Frame count: 10, Thread     Average: 4.2 ms, thread max latency: 6.0 ms, "
        "thread min latency: 3.0 ms, FPS: 238.1\n"
        "Average        latency        is: 4.1 ms\n"
        "Frame          rate           is: 240.0 FPS"
    )

    assert model_info["models"][0]["inputs"][0]["name"] == "data"
    assert infer == {
        "inference_count": 2,
        "latency_ms": 2.75,
        "latency_avg_ms": 3.0,
        "latency_min_ms": 2.75,
        "latency_max_ms": 3.25,
    }
    assert perf == {
        "latency_avg_ms": 4.1,
        "latency_min_ms": 3.0,
        "latency_max_ms": 6.0,
        "fps": 240.0,
    }
    assert detect_platform("D-Robotics RDK S100 J6E") == "s100"
    assert detect_platform("Nash-P J6P") == "s600"


def test_host_key_policy_requires_explicit_pin_and_rejects_changes() -> None:
    key = SimpleNamespace(asbytes=lambda: b"host-public-key")
    observed = ssh_fingerprint(key)

    with pytest.raises(BoardGatewayError) as untrusted:
        PinnedHostKeyPolicy(None).missing_host_key(None, "board", key)
    assert untrusted.value.code == "HOST_KEY_UNTRUSTED"
    assert untrusted.value.observed_fingerprint == observed

    with pytest.raises(BoardGatewayError) as changed:
        PinnedHostKeyPolicy("SHA256:" + "A" * 43).missing_host_key(None, "board", key)
    assert changed.value.code == "HOST_KEY_MISMATCH"
    assert changed.value.observed_fingerprint == observed

    PinnedHostKeyPolicy(observed).missing_host_key(None, "board", key)


def test_board_core_options_follow_platform_capabilities() -> None:
    with pytest.raises(BoardError) as invalid:
        BoardService._validate_options("infer", "s100", {"core_id": 2})
    assert invalid.value.code == "BOARD_CORE_INVALID"

    BoardService._validate_options("infer", "s100", {"core_id": 0})
    BoardService._validate_options("infer", "s100", {"core_id": 1})
    BoardService._validate_options("infer", "s600", {"core_id": 2})


def test_board_commands_follow_openexplorer_3_7_runtime_contract() -> None:
    remote = "/tmp/rdkwt/11111111-1111-1111-1111-111111111111"
    infer = build_hrt_model_exec_args(
        mode="infer",
        remote_dir=remote,
        options={"core_id": 1, "input_filename": "input.npy"},
    )
    perf = build_hrt_model_exec_args(
        mode="perf",
        remote_dir=remote,
        options={"core_id": 2, "thread_num": 32, "perf_time_minutes": 3},
    )

    assert infer == [
        "hrt_model_exec",
        "infer",
        f"--model_file={remote}/model.hbm",
        f"--input_file={remote}/input/input.npy",
        "--core_id=1",
        "--enable_dump=true",
        "--dump_format=npy",
        f"--dump_path={remote}/output",
    ]
    assert perf[-4:] == [
        "--core_id=2",
        "--thread_num=32",
        f"--profile_path={remote}/profile",
        "--perf_time=3",
    ]
    assert all("perf_time_in_seconds" not in argument for argument in perf)


def test_unexpected_ssh_channel_close_is_not_reported_as_user_cancel() -> None:
    channel = SimpleNamespace(
        closed=True,
        exec_command=lambda _command: None,
        recv_ready=lambda: False,
        recv_stderr_ready=lambda: False,
        exit_status_ready=lambda: False,
    )
    transport = SimpleNamespace(
        is_active=lambda: True,
        open_session=lambda timeout: channel,
    )
    client = SimpleNamespace(get_transport=lambda: transport)
    gateway = BoardGateway(connect_timeout=1, command_timeout=1)

    with pytest.raises(BoardGatewayError) as captured:
        gateway._run(client, ["hrt_model_exec", "--version"], run_id=None)

    assert captured.value.code == "BOARD_CONNECTION_LOST"


def test_device_probe_never_serializes_credentials(settings) -> None:
    gateway = FakeBoardGateway()
    app = create_app(settings, docker_client=FakeDockerClient(), board_gateway=gateway)
    device = _ready_device(app)

    encoded = json.dumps(device)
    database = settings.state_dir / "db" / "rdkwt.sqlite3"
    assert "board-secret" not in encoded
    assert b"board-secret" not in database.read_bytes()
    assert device["status"] == "READY"
    assert device["detected_platform"] == "s100"


def test_probe_requires_explicit_host_key_trust(settings) -> None:
    app = create_app(
        settings,
        docker_client=FakeDockerClient(),
        board_gateway=UntrustedBoardGateway(),
    )
    service = app.state.services.device_service
    device = service.create(
        name="untrusted",
        platform="s100",
        host="192.0.2.11",
        port=22,
        user="root",
        auth_type="password",
        credential={"password": "board-secret"},
        host_key_fingerprint=None,
    )

    with pytest.raises(BoardError) as captured:
        service.probe(device["id"])

    assert captured.value.code == "HOST_KEY_UNTRUSTED"
    assert captured.value.observed_fingerprint == "SHA256:" + "A" * 43
    assert service.get(device["id"])["status"] == "ERROR"


def test_probe_rejects_platform_mismatch(settings) -> None:
    app = create_app(
        settings,
        docker_client=FakeDockerClient(),
        board_gateway=FakeBoardGateway(platform="s600"),
    )
    service = app.state.services.device_service
    device = service.create(
        name="wrong platform",
        platform="s100",
        host="192.0.2.12",
        port=22,
        user="root",
        auth_type="password",
        credential={"password": "board-secret"},
        host_key_fingerprint="SHA256:" + "A" * 43,
    )

    with pytest.raises(BoardError, match="does not match") as captured:
        service.probe(device["id"])
    assert captured.value.code == "BOARD_PLATFORM_MISMATCH"


def test_probe_reports_missing_board_runtime(settings) -> None:
    app = create_app(
        settings,
        docker_client=FakeDockerClient(),
        board_gateway=ToolMissingBoardGateway(),
    )
    service = app.state.services.device_service
    device = service.create(
        name="missing runtime",
        platform="s100",
        host="192.0.2.13",
        port=22,
        user="root",
        auth_type="password",
        credential={"password": "board-secret"},
        host_key_fingerprint="SHA256:" + "A" * 43,
    )

    with pytest.raises(BoardError) as captured:
        service.probe(device["id"])
    assert captured.value.code == "BOARD_TOOL_UNAVAILABLE"
    assert service.get(device["id"])["status"] == "ERROR"


def test_model_info_infer_and_perf_are_persisted(settings, tmp_path: Path) -> None:
    gateway = FakeBoardGateway()
    app = create_app(settings, docker_client=FakeDockerClient(), board_gateway=gateway)
    device = _ready_device(app)
    conversion = _configure_fake_conversion(app, tmp_path)
    service = app.state.services.board_service

    model_info = asyncio.run(
        service.submit(
            device_id=device["id"],
            conversion_run_id=conversion.run_id,
            mode="model_info",
            options={},
        )
    )
    service.execute(model_info["id"])

    infer = asyncio.run(
        service.submit(
            device_id=device["id"],
            conversion_run_id=conversion.run_id,
            mode="infer",
            options={"core_id": 0},
            input_filename="input.bin",
            input_chunks=_chunks(b"input-data"),
            content_length=10,
        )
    )
    service.execute(infer["id"])

    perf = asyncio.run(
        service.submit(
            device_id=device["id"],
            conversion_run_id=conversion.run_id,
            mode="perf",
            options={"core_id": 0, "thread_num": 2, "frame_count": 100},
        )
    )
    service.execute(perf["id"])

    details = [service.get(item["id"]) for item in (model_info, infer, perf)]
    assert [item["status"] for item in details] == ["SUCCEEDED"] * 3
    assert details[0]["result"]["metrics"]["models"][0]["name"] == "demo"
    assert details[1]["result"]["metrics"]["latency_ms"] == 2.5
    assert details[1]["options"]["input_sha256"] == hashlib.sha256(b"input-data").hexdigest()
    assert details[2]["result"]["metrics"]["fps"] == 240.0
    assert details[0]["device"]["runtime"]["hrt_model_exec_version"] == "1.0.0"
    assert all(item["result"]["artifacts"][0]["name"] == "board.log" for item in details)
    log_path, log_metadata = service.artifact_file(model_info["id"], 0)
    assert log_path.read_text() == "model_info ok\n"
    assert log_metadata["sha256"] == hashlib.sha256(log_path.read_bytes()).hexdigest()


def test_board_run_api_and_device_api_hide_secrets(settings, tmp_path: Path) -> None:
    gateway = FakeBoardGateway()
    app = create_app(settings, docker_client=FakeDockerClient(), board_gateway=gateway)
    conversion = _configure_fake_conversion(app, tmp_path)

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session = await client.get("/api/v1/session")
            headers = {"X-RDKWT-CSRF": session.json()["csrf_token"]}
            created_response = await client.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "name": "API board",
                    "platform": "s100",
                    "host": "192.0.2.20",
                    "port": 22,
                    "user": "root",
                    "auth_type": "password",
                    "credential": {"password": "board-secret"},
                    "host_key_fingerprint": "SHA256:" + "A" * 43,
                },
            )
            assert created_response.status_code == 201
            created = created_response.json()
            invalid_update = await client.patch(
                f"/api/v1/devices/{created['id']}", headers=headers, json={"host": None}
            )
            assert invalid_update.status_code == 422
            probe = await client.post(f"/api/v1/devices/{created['id']}/probe", headers=headers)
            assert probe.status_code == 200
            submitted_response = await client.post(
                "/api/v1/board-runs",
                headers=headers,
                json={
                    "device_id": created["id"],
                    "conversion_run_id": conversion.run_id,
                    "mode": "model_info",
                },
            )
            assert submitted_response.status_code == 202
            return created, submitted_response.json()

    device, run = asyncio.run(exercise())
    assert "board-secret" not in json.dumps(device)
    assert run["status"] == "QUEUED"
    assert run["device"]["host_key_fingerprint"].startswith("SHA256:")


def test_invalid_device_request_does_not_reflect_credentials(settings) -> None:
    app = create_app(
        settings,
        docker_client=FakeDockerClient(),
        board_gateway=FakeBoardGateway(),
    )

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session = await client.get("/api/v1/session")
            return await client.post(
                "/api/v1/devices",
                headers={"X-RDKWT-CSRF": session.json()["csrf_token"]},
                json={
                    "name": "invalid",
                    "platform": "s100",
                    "host": "192.0.2.30",
                    "user": "root",
                    "auth_type": "password",
                    "credential": {
                        "password": "must-not-be-reflected",
                        "unexpected": "must-not-be-reflected-either",
                    },
                },
            )

    response = asyncio.run(exercise())
    assert response.status_code == 422
    assert "must-not-be-reflected" not in response.text
    assert response.json()["code"] == "DEVICE_REQUEST_INVALID"


def test_board_cancel_and_restart_states_are_persisted(settings, tmp_path: Path) -> None:
    gateway = FakeBoardGateway()
    app = create_app(settings, docker_client=FakeDockerClient(), board_gateway=gateway)
    device = _ready_device(app)
    conversion = _configure_fake_conversion(app, tmp_path)
    service = app.state.services.board_service
    repository = service.repository

    queued = asyncio.run(
        service.submit(
            device_id=device["id"],
            conversion_run_id=conversion.run_id,
            mode="model_info",
            options={},
        )
    )
    assert service.cancel(queued["id"])["status"] == "CANCELLED"
    service.execute(queued["id"])
    assert service.get(queued["id"])["status"] == "CANCELLED"

    active = asyncio.run(
        service.submit(
            device_id=device["id"],
            conversion_run_id=conversion.run_id,
            mode="perf",
            options={"core_id": 0, "thread_num": 1, "frame_count": 10},
        )
    )
    assert repository.claim_board_run(active["id"])
    assert service.cancel(active["id"])["status"] == "CANCELLING"
    assert gateway.cancelled == [active["id"]]
    repository.interrupt_active()
    interrupted = service.get(active["id"])
    assert interrupted["status"] == "INTERRUPTED"
    assert interrupted["error"]["code"] == "BOARD_RUN_INTERRUPTED"


def test_infer_rejects_input_changed_while_queued(settings, tmp_path: Path) -> None:
    app = create_app(
        settings,
        docker_client=FakeDockerClient(),
        board_gateway=FakeBoardGateway(),
    )
    device = _ready_device(app)
    conversion = _configure_fake_conversion(app, tmp_path)
    service = app.state.services.board_service
    run = asyncio.run(
        service.submit(
            device_id=device["id"],
            conversion_run_id=conversion.run_id,
            mode="infer",
            options={"core_id": 1},
            input_filename="input.npy",
            input_chunks=_chunks(b"original-input"),
            content_length=14,
        )
    )
    input_path = settings.board_runs_dir / run["id"] / "input" / "input.npy"
    input_path.write_bytes(b"changed-input")

    service.execute(run["id"])

    detail = service.get(run["id"])
    assert detail["status"] == "FAILED"
    assert detail["error"]["code"] == "BOARD_INPUT_CHANGED"


def test_device_deletion_removes_encrypted_credential(settings) -> None:
    app = create_app(
        settings,
        docker_client=FakeDockerClient(),
        board_gateway=FakeBoardGateway(),
    )
    device = _ready_device(app)
    secret_files = list(settings.secrets_dir.glob("*.secret"))
    assert len(secret_files) == 1

    result = app.state.services.device_service.delete(device["id"])

    assert result["deleted"] is True
    assert not secret_files[0].exists()
    assert os.listdir(settings.secrets_dir) == ["master.key"]
