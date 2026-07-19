from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from rdkwt_controller.profiles import ProfileRegistry
from rdkwt_runner.adapters.openexplorer_v3_7 import (
    OpenExplorer370Adapter,
    _build_hb_verifier_command,
    _load_npy,
    parse_quantized_cosines,
    parse_static_metrics,
    parse_verifier_metrics,
    render_openexplorer_config,
    validate_configuration,
)


def test_hb_verifier_command_repeats_input_option_for_multi_input(tmp_path: Path) -> None:
    optimized = tmp_path / "optimized.onnx"
    calibrated = tmp_path / "calibrated.onnx"
    input_paths = [tmp_path / "data.npy", tmp_path / "aux.npy"]

    command = _build_hb_verifier_command(
        optimized=optimized,
        calibrated=calibrated,
        input_paths=input_paths,
        compare_digits=7,
    )

    assert command == [
        "hb_verifier",
        "--model",
        f"{optimized},{calibrated}",
        "--input",
        str(input_paths[0]),
        "--input",
        str(input_paths[1]),
        "--compare_digits",
        "7",
    ]


def test_npy_loader_supports_oe_bundled_numpy_without_max_header_size(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    source = tmp_path / "sample.npy"
    np.save(source, np.arange(4, dtype=np.float32), allow_pickle=False)

    class OldNumpy:
        @staticmethod
        def load(*args: object, **kwargs: object) -> object:
            if "max_header_size" in kwargs:
                raise TypeError("load() got an unexpected keyword argument 'max_header_size'")
            return np.load(*args, **kwargs)

    loaded = _load_npy(OldNumpy, source)

    assert loaded.tolist() == [0.0, 1.0, 2.0, 3.0]


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
            "source_type": "images",
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
            "cache_key": None,
        },
        "verification": {"mode": "basic", "compare_digits": 5},
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


def test_configuration_rejects_featuremap_train_type_for_image_calibration() -> None:
    configuration = make_configuration("s100-oe-3.7.0")
    configuration["inputs"][0]["train_type"] = "featuremap"  # type: ignore[index]

    with pytest.raises(ValueError, match="image calibration requires"):
        validate_configuration(configuration)


def test_configuration_rejects_single_input_multi_npy_calibration() -> None:
    configuration = make_configuration("s100-oe-3.7.0")
    configuration["calibration"] = {  # type: ignore[index]
        "source_type": "npy_multi",
        "algorithm": "default",
        "sample_limit": 20,
        "recipe": None,
    }

    with pytest.raises(ValueError, match="at least two"):
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
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "openexplorer-3.7.0" / fixture_name
    metrics = parse_static_metrics(json.loads(fixture.read_text()))

    assert metrics["march"] == march
    assert metrics["core_num"] == core_num
    assert metrics["l2m_bytes_per_run"] == l2m_bytes


def test_quantized_cosine_table_parser() -> None:
    metrics = parse_quantized_cosines(
        """
2026-07-19 13:46:53 INFO | Node | NodeType | ON | T | Calibrated | Quantized Cosine | Type |
2026-07-19 13:46:53 INFO | Conv_0 | Conv | BPU | 2.0 | 0.99 | 0.91 | si8 |
2026-07-19 13:46:53 INFO | Relu_1 | Relu | BPU | -- | 0.98 | 0.87 | si8 |
2026-07-19 13:46:53 INFO | TensorName | Calibrated Cosine | Quantized Cosine |
2026-07-19 13:46:53 INFO | output | 0.98 | 0.94 |
"""
    )

    assert metrics["node_count"] == 2
    assert metrics["minimum_node"] == {
        "name": "Relu_1",
        "type": "Relu",
        "device": "BPU",
        "quantized_cosine": 0.87,
    }
    assert metrics["output_cosines"] == [{"name": "output", "quantized_cosine": 0.94}]


def test_verifier_table_parser() -> None:
    metrics = parse_verifier_metrics(
        """
2026-07-19 13:54:15 INFO | NodeName | TensorName | CosineSimilarity |
2026-07-19 13:54:15 INFO | Conv_0 | 365 | 0.999978 |
2026-07-19 13:54:15 INFO | Relu_2 | output | 0.984707 |
"""
    )

    assert metrics["minimum_cosine"] == {
        "node_name": "Relu_2",
        "tensor_name": "output",
        "cosine_similarity": 0.984707,
    }
    assert len(metrics["cosines"]) == 2


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
    assert generated["calibration_parameters"]["cal_data_dir"] == str(adapter.calibration_root)


