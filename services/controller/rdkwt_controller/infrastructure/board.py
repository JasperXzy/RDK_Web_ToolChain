from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import shlex
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import paramiko

_REMOTE_ROOT = re.compile(
    r"^/tmp/rdkwt/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_RUNTIME_INPUT_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_HRT_MODEL_EXEC = "/usr/hobot/bin/hrt_model_exec"
_FLOAT = r"([0-9]+(?:\.[0-9]+)?)"
logger = logging.getLogger(__name__)


class BoardGatewayError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        observed_fingerprint: str | None = None,
        command: str | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ):
        super().__init__(message)
        self.code = code
        self.observed_fingerprint = observed_fingerprint
        self.command = command
        self.stdout = stdout
        self.stderr = stderr


class BoardCancelled(BoardGatewayError):
    def __init__(
        self, *, command: str | None = None, stdout: bytes = b"", stderr: bytes = b""
    ) -> None:
        super().__init__(
            "BOARD_RUN_CANCELLED",
            "board task was cancelled",
            command=command,
            stdout=stdout,
            stderr=stderr,
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str


class PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str | None) -> None:
        self.expected = expected

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        del client, hostname
        observed = ssh_fingerprint(key)
        if self.expected is None:
            raise BoardGatewayError(
                "HOST_KEY_UNTRUSTED",
                f"SSH host key is not trusted; explicitly save fingerprint {observed}",
                observed_fingerprint=observed,
            )
        if not _constant_time_equal(self.expected, observed):
            raise BoardGatewayError(
                "HOST_KEY_MISMATCH",
                f"SSH host key changed (expected {self.expected}, observed {observed})",
                observed_fingerprint=observed,
            )


def _constant_time_equal(left: str, right: str) -> bool:
    import secrets

    return secrets.compare_digest(left.encode(), right.encode())


def ssh_fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def detect_platform(*texts: str) -> str | None:
    combined = "\n".join(texts).lower()
    s600 = any(token in combined for token in ("s600", "j6p", "nash-p"))
    s100 = any(token in combined for token in ("s100", "j6e", "j6em", "nash-e", "bayes-e"))
    if s600 and s100:
        return None
    if s600:
        return "s600"
    if s100:
        return "s100"
    return None


def parse_model_info(output: str) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    tensor: dict[str, Any] | None = None
    for raw in output.splitlines():
        line = raw.strip()
        model_match = re.match(r"\[model name\]\s*[:=]?\s*(.+)", line, re.I)
        if model_match:
            current = {"name": model_match.group(1).strip(), "inputs": [], "outputs": []}
            models.append(current)
            tensor = None
            continue
        tensor_match = re.match(r"(input|output)\s*\[?\s*(\d+)\s*\]?\s*:?$", line, re.I)
        if tensor_match and current is not None:
            tensor = {"index": int(tensor_match.group(2))}
            current[tensor_match.group(1).lower() + "s"].append(tensor)
            continue
        field_match = re.match(r"([A-Za-z][A-Za-z _]+?)\s*[:=]\s*(.+)", line)
        if field_match and tensor is not None:
            key = re.sub(r"\s+", "_", field_match.group(1).strip().lower())
            tensor[key] = field_match.group(2).strip()
    return {"models": models}


def parse_infer_metrics(output: str) -> dict[str, Any]:
    values = [
        float(value)
        for value in re.findall(r"Infer\s+time\s*:\s*" + _FLOAT + r"\s*ms", output, re.I)
    ]
    if not values:
        return {}
    return {
        "inference_count": len(values),
        "latency_ms": values[-1],
        "latency_avg_ms": sum(values) / len(values),
        "latency_min_ms": min(values),
        "latency_max_ms": max(values),
    }


