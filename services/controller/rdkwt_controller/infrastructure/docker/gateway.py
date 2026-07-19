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
from docker.types import DeviceRequest

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
        for name in (
            self._settings.assets_volume,
            self._settings.runs_volume,
            self._settings.cache_volume,
        ):
            if not VOLUME_NAME.fullmatch(name):
                raise ValueError(f"invalid configured Docker volume name: {name!r}")
        if (
            len(
                {
                    self._settings.assets_volume,
                    self._settings.runs_volume,
                    self._settings.cache_volume,
                }
            )
            != 3
        ):
            raise ValueError("assets, runs, and cache must use different Docker volumes")

    def ping(self) -> bool:
        return bool(self._docker().ping())

    def resolve_cpu_image(self) -> ResolvedRunnerImage:
        return self.resolve_runner_image("cpu")

    def resolve_runner_image(self, runner_mode: str) -> ResolvedRunnerImage:
        if runner_mode == "cpu":
            reference = self._settings.cpu_runner_image
        elif runner_mode == "gpu":
            if not self._settings.gpu_enabled:
                raise ManagedContainerError(
                    "GPU Runner is disabled; CPU conversions remain available"
                )
            if not self._settings.gpu_runner_image:
                raise ManagedContainerError("GPU Runner image is not configured")
            reference = self._settings.gpu_runner_image
        else:
            raise ValueError("runner_mode must be cpu or gpu")
        image = self._docker().images.get(reference)
        immutable_id = str(image.id)
        if not immutable_id.startswith("sha256:"):
            raise ManagedContainerError("Docker did not return an immutable image ID")
        return ResolvedRunnerImage(
            logical_id=f"openexplorer-3.7.0-{runner_mode}",
            configured_reference=reference,
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
            "gpu": self.gpu_capability(info=info),
        }

    def gpu_capability(self, *, info: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._settings.gpu_enabled:
            return {
                "enabled": False,
                "available": False,
                "status": "DISABLED",
                "message": (
                    "GPU Runner is disabled by local settings; CPU conversion is unaffected"
                ),
                "image": None,
                "device_ids": [],
            }
        try:
            docker_info = self._docker().info() if info is None else info
            runtimes = docker_info.get("Runtimes") or {}
            runtime_names = sorted(str(name) for name in runtimes)
            if "nvidia" not in runtimes:
                return {
                    "enabled": True,
                    "available": False,
                    "status": "UNAVAILABLE",
                    "message": "NVIDIA Container Runtime is not registered with Docker",
                    "runtimes": runtime_names,
                    "image": self._settings.gpu_runner_image,
                    "device_ids": list(self._settings.gpu_device_ids),
                }
            image = self.resolve_runner_image("gpu")
            return {
                "enabled": True,
                "available": True,
                "status": "READY",
                "message": (
                    "GPU runtime and fixed Runner image are available; toolchain/GPU "
                    "compatibility is verified again by the task"
                ),
                "runtimes": runtime_names,
                "image": {
                    "reference": image.configured_reference,
                    "immutable_id": image.immutable_id,
                },
                "device_ids": list(self._settings.gpu_device_ids),
            }
        except Exception as exc:
            return {
                "enabled": True,
                "available": False,
                "status": "UNAVAILABLE",
                "message": str(exc),
                "image": self._settings.gpu_runner_image,
                "device_ids": list(self._settings.gpu_device_ids),
            }

    def container_create_kwargs(
        self,
        *,
        run_id: str,
        attempt: int,
        image: ResolvedRunnerImage,
        runner_mode: str = "cpu",
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
        if runner_mode not in {"cpu", "gpu"}:
            raise ValueError("runner_mode must be cpu or gpu")
        options: dict[str, Any] = {
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
                self._settings.cache_volume: {"bind": "/cache", "mode": "rw"},
            },
            "labels": labels,
        }
        if runner_mode == "gpu":
            device_ids = list(self._settings.gpu_device_ids)
            device_options: dict[str, Any] = {
                "driver": "nvidia",
                "capabilities": [["gpu"]],
            }
            if device_ids:
                device_options["device_ids"] = device_ids
            else:
                device_options["count"] = -1
            options["device_requests"] = [DeviceRequest(**device_options)]
            options["shm_size"] = self._settings.gpu_shm_size
        return options

    def create_attempt(self, *, run_id: str, attempt: int, runner_mode: str = "cpu") -> Container:
        image = self.resolve_runner_image(runner_mode)
        options = self.container_create_kwargs(
            run_id=run_id,
            attempt=attempt,
            image=image,
            runner_mode=runner_mode,
        )
        return self._docker().containers.create(**options)

    def create_frozen_attempt(
        self,
        *,
        run_id: str,
        attempt: int,
        image_reference: str | None,
        image_id: str | None,
        runner_mode: str = "cpu",
    ) -> Container:
        if image_reference is None or image_id is None:
            raise ManagedContainerError("run does not contain a frozen Runner image")
        if not image_id.startswith("sha256:"):
            raise ManagedContainerError("frozen Runner image ID is not immutable")
        image_object = self._docker().images.get(image_id)
        if str(image_object.id) != image_id:
            raise ManagedContainerError("resolved Runner image does not match the frozen ID")
        image = ResolvedRunnerImage(
            logical_id=f"openexplorer-3.7.0-{runner_mode}",
            configured_reference=image_reference,
            immutable_id=image_id,
            repo_digests=tuple(image_object.attrs.get("RepoDigests") or ()),
        )
        if runner_mode == "gpu" and not self.gpu_capability().get("available"):
            raise ManagedContainerError("GPU capability is unavailable on this host")
        options = self.container_create_kwargs(
            run_id=run_id,
            attempt=attempt,
            image=image,
            runner_mode=runner_mode,
        )
        return self._docker().containers.create(**options)

    def recover_attempt(self, container_id: str, *, run_id: str, attempt: int) -> Container:
        return self._verified_container(container_id, run_id=run_id, attempt=attempt)

    @staticmethod
    def start(container: Container) -> None:
        container.start()

    @staticmethod
    def logs(container: Container) -> Iterator[bytes]:
        yield from container.logs(stream=True, follow=True, stdout=True, stderr=True)

    @staticmethod
    def logs_demux(
        container: Container,
    ) -> Iterator[tuple[bytes | None, bytes | None]]:
        yield from container.attach(
            stream=True,
            logs=True,
            stdout=True,
            stderr=True,
            demux=True,
        )

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

    @staticmethod
    def managed_identity(container: Container) -> tuple[str, int] | None:
        container.reload()
        labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
        if labels.get(MANAGED_LABEL) != "true":
            return None
        try:
            return str(uuid.UUID(labels[RUN_ID_LABEL])), int(labels[ATTEMPT_LABEL])
        except (KeyError, TypeError, ValueError):
            return None

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
