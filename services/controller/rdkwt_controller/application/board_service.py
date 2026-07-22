from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

from rdkwt_controller.application.run_service import RunService
from rdkwt_controller.infrastructure.board import (
    BoardCancelled,
    BoardGateway,
    BoardGatewayError,
)
from rdkwt_controller.infrastructure.credentials import CredentialStore, CredentialStoreError
from rdkwt_controller.infrastructure.db import BoardRepository
from rdkwt_controller.profiles import ProfileRegistry
from rdkwt_controller.settings import Settings

_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INPUT_SUFFIXES = {".bin", ".jpeg", ".jpg", ".npy", ".png", ".txt"}


class BoardError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 422,
        *,
        observed_fingerprint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.observed_fingerprint = observed_fingerprint


class DeviceService:
    def __init__(
        self,
        *,
        repository: BoardRepository,
        credential_store: CredentialStore,
        gateway: BoardGateway,
    ) -> None:
        self.repository = repository
        self._credentials = credential_store
        self._gateway = gateway

    def list(self) -> list[dict[str, Any]]:
        return self.repository.list_devices()

    def get(self, device_id: str) -> dict[str, Any]:
        try:
            return self.repository.get_device(device_id)
        except KeyError as exc:
            raise BoardError("DEVICE_NOT_FOUND", str(exc), 404) from exc

    def create(
        self,
        *,
        name: str,
        platform: str,
        host: str,
        port: int,
        user: str,
        auth_type: str,
        credential: dict[str, Any],
        host_key_fingerprint: str | None,
    ) -> dict[str, Any]:
        self._validate_fingerprint(host_key_fingerprint)
        self._validate_credential(auth_type, credential)
        reference = self._credentials.put(credential)
        try:
            return self.repository.create_device(
                name=name,
                platform=platform,
                host=host,
                port=port,
                user=user,
                auth_type=auth_type,
                credential_ref=reference,
                host_key_fingerprint=host_key_fingerprint,
            )
        except Exception:
            self._credentials.delete(reference)
            raise

    def update(
        self,
        device_id: str,
        *,
        changes: dict[str, Any],
        credential: dict[str, Any] | None,
    ) -> dict[str, Any]:
        current = self.get(device_id)
        if "host_key_fingerprint" in changes:
            self._validate_fingerprint(changes["host_key_fingerprint"])
        effective_auth = changes.get("auth_type", current["auth_type"])
        if effective_auth != current["auth_type"] and credential is None:
            raise BoardError(
                "DEVICE_CREDENTIAL_REQUIRED",
                "changing authentication type requires a replacement credential",
            )
        if credential is not None:
            self._validate_credential(effective_auth, credential)
            reference = self.repository.credential_ref(device_id)
            previous_credential = self._credentials.get(reference)
            self._credentials.put(credential, reference=reference)
            changes["credential_ref"] = reference
        else:
            previous_credential = None
        try:
            return self.repository.update_device(device_id, **changes)
        except KeyError as exc:
            if credential is not None and previous_credential is not None:
                self._credentials.put(previous_credential, reference=reference)
            raise BoardError("DEVICE_NOT_FOUND", str(exc), 404) from exc
        except ValueError as exc:
            if credential is not None and previous_credential is not None:
                self._credentials.put(previous_credential, reference=reference)
            raise BoardError("DEVICE_BUSY", str(exc), 409) from exc
        except Exception:
            if credential is not None and previous_credential is not None:
                self._credentials.put(previous_credential, reference=reference)
            raise

    def delete(self, device_id: str) -> dict[str, Any]:
        try:
            reference = self.repository.delete_device(device_id)
        except KeyError as exc:
            raise BoardError("DEVICE_NOT_FOUND", str(exc), 404) from exc
        except ValueError as exc:
            raise BoardError("DEVICE_BUSY", str(exc), 409) from exc
        self._credentials.delete(reference)
        return {"id": device_id, "deleted": True}

    def probe(self, device_id: str) -> dict[str, Any]:
        device, credential = self._probe_inputs(device_id)
        try:
            result = self._gateway.probe(device, credential)
        except (BoardGatewayError, CredentialStoreError) as exc:
            self._record_probe_failure(device_id, exc)
        return self._record_probe_success(device_id, device, result)

    async def probe_async(self, device_id: str) -> dict[str, Any]:
        # Keep SQLite and Secret Store access on the request thread. Only the blocking
        # SSH/SFTP exchange crosses into the executor, so SQLite pooled connections are
        # never used from a different thread.
        device, credential = self._probe_inputs(device_id)
        try:
            result = await self._probe_gateway_async(device, credential)
        except (BoardGatewayError, CredentialStoreError) as exc:
            self._record_probe_failure(device_id, exc)
        return self._record_probe_success(device_id, device, result)

    async def _probe_gateway_async(
        self, device: dict[str, Any], credential: dict[str, Any]
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[dict[str, Any]] = loop.create_future()

        def succeed(result: dict[str, Any]) -> None:
            if not completion.done():
                completion.set_result(result)

        def fail(exc: BaseException) -> None:
            if not completion.done():
                completion.set_exception(exc)

        def execute() -> None:
            try:
                result = self._gateway.probe(device, credential)
            except BaseException as exc:
                loop.call_soon_threadsafe(fail, exc)
            else:
                loop.call_soon_threadsafe(succeed, result)

        threading.Thread(
            target=execute,
            name="rdkwt-device-probe",
            daemon=True,
        ).start()
        return await completion

    def _probe_inputs(self, device_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        device = self.get(device_id)
        try:
            credential = self._credentials.get(self.repository.credential_ref(device_id))
        except CredentialStoreError as exc:
            self._record_probe_failure(device_id, exc)
        return device, credential

    def _record_probe_success(
        self, device_id: str, device: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        detected = result.get("detected_platform")
        if detected is None:
            self._record_probe_failure(
                device_id,
                BoardGatewayError(
                    "BOARD_PLATFORM_UNKNOWN", "unable to identify the board platform"
                ),
            )
        if detected != device["platform"]:
            self._record_probe_failure(
                device_id,
                BoardGatewayError(
                    "BOARD_PLATFORM_MISMATCH",
                    f"configured platform {device['platform']} does not match detected {detected}",
                ),
            )
        return self.repository.record_probe_success(
            device_id, result=result, detected_platform=detected
        )

    def _record_probe_failure(
        self, device_id: str, exc: BoardGatewayError | CredentialStoreError
    ) -> None:
        message = str(exc)
        self.repository.record_probe_failure(device_id, error=message)
        if isinstance(exc, BoardGatewayError):
            raise BoardError(
                exc.code,
                message,
                409 if exc.code.startswith("HOST_KEY_") else 422,
                observed_fingerprint=exc.observed_fingerprint,
            ) from exc
        raise BoardError("DEVICE_CREDENTIAL_UNAVAILABLE", message, 422) from exc

    @staticmethod
    def _validate_fingerprint(value: str | None) -> None:
        if value is not None and not _FINGERPRINT.fullmatch(value):
            raise BoardError("HOST_KEY_INVALID", "host key must be an OpenSSH SHA256 fingerprint")

    @staticmethod
    def _validate_credential(auth_type: str, credential: dict[str, Any]) -> None:
        if auth_type == "password":
            if not isinstance(credential.get("password"), str) or not credential["password"]:
                raise BoardError("DEVICE_CREDENTIAL_INVALID", "a non-empty password is required")
            if set(credential) != {"password"}:
                raise BoardError(
                    "DEVICE_CREDENTIAL_INVALID", "password credentials contain invalid fields"
                )
        elif auth_type == "private_key":
            if not isinstance(credential.get("private_key"), str) or not credential["private_key"]:
                raise BoardError("DEVICE_CREDENTIAL_INVALID", "a non-empty private key is required")
            if set(credential) - {"private_key", "passphrase"}:
                raise BoardError(
                    "DEVICE_CREDENTIAL_INVALID", "private-key credentials contain invalid fields"
                )
        else:
            raise BoardError("DEVICE_AUTH_INVALID", "unsupported authentication type")


class BoardService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: BoardRepository,
        credential_store: CredentialStore,
        gateway: BoardGateway,
        run_service: RunService,
        profiles: ProfileRegistry,
    ) -> None:
        self._settings = settings
        self.repository = repository
        self._credentials = credential_store
        self._gateway = gateway
        self._runs = run_service
        self._profiles = profiles

    async def submit(
        self,
        *,
        device_id: str,
        conversion_run_id: str,
        mode: str,
        options: dict[str, Any],
        input_filename: str | None = None,
        input_chunks: AsyncIterator[bytes] | None = None,
        content_length: int | None = None,
    ) -> dict[str, Any]:
        try:
            device = self.repository.get_device(device_id)
        except KeyError as exc:
            raise BoardError("DEVICE_NOT_FOUND", str(exc), 404) from exc
        if device["status"] != "READY" or device["detected_platform"] != device["platform"]:
            raise BoardError(
                "DEVICE_NOT_READY", "probe the device successfully before submitting a task", 409
            )
        hbm_path, hbm_metadata, conversion = self._resolve_hbm(conversion_run_id)
        try:
            conversion_platform = self._profiles.get(conversion["profile_id"]).platform
        except KeyError as exc:
            raise BoardError("BOARD_PROFILE_UNKNOWN", str(exc), 422) from exc
        if conversion_platform != device["platform"]:
            raise BoardError(
                "BOARD_PLATFORM_MISMATCH",
                f"HBM target {conversion_platform} does not match device {device['platform']}",
            )
        if hbm_metadata["size_bytes"] > self._settings.board_max_upload_bytes:
            raise BoardError(
                "BOARD_HBM_TOO_LARGE", "HBM exceeds the configured board upload limit", 413
            )
        self._validate_options(mode, device["platform"], options)
        run_id = str(uuid.uuid4())
        local_dir = self._settings.board_runs_dir / run_id
        local_dir.mkdir(parents=True, exist_ok=False)
        try:
            if mode == "infer":
                if input_filename is None or input_chunks is None:
                    raise BoardError("BOARD_INPUT_REQUIRED", "infer mode requires an input file")
                safe_name = self._safe_filename(input_filename)
                if (
                    content_length is not None
                    and content_length > self._settings.board_max_upload_bytes
                ):
                    raise BoardError(
                        "BOARD_INPUT_TOO_LARGE", "input exceeds the board upload limit", 413
                    )
                input_dir = local_dir / "input"
                input_dir.mkdir()
                input_metadata = await self._write_input(
                    input_dir / safe_name, input_chunks, self._settings.board_max_upload_bytes
                )
                options = {
                    **options,
                    "input_filename": safe_name,
                    "input_size_bytes": input_metadata["size_bytes"],
                    "input_sha256": input_metadata["sha256"],
                }
            remote_dir = f"/tmp/rdkwt/{run_id}"
            snapshot = {
                key: device[key]
                for key in (
                    "id",
                    "name",
                    "platform",
                    "host",
                    "port",
                    "user",
                    "auth_type",
                    "host_key_fingerprint",
                    "detected_platform",
                )
            }
            probe = device.get("probe") or {}
            snapshot["runtime"] = {
                "hrt_model_exec_version": probe.get("hrt_model_exec_version"),
                "os_release": probe.get("os_release"),
                "board_model": probe.get("board_model"),
                "uname": probe.get("uname"),
            }
            return self.repository.create_board_run(
                device_id=device_id,
                conversion_run_id=conversion_run_id,
                mode=mode,
                device_snapshot=snapshot,
                options=options,
                local_dir=str(local_dir),
                remote_dir=remote_dir,
                hbm_sha256=hbm_metadata["sha256"],
                hbm_size_bytes=hbm_metadata["size_bytes"],
            )
        except Exception:
            self._remove_local_tree(local_dir)
            raise

    def execute(self, run_id: str) -> None:
        if not self.repository.claim_board_run(run_id):
            return
        execution = self.repository.board_execution(run_id)
        local_dir = self._settings.board_runs_dir / run_id
        try:
            if Path(execution.local_dir) != local_dir:
                raise BoardError("BOARD_LOCAL_PATH_INVALID", "board run path is invalid", 409)
            if execution.device_id is None or execution.conversion_run_id is None:
                raise BoardError("BOARD_RUN_REFERENCE_MISSING", "board run references were deleted")
            device = execution.device_snapshot
            self._validate_execution_options(execution.mode, device["platform"], execution.options)
            if execution.mode == "infer":
                self._verify_input(local_dir, execution.options)
            credential = self._credentials.get(self.repository.credential_ref(execution.device_id))
            hbm_path, metadata, _conversion = self._resolve_hbm(execution.conversion_run_id)
            if metadata["sha256"] != self.repository.get_board_run(run_id)["hbm_sha256"]:
                raise BoardError(
                    "BOARD_HBM_CHANGED", "HBM hash changed after board task submission", 409
                )
            result = self._gateway.execute(
                run_id=run_id,
                device=device,
                credential=credential,
                local_dir=local_dir,
                remote_dir=execution.remote_dir,
                hbm_path=hbm_path,
                mode=execution.mode,
                options=execution.options,
                on_phase=lambda phase: self.repository.set_board_phase(run_id, phase),
                keep_remote=self._settings.board_keep_remote,
            )
            if self.repository.cancel_requested(run_id):
                raise BoardCancelled()
            log_path = local_dir / "board.log"
            if log_path.is_file():
                result["artifacts"].insert(0, self._artifact(log_path, local_dir))
            result.update(
                {
                    "schema_version": "1",
                    "mode": execution.mode,
                    "hbm": {
                        "sha256": metadata["sha256"],
                        "size_bytes": metadata["size_bytes"],
                    },
                }
            )
            self._atomic_json(local_dir / "result.json", result)
            self.repository.finish_board_run(run_id, status="SUCCEEDED", result_payload=result)
        except BoardCancelled:
            self.repository.finish_board_run(run_id, status="CANCELLED", result_payload=None)
        except BoardGatewayError as exc:
            if self.repository.cancel_requested(run_id):
                self.repository.finish_board_run(run_id, status="CANCELLED", result_payload=None)
            else:
                self.repository.finish_board_run(
                    run_id,
                    status="FAILED",
                    result_payload=None,
                    error_code=exc.code,
                    error_message=str(exc),
                )
        except (BoardError, CredentialStoreError, KeyError, OSError, ValueError) as exc:
            if self.repository.cancel_requested(run_id):
                self.repository.finish_board_run(run_id, status="CANCELLED", result_payload=None)
            else:
                code = exc.code if isinstance(exc, BoardError) else "BOARD_RUN_FAILED"
                self.repository.finish_board_run(
                    run_id,
                    status="FAILED",
                    result_payload=None,
                    error_code=code,
                    error_message=str(exc),
                )
        except Exception:
            if self.repository.cancel_requested(run_id):
                self.repository.finish_board_run(run_id, status="CANCELLED", result_payload=None)
            else:
                self.repository.finish_board_run(
                    run_id,
                    status="FAILED",
                    result_payload=None,
                    error_code="BOARD_RUN_FAILED",
                    error_message="unexpected board transport failure; inspect the Controller log",
                )

    def cancel(self, run_id: str) -> dict[str, Any]:
        try:
            result = self.repository.request_cancel(run_id)
        except KeyError as exc:
            raise BoardError("BOARD_RUN_NOT_FOUND", str(exc), 404) from exc
        except ValueError as exc:
            raise BoardError("BOARD_RUN_TERMINAL", str(exc), 409) from exc
        if result["status"] == "CANCELLING":
            self._gateway.cancel(run_id)
        return result

    def list(self, *, device_id: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_board_runs(device_id=device_id)

    def get(self, run_id: str) -> dict[str, Any]:
        try:
            return self.repository.get_board_run(run_id)
        except KeyError as exc:
            raise BoardError("BOARD_RUN_NOT_FOUND", str(exc), 404) from exc

    def artifact_file(self, run_id: str, artifact_index: int) -> tuple[Path, dict[str, Any]]:
        detail = self.get(run_id)
        if detail["status"] != "SUCCEEDED" or not isinstance(detail.get("result"), dict):
            raise BoardError(
                "BOARD_ARTIFACT_UNAVAILABLE", "board run has no completed artifacts", 409
            )
        artifacts = detail["result"].get("artifacts", [])
        if artifact_index < 0 or artifact_index >= len(artifacts):
            raise BoardError("BOARD_ARTIFACT_NOT_FOUND", "unknown board artifact index", 404)
        metadata = artifacts[artifact_index]
        base = self._settings.board_runs_dir.resolve(strict=True)
        run_root = base / run_id
        if run_root.is_symlink():
            raise BoardError("BOARD_ARTIFACT_INVALID", "artifact root is invalid", 409)
        root = run_root.resolve(strict=True)
        if base not in root.parents:
            raise BoardError("BOARD_ARTIFACT_INVALID", "artifact root is invalid", 409)
        relative = PurePosixPath(metadata["relative_path"])
        if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
            raise BoardError("BOARD_ARTIFACT_INVALID", "artifact path is invalid", 409)
        candidate = root / relative.as_posix()
        if candidate.is_symlink():
            raise BoardError("BOARD_ARTIFACT_INVALID", "artifact is not a regular file", 409)
        path = candidate.resolve(strict=True)
        if root not in path.parents or not path.is_file():
            raise BoardError("BOARD_ARTIFACT_INVALID", "artifact is not a regular file", 409)
        if (
            path.stat().st_size != metadata["size_bytes"]
            or self._sha256(path) != metadata["sha256"]
        ):
            raise BoardError("BOARD_ARTIFACT_CHANGED", "artifact no longer matches its record", 409)
        return path, metadata

    def log_file(self, run_id: str) -> Path:
        self.get(run_id)
        base = self._settings.board_runs_dir.resolve(strict=True)
        run_root = base / run_id
        if run_root.is_symlink():
            raise BoardError("BOARD_LOG_NOT_FOUND", "board log is not available", 404)
        root = run_root.resolve(strict=True)
        candidate = root / "board.log"
        if base not in root.parents or candidate.is_symlink():
            raise BoardError("BOARD_LOG_NOT_FOUND", "board log is not available", 404)
        path = candidate.resolve(strict=False)
        if root not in path.parents or not path.is_file():
            raise BoardError("BOARD_LOG_NOT_FOUND", "board log is not available", 404)
        return path

    def _resolve_hbm(self, conversion_run_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        try:
            detail = self._runs.result_detail(conversion_run_id)
        except KeyError as exc:
            raise BoardError("CONVERSION_RUN_NOT_FOUND", str(exc), 404) from exc
        if detail["kind"] != "CONVERSION" or detail["status"] != "SUCCEEDED":
            raise BoardError(
                "CONVERSION_RUN_NOT_READY", "a successful conversion run is required", 409
            )
        hbm_index = next(
            (index for index, item in enumerate(detail["artifacts"]) if item.get("kind") == "hbm"),
            None,
        )
        if hbm_index is None:
            raise BoardError("CONVERSION_HBM_MISSING", "conversion run has no HBM artifact", 409)
        attempt = int(detail["attempts"][-1]["number"])
        try:
            path, metadata = self._runs.artifact_file(conversion_run_id, attempt, hbm_index)
        except (KeyError, ValueError) as exc:
            raise BoardError("CONVERSION_HBM_INVALID", str(exc), 409) from exc
        return path, metadata, detail

    @staticmethod
    def _validate_options(mode: str, platform: str, options: dict[str, Any]) -> None:
        if mode not in {"model_info", "infer", "perf"}:
            raise BoardError("BOARD_MODE_INVALID", "unsupported board task mode")
        if mode == "model_info":
            if options:
                raise BoardError("BOARD_OPTIONS_INVALID", "model_info does not accept options")
            return
        allowed_keys = (
            {"core_id"}
            if mode == "infer"
            else {"core_id", "thread_num", "frame_count", "perf_time_minutes"}
        )
        if set(options) - allowed_keys:
            raise BoardError("BOARD_OPTIONS_INVALID", "board task contains unsupported options")
        allowed_cores = {0, 1} if platform == "s100" else {0, 1, 2}
        core_id = options.get("core_id")
        if type(core_id) is not int or core_id not in allowed_cores:
            raise BoardError("BOARD_CORE_INVALID", f"core_id is invalid for {platform}")
        if mode == "perf":
            if type(options.get("thread_num")) is not int or not 1 <= options["thread_num"] <= 32:
                raise BoardError("BOARD_THREAD_INVALID", "thread_num must be between 1 and 32")
            frame_count = options.get("frame_count")
            perf_time = options.get("perf_time_minutes")
            if (frame_count is None) == (perf_time is None):
                raise BoardError(
                    "BOARD_PERF_DURATION_INVALID",
                    "choose exactly one of frame_count or perf_time_minutes",
                )
            if frame_count is not None and (
                type(frame_count) is not int or not 1 <= frame_count <= 1_000_000
            ):
                raise BoardError("BOARD_FRAME_COUNT_INVALID", "frame_count is out of range")
            if perf_time is not None and (
                type(perf_time) is not int or not 1 <= perf_time <= 1_440
            ):
                raise BoardError("BOARD_PERF_TIME_INVALID", "perf_time_minutes is out of range")

    @classmethod
    def _validate_execution_options(cls, mode: str, platform: str, options: dict[str, Any]) -> None:
        if mode != "infer":
            cls._validate_options(mode, platform, options)
            return
        expected = {"core_id", "input_filename", "input_size_bytes", "input_sha256"}
        if set(options) != expected:
            raise BoardError("BOARD_OPTIONS_INVALID", "infer task metadata is incomplete")
        if type(options["input_size_bytes"]) is not int or options["input_size_bytes"] <= 0:
            raise BoardError("BOARD_OPTIONS_INVALID", "infer input size is invalid")
        if not isinstance(options["input_sha256"], str) or not _SHA256.fullmatch(
            options["input_sha256"]
        ):
            raise BoardError("BOARD_OPTIONS_INVALID", "infer input hash is invalid")
        cls._safe_filename(options["input_filename"])
        cls._validate_options(mode, platform, {"core_id": options["core_id"]})

    @classmethod
    def _verify_input(cls, local_dir: Path, options: dict[str, Any]) -> None:
        input_root = local_dir / "input"
        try:
            run_root = local_dir.resolve(strict=True)
            if local_dir.is_symlink() or input_root.is_symlink():
                raise BoardError("BOARD_INPUT_INVALID", "infer input path is invalid", 409)
            root = input_root.resolve(strict=True)
            candidate = root / options["input_filename"]
            if candidate.is_symlink():
                raise BoardError("BOARD_INPUT_INVALID", "infer input path is invalid", 409)
            path = candidate.resolve(strict=True)
        except OSError as exc:
            raise BoardError("BOARD_INPUT_MISSING", "infer input file is missing", 409) from exc
        if run_root not in root.parents or root not in path.parents or not path.is_file():
            raise BoardError("BOARD_INPUT_INVALID", "infer input path is invalid", 409)
        if (
            path.stat().st_size != options["input_size_bytes"]
            or cls._sha256(path) != options["input_sha256"]
        ):
            raise BoardError(
                "BOARD_INPUT_CHANGED", "infer input changed after task submission", 409
            )

    async def _write_input(
        self, path: Path, chunks: AsyncIterator[bytes], limit: int
    ) -> dict[str, Any]:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        total = 0
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as handle:
                async for chunk in chunks:
                    total += len(chunk)
                    if total > limit:
                        raise BoardError(
                            "BOARD_INPUT_TOO_LARGE", "input exceeds the board upload limit", 413
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if total == 0:
                raise BoardError("BOARD_INPUT_EMPTY", "input file is empty")
            os.replace(temporary, path)
            return {"size_bytes": total, "sha256": digest.hexdigest()}
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _safe_filename(value: str) -> str:
        if (
            not isinstance(value, str)
            or PurePath(value).name != value
            or not _FILENAME.fullmatch(value)
        ):
            raise BoardError("BOARD_INPUT_FILENAME_INVALID", "input filename is invalid")
        if PurePath(value).suffix.lower() not in _INPUT_SUFFIXES:
            raise BoardError(
                "BOARD_INPUT_FORMAT_INVALID",
                "input must use a hrt_model_exec-supported extension",
            )
        return value

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _artifact(cls, path: Path, root: Path) -> dict[str, Any]:
        return {
            "relative_path": path.relative_to(root).as_posix(),
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": cls._sha256(path),
            "mime_type": "text/plain",
        }

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _remove_local_tree(root: Path) -> None:
        if not root.exists() or root.is_symlink():
            return
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        root.rmdir()
