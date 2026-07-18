from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_m2_migration_preserves_existing_m0_runs(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    database = tmp_path / "migration.sqlite3"
    config = Config(str(root / "services" / "controller" / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database}")
    command.upgrade(config, "0001_m0_runs")
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO conversion_runs (
                    id, profile_id, profile_sha256, status, request_snapshot,
                    created_at, updated_at
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001', 's100-oe-3.7.0',
                    :digest, 'SUCCEEDED', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {"digest": "a" * 64},
        )

    command.upgrade(config, "head")

    tables = set(inspect(engine).get_table_names())
    columns = {item["name"] for item in inspect(engine).get_columns("conversion_runs")}
    model_columns = {
        item["name"] for item in inspect(engine).get_columns("model_versions")
    }
    attempt_columns = {
        item["name"] for item in inspect(engine).get_columns("run_attempts")
    }
    calibration_sample_columns = {
        item["name"] for item in inspect(engine).get_columns("calibration_samples")
    }
    with engine.connect() as connection:
        preserved = connection.scalar(
            text(
                "SELECT status FROM conversion_runs "
                "WHERE id = '00000000-0000-0000-0000-000000000001'"
            )
        )

    assert {
        "projects",
        "assets",
        "models",
        "model_versions",
        "calibration_sets",
        "calibration_versions",
        "calibration_samples",
    }.issubset(tables)
    assert {
        "project_id",
        "model_version_id",
        "calibration_version_id",
        "kind",
        "runner_image_reference",
        "runner_image_id",
        "contract_version",
        "app_version",
        "generated_yaml",
    }.issubset(columns)
    assert {"inspection_run_id", "inspected_at"}.issubset(model_columns)
    assert {"stage", "cancel_requested_at", "recovered"}.issubset(attempt_columns)
    assert "validation" in calibration_sample_columns
    assert preserved == "SUCCEEDED"
