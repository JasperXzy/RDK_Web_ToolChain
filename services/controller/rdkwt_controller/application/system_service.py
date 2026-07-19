from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from rdkwt_controller.infrastructure.db import RunRepository
from rdkwt_controller.infrastructure.docker import DockerGateway
from rdkwt_controller.settings import Settings


class SystemService:
    def __init__(
        self,
        *,
        settings: Settings,
        docker_gateway: DockerGateway,
        repository: RunRepository,
    ) -> None:
        self._settings = settings
        self._docker = docker_gateway
        self._repository = repository

    def preflight(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        details: dict[str, Any] = {}
        try:
            docker_details = self._docker.preflight()
            details.update(docker_details)
            docker_info = docker_details["docker"]
            compatible = (
                docker_info.get("os") == "linux" and docker_info.get("architecture") == "amd64"
            )
            checks.append(
                {
                    "id": "docker-engine",
                    "status": "PASS" if compatible else "BLOCKED",
                    "message": (
                        "Docker Engine is reachable on linux/amd64"
                        if compatible
                        else "Docker Engine must report linux/amd64"
                    ),
                }
            )
            gpu = docker_details.get("gpu") or {}
            checks.append(
                {
                    "id": "gpu-runner",
                    "status": "PASS" if gpu.get("available") else "SKIPPED",
                    "message": str(gpu.get("message") or "GPU status is unavailable"),
                }
            )
            checks.append(
                {
                    "id": "runner-image",
                    "status": "PASS",
                    "message": (
                        f"Runner image {docker_details['runner_image']['immutable_id']} is present"
                    ),
                }
            )
        except Exception as exc:
            checks.extend(
                [
                    {
                        "id": "docker-engine",
                        "status": "BLOCKED",
                        "message": str(exc),
                    },
                    {
                        "id": "runner-image",
                        "status": "BLOCKED",
                        "message": "Runner image cannot be verified until Docker is available",
                    },
                    {
                        "id": "gpu-runner",
                        "status": "SKIPPED",
                        "message": "GPU is optional and does not block CPU conversion",
                    },
                ]
            )

        storage: dict[str, Any] = {}
        for name, path in {
            "state": self._settings.state_dir,
            "assets": self._settings.assets_dir,
            "runs": self._settings.runs_dir,
            "cache": self._settings.effective_cache_dir,
        }.items():
            storage[name] = self._check_storage(name, path, checks, required=name != "cache")
        details["storage"] = storage
        details["minimum_free_bytes"] = self._settings.min_free_bytes
        details["runner_smoke_test"] = self._runner_smoke_test()
        available = all(item["status"] != "BLOCKED" for item in checks)
        return {"available": available, "checks": checks, "details": details}

    def ensure_runner_probe_asset(self) -> str:
        logical_path = Path("system") / "runner-preflight.bin"
        path = self._settings.assets_dir / logical_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"RDK WebToolChain controlled runner preflight\n"
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise RuntimeError("the controlled Runner preflight asset is invalid")
            return logical_path.as_posix()
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return logical_path.as_posix()

    def _runner_smoke_test(self) -> dict[str, Any]:
        latest = self._repository.latest_kind("PROBE")
        if latest is None:
            return {"status": "NOT_RUN", "run_id": None, "toolchain_versions": {}}
        attempt = latest["attempts"][-1]
        result = attempt.get("result")
        versions = result.get("toolchain_versions", {}) if isinstance(result, dict) else {}
        return {
            "status": latest["status"],
            "run_id": latest["id"],
            "finished_at": attempt.get("finished_at"),
            "toolchain_versions": versions,
        }

    def _check_storage(
        self,
        name: str,
        path: Path,
        checks: list[dict[str, Any]],
        *,
        required: bool = True,
    ) -> dict[str, Any]:
        probe = path / f".rdkwt-preflight-{uuid.uuid4().hex}"
        writable = False
        error = None
        try:
            payload = os.urandom(32)
            with probe.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            writable = probe.read_bytes() == payload
        except OSError as exc:
            error = str(exc)
        finally:
            if probe.exists():
                probe.unlink()
        usage = shutil.disk_usage(path)
        enough_space = usage.free >= self._settings.min_free_bytes
        status = "PASS" if writable and enough_space else "BLOCKED" if required else "WARN"
        message = (
            f"{name} storage is writable with {usage.free} bytes free"
            if status == "PASS"
            else error or f"{name} storage has less than {self._settings.min_free_bytes} bytes free"
        )
        checks.append({"id": f"storage-{name}", "status": status, "message": message})
        return {
            "path": str(path),
            "writable": writable,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