def test_direct_npy_preprocess_validates_and_preserves_samples(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    model = tmp_path / "assets" / "model.onnx"
    calibration = tmp_path / "assets" / "calibration"
    attempt = tmp_path / "runs" / "attempt"
    model.parent.mkdir(parents=True)
    calibration.mkdir()
    attempt.mkdir(parents=True)
    model.write_bytes(b"onnx-placeholder")
    for index in range(20):
        np.save(
            calibration / f"sample-{index:02d}.npy",
            np.full((3, 2, 2), index, dtype=np.float32),
            allow_pickle=False,
        )
    configuration = make_configuration("s100-oe-3.7.0")
    configuration["inputs"][0]["target_shape"] = [1, 3, 2, 2]  # type: ignore[index]
    configuration["calibration"] = {  # type: ignore[index]
        "source_type": "npy",
        "algorithm": "default",
        "sample_limit": 20,
        "recipe": None,
    }
    adapter = OpenExplorer370Adapter(
        request={
            "configuration": configuration,
            "limits": {"timeout_seconds": 30, "max_log_bytes": 1_048_576},
        },
        model_path=model,
        calibration_source=calibration,
        attempt_root=attempt,
        is_cancel_requested=lambda: False,
    )

    details = adapter.preprocess()
    manifest = json.loads(adapter.calibration_manifest_path.read_text())
    outputs = sorted(adapter.calibration_root.glob("*.npy"))

    assert details["source_type"] == "npy"
    assert details["first_sample_statistics"]["shape"] == [3, 2, 2]
    assert details["preview"] is None
    assert manifest["source_type"] == "npy"
    assert manifest["recipe"] is None
    assert len(outputs) == 20
    assert np.load(outputs[-1], allow_pickle=False).mean() == 19.0


def test_multi_input_npy_preprocess_keeps_samples_aligned(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    model = tmp_path / "assets" / "model.onnx"
    calibration = tmp_path / "assets" / "calibration"
    attempt = tmp_path / "runs" / "attempt"
    model.parent.mkdir(parents=True)
    calibration.mkdir()
    attempt.mkdir(parents=True)
    model.write_bytes(b"onnx-placeholder")
    for input_name, shape in (("data", (3, 2, 2)), ("aux", (4,))):
        source = calibration / input_name
        source.mkdir()
        for index in range(20):
            np.save(
                source / f"sample-{index:02d}.npy",
                np.full(shape, index, dtype=np.float32),
                allow_pickle=False,
            )
    configuration = make_configuration("s100-oe-3.7.0")
    configuration["inputs"] = [  # type: ignore[index]
        {
            "name": "data",
            "target_shape": [1, 3, 2, 2],
            "train_type": "featuremap",
            "train_layout": "NCHW",
            "runtime_type": "featuremap",
            "normalization": {"mean": [], "scale": [], "std": []},
        },
        {
            "name": "aux",
            "target_shape": [1, 4],
            "train_type": "featuremap",
            "train_layout": "NCHW",
            "runtime_type": "featuremap",
            "normalization": {"mean": [], "scale": [], "std": []},
        },
    ]
    configuration["calibration"] = {  # type: ignore[index]
        "source_type": "npy_multi",
        "algorithm": "default",
        "sample_limit": 20,
        "recipe": None,
    }
    adapter = OpenExplorer370Adapter(
        request={
            "configuration": configuration,
            "limits": {"timeout_seconds": 30, "max_log_bytes": 1_048_576},
        },
        model_path=model,
        calibration_source=calibration,
        attempt_root=attempt,
        is_cancel_requested=lambda: False,
    )

    details = adapter.preprocess()
    generated = yaml.safe_load(adapter.generated_config.read_text())

    assert details["sample_count"] == 20
    assert details["input_count"] == 2
    assert generated["input_parameters"]["input_name"] == "data;aux"
    assert generated["input_parameters"]["input_shape"] == "1x3x2x2;1x4"
    assert generated["calibration_parameters"]["cal_data_dir"] == (
        f"{adapter.calibration_root / 'data'};{adapter.calibration_root / 'aux'}"
    )
    assert len(list((adapter.calibration_root / "data").glob("*.npy"))) == 20
    assert len(list((adapter.calibration_root / "aux").glob("*.npy"))) == 20