def parse_perf_metrics(output: str) -> dict[str, Any]:
    averages = [
        float(value)
        for value in re.findall(
            r"(?:Thread\s+Average|Average\s+latency\s+is)\s*:?\s*" + _FLOAT + r"\s*ms",
            output,
            re.I,
        )
    ]
    maxima = [
        float(value)
        for value in re.findall(r"thread\s+max\s+latency\s*:\s*" + _FLOAT + r"\s*ms", output, re.I)
    ]
    minima = [
        float(value)
        for value in re.findall(r"thread\s+min\s+latency\s*:\s*" + _FLOAT + r"\s*ms", output, re.I)
    ]
    fps_values = [
        float(value)
        for value in re.findall(
            r"(?:FPS|Frame\s+rate\s+is)\s*:?\s*" + _FLOAT + r"\s*(?:FPS)?",
            output,
            re.I,
        )
    ]
    metrics: dict[str, Any] = {}
    if averages:
        metrics["latency_avg_ms"] = averages[-1]
    if minima:
        metrics["latency_min_ms"] = min(minima)
    if maxima:
        metrics["latency_max_ms"] = max(maxima)
    if fps_values:
        metrics["fps"] = fps_values[-1]
    return metrics


def build_hrt_model_exec_args(*, mode: str, remote_dir: str, options: dict[str, Any]) -> list[str]:
    """Build the complete, allowlisted argv defined by the OE 3.7 runtime contract."""
    if not _REMOTE_ROOT.fullmatch(remote_dir):
        raise BoardGatewayError("BOARD_REMOTE_PATH_INVALID", "remote workspace path is invalid")
    args = [_HRT_MODEL_EXEC, mode, f"--model_file={remote_dir}/model.hbm"]
    if mode == "model_info":
        if options:
            raise BoardGatewayError("BOARD_OPTIONS_INVALID", "model_info does not accept options")
        return args
    core_id = options.get("core_id")
    if type(core_id) is not int or core_id not in {0, 1, 2}:
        raise BoardGatewayError("BOARD_CORE_INVALID", "invalid hrt_model_exec core selector")
    if mode == "infer":
        filename = options.get("input_filename")
        if not isinstance(filename, str) or not _RUNTIME_INPUT_FILENAME.fullmatch(filename):
            raise BoardGatewayError("BOARD_INPUT_FILENAME_INVALID", "invalid runtime input name")
        if set(options) - {
            "core_id",
            "input_adapter",
            "input_filename",
            "input_size_bytes",
            "input_sha256",
        }:
            raise BoardGatewayError("BOARD_OPTIONS_INVALID", "infer contains unsupported options")
        input_adapter = options.get("input_adapter", "raw")
        if input_adapter not in {"raw", "nv12_image_y_uv"}:
            raise BoardGatewayError("BOARD_INPUT_ADAPTER_INVALID", "invalid runtime input adapter")
        remote_input = f"{remote_dir}/input/{filename}"
        input_files = (
            f"{remote_input},{remote_input}"
            if input_adapter == "nv12_image_y_uv"
            else remote_input
        )
        image_properties = (
            ["--input_img_properties=Y,UV"] if input_adapter == "nv12_image_y_uv" else []
        )
        return [
            *args,
            f"--input_file={input_files}",
            *image_properties,
            f"--core_id={core_id}",
            "--enable_dump=true",
            "--dump_format=bin",
            f"--dump_path={remote_dir}/output",
        ]
    if mode != "perf":
        raise BoardGatewayError("BOARD_MODE_INVALID", "unsupported board task mode")
    if set(options) - {"core_id", "thread_num", "frame_count", "perf_time_minutes"}:
        raise BoardGatewayError("BOARD_OPTIONS_INVALID", "perf contains unsupported options")
    thread_num = options.get("thread_num")
    frame_count = options.get("frame_count")
    perf_time = options.get("perf_time_minutes")
    if type(thread_num) is not int or not 1 <= thread_num <= 32:
        raise BoardGatewayError("BOARD_THREAD_INVALID", "invalid hrt_model_exec thread count")
    if (frame_count is None) == (perf_time is None):
        raise BoardGatewayError("BOARD_PERF_DURATION_INVALID", "invalid perf stop condition")
    args.extend(
        [
            f"--core_id={core_id}",
            f"--thread_num={thread_num}",
            f"--profile_path={remote_dir}/profile",
        ]
    )
    if perf_time is not None:
        if type(perf_time) is not int or not 1 <= perf_time <= 1_440:
            raise BoardGatewayError("BOARD_PERF_TIME_INVALID", "invalid perf time")
        args.append(f"--perf_time={perf_time}")
    else:
        if type(frame_count) is not int or not 1 <= frame_count <= 1_000_000:
            raise BoardGatewayError("BOARD_FRAME_COUNT_INVALID", "invalid perf frame count")
        args.append(f"--frame_count={frame_count}")
    return args


