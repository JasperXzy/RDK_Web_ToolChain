from __future__ import annotations

import importlib.metadata
import subprocess
from pathlib import Path
from typing import Any

from .filesystem import sha256_file


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def inspect_asset(model_path: Path) -> dict[str, Any]:
    if not model_path.is_file():
        raise FileNotFoundError(f"model asset is not a regular file: {model_path.name}")
    return {
        "size_bytes": model_path.stat().st_size,
        "sha256": sha256_file(model_path),
    }


def check_toolchain(timeout_seconds: int) -> dict[str, Any]:
    completed = subprocess.run(
        ["hb_compile", "--help"],
        capture_output=True,
        check=False,
        text=True,
        timeout=min(timeout_seconds, 60),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"hb_compile --help exited with {completed.returncode}")
    return {
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    }


def toolchain_versions() -> dict[str, str]:
    return {
        "hmct": _distribution_version("hmct"),
        "hbdk4_compiler": _distribution_version("hbdk4_compiler"),
    }
