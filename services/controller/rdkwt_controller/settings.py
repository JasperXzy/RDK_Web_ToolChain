from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    value = default if raw is None else int(raw)
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _gpu_device_ids() -> tuple[str, ...]:
    raw = os.environ.get("RDKWT_GPU_DEVICE_IDS", "").strip()
    if not raw or raw.lower() == "all":
        return ()
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values or any(not item.isdigit() for item in values):
        raise ValueError("RDKWT_GPU_DEVICE_IDS must be 'all' or comma-separated integers")
    return values


def _allowed_hosts() -> tuple[str, ...]:
    raw = os.environ.get("RDKWT_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]")
    hosts = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not hosts:
        raise ValueError("RDKWT_ALLOWED_HOSTS must contain at least one host")
    return hosts


@dataclass(frozen=True, slots=True)
class Settings:
    state_dir: Path
    assets_dir: Path
    runs_dir: Path
    profile_dir: Path
    assets_volume: str
    runs_volume: str
    cpu_runner_image: str
    cache_dir: Path | None = None
    cache_volume: str = "rdkwt-cache"
    gpu_enabled: bool = False
    gpu_runner_image: str | None = None
    gpu_device_ids: tuple[str, ...] = ()
    gpu_shm_size: str = "15g"
    bind_host: str = "127.0.0.1"
    port: int = 8080
    default_timeout_seconds: int = 14_400
    max_log_bytes: int = 100 * 1024 * 1024
    runner_memory: str = "8g"
    runner_nano_cpus: int = 4_000_000_000
    runner_pids_limit: int = 512
    stop_timeout_seconds: int = 15
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    min_free_bytes: int = 512 * 1024 * 1024
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]")
    board_connect_timeout_seconds: int = 10
    board_command_timeout_seconds: int = 1_800
    board_max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    board_keep_remote: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        local_data = _project_root() / ".rdkwt"
        return cls(
            state_dir=Path(os.environ.get("RDKWT_STATE_DIR", local_data / "state")),
            assets_dir=Path(os.environ.get("RDKWT_ASSETS_DIR", local_data / "assets")),
            runs_dir=Path(os.environ.get("RDKWT_RUNS_DIR", local_data / "runs")),
            cache_dir=Path(os.environ.get("RDKWT_CACHE_DIR", local_data / "cache")),
            profile_dir=Path(
                os.environ.get("RDKWT_PROFILE_DIR", _project_root() / "profiles" / "targets")
            ),
            assets_volume=os.environ.get("RDKWT_ASSETS_VOLUME", "rdkwt-assets"),
            runs_volume=os.environ.get("RDKWT_RUNS_VOLUME", "rdkwt-runs"),
            cache_volume=os.environ.get("RDKWT_CACHE_VOLUME", "rdkwt-cache"),
            cpu_runner_image=os.environ.get(
                "RDKWT_CPU_RUNNER_IMAGE",
                "rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1",
            ),
            gpu_enabled=_boolean("RDKWT_GPU_ENABLED", False),
            gpu_runner_image=(os.environ.get("RDKWT_GPU_RUNNER_IMAGE", "").strip() or None),
            gpu_device_ids=_gpu_device_ids(),
            gpu_shm_size=os.environ.get("RDKWT_GPU_SHM_SIZE", "15g"),
            bind_host=os.environ.get("RDKWT_BIND_HOST", "127.0.0.1"),
            port=_positive_int("RDKWT_PORT", 8080),
            default_timeout_seconds=_positive_int("RDKWT_DEFAULT_TIMEOUT_SECONDS", 14_400),
            max_log_bytes=_positive_int("RDKWT_MAX_LOG_BYTES", 100 * 1024 * 1024),
            runner_memory=os.environ.get("RDKWT_RUNNER_MEMORY", "8g"),
            runner_nano_cpus=_positive_int("RDKWT_RUNNER_NANO_CPUS", 4_000_000_000),
            runner_pids_limit=_positive_int("RDKWT_RUNNER_PIDS_LIMIT", 512),
            stop_timeout_seconds=_positive_int("RDKWT_STOP_TIMEOUT_SECONDS", 15),
            max_upload_bytes=_positive_int("RDKWT_MAX_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024),
            min_free_bytes=_positive_int("RDKWT_MIN_FREE_DISK_BYTES", 512 * 1024 * 1024),
            allowed_hosts=_allowed_hosts(),
            board_connect_timeout_seconds=_positive_int("RDKWT_BOARD_CONNECT_TIMEOUT_SECONDS", 10),
            board_command_timeout_seconds=_positive_int(
                "RDKWT_BOARD_COMMAND_TIMEOUT_SECONDS", 1_800
            ),
            board_max_upload_bytes=_positive_int(
                "RDKWT_BOARD_MAX_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024
            ),
            board_keep_remote=_boolean("RDKWT_BOARD_KEEP_REMOTE", False),
        )

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.state_dir / 'db' / 'rdkwt.sqlite3'}"

    @property
    def alembic_config_path(self) -> Path:
        configured = os.environ.get("RDKWT_ALEMBIC_CONFIG")
        if configured is not None:
            path = Path(configured)
            if not path.is_file():
                raise FileNotFoundError(f"configured Alembic file does not exist: {configured}")
            return path
        candidates = [
            Path(__file__).resolve().parents[1] / "alembic.ini",
            Path.cwd() / "services" / "controller" / "alembic.ini",
            _project_root() / "services" / "controller" / "alembic.ini",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise FileNotFoundError("Alembic configuration was not found; set RDKWT_ALEMBIC_CONFIG")

    def ensure_directories(self) -> None:
        for path in (
            self.state_dir / "db",
            self.assets_dir,
            self.runs_dir,
            self.effective_cache_dir,
            self.secrets_dir,
            self.board_runs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.chmod(0o700)

    @property
    def effective_cache_dir(self) -> Path:
        return self.cache_dir or self.state_dir.parent / "cache"

    @property
    def secrets_dir(self) -> Path:
        return self.state_dir / "secrets"

    @property
    def board_runs_dir(self) -> Path:
        return self.runs_dir / "board-runs"
