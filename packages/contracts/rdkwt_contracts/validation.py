from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


class ContractValidationError(ValueError):
    """Raised when a Runner contract payload does not match its schema."""


@cache
def load_schema(name: str) -> dict[str, Any]:
    schema_file = files("rdkwt_contracts.schemas").joinpath(f"{name}.schema.json")
    if not schema_file.is_file():
        raise KeyError(f"unknown contract schema: {name}")
    return json.loads(schema_file.read_text(encoding="utf-8"))


def validate_payload(name: str, payload: Any) -> None:
    validator = Draft202012Validator(load_schema(name), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
    if not errors:
        return

    details = []
    for error in errors[:10]:
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        details.append(f"{path}: {error.message}")
    raise ContractValidationError("; ".join(details))
