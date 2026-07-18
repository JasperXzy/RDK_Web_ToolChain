from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from rdkwt_controller import __version__
from rdkwt_controller.settings import Settings

import docker
from docker.models.containers import Container

MANAGED_LABEL = "io.drobotics.rdkwt.managed"
RUN_ID_LABEL = "io.drobotics.rdkwt.run_id"
ATTEMPT_LABEL = "io.drobotics.rdkwt.attempt"
CONTRACT_LABEL = "io.drobotics.rdkwt.contract_version"
APP_VERSION_LABEL = "io.drobotics.rdkwt.app_version"
VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ManagedContainerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedRunnerImage:
    logical_id: str
    configured_reference: str
    immutable_id: str
    repo_digests: tuple[str, ...]
    contract_version: str = "1.0"


class DockerGateway:
    def __init__(self, client: Any, settings: Settings) -> None:
        self._client = client
        self._client_lock = threading.Lock()
        self._settings = settings
        self._validate_deployment_boundary()

    @classmethod
    def from_env(cls, settings: Settings) -> DockerGateway:
        return cls(None, settings)

    def _docker(self) -> Any:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = docker.from_env()
        return self._client

    def _validate_deployment_boundary(self) -> None:
        for name in (self._settings.assets_volume, self._settings.runs_volume):
            if not VOLUME_NAME.fullmatch(name):
                raise ValueError(f"invalid configured Docker volume name: {name!r}")
        if self._settings.assets_volume == self._settings.runs_volume:
            raise ValueError("assets and runs must use different Docker volumes")

    def ping(self) -> bool:
        return bool(self._docker().ping())

    def resolve_cpu_image(self) -> ResolvedRunnerImage:
        image = self._docker().images.get(self._settings.cpu_runner_image)
        immutable_id = str(image.id)
        if not immutable_id.startswith("sha256:"):
            raise ManagedContainerError("Docker did not return an immutable image ID")
        return ResolvedRunnerImage(
            logical_id="openexplorer-3.7.0-cpu",
            configured_reference=self._settings.cpu_runner_image,
            immutable_id=immutable_id,
            repo_digests=tuple(image.attrs.get("RepoDigests") or ()),
        )

    def preflight(self) -> dict[str, Any]:
        self.ping()
        client = self._docker()
        version = client.version()
        info = client.info()
        image = self.resolve_cpu_image()
        return {
            "docker": {
                "version": version.get("Version"),
                "api_version": version.get("ApiVersion"),
                "os": version.get("Os"),
                "architecture": version.get("Arch"),
            },
            "engine": {
                "operating_system": info.get("OperatingSystem"),
                "docker_root_dir": info.get("DockerRootDir"),
            },
            "runner_image": {
                "logical_id": image.logical_id,
                "configured_reference": image.configured_reference,
                "immutable_id": image.immutable_id,
                "repo_digests": image.repo_digests,
                "contract_version": image.contract_version,
            },
        }

    def container_create_kwargs(
        self,
        *,
        run_id: str,
        attempt: int,
        image: ResolvedRunnerImage,
    ) -> dict[str, Any]:
        parsed_run_id = str(uuid.UUID(run_id))
        if attempt < 1:
            raise ValueError("attempt must be a positive integer")
        request_path = f"/runs/{parsed_run_id}/attempts/{attempt}/request.json"
        labels = {
            MANAGED_LABEL: "true",
            APP_VERSION_LABEL: __version__,
            RUN_ID_LABEL: parsed_run_id,
            ATTEMPT_LABEL: str(attempt),
            CONTRACT_LABEL: image.contract_version,
        }
        return {
            "image": image.immutable_id,
            "name": f"rdkwt-run-{parsed_run_id.replace('-', '')[:12]}-a{attempt}",
            "command": ["--request", request_path],
            "detach": True,
            "stdin_open": False,
            "tty": False,
            "auto_remove": False,
            "network_disabled": True,
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "pids_limit": self._settings.runner_pids_limit,
            "mem_limit": self._settings.runner_memory,
            "nano_cpus": self._settings.runner_nano_cpus,
            "init": True,
            "tmpfs": {"/tmp": "rw,noexec,nosuid,size=512m"},
            "volumes": {
                self._settings.assets_volume: {"bind": "/assets", "mode": "ro"},
                self._settings.runs_volume: {"bind": "/runs", "mode": "rw"},
            },
            "labels": labels,
        }

    def create_attempt(self, *, run_id: str, attempt: int) -> Container:
        image = self.resolve_cpu_image()
        options = self.container_create_kwargs(run_id=run_id, attempt=attempt, image=image)
        return self._docker().containers.create(**options)

    @staticmethod
    def start(container: Container) -> None:
        container.start()

    @staticmethod
    def logs(container: Container) -> Iterator[bytes]:
        yield from container.logs(stream=True, follow=True, stdout=True, stderr=True)

    @staticmethod
    def wait(container: Container) -> int:
        response = container.wait()
        return int(response["StatusCode"])

    def stop_managed(self, container_id: str, *, run_id: str, attempt: int) -> None:
        container = self._verified_container(container_id, run_id=run_id, attempt=attempt)
        container.stop(timeout=self._settings.stop_timeout_seconds)

    def remove_managed(self, container_id: str, *, run_id: str, attempt: int) -> None:
        container = self._verified_container(container_id, run_id=run_id, attempt=attempt)
        container.remove(force=False, v=True)

    def managed_containers(self) -> list[Container]:
        return self._docker().containers.list(
            all=True,
            filters={"label": f"{MANAGED_LABEL}=true"},
        )

    def _verified_container(self, container_id: str, *, run_id: str, attempt: int) -> Container:
        container = self._docker().containers.get(container_id)
        container.reload()
        labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
        expected = {
            MANAGED_LABEL: "true",
            RUN_ID_LABEL: str(uuid.UUID(run_id)),
            ATTEMPT_LABEL: str(attempt),
        }
        mismatches = {
            key: (labels.get(key), value)
            for key, value in expected.items()
            if labels.get(key) != value
        }
        if mismatches or container.id != container_id:
            raise ManagedContainerError(
                f"refusing to manage container with mismatched identity: {mismatches}"
            )
        return container
