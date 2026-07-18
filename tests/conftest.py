from __future__ import annotations

from pathlib import Path

import pytest
from rdkwt_controller.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = Path(__file__).resolve().parents[1]
    configured = Settings(
        state_dir=tmp_path / "state",
        assets_dir=tmp_path / "assets",
        runs_dir=tmp_path / "runs",
        profile_dir=root / "profiles" / "targets",
        assets_volume="rdkwt-test-assets",
        runs_volume="rdkwt-test-runs",
        cpu_runner_image="rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1",
    )
    configured.ensure_directories()
    return configured
