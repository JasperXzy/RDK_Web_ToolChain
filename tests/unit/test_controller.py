from __future__ import annotations

import asyncio
import base64
import io
import json
import uuid
import zipfile

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from rdkwt_controller.api.routes import ConversionRunRequest
from rdkwt_controller.application import CatalogError
from rdkwt_controller.infrastructure.db import CalibrationSample
from rdkwt_controller.main import create_app
from sqlalchemy import update

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def _chunks(payload: bytes):
    yield payload


def _npy_bytes(
    *, shape: tuple[int, ...] = (3, 2, 2), value: float = 0.0, dtype=np.float32
) -> bytes:
    output = io.BytesIO()
    np.save(output, np.full(shape, value, dtype=dtype), allow_pickle=False)
    return output.getvalue()


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return output.getvalue()


async def _prepare_catalog(app):
    service = app.state.services.catalog_service
    project = service.create_project(name="ResNet18", description="M2 test")
    model = await service.upload_model(
        project_id=project["id"],
        filename="resnet18.onnx",
        model_name="ResNet18",
        content_length=5,
        chunks=_chunks(b"onnx\x00"),
    )
    inspection = {
        "schema_version": "1",
        "format": "onnx",
        "size_bytes": model["asset"]["size_bytes"],
        "sha256": model["asset"]["sha256"],
        "ir_version": 9,
        "opsets": [{"domain": "ai.onnx", "version": 13}],
        "inputs": [
            {
                "name": "data",
                "shape": [1, 3, 224, 224],
                "dtype": "FLOAT",
                "dynamic": False,
            }
        ],
        "outputs": [
            {
                "name": "output",
                "shape": [1, 1000],
                "dtype": "FLOAT",
                "dynamic": False,
            }
        ],
        "operators": {"Conv": 1},
        "external_data": False,
        "external_tensor_count": 0,
        "compatibility_status": "READY",
        "blockers": [],
        "warnings": [],
    }
    model = service.repository.set_model_inspection(
        model["id"],
        run_id=str(uuid.uuid4()),
        status="READY",
        inspection=inspection,
    )
    calibration_set = service.create_calibration_set(
        project_id=project["id"], name="ImageNet", description=""
    )
    calibration_version_id = calibration_set["versions"][0]["id"]
    for index in range(20):
        await service.upload_calibration_sample(
            version_id=calibration_version_id,
            filename=f"sample-{index:02d}.png",
            content_length=len(PNG_1X1),
            chunks=_chunks(PNG_1X1),
        )
    calibration = service.finalize_calibration_version(calibration_version_id)
    return project, model, calibration


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


def test_health_profiles_and_preflight(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())

    async def exercise_app():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await asyncio.gather(
                client.get("/"),
                client.get("/api/v1/health"),
                client.get("/api/v1/profiles"),
                client.get("/api/v1/system/preflight"),
            )

    index, health, profiles, preflight = asyncio.run(exercise_app())

    assert index.status_code == 200
    assert "六步转换向导" in index.text
    assert "直接 NPY" in index.text
    assert "导入 ZIP" in index.text
    assert "frame-ancestors 'none'" in index.headers["content-security-policy"]
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert {item["platform"] for item in profiles.json()} == {"s100", "s600"}
    assert all(len(item["sha256"]) == 64 for item in profiles.json())
    assert preflight.json()["available"] is True
    assert preflight.json()["details"]["runner_image"]["immutable_id"].startswith("sha256:")


def test_probe_rejects_missing_asset_before_docker(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())

    async def exercise_app():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session = await client.get("/api/v1/session")
            assert session.headers["cache-control"] == "no-store"
            return await client.post(
                "/api/v1/system/runner-probes",
                json={"profile_id": "s100-oe-3.7.0", "asset_path": "missing.onnx"},
                headers={"X-RDKWT-CSRF": session.json()["csrf_token"]},
            )

    response = asyncio.run(exercise_app())
    assert response.status_code == 422


