from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from rdkwt_contracts import validate_payload
from rdkwt_runner.filesystem import UnsafePathError, resolve_within
from rdkwt_runner.main import execute_request, run_request_file


def make_request(run_id: str, asset_path: str = "models/probe.bin") -> dict[str, object]:
    return {
        "contract_version": "1.0",
        "run_id": run_id,
        "attempt": 1,
        "adapter": "contract-probe-1.0",
        "runner_mode": "cpu",
        "pipeline": ["inspect", "check", "collect"],
        "paths": {
            "model": asset_path,
            "calibration_source": None,
            "attempt_root": f"{run_id}/attempts/1",
        },
        "configuration": {},
        "limits": {"timeout_seconds": 30, "max_log_bytes": 1_048_576},
    }


def test_runner_emits_valid_result_manifest_and_monotonic_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tmp_path / "assets"
    runs = tmp_path / "runs"
    model = assets / "models" / "probe.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"contract-probe")
    runs.mkdir()
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(
        "rdkwt_runner.main.check_toolchain",
        lambda _timeout: {"exit_code": 0, "stdout_tail": "usage", "stderr_tail": ""},
    )

    result = execute_request(make_request(run_id), assets, runs)
    attempt_root = runs / run_id / "attempts" / "1"
    manifest = json.loads((attempt_root / "artifact-manifest.json").read_text())
    events = [json.loads(line) for line in (attempt_root / "events.jsonl").read_text().splitlines()]

    validate_payload("result", result)
    validate_payload("artifact-manifest", manifest)
    for event in events:
        validate_payload("event", event)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert result["status"] == "succeeded"
    assert (attempt_root / "artifacts" / "probe.json").is_file()


def test_runner_writes_failed_result_when_a_step_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tmp_path / "assets"
    runs = tmp_path / "runs"
    model = assets / "models" / "probe.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"contract-probe")
    runs.mkdir()
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(
        "rdkwt_runner.main.check_toolchain",
        lambda _timeout: (_ for _ in ()).throw(RuntimeError("toolchain unavailable")),
    )

    with pytest.raises(RuntimeError, match="toolchain unavailable"):
        execute_request(make_request(run_id), assets, runs)

    result = json.loads((runs / run_id / "attempts" / "1" / "result.json").read_text())
    manifest = json.loads((runs / run_id / "attempts" / "1" / "artifact-manifest.json").read_text())
    validate_payload("result", result)
    validate_payload("artifact-manifest", manifest)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "RUNNER_FAILED"
    assert manifest["artifacts"] == []


def test_runner_rejects_attempt_root_with_another_identity(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    runs = tmp_path / "runs"
    model = assets / "models" / "probe.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"contract-probe")
    runs.mkdir()
    run_id = str(uuid.uuid4())
    request = make_request(run_id)
    request["paths"]["attempt_root"] = f"{uuid.uuid4()}/attempts/1"  # type: ignore[index]

    with pytest.raises(ValueError, match="does not match"):
        execute_request(request, assets, runs)


def test_runner_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "outside").symlink_to(tmp_path)
    with pytest.raises(UnsafePathError, match="symbolic links"):
        resolve_within(root, "outside/secret", must_exist=False)


def test_runner_rejects_symlink_request_file(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    runs = tmp_path / "runs"
    assets.mkdir()
    runs.mkdir()
    actual_request = runs / "actual.json"
    actual_request.write_text("{}")
    request_link = runs / "request.json"
    request_link.symlink_to(actual_request)

    with pytest.raises(ValueError, match="escapes runs root"):
        run_request_file(request_link, assets, runs)
