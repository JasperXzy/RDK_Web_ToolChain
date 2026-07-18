from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from rdkwt_controller.profiles import ProfileRegistry
from rdkwt_runner.adapters.openexplorer_v3_7 import (
    OpenExplorer370Adapter,
    parse_quantized_cosines,
    parse_static_metrics,
    render_openexplorer_config,
    validate_configuration,
)


def make_configuration(profile_id: str) -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    profile = ProfileRegistry.load(root / "profiles" / "targets").get(profile_id)
    s600 = profile.platform == "s600"
    return {
        "schema_version": "1",
        "target_profile": profile.snapshot(),
        "output_prefix": "resnet18_224x224_nv12",
        "inputs": [
            {
                "name": "data",
                "target_shape": [1, 3, 224, 224],
                "train_type": "rgb",
                "train_layout": "NCHW",
                "runtime_type": "nv12",
                "normalization": {
                    "mean": [123.675, 116.28, 103.53],
                    "scale": [0.01712475, 0.017507, 0.01742919],
                    "std": [],
                },
            }
        ],
        "calibration": {
            "algorithm": "default",
            "sample_limit": 20,
            "recipe": {
                "id": "imagenet-resnet18",
                "version": "1",
                "resize_short": 256,
                "crop_size": [224, 224],
                "mean": [0.485, 0.456, 0.406],
                "std": [0.229, 0.224, 0.225],
            },
        },
        "compiler": {
            "compile_mode": "latency",
            "balance_factor": None,
            "core_num": 2 if s600 else 1,
            "optimize_level": "O2",
            "max_l2m_size": "auto" if s600 else 0,
            "max_time_per_fc": 0,
            "jobs": 8,
            "cache_mode": "disable",
        },
    }


def test_render_locks_s100_platform_fields(tmp_path: Path) -> None:
    generated = render_openexplorer_config(
        make_configuration("s100-oe-3.7.0"),
        model_path=tmp_path / "model.onnx",
        calibration_dir=tmp_path / "calibration",
        working_dir=tmp_path / "output",
    )

    assert generated["model_parameters"]["march"] == "nash-e"
    assert generated["compiler_parameters"]["core_num"] == 1
    assert generated["compiler_parameters"]["max_l2m_size"] == 0
    assert generated["input_parameters"]["input_shape"] == "1x3x224x224"


def test_render_maps_s600_auto_l2m_to_yaml_null(tmp_path: Path) -> None:
    generated = render_openexplorer_config(
        make_configuration("s600-oe-3.7.0"),
        model_path=tmp_path / "model.onnx",
        calibration_dir=tmp_path / "calibration",
        working_dir=tmp_path / "output",
    )
    rendered = yaml.safe_dump(generated, sort_keys=False)

    assert generated["model_parameters"]["march"] == "nash-p"
    assert generated["compiler_parameters"]["core_num"] == 2
    assert generated["compiler_parameters"]["max_l2m_size"] is None
    assert yaml.safe_load(rendered)["compiler_parameters"]["max_l2m_size"] is None


def test_configuration_rejects_s100_dual_core() -> None:
    configuration = make_configuration("s100-oe-3.7.0")
    configuration["compiler"]["core_num"] = 2  # type: ignore[index]

    with pytest.raises(ValueError, match="core_num"):
        validate_configuration(configuration)


def test_configuration_rejects_boolean_integer_fields() -> None:
    configuration = make_configuration("s100-oe-3.7.0")
    configuration["compiler"]["core_num"] = True  # type: ignore[index]

    with pytest.raises(ValueError, match="core_num"):
        validate_configuration(configuration)


def test_configuration_rejects_self_hashed_tampered_profile() -> None:
    configuration = make_configuration("s100-oe-3.7.0")
    snapshot = configuration["target_profile"]  # type: ignore[assignment]
    snapshot["profile"]["capabilities"]["core_num"]["allowed"] = [1, 2]
    encoded = json.dumps(
        snapshot["profile"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    snapshot["sha256"] = hashlib.sha256(encoded.encode()).hexdigest()

    with pytest.raises(ValueError, match="core_num capability"):
        validate_configuration(configuration)


@pytest.mark.parametrize(
    ("fixture_name", "march", "core_num", "l2m_bytes"),
    [
        ("s100-static-perf.json", "nash-e", 1, None),
        ("s600-static-perf.json", "nash-p", 2, 9_229_312),
    ],
)
def test_static_performance_golden_parser(
    fixture_name: str, march: str, core_num: int, l2m_bytes: int | None
) -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "openexplorer-3.7.0"
        / fixture_name
    )
    metrics = parse_static_metrics(json.loads(fixture.read_text()))

    assert metrics["march"] == march
    assert metrics["core_num"] == core_num
    assert metrics["l2m_bytes_per_run"] == l2m_bytes


def test_quantized_cosine_table_parser() -> None:
    metrics = parse_quantized_cosines(
        """
| Node | NodeType | ON | Threshold | Calibrated Cosine | Quantized Cosine | Output Data Type |
| Conv_0 | Conv | BPU | 2.0 | 0.99 | 0.91 | si8 |
| Relu_1 | Relu | BPU | -- | 0.98 | 0.87 | si8 |
| TensorName | Calibrated Cosine | Quantized Cosine |
| output | 0.98 | 0.94 |
"""
    )

    assert metrics["node_count"] == 2
    assert metrics["minimum_node"] == {
        "name": "Relu_1",
        "type": "Relu",
        "device": "BPU",
        "quantized_cosine": 0.87,
    }
    assert metrics["output_cosines"] == [
        {"name": "output", "quantized_cosine": 0.94}
    ]


def test_preprocess_writes_deterministic_npy_manifest_and_yaml(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    image_module = pytest.importorskip("PIL.Image")
    model = tmp_path / "assets" / "model.onnx"
    calibration = tmp_path / "assets" / "calibration"
    attempt = tmp_path / "runs" / "attempt"
    model.parent.mkdir(parents=True)
    calibration.mkdir()
    attempt.mkdir(parents=True)
    model.write_bytes(b"onnx-placeholder")
    for index in range(20):
        image_module.new("RGB", (300, 256), color=(index, 128, 255 - index)).save(
            calibration / f"sample-{index:02d}.jpg"
        )
    request = {
        "configuration": make_configuration("s100-oe-3.7.0"),
        "limits": {"timeout_seconds": 30, "max_log_bytes": 1_048_576},
    }
    adapter = OpenExplorer370Adapter(
        request=request,
        model_path=model,
        calibration_source=calibration,
        attempt_root=attempt,
        is_cancel_requested=lambda: False,
    )

    details = adapter.preprocess()
    manifest = json.loads(adapter.calibration_manifest_path.read_text())
    outputs = sorted(adapter.calibration_root.glob("*.rgb.npy"))
    generated = yaml.safe_load(adapter.generated_config.read_text())

    assert details["sample_count"] == 20
    assert manifest["sample_count"] == 20
    assert len(outputs) == 20
    assert np.load(outputs[0], allow_pickle=False).shape == (3, 224, 224)
    assert generated["calibration_parameters"]["cal_data_dir"] == str(
        adapter.calibration_root
    )