class BoardGateway:
    def __init__(
        self,
        *,
        connect_timeout: int,
        command_timeout: int,
        max_output_bytes: int = 100 * 1024 * 1024,
        max_download_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._max_output_bytes = max_output_bytes
        self._max_download_bytes = max_download_bytes
        self._active: dict[str, tuple[paramiko.SSHClient, paramiko.Channel | None]] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def probe(self, device: dict[str, Any], credential: dict[str, Any]) -> dict[str, Any]:
        client = self._connect(device, credential)
        try:
            sftp = client.open_sftp()
            try:
                os_release = self._read_remote_text(sftp, "/etc/os-release", 64 * 1024)
                board_model = self._first_remote_text(
                    sftp,
                    (
                        "/proc/device-tree/model",
                        "/sys/devices/soc0/machine",
                        "/sys/devices/soc0/soc_id",
                    ),
                )
                disk = self._remote_disk(client)
            finally:
                sftp.close()
            uname = self._run(client, ["uname", "-a"], run_id=None)
            version = self._run(client, [_HRT_MODEL_EXEC, "--version"], run_id=None)
            if version.exit_code != 0:
                raise BoardGatewayError(
                    "BOARD_TOOL_UNAVAILABLE",
                    f"{_HRT_MODEL_EXEC} --version failed on the device",
                )
            detected = detect_platform(os_release, board_model, uname.stdout)
            if detected is None:
                detected = detect_platform(version.stdout, version.stderr)
            return {
                "detected_platform": detected,
                "os_release": _parse_os_release(os_release),
                "board_model": board_model.strip("\x00\n "),
                "uname": uname.stdout.strip(),
                "hrt_model_exec_version": (version.stdout or version.stderr).strip(),
                "disk": disk,
                "ssh": {"sftp": True, "host_key_fingerprint": device["host_key_fingerprint"]},
            }
        finally:
            client.close()

    def execute(
        self,
        *,
        run_id: str,
        device: dict[str, Any],
        credential: dict[str, Any],
        local_dir: Path,
        remote_dir: str,
        hbm_path: Path,
        mode: str,
        options: dict[str, Any],
        on_phase: Callable[[str], None],
        keep_remote: bool,
    ) -> dict[str, Any]:
        self._validate_remote_root(remote_dir)
        args = build_hrt_model_exec_args(mode=mode, remote_dir=remote_dir, options=options)
        client = self._connect(device, credential)
        with self._lock:
            self._active[run_id] = (client, None)
        result: CommandResult | None = None
        collected: list[dict[str, Any]] = []
        completed = False
        try:
            self._raise_if_cancelled(run_id)
            on_phase("UPLOADING")
            sftp = client.open_sftp()
            try:
                required_bytes = hbm_path.stat().st_size
                if mode == "infer":
                    required_bytes += (
                        (local_dir / "input" / options["input_filename"]).stat().st_size
                    )
                self._remote_disk(client, required_bytes=required_bytes)
                self._mkdirs(sftp, remote_dir)
                remote_hbm = f"{remote_dir}/model.hbm"
                sftp.put(
                    str(hbm_path),
                    remote_hbm,
                    callback=lambda transferred, total: self._upload_progress(
                        run_id, transferred, total
                    ),
                    confirm=True,
                )
                if mode == "infer":
                    local_input = local_dir / "input" / options["input_filename"]
                    remote_input = f"{remote_dir}/input/{options['input_filename']}"
                    self._mkdirs(sftp, f"{remote_dir}/input")
                    sftp.put(
                        str(local_input),
                        remote_input,
                        callback=lambda transferred, total: self._upload_progress(
                            run_id, transferred, total
                        ),
                        confirm=True,
                    )
                    self._mkdirs(sftp, f"{remote_dir}/output")
                elif mode == "perf":
                    self._mkdirs(sftp, f"{remote_dir}/profile")
                on_phase("RUNNING")
                log_path = local_dir / "board.log"
                try:
                    result = self._run(client, args, run_id=run_id)
                except BoardGatewayError as exc:
                    log_path.write_text(
                        exc.stdout.decode("utf-8", errors="replace")
                        + (
                            "\n[stderr]\n" + exc.stderr.decode("utf-8", errors="replace")
                            if exc.stderr
                            else ""
                        ),
                        encoding="utf-8",
                    )
                    raise
                log_path.write_text(
                    result.stdout + ("\n[stderr]\n" + result.stderr if result.stderr else ""),
                    encoding="utf-8",
                )
                if result.exit_code != 0:
                    raise BoardGatewayError(
                        "BOARD_COMMAND_FAILED",
                        f"hrt_model_exec {mode} exited with code {result.exit_code}",
                    )
                on_phase("COLLECTING")
                if mode in {"infer", "perf"}:
                    remote_output = f"{remote_dir}/{'output' if mode == 'infer' else 'profile'}"
                    local_output = local_dir / ("output" if mode == "infer" else "profile")
                    collected = self._download_tree(
                        sftp,
                        remote_output,
                        local_output,
                        artifact_root=local_dir,
                        downloaded=[0],
                    )
            finally:
                sftp.close()
            combined = result.stdout + "\n" + result.stderr
            parsed = (
                parse_model_info(combined)
                if mode == "model_info"
                else parse_infer_metrics(combined)
                if mode == "infer"
                else parse_perf_metrics(combined)
            )
            payload = {
                "command": result.command,
                "exit_code": result.exit_code,
                "metrics": parsed,
                "artifacts": collected,
            }
            completed = True
            return payload
        finally:
            cleanup_error: Exception | None = None
            if not keep_remote:
                try:
                    on_phase("CLEANING")
                    sftp = client.open_sftp()
                    try:
                        self._remove_tree(sftp, remote_dir)
                    finally:
                        sftp.close()
                except Exception as exc:
                    cleanup_error = exc
                    logger.warning("failed to clean remote board workspace %s: %s", remote_dir, exc)
            with self._lock:
                self._active.pop(run_id, None)
                self._cancelled.discard(run_id)
            client.close()
            if completed and cleanup_error is not None:
                raise BoardGatewayError(
                    "BOARD_REMOTE_CLEANUP_FAILED",
                    "board command completed but its remote workspace could not be removed",
                ) from cleanup_error

    def cancel(self, run_id: str) -> None:
        with self._lock:
            active = self._active.get(run_id)
            self._cancelled.add(run_id)
        if active is None:
            return
        client, channel = active
        if channel is not None:
            channel.close()
        client.close()

    def _raise_if_cancelled(self, run_id: str) -> None:
        with self._lock:
            cancelled = run_id in self._cancelled
        if cancelled:
            raise BoardCancelled()

    def _upload_progress(self, run_id: str, transferred: int, total: int) -> None:
        del transferred, total
        self._raise_if_cancelled(run_id)

    def _connect(self, device: dict[str, Any], credential: dict[str, Any]) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(PinnedHostKeyPolicy(device.get("host_key_fingerprint")))
        kwargs: dict[str, Any] = {
            "hostname": device["host"],
            "port": device["port"],
            "username": device["user"],
            "timeout": self._connect_timeout,
            "banner_timeout": self._connect_timeout,
            "auth_timeout": self._connect_timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }
        auth_type = device["auth_type"]
        if auth_type == "password":
            kwargs["password"] = credential.get("password")
        elif auth_type == "private_key":
            kwargs["pkey"] = _load_private_key(
                credential.get("private_key", ""), credential.get("passphrase")
            )
        else:
            raise BoardGatewayError("BOARD_AUTH_INVALID", "unsupported board authentication type")
        try:
            client.connect(**kwargs)
            return client
        except BoardGatewayError:
            client.close()
            raise
        except paramiko.AuthenticationException as exc:
            client.close()
            raise BoardGatewayError("BOARD_AUTH_FAILED", "SSH authentication failed") from exc
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise BoardGatewayError(
                "BOARD_CONNECTION_FAILED", f"SSH connection failed: {exc}"
            ) from exc

    def _run(
        self, client: paramiko.SSHClient, args: list[str], *, run_id: str | None
    ) -> CommandResult:
        command = shlex.join(args)
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise BoardGatewayError("BOARD_CONNECTION_LOST", "SSH transport is not active")
        channel = transport.open_session(timeout=self._connect_timeout)
        if run_id is not None:
            with self._lock:
                self._active[run_id] = (client, channel)
        channel.exec_command(command)
        started = time.monotonic()
        stdout = bytearray()
        stderr = bytearray()
        while True:
            if run_id is not None:
                with self._lock:
                    cancelled = run_id in self._cancelled
                if cancelled:
                    channel.close()
                    raise BoardCancelled(
                        command=command, stdout=bytes(stdout), stderr=bytes(stderr)
                    )
            if channel.recv_ready():
                stdout.extend(channel.recv(65536))
            if channel.recv_stderr_ready():
                stderr.extend(channel.recv_stderr(65536))
            if len(stdout) + len(stderr) > self._max_output_bytes:
                channel.close()
                captured_stdout = bytes(stdout[: self._max_output_bytes])
                remaining = self._max_output_bytes - len(captured_stdout)
                raise BoardGatewayError(
                    "BOARD_OUTPUT_TOO_LARGE",
                    "board command output exceeded its limit",
                    command=command,
                    stdout=captured_stdout,
                    stderr=bytes(stderr[:remaining]),
                )
            if channel.exit_status_ready():
                while channel.recv_ready():
                    stdout.extend(channel.recv(65536))
                    if len(stdout) + len(stderr) > self._max_output_bytes:
                        break
                while channel.recv_stderr_ready():
                    if len(stdout) + len(stderr) > self._max_output_bytes:
                        break
                    stderr.extend(channel.recv_stderr(65536))
                if len(stdout) + len(stderr) > self._max_output_bytes:
                    channel.close()
                    captured_stdout = bytes(stdout[: self._max_output_bytes])
                    remaining = self._max_output_bytes - len(captured_stdout)
                    raise BoardGatewayError(
                        "BOARD_OUTPUT_TOO_LARGE",
                        "board command output exceeded its limit",
                        command=command,
                        stdout=captured_stdout,
                        stderr=bytes(stderr[:remaining]),
                    )
                break
            if channel.closed:
                if run_id is not None:
                    with self._lock:
                        cancelled = run_id in self._cancelled
                    if cancelled:
                        raise BoardCancelled(
                            command=command, stdout=bytes(stdout), stderr=bytes(stderr)
                        )
                raise BoardGatewayError(
                    "BOARD_CONNECTION_LOST",
                    "SSH channel closed before the command completed",
                    command=command,
                    stdout=bytes(stdout),
                    stderr=bytes(stderr),
                )
            if time.monotonic() - started > self._command_timeout:
                channel.close()
                raise BoardGatewayError(
                    "BOARD_COMMAND_TIMEOUT",
                    "board command exceeded its timeout",
                    command=command,
                    stdout=bytes(stdout),
                    stderr=bytes(stderr),
                )
            time.sleep(0.05)
        exit_code = channel.recv_exit_status()
        channel.close()
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _read_remote_text(sftp: paramiko.SFTPClient, path: str, limit: int) -> str:
        try:
            with sftp.open(path, "rb") as handle:
                return handle.read(limit).decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _first_remote_text(self, sftp: paramiko.SFTPClient, paths: tuple[str, ...]) -> str:
        for path in paths:
            value = self._read_remote_text(sftp, path, 16 * 1024)
            if value:
                return value
        return ""

    def _remote_disk(
        self, client: paramiko.SSHClient, *, required_bytes: int = 0
    ) -> dict[str, int | str]:
        result = self._run(client, ["df", "-Pk", "/tmp"], run_id=None)
        try:
            if result.exit_code != 0:
                raise ValueError("df command failed")
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            fields = lines[-1].split()
            if len(fields) < 6:
                raise ValueError("unexpected df output")
            total_kib = int(fields[-5])
            free_kib = int(fields[-3])
            if total_kib < 0 or free_kib < 0:
                raise ValueError("negative df values")
        except (IndexError, ValueError) as exc:
            raise BoardGatewayError(
                "BOARD_DISK_CHECK_FAILED",
                "unable to inspect free space in /tmp",
                command=result.command,
                stdout=result.stdout.encode("utf-8"),
                stderr=result.stderr.encode("utf-8"),
            ) from exc
        total_bytes = total_kib * 1024
        free_bytes = free_kib * 1024
        disk: dict[str, int | str] = {
            "path": "/tmp",
            "total_bytes": total_bytes,
            "free_bytes": free_bytes,
        }
        if free_bytes < required_bytes:
            raise BoardGatewayError(
                "BOARD_REMOTE_DISK_INSUFFICIENT",
                "the board does not have enough free space for task inputs",
            )
        return disk

    @staticmethod
    def _mkdirs(sftp: paramiko.SFTPClient, path: str) -> None:
        current = PurePosixPath("/")
        for part in PurePosixPath(path).parts[1:]:
            current /= part
            try:
                metadata = sftp.lstat(current.as_posix())
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise BoardGatewayError(
                        "BOARD_REMOTE_PATH_INVALID", "remote path is not a directory"
                    )
            except OSError:
                sftp.mkdir(current.as_posix(), mode=0o700)

    def _download_tree(
        self,
        sftp: paramiko.SFTPClient,
        remote: str,
        local: Path,
        *,
        artifact_root: Path,
        downloaded: list[int],
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        try:
            entries = sftp.listdir_attr(remote)
        except OSError:
            return artifacts
        local.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            if entry.filename in {".", ".."} or "/" in entry.filename:
                continue
            remote_child = f"{remote}/{entry.filename}"
            local_child = local / entry.filename
            if stat.S_ISDIR(entry.st_mode):
                artifacts.extend(
                    self._download_tree(
                        sftp,
                        remote_child,
                        local_child,
                        artifact_root=artifact_root,
                        downloaded=downloaded,
                    )
                )
            elif stat.S_ISREG(entry.st_mode):
                self._download_file(sftp, remote_child, local_child, downloaded=downloaded)
                artifacts.append(_local_artifact(local_child, artifact_root))
        return artifacts

    def _download_file(
        self,
        sftp: paramiko.SFTPClient,
        remote: str,
        local: Path,
        *,
        downloaded: list[int],
    ) -> None:
        try:
            with sftp.open(remote, "rb") as source, local.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    downloaded[0] += len(chunk)
                    if downloaded[0] > self._max_download_bytes:
                        raise BoardGatewayError(
                            "BOARD_ARTIFACTS_TOO_LARGE",
                            "board artifacts exceeded the configured download limit",
                        )
                    target.write(chunk)
        except Exception:
            if local.exists() and local.is_file() and not local.is_symlink():
                local.unlink()
            raise

    def _remove_tree(self, sftp: paramiko.SFTPClient, remote: str) -> None:
        self._validate_remote_root(remote)
        try:
            entries = sftp.listdir_attr(remote)
        except OSError:
            return
        for entry in entries:
            if entry.filename in {".", ".."} or "/" in entry.filename:
                continue
            child = f"{remote}/{entry.filename}"
            if stat.S_ISDIR(entry.st_mode):
                self._remove_tree_child(sftp, child)
            else:
                sftp.remove(child)
        sftp.rmdir(remote)

    def _remove_tree_child(self, sftp: paramiko.SFTPClient, remote: str) -> None:
        for entry in sftp.listdir_attr(remote):
            if entry.filename in {".", ".."} or "/" in entry.filename:
                continue
            child = f"{remote}/{entry.filename}"
            if stat.S_ISDIR(entry.st_mode):
                self._remove_tree_child(sftp, child)
            else:
                sftp.remove(child)
        sftp.rmdir(remote)

    @staticmethod
    def _validate_remote_root(remote: str) -> None:
        if not _REMOTE_ROOT.fullmatch(remote):
            raise BoardGatewayError("BOARD_REMOTE_PATH_INVALID", "remote workspace path is invalid")


def _load_private_key(value: str, passphrase: str | None) -> paramiko.PKey:
    if not value.strip():
        raise BoardGatewayError("BOARD_AUTH_INVALID", "private key is empty")
    errors: list[Exception] = []
    for key_type in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_type.from_private_key(io.StringIO(value), password=passphrase or None)
        except (paramiko.SSHException, ValueError) as exc:
            errors.append(exc)
    raise BoardGatewayError(
        "BOARD_AUTH_INVALID", "private key format or passphrase is invalid"
    ) from errors[-1]


def _parse_os_release(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in value.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, raw = line.split("=", 1)
        result[key] = raw.strip().strip('"')
    return result


def _local_artifact(path: Path, root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    suffix = path.suffix.lower()
    mime_type = {
        ".npy": "application/x-npy",
        ".json": "application/json",
        ".csv": "text/csv",
        ".log": "text/plain",
        ".txt": "text/plain",
    }.get(suffix, "application/octet-stream")
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "mime_type": mime_type,
    }
