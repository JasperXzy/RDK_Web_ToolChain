from __future__ import annotations

import copy
import uuid

import pytest
from jsonschema import Draft202012Validator
from rdkwt_contracts import ContractValidationError, load_schema, validate_payload


def request_payload() -> dict[str, object]:
    run_id = str(uuid.uuid4())
    return {
        "contract_version": "1.0",
        "run_id": run_id,
        "attempt": 1,
        "adapter": "contract-probe-1.0",
        "runner_mode": "cpu",
        "pipeline": ["inspect", "check", "collect"],
        "paths": {
            "model": "blobs/sha256/ab/model.onnx",
            "calibration_source": None,
            "attempt_root": f"{run_id}/attempts/1",
        },
        "configuration": {},
        "limits": {"timeout_seconds": 300, "max_log_bytes": 1_048_576},
    }


@pytest.mark.parametrize(
    "schema_name",
    ["request", "event", "result", "artifact-manifest"],
)
def test_contract_schema_is_valid(schema_name: str) -> None:
    Draft202012Validator.check_schema(load_schema(schema_name))


def test_request_accepts_explicit_relative_paths() -> None:
    validate_payload("request", request_payload())


@pytest.mark.parametrize(
    "invalid_path",
    ["/etc/passwd", "../model.onnx", "models/../../etc/passwd", "models\\model.onnx"],
)
def test_request_rejects_unsafe_paths(invalid_path: str) -> None:
    payload = request_payload()
    payload["paths"]["model"] = invalid_path  # type: ignore[index]
    with pytest.raises(ContractValidationError):
        validate_payload("request", payload)


def test_request_rejects_uncontracted_fields() -> None:
    payload = copy.deepcopy(request_payload())
    payload["image"] = "attacker/image:latest"
    with pytest.raises(ContractValidationError):
        validate_payload("request", payload)
