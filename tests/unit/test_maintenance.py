from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import stat
import time
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from rdkwt_controller.application import CatalogError, MaintenanceError
from rdkwt_controller.infrastructure.archives import ArchiveError, BackupArchive
from rdkwt_controller.main import create_app

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def _chunks(payload: bytes):
    yield payload


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


def test_cleanup_requires_preview_and_rejects_changed_plan(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.maintenance_service
    stale = settings.assets_dir / "upload-staging" / "abandoned.part"
    stale.write_bytes(b"stale")
    old = time.time() - 25 * 60 * 60
    os.utime(stale, (old, old))

    preview = service.cleanup_preview(["stale_uploads"])

    assert preview["candidate_count"] == 1
    assert preview["reclaimable_bytes"] == 5
    with pytest.raises(MaintenanceError, match="missing, expired"):
        service.cleanup(token="wrong", categories=["stale_uploads"])

    stale.write_bytes(b"changed")
    with pytest.raises(MaintenanceError, match="changed"):
        service.cleanup(token=preview["confirmation_token"], categories=["stale_uploads"])
    assert stale.exists()

    os.utime(stale, (old, old))
    refreshed = service.cleanup_preview(["stale_uploads"])
    result = service.cleanup(token=refreshed["confirmation_token"], categories=["stale_uploads"])
    assert result["deleted_count"] == 1
    assert not stale.exists()


def test_backup_is_hash_verified_and_restore_keeps_safety_copy(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    catalog = app.state.services.catalog_service
    catalog.create_project(name="before", description="in backup")
    asset = settings.assets_dir / "restore-probe.bin"
    run_file = settings.runs_dir / "restore-probe" / "result.json"
    secret = settings.secrets_dir / "restore-probe.secret"
    asset.write_bytes(b"asset-before")
    run_file.parent.mkdir()
    run_file.write_bytes(b"run-before")
    secret.write_bytes(b"secret-before")
    manager = BackupArchive(settings, app_version="test")
    backup = manager.create(include_runs=True, include_credentials=True)
    archive_path = manager.resolve(backup["filename"])

    assert manager.verify(archive_path)["verified"] is True
    assert stat.S_IMODE(archive_path.stat().st_mode) == 0o600
    catalog.create_project(name="after", description="not in backup")
    asset.write_bytes(b"asset-after")
    run_file.write_bytes(b"run-after")
    secret.write_bytes(b"secret-after")
    (settings.assets_dir / "created-after.bin").write_bytes(b"remove me")

    restored = manager.restore(archive_path, confirmation="RESTORE")

    connection = sqlite3.connect(settings.state_dir / "db" / "rdkwt.sqlite3")
    try:
        names = [row[0] for row in connection.execute("SELECT name FROM projects")]
    finally:
        connection.close()
    assert names == ["before"]
    assert asset.read_bytes() == b"asset-before"
    assert run_file.read_bytes() == b"run-before"
    assert secret.read_bytes() == b"secret-before"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE((settings.state_dir / "db" / "rdkwt.sqlite3").stat().st_mode) == 0o600
    assert not (settings.assets_dir / "created-after.bin").exists()
    assert restored["restored"] is True
    assert manager.resolve(restored["safety_backup"]).is_file()


def test_backup_rejects_an_unlisted_zip_member(settings) -> None:
    create_app(settings, docker_client=FakeDockerClient())
    manager = BackupArchive(settings, app_version="test")
    backup = manager.create(include_runs=False, include_credentials=False)
    archive_path = manager.resolve(backup["filename"])
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr("assets/unlisted", b"tampered")

    with pytest.raises(ArchiveError, match="manifest does not match"):
        manager.verify(archive_path)


def test_restore_rolls_back_every_swapped_scope_on_failure(settings, monkeypatch) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    catalog = app.state.services.catalog_service
    catalog.create_project(name="before", description="")
    asset = settings.assets_dir / "rollback.bin"
    run_file = settings.runs_dir / "rollback" / "result.json"
    asset.write_bytes(b"before")
    run_file.parent.mkdir()
    run_file.write_bytes(b"before")
    manager = BackupArchive(settings, app_version="test")
    backup = manager.create(include_runs=True, include_credentials=False)

    catalog.create_project(name="after", description="")
    asset.write_bytes(b"after")
    run_file.write_bytes(b"after")
    original = manager._replace_root_children
    replacements = 0

    def fail_after_runs(stage, target, restore_id, moved, installed, rollback_roots):
        nonlocal replacements
        original(stage, target, restore_id, moved, installed, rollback_roots)
        replacements += 1
        if replacements == 2:
            raise RuntimeError("injected restore failure")

    monkeypatch.setattr(manager, "_replace_root_children", fail_after_runs)
    with pytest.raises(RuntimeError, match="injected restore failure"):
        manager.restore(manager.resolve(backup["filename"]), confirmation="RESTORE")

    connection = sqlite3.connect(settings.state_dir / "db" / "rdkwt.sqlite3")
    try:
        names = [row[0] for row in connection.execute("SELECT name FROM projects ORDER BY name")]
    finally:
        connection.close()
    assert names == ["after", "before"]
    assert asset.read_bytes() == b"after"
    assert run_file.read_bytes() == b"after"
    assert not any(path.name.startswith(".rollback-") for path in settings.assets_dir.iterdir())
    assert not any(path.name.startswith(".rollback-") for path in settings.runs_dir.iterdir())


def test_archives_reject_symlink_directories_and_invalid_scope(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    manager = BackupArchive(settings, app_version="test")
    backup = manager.create(include_runs=False, include_credentials=False)
    archive_path = manager.resolve(backup["filename"])
    symlink = zipfile.ZipInfo("assets/link/")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr(symlink, b"target")
    with pytest.raises(ArchiveError, match="not a file"):
        manager.verify(archive_path)

    clean = manager.create(include_runs=False, include_credentials=False)
    clean_path = manager.resolve(clean["filename"])
    invalid_scope = settings.backups_dir / "invalid-scope.rdkwt-backup.zip"
    with zipfile.ZipFile(clean_path) as source, zipfile.ZipFile(invalid_scope, "x") as target:
        for item in source.infolist():
            payload = source.read(item)
            if item.filename == "manifest.json":
                manifest = json.loads(payload)
                manifest["scope"]["assets"] = False
                payload = json.dumps(manifest).encode()
            target.writestr(item, payload)
    with pytest.raises(ArchiveError, match="scope is invalid"):
        manager.verify(invalid_scope)

    project = app.state.services.catalog_service.create_project(name="unsafe", description="")
    package = app.state.services.catalog_service.export_project(project["id"])
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr(symlink, b"target")
    with pytest.raises(CatalogError, match="unsafe entry"):
        asyncio.run(
            app.state.services.catalog_service.import_project(
                content_length=package.stat().st_size,
                chunks=_chunks(package.read_bytes()),
            )
        )


def test_project_package_round_trip_revalidates_assets(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    project = service.create_project(name="portable", description="round trip")
    asyncio.run(
        service.upload_model(
            project_id=project["id"],
            filename="model.onnx",
            model_name="model",
            content_length=5,
            chunks=_chunks(b"onnx\0"),
        )
    )
    calibration_set = service.create_calibration_set(
        project_id=project["id"], name="sample", description=""
    )
    version_id = calibration_set["versions"][0]["id"]
    asyncio.run(
        service.upload_calibration_sample(
            version_id=version_id,
            filename="sample.png",
            content_length=len(PNG_1X1),
            chunks=_chunks(PNG_1X1),
        )
    )
    service.finalize_calibration_version(version_id)
    package = service.export_project(project["id"])

    imported = asyncio.run(
        service.import_project(
            content_length=package.stat().st_size,
            chunks=_chunks(package.read_bytes()),
        )
    )

    imported_project = imported["project"]
    assert imported_project["name"] == "portable"
    assert imported_project["id"] != project["id"]
    assert imported_project["models"][0]["versions"][0]["compatibility_status"] == (
        "PENDING_INSPECTION"
    )
    assert imported_project["calibration_sets"][0]["versions"][0]["status"] == "READY"
    assert imported["inspection_required"] == 1


def test_maintenance_api_requires_session_for_mutation_and_redacts_diagnostics(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            rejected = await client.post(
                "/api/v1/maintenance/backups",
                json={"include_runs": False, "include_credentials": False},
            )
            session = await client.get("/api/v1/session")
            headers = {"X-RDKWT-CSRF": session.json()["csrf_token"]}
            created = await client.post(
                "/api/v1/maintenance/backups",
                json={"include_runs": False, "include_credentials": False},
                headers=headers,
            )
            return (
                rejected,
                created,
                await client.get("/api/v1/maintenance/storage"),
                await client.get("/api/v1/maintenance/backups"),
                await client.get("/api/v1/maintenance/diagnostics"),
                await client.get("/"),
            )

    rejected, created, storage, backups, diagnostics, index = asyncio.run(exercise())

    assert rejected.status_code == 403
    assert created.status_code == 201
    assert created.json()["verified"] is True
    assert storage.status_code == 200
    assert {item["name"] for item in storage.json()["roots"]} == {
        "state",
        "assets",
        "runs",
        "cache",
    }
    assert backups.json()[0]["filename"] == created.json()["filename"]
    assert diagnostics.headers["content-disposition"].startswith("attachment;")
    assert diagnostics.json()["redaction"]["credentials_included"] is False
    assert "maintenance-view" in index.text
