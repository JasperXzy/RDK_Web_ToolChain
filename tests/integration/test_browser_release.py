from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import replace

import pytest
import uvicorn
from rdkwt_controller.main import create_app


class FakeImage:
    id = "sha256:" + "a" * 64
    attrs = {"RepoDigests": ["example.invalid/runner@sha256:" + "a" * 64]}


class FakeImages:
    def get(self, _reference: str) -> FakeImage:
        return FakeImage()


class FakeDockerClient:
    images = FakeImages()

    @staticmethod
    def ping() -> bool:
        return True

    @staticmethod
    def version() -> dict[str, str]:
        return {"Version": "test", "ApiVersion": "test", "Os": "linux", "Arch": "amd64"}

    @staticmethod
    def info() -> dict[str, str]:
        return {"OperatingSystem": "test", "DockerRootDir": "/test"}


@pytest.mark.integration
@pytest.mark.release
def test_headless_browser_initializes_workbench(settings, tmp_path) -> None:
    if os.environ.get("RDKWT_RUN_BROWSER_TESTS") != "1":
        pytest.skip("set RDKWT_RUN_BROWSER_TESTS=1 to run the headless browser gate")
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        pytest.skip("Chrome or Chromium is unavailable")

    configured = replace(settings, allowed_hosts=("127.0.0.1", "localhost"))
    app = create_app(configured, docker_client=FakeDockerClient())
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        result = subprocess.run(
            [
                chrome,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--virtual-time-budget=3000",
                f"--user-data-dir={tmp_path / 'chrome-profile'}",
                "--dump-dom",
                f"http://127.0.0.1:{port}/",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()

    assert "模型转换，" in result.stdout
    assert "M5 · 发布与维护" not in result.stdout
    assert "Recommended action" not in result.stdout
    assert 'id="maintenance-view"' in result.stdout
    assert "正在检查环境" not in result.stdout
    assert "环境可用" in result.stdout
