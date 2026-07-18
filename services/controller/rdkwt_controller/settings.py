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


@dataclass(frozen=True, slots=True)
class Settings:
    state_dir: Path
    assets_dir: Path
    runs_dir: Path
    profile_dir: Path
    assets_volume: str
    runs_volume: str
    cpu_runner_image: str
    bind_host: str = "127.0.0.1"
    port: int = 8080
    default_timeout_seconds: int = 14_400
    max_log_bytes: int = 100 * 1024 * 1024
    runner_memory: str = "8g"
    runner_nano_cpus: int = 4_000_000_000
    runner_pids_limit: int = 512
    stop_timeout_seconds: int = 15

    @classmethod
    def from_env(cls) -> Settings:
        local_data = _project_root() / ".rdkwt"
        return cls(
            state_dir=Path(os.environ.get("RDKWT_STATE_DIR", local_data / "state")),
            assets_dir=Path(os.environ.get("RDKWT_ASSETS_DIR", local_data / "assets")),
            runs_dir=Path(os.environ.get("RDKWT_RUNS_DIR", local_data / "runs")),
            profile_dir=Path(
                os.environ.get("RDKWT_PROFILE_DIR", _project_root() / "profiles" / "targets")
            ),
            assets_volume=os.environ.get("RDKWT_ASSETS_VOLUME", "rdkwt-assets"),
            runs_volume=os.environ.get("RDKWT_RUNS_VOLUME", "rdkwt-runs"),
            cpu_runner_image=os.environ.get(
                "RDKWT_CPU_RUNNER_IMAGE",
                "rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1",
            ),
            bind_host=os.environ.get("RDKWT_BIND_HOST", "127.0.0.1"),
            port=_positive_int("RDKWT_PORT", 8080),
            default_timeout_seconds=_positive_int("RDKWT_DEFAULT_TIMEOUT_SECONDS", 14_400),
            max_log_bytes=_positive_int("RDKWT_MAX_LOG_BYTES", 100 * 1024 * 1024),
            runner_memory=os.environ.get("RDKWT_RUNNER_MEMORY", "8g"),
            runner_nano_cpus=_positive_int("RDKWT_RUNNER_NANO_CPUS", 4_000_000_000),
            runner_pids_limit=_positive_int("RDKWT_RUNNER_PIDS_LIMIT", 512),
            stop_timeout_seconds=_positive_int("RDKWT_STOP_TIMEOUT_SECONDS", 15),
        )

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.state_dir / 'db' / 'rdkwt.sqlite3'}"

    def ensure_directories(self) -> None:
        for path in (self.state_dir / "db", self.assets_dir, self.runs_dir):
            path.mkdir(parents=True, exist_ok=True)