def test_conversion_submission_writes_normalized_s600_request(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    project, model, calibration = asyncio.run(_prepare_catalog(app))

    submission = app.state.services.run_service.submit_conversion(
        profile_id="s600-oe-3.7.0",
        model_version_id=model["id"],
        calibration_version_id=calibration["id"],
        output_prefix="resnet18_s600",
        core_num=2,
        max_l2m_size="auto",
        compile_mode="latency",
        balance_factor=None,
        optimize_level="O2",
        sample_limit=20,
        jobs=8,
    )
    request_path = (
        settings.runs_dir
        / submission.run_id
        / "attempts"
        / str(submission.attempt)
        / "request.json"
    )
    request = json.loads(request_path.read_text())

    assert request["adapter"] == "openexplorer-3.7.0"
    assert request["configuration"]["target_profile"]["profile"]["march"] == "nash-p"
    assert request["configuration"]["compiler"]["core_num"] == 2
    assert request["configuration"]["compiler"]["max_l2m_size"] == "auto"
    assert request["paths"]["model"].startswith("blobs/sha256/")
    assert request["paths"]["calibration_source"].endswith("/source")
    detail = app.state.services.repository.get(submission.run_id)
    assert detail["project_id"] == project["id"]
    assert detail["model_version_id"] == model["id"]
    assert detail["calibration_version_id"] == calibration["id"]
    preview = app.state.services.catalog_service.project_deletion_preview(project["id"])
    assert preview["can_delete"] is False
    with pytest.raises(CatalogError, match="queued or running"):
        app.state.services.catalog_service.delete_project(project["id"], confirmation=project["id"])


def test_conversion_submission_rejects_s100_dual_core(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    _project, model, calibration = asyncio.run(_prepare_catalog(app))

    with pytest.raises(ValueError, match="core_num"):
        app.state.services.run_service.submit_conversion(
            profile_id="s100-oe-3.7.0",
            model_version_id=model["id"],
            calibration_version_id=calibration["id"],
            output_prefix="resnet18_s100",
            core_num=2,
            max_l2m_size=0,
            compile_mode="latency",
            balance_factor=None,
            optimize_level="O2",
            sample_limit=20,
            jobs=8,
        )


def test_conversion_request_rejects_boolean_integer_fields() -> None:
    with pytest.raises(ValidationError):
        ConversionRunRequest.model_validate(
            {
                "profile_id": "s100-oe-3.7.0",
                "model_version_id": str(uuid.uuid4()),
                "calibration_version_id": str(uuid.uuid4()),
                "core_num": True,
            }
        )


def test_successful_conversion_runs_can_be_compared(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    model_id = str(uuid.uuid4())
    run_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    details = {}
    for index, run_id in enumerate(run_ids):
        details[run_id] = {
            "id": run_id,
            "kind": "CONVERSION",
            "status": "SUCCEEDED",
            "model_version_id": model_id,
            "calibration_version_id": str(uuid.uuid4()),
            "profile_id": "s100-oe-3.7.0",
            "runner_mode": "cpu",
            "cache": {"key": "a" * 64, "hit": bool(index)},
            "request": {
                "configuration": {
                    "calibration": {
                        "source_type": "images",
                        "algorithm": "default",
                        "sample_limit": 20,
                    },
                    "compiler": {
                        "compile_mode": "latency",
                        "core_num": 1,
                        "optimize_level": "O2",
                        "max_l2m_size": 0,
                    },
                }
            },
            "summary": {
                "hbm": {"size_bytes": 1_000 + index},
                "static_performance": {
                    "fps": 100 + index,
                    "latency_us": 10_000 - index,
                },
                "quantization": {"minimum_node": {"quantized_cosine": 0.99 - index * 0.01}},
                "verification": {
                    "hbruntime": {"duration_ms": 10 + index},
                    "hb_verifier": {"minimum_cosine": {"cosine_similarity": 0.98 - index * 0.01}},
                },
                "total_duration_ms": 1_000 + index,
            },
        }
    service = app.state.services.run_service
    monkeypatch.setattr(service, "result_detail", lambda run_id: details[run_id])

    comparison = service.compare_runs(run_ids)

    assert comparison["model_version_id"] == model_id
    assert comparison["common"]["profile_id"] == "s100-oe-3.7.0"
    assert comparison["differences"]["fps"] == [100, 101]
    assert comparison["differences"]["cache_hit"] == [False, True]


def test_project_mutations_require_csrf_and_catalog_upload_deduplicates(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())

    async def exercise_app():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            rejected = await client.post("/api/v1/projects", json={"name": "Blocked"})
            session = (await client.get("/api/v1/session")).json()
            headers = {"X-RDKWT-CSRF": session["csrf_token"]}
            wrong_origin = await client.post(
                "/api/v1/projects",
                json={"name": "Blocked origin"},
                headers={**headers, "Origin": "https://example.invalid"},
            )
            project = await client.post(
                "/api/v1/projects", json={"name": "Catalog"}, headers=headers
            )
            project_id = project.json()["id"]
            upload_headers = {
                **headers,
                "X-Filename": "resnet18.onnx",
                "X-Model-Name": "ResNet18",
                "Content-Type": "application/octet-stream",
            }
            first = await client.post(
                f"/api/v1/projects/{project_id}/models",
                content=b"same-onnx-content",
                headers=upload_headers,
            )
            second = await client.post(
                f"/api/v1/projects/{project_id}/models",
                content=b"same-onnx-content",
                headers={**upload_headers, "X-Model-Name": "ResNet18 copy"},
            )
            return rejected, wrong_origin, project, first, second

    rejected, wrong_origin, project, first, second = asyncio.run(exercise_app())

    assert rejected.status_code == 403
    assert rejected.json()["code"] == "CSRF_TOKEN_INVALID"
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"
    assert project.status_code == 201
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["storage_reused"] is False
    assert second.json()["storage_reused"] is True
    assert first.json()["asset"]["id"] == second.json()["asset"]["id"]
    assert len(list((settings.assets_dir / "blobs" / "sha256").glob("*/*"))) == 1


def test_calibration_finalize_writes_immutable_manifest(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    _project, _model, calibration = asyncio.run(_prepare_catalog(app))

    manifest_path = settings.assets_dir / "calibration-sets" / calibration["id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source_files = list(manifest_path.parent.joinpath("source").iterdir())

    assert calibration["status"] == "READY"
    assert calibration["sample_count"] == 20
    assert len(source_files) == 20
    assert manifest["manifest_sha256"] == calibration["manifest_sha256"]
    assert manifest["validation_report"]["duplicate_content_count"] == 19
    with pytest.raises(CatalogError, match="already finalized"):
        app.state.services.catalog_service.finalize_calibration_version(calibration["id"])


def test_calibration_finalize_rolls_back_materialization_on_metadata_failure(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    project = service.create_project(name="Rollback", description="")
    calibration_set = service.create_calibration_set(
        project_id=project["id"], name="Rollback set", description=""
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

    def fail_metadata_update(*_args, **_kwargs):
        raise RuntimeError("simulated metadata failure")

    monkeypatch.setattr(service.repository, "finalize_calibration_version", fail_metadata_update)

    with pytest.raises(RuntimeError, match="simulated metadata failure"):
        service.finalize_calibration_version(version_id)

    assert not (settings.assets_dir / "calibration-sets" / version_id).exists()
    assert service.get_calibration_version(version_id)["status"] == "DRAFT"


def test_image_finalize_revalidates_samples_created_before_m2_1(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    project = service.create_project(name="Upgrade", description="")
    calibration_set = service.create_calibration_set(
        project_id=project["id"], name="Legacy draft", description=""
    )
    version_id = calibration_set["versions"][0]["id"]
    asyncio.run(
        service.upload_calibration_sample(
            version_id=version_id,
            filename="legacy.png",
            content_length=len(PNG_1X1),
            chunks=_chunks(PNG_1X1),
        )
    )
    with service.repository._session_factory.begin() as session:
        session.execute(
            update(CalibrationSample)
            .where(CalibrationSample.calibration_version_id == version_id)
            .values(validation=None)
        )

    finalized = service.finalize_calibration_version(version_id)
    manifest = json.loads(
        (settings.assets_dir / "calibration-sets" / version_id / "manifest.json").read_text()
    )

    assert finalized["status"] == "READY"
    assert manifest["samples"][0]["validation"] == {
        "format": "image",
        "mime_type": "image/png",
    }


def test_npy_archive_import_validates_and_freezes_metadata(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    project = service.create_project(name="Direct NPY", description="M2.1")
    calibration_set = service.create_calibration_set(
        project_id=project["id"],
        name="Preprocessed tensors",
        description="",
        source_type="npy",
    )
    version_id = calibration_set["versions"][0]["id"]
    first_payload = _npy_bytes(value=1.0)
    first = asyncio.run(
        service.upload_calibration_sample(
            version_id=version_id,
            filename="sample-00.npy",
            content_length=len(first_payload),
            chunks=_chunks(first_payload),
        )
    )
    archive_payload = _zip_bytes(
        [
            (f"nested/sample-{index:02d}.npy", _npy_bytes(value=float(index)))
            for index in range(1, 20)
        ]
    )
    imported = asyncio.run(
        service.upload_calibration_archive(
            version_id=version_id,
            filename="calibration.zip",
            content_length=len(archive_payload),
            chunks=_chunks(archive_payload),
        )
    )
    finalized = service.finalize_calibration_version(version_id)
    manifest = json.loads(
        (settings.assets_dir / "calibration-sets" / version_id / "manifest.json").read_text()
    )

    assert first["validation"]["shape"] == [3, 2, 2]
    assert first["validation"]["dtype"] == "float32"
    assert imported["imported_count"] == 19
    assert [item["ordinal"] for item in imported["samples"]] == list(range(1, 20))
    assert finalized["source_type"] == "npy"
    assert finalized["sample_count"] == 20
    assert finalized["validation_report"]["shape"] == [3, 2, 2]
    assert finalized["validation_report"]["dtype"] == "float32"
    assert finalized["validation_report"]["first_sample_statistics"]["mean"] == 1.0
    assert manifest["source_type"] == "npy"
    assert all(item["materialized_name"].endswith(".npy") for item in manifest["samples"])


def test_rank_one_model_can_register_scalar_batch_free_npy(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    project = service.create_project(name="Scalar input", description="M3")
    calibration_set = service.create_calibration_set(
        project_id=project["id"], name="Scalar tensors", description="", source_type="npy"
    )
    version_id = calibration_set["versions"][0]["id"]
    payload = _npy_bytes(shape=(), value=3.5)

    sample = asyncio.run(
        service.upload_calibration_sample(
            version_id=version_id,
            filename="scalar.npy",
            content_length=len(payload),
            chunks=_chunks(payload),
        )
    )

    assert sample["validation"]["shape"] == []
    assert sample["validation"]["element_count"] == 1
    assert sample["validation"]["statistics"]["mean"] == 3.5


def test_npy_import_rejects_unsafe_zip_and_inconsistent_shapes(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    project = service.create_project(name="NPY rejection", description="M2.1")
    unsafe = service.create_calibration_set(
        project_id=project["id"], name="Unsafe", description="", source_type="npy"
    )
    unsafe_version = unsafe["versions"][0]["id"]
    unsafe_zip = _zip_bytes([("../escape.npy", _npy_bytes())])
    with pytest.raises(CatalogError) as unsafe_error:
        asyncio.run(
            service.upload_calibration_archive(
                version_id=unsafe_version,
                filename="unsafe.zip",
                content_length=len(unsafe_zip),
                chunks=_chunks(unsafe_zip),
            )
        )
    assert unsafe_error.value.code == "CALIBRATION_ARCHIVE_PATH_INVALID"
    assert service.get_calibration_version(unsafe_version)["sample_count"] == 0

    corrupt = service.create_calibration_set(
        project_id=project["id"], name="Corrupt", description="", source_type="npy"
    )
    corrupt_version = corrupt["versions"][0]["id"]
    corrupt_zip = _zip_bytes([("00-valid.npy", _npy_bytes()), ("01-corrupt.npy", b"not-an-npy")])
    with pytest.raises(CatalogError) as corrupt_error:
        asyncio.run(
            service.upload_calibration_archive(
                version_id=corrupt_version,
                filename="corrupt.zip",
                content_length=len(corrupt_zip),
                chunks=_chunks(corrupt_zip),
            )
        )
    assert corrupt_error.value.code == "CALIBRATION_FORMAT_INVALID"
    assert service.get_calibration_version(corrupt_version)["sample_count"] == 0
    assert list((settings.assets_dir / "blobs" / "sha256").glob("*/*")) == []
    assert list((settings.assets_dir / "upload-staging").iterdir()) == []

    inconsistent = service.create_calibration_set(
        project_id=project["id"],
        name="Inconsistent",
        description="",
        source_type="npy",
    )
    inconsistent_version = inconsistent["versions"][0]["id"]
    for index, shape in enumerate(((3, 2, 2), (3, 3, 3))):
        payload = _npy_bytes(shape=shape)
        asyncio.run(
            service.upload_calibration_sample(
                version_id=inconsistent_version,
                filename=f"sample-{index}.npy",
                content_length=len(payload),
                chunks=_chunks(payload),
            )
        )
    with pytest.raises(CatalogError) as inconsistent_error:
        service.finalize_calibration_version(inconsistent_version)
    assert inconsistent_error.value.code == "CALIBRATION_NPY_INCONSISTENT"
    assert service.get_calibration_version(inconsistent_version)["status"] == "DRAFT"
    assert not (settings.assets_dir / "calibration-sets" / inconsistent_version).exists()


def test_npy_import_rejects_object_arrays(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    project = service.create_project(name="Object NPY", description="M2.1")
    calibration_set = service.create_calibration_set(
        project_id=project["id"], name="Object", description="", source_type="npy"
    )
    version_id = calibration_set["versions"][0]["id"]
    output = io.BytesIO()
    np.save(output, np.asarray([{"unsafe": True}], dtype=object), allow_pickle=True)
    payload = output.getvalue()
    with pytest.raises(CatalogError) as captured:
        asyncio.run(
            service.upload_calibration_sample(
                version_id=version_id,
                filename="object.npy",
                content_length=len(payload),
                chunks=_chunks(payload),
            )
        )
    assert captured.value.code == "CALIBRATION_NPY_INVALID"


def test_npy_import_rejects_unrepresentable_statistics(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    project = service.create_project(name="Extreme NPY", description="M2.1")
    calibration_set = service.create_calibration_set(
        project_id=project["id"], name="Extreme", description="", source_type="npy"
    )
    version_id = calibration_set["versions"][0]["id"]
    payload = _npy_bytes(shape=(2,), value=1e308, dtype=np.float64)

    with pytest.raises(CatalogError) as captured:
        asyncio.run(
            service.upload_calibration_sample(
                version_id=version_id,
                filename="extreme.npy",
                content_length=len(payload),
                chunks=_chunks(payload),
            )
        )
    assert captured.value.code == "CALIBRATION_NPY_STATISTICS_UNREPRESENTABLE"


def test_direct_npy_conversion_normalizes_without_image_recipe(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    project, model, _image_calibration = asyncio.run(_prepare_catalog(app))
    dynamic_inspection = dict(model["inspection"])
    dynamic_inspection["inputs"] = [
        {
            "name": "data",
            "shape": ["N", 3, "H", "W"],
            "dtype": "FLOAT",
            "dynamic": True,
        }
    ]
    model = app.state.services.catalog_service.repository.set_model_inspection(
        model["id"],
        run_id=str(uuid.uuid4()),
        status="READY",
        inspection=dynamic_inspection,
    )
    service = app.state.services.catalog_service
    calibration_set = service.create_calibration_set(
        project_id=project["id"], name="Direct", description="", source_type="npy"
    )
    version_id = calibration_set["versions"][0]["id"]
    archive_payload = _zip_bytes(
        [
            (f"sample-{index:02d}.npy", _npy_bytes(shape=(3, 2, 2), value=index))
            for index in range(20)
        ]
    )
    asyncio.run(
        service.upload_calibration_archive(
            version_id=version_id,
            filename="direct.zip",
            content_length=len(archive_payload),
            chunks=_chunks(archive_payload),
        )
    )
    calibration = service.finalize_calibration_version(version_id)

    preview = app.state.services.run_service.preview_conversion(
        profile_id="s100-oe-3.7.0",
        model_version_id=model["id"],
        calibration_version_id=calibration["id"],
        output_prefix="direct_npy",
        core_num=1,
        max_l2m_size=0,
        compile_mode="latency",
        balance_factor=None,
        optimize_level="O2",
        sample_limit=20,
        jobs=8,
        input_options={"target_shape": [1, 3, 2, 2]},
    )

    assert preview["configuration"]["calibration"]["source_type"] == "npy"
    assert preview["configuration"]["calibration"]["recipe"] is None


def test_direct_npy_conversion_requires_batch_free_model_shape(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    project, model, _image_calibration = asyncio.run(_prepare_catalog(app))
    service = app.state.services.catalog_service
    calibration_set = service.create_calibration_set(
        project_id=project["id"],
        name="Wrong Shape",
        description="",
        source_type="npy",
    )
    version_id = calibration_set["versions"][0]["id"]
    archive_payload = _zip_bytes(
        [(f"sample-{index:02d}.npy", _npy_bytes(shape=(3, 2, 2))) for index in range(20)]
    )
    asyncio.run(
        service.upload_calibration_archive(
            version_id=version_id,
            filename="wrong-shape.zip",
            content_length=len(archive_payload),
            chunks=_chunks(archive_payload),
        )
    )
    calibration = service.finalize_calibration_version(version_id)

    with pytest.raises(ValueError, match="batch-free model input Shape"):
        app.state.services.run_service.preview_conversion(
            profile_id="s100-oe-3.7.0",
            model_version_id=model["id"],
            calibration_version_id=calibration["id"],
            output_prefix="wrong_shape",
            core_num=1,
            max_l2m_size=0,
            compile_mode="latency",
            balance_factor=None,
            optimize_level="O2",
            sample_limit=20,
            jobs=8,
        )


def test_multi_input_npy_archive_is_aligned_and_normalized(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    project, model, _image_calibration = asyncio.run(_prepare_catalog(app))
    inspection = dict(model["inspection"])
    inspection["inputs"] = [
        {
            "name": "data",
            "shape": ["N", 3, "H", "W"],
            "dtype": "FLOAT",
            "dynamic": True,
        },
        {
            "name": "aux",
            "shape": [1, 4],
            "dtype": "FLOAT",
            "dynamic": False,
        },
    ]
    model = app.state.services.catalog_service.repository.set_model_inspection(
        model["id"],
        run_id=str(uuid.uuid4()),
        status="READY",
        inspection=inspection,
    )
    service = app.state.services.catalog_service
    calibration_set = service.create_calibration_set(
        project_id=project["id"],
        name="Aligned multi-input",
        description="M3",
        source_type="npy_multi",
    )
    version_id = calibration_set["versions"][0]["id"]
    archive_payload = _zip_bytes(
        [
            (f"data/sample-{index:02d}.npy", _npy_bytes(shape=(3, 2, 2), value=index))
            for index in range(20)
        ]
        + [
            (f"aux/sample-{index:02d}.npy", _npy_bytes(shape=(4,), value=index))
            for index in range(20)
        ]
    )
    imported = asyncio.run(
        service.upload_calibration_archive(
            version_id=version_id,
            filename="multi-input.zip",
            content_length=len(archive_payload),
            chunks=_chunks(archive_payload),
        )
    )
    calibration = service.finalize_calibration_version(version_id)

    preview = app.state.services.run_service.preview_conversion(
        profile_id="s100-oe-3.7.0",
        model_version_id=model["id"],
        calibration_version_id=calibration["id"],
        output_prefix="multi_input",
        core_num=1,
        max_l2m_size=0,
        compile_mode="latency",
        balance_factor=None,
        optimize_level="O2",
        sample_limit=20,
        jobs=8,
        input_options=[
            {
                "name": "data",
                "target_shape": [1, 3, 2, 2],
                "train_type": "featuremap",
                "train_layout": "NCHW",
                "runtime_type": "featuremap",
                "normalization": {},
            },
            {
                "name": "aux",
                "target_shape": [1, 4],
                "train_type": "featuremap",
                "train_layout": "NCHW",
                "runtime_type": "featuremap",
                "normalization": {},
            },
        ],
        cache_mode="enable",
    )

    assert imported["imported_count"] == 20
    assert imported["imported_file_count"] == 40
    assert calibration["sample_count"] == 20
    assert calibration["validation_report"]["physical_file_count"] == 40
    assert {item["name"] for item in calibration["validation_report"]["inputs"]} == {
        "data",
        "aux",
    }
    assert preview["configuration"]["calibration"]["source_type"] == "npy_multi"
    assert len(preview["configuration"]["inputs"]) == 2
    assert len(preview["resource_snapshot"]["cache_key"]) == 64
    assert "/data;" in preview["yaml"]
    assert "/aux" in preview["yaml"]


def test_multi_input_npy_archive_rejects_unaligned_samples_atomically(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    project = service.create_project(name="Unaligned", description="M3")
    calibration_set = service.create_calibration_set(
        project_id=project["id"],
        name="Unaligned tensors",
        description="",
        source_type="npy_multi",
    )
    version_id = calibration_set["versions"][0]["id"]
    archive_payload = _zip_bytes(
        [
            ("data/sample-00.npy", _npy_bytes()),
            ("aux/different.npy", _npy_bytes(shape=(4,))),
        ]
    )

    with pytest.raises(CatalogError) as captured:
        asyncio.run(
            service.upload_calibration_archive(
                version_id=version_id,
                filename="unaligned.zip",
                content_length=len(archive_payload),
                chunks=_chunks(archive_payload),
            )
        )

    assert captured.value.code == "CALIBRATION_MULTI_INPUT_ALIGNMENT_INVALID"
    assert service.get_calibration_version(version_id)["sample_count"] == 0
    assert list((settings.assets_dir / "upload-staging").iterdir()) == []


def test_project_delete_requires_confirmation_and_preserves_shared_blob(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    service = app.state.services.catalog_service
    first_project = service.create_project(name="First", description="")
    second_project = service.create_project(name="Second", description="")

    async def upload_both():
        first = await service.upload_model(
            project_id=first_project["id"],
            filename="shared.onnx",
            model_name="Shared",
            content_length=6,
            chunks=_chunks(b"shared"),
        )
        second = await service.upload_model(
            project_id=second_project["id"],
            filename="shared.onnx",
            model_name="Shared",
            content_length=6,
            chunks=_chunks(b"shared"),
        )
        return first, second

    first, second = asyncio.run(upload_both())
    blob = next((settings.assets_dir / "blobs" / "sha256").glob("*/*"))

    assert first["asset"]["id"] == second["asset"]["id"]
    with pytest.raises(CatalogError, match="must exactly match"):
        service.delete_project(first_project["id"], confirmation="wrong")
    service.delete_project(first_project["id"], confirmation=first_project["id"])
    assert blob.is_file()
    service.delete_project(second_project["id"], confirmation=second_project["id"])
    assert not blob.exists()


def test_conversion_rechecks_catalog_content_before_submission(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    _project, model, calibration = asyncio.run(_prepare_catalog(app))

    def submit():
        return app.state.services.run_service.submit_conversion(
            profile_id="s100-oe-3.7.0",
            model_version_id=model["id"],
            calibration_version_id=calibration["id"],
            output_prefix="integrity_check",
            core_num=1,
            max_l2m_size=0,
            compile_mode="latency",
            balance_factor=None,
            optimize_level="O2",
            sample_limit=20,
            jobs=8,
        )

    model_blob = next((settings.assets_dir / "blobs" / "sha256").glob("*/*.onnx"))
    model_blob.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="model asset hash"):
        submit()

    model_blob.write_bytes(b"onnx\x00")
    calibration_source = settings.assets_dir / "calibration-sets" / calibration["id"] / "source"
    (calibration_source / "unexpected.png").write_bytes(PNG_1X1)
    with pytest.raises(ValueError, match="file list"):
        submit()


def test_calibration_sample_content_is_verified_and_served_inline(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    _project, _model, calibration = asyncio.run(_prepare_catalog(app))
    path, metadata = app.state.services.catalog_service.calibration_sample_file(
        calibration["id"], 0
    )

    assert path.read_bytes() == PNG_1X1
    assert metadata["mime_type"] == "image/png"
    assert metadata["original_filename"] == "sample-00.png"


def test_session_cookie_is_required_in_addition_to_csrf(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())

    async def exercise_app():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session = await client.get("/api/v1/session")
            client.cookies.clear()
            return await client.post(
                "/api/v1/projects",
                json={"name": "Missing cookie"},
                headers={"X-RDKWT-CSRF": session.json()["csrf_token"]},
            )

    response = asyncio.run(exercise_app())

    assert response.status_code == 403
    assert response.json()["code"] == "SESSION_COOKIE_INVALID"


def test_runner_smoke_test_is_persisted_in_preflight(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())

    async def exercise_app():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session = await client.get("/api/v1/session")
            submission = await client.post(
                "/api/v1/system/preflight/runner-smoke-test",
                json={"profile_id": "s100-oe-3.7.0"},
                headers={"X-RDKWT-CSRF": session.json()["csrf_token"]},
            )
            preflight = await client.get("/api/v1/system/preflight")
            return submission, preflight

    submission, preflight = asyncio.run(exercise_app())

    assert submission.status_code == 202
    smoke = preflight.json()["details"]["runner_smoke_test"]
    assert smoke["run_id"] == submission.json()["run_id"]
    assert smoke["status"] == "QUEUED"
    assert (settings.assets_dir / "system" / "runner-preflight.bin").is_file()


def test_retry_uses_new_attempt_without_overwriting_snapshot(settings) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    _project, model, calibration = asyncio.run(_prepare_catalog(app))
    service = app.state.services.run_service
    repository = app.state.services.repository
    submission = service.submit_conversion(
        profile_id="s100-oe-3.7.0",
        model_version_id=model["id"],
        calibration_version_id=calibration["id"],
        output_prefix="retry_snapshot",
        core_num=1,
        max_l2m_size=0,
        compile_mode="latency",
        balance_factor=None,
        optimize_level="O2",
        sample_limit=20,
        jobs=8,
    )
    first_request = repository.get(submission.run_id)["request"]

    cancellation = service.cancel(submission.run_id)
    cancelled = repository.get(submission.run_id)
    assert cancellation["terminal"] is True
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["attempts"][0]["status"] == "CANCELLED"
    retry = service.retry(submission.run_id)
    second_request_path = settings.runs_dir / submission.run_id / "attempts" / "2" / "request.json"
    second_request = json.loads(second_request_path.read_text())

    assert retry.attempt == 2
    assert second_request["run_id"] == first_request["run_id"]
    assert second_request["attempt"] == 2
    assert second_request["paths"]["attempt_root"].endswith("/attempts/2")
    assert first_request["attempt"] == 1
    assert (settings.runs_dir / submission.run_id / "attempts" / "1" / "request.json").is_file()

    assert repository.claim_queued(submission.run_id, 2) is True
    assert repository.set_running(submission.run_id, 2, "container-two") is True
    active = repository.active()
    assert [(item.run_id, item.attempt) for item in active] == [(submission.run_id, 2)]


def test_successful_run_exports_reproducible_package_and_project_deletes_runs(
    settings,
) -> None:
    app = create_app(settings, docker_client=FakeDockerClient())
    project, model, calibration = asyncio.run(_prepare_catalog(app))
    service = app.state.services.run_service
    repository = app.state.services.repository
    submission = service.submit_conversion(
        profile_id="s100-oe-3.7.0",
        model_version_id=model["id"],
        calibration_version_id=calibration["id"],
        output_prefix="exportable",
        core_num=1,
        max_l2m_size=0,
        compile_mode="latency",
        balance_factor=None,
        optimize_level="O2",
        sample_limit=20,
        jobs=8,
    )
    attempt_root = settings.runs_dir / submission.run_id / "attempts" / "1"
    artifacts = attempt_root / "artifacts"
    artifacts.mkdir()
    hbm = artifacts / "exportable.hbm"
    hbm.write_bytes(b"valid-hbm")
    import hashlib

    manifest = {
        "schema_version": "1",
        "artifacts": [
            {
                "kind": "hbm",
                "relative_path": "artifacts/exportable.hbm",
                "size_bytes": hbm.stat().st_size,
                "sha256": hashlib.sha256(hbm.read_bytes()).hexdigest(),
                "mime_type": "application/octet-stream",
                "required": True,
            }
        ],
    }
    (attempt_root / "artifact-manifest.json").write_text(json.dumps(manifest))
    (attempt_root / "generated.yaml").write_text("model_parameters: {}\n")
    result = {
        "contract_version": "1.0",
        "run_id": submission.run_id,
        "attempt": 1,
        "status": "succeeded",
        "started_at": "2026-07-19T00:00:00Z",
        "finished_at": "2026-07-19T00:00:01Z",
        "steps": [],
        "toolchain_versions": {"openexplorer": "3.7.0"},
        "metrics": {},
        "warnings": [],
        "error": None,
        "artifact_manifest": "artifact-manifest.json",
    }
    (attempt_root / "result.json").write_text(json.dumps(result))
    repository.finish(
        submission.run_id,
        1,
        status="SUCCEEDED",
        exit_code=0,
        result_payload=result,
    )

    exported = service.export_run(submission.run_id)
    with zipfile.ZipFile(exported) as archive:
        names = set(archive.namelist())
        version_manifest = json.loads(archive.read("version-manifest.json"))

    assert {
        "metadata.json",
        "version-manifest.json",
        "attempt/request.json",
        "attempt/result.json",
        "attempt/artifact-manifest.json",
        "attempt/generated.yaml",
        "attempt/artifacts/exportable.hbm",
        "inputs/model.onnx",
        "inputs/calibration-manifest.json",
    }.issubset(names)
    assert version_manifest["runner_image"]["immutable_id"].startswith("sha256:")
    preview = app.state.services.catalog_service.project_deletion_preview(project["id"])
    assert preview["can_delete"] is True
    assert preview["run_count"] == 1
    app.state.services.catalog_service.delete_project(project["id"], confirmation=project["id"])
    assert repository.get(submission.run_id) is None
    assert not (settings.runs_dir / submission.run_id).exists()
