from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..filesystem import atomic_write_json, atomic_write_text, sha256_file
from ..inspection import inspect_onnx

ADAPTER_ID = "openexplorer-3.7.0"
OUTPUT_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SUPPORTED_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png"}
SUPPORTED_CALIBRATION_ALGORITHMS = {"default", "mix", "kl", "max"}
SUPPORTED_COMPILE_MODES = {"latency", "bandwidth", "balance"}
SUPPORTED_OPTIMIZE_LEVELS = {"O0", "O1", "O2"}
SUPPORTED_TRAIN_TYPES = {"rgb", "bgr", "gray", "yuv444", "featuremap"}
SUPPORTED_RUNTIME_TYPES = {"nv12", "rgb", "bgr", "yuv444", "gray", "featuremap"}
MAX_NPY_HEADER_BYTES = 16 * 1024


class AdapterExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        step: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.step = step
        self.details = details or {}


def _load_npy(np: Any, path: Path) -> Any:
    """Load a bounded-header NPY across both current and OE-bundled NumPy releases."""
    with path.open("rb") as handle:
        prefix = handle.read(12)
    if len(prefix) < 10 or prefix[:6] != b"\x93NUMPY":
        raise ValueError("file does not contain a valid NPY header")
    major = prefix[6]
    if major == 1:
        header_offset = 10
        header_size = int.from_bytes(prefix[8:10], "little")
    elif major in {2, 3} and len(prefix) >= 12:
        header_offset = 12
        header_size = int.from_bytes(prefix[8:12], "little")
    else:
        raise ValueError(f"unsupported NPY format version {major}.{prefix[7]}")
    if header_size < 1 or header_size > MAX_NPY_HEADER_BYTES:
        raise ValueError(f"NPY header exceeds {MAX_NPY_HEADER_BYTES} bytes")
    if header_offset + header_size > path.stat().st_size:
        raise ValueError("NPY header exceeds the file size")
    try:
        return np.load(
            path,
            allow_pickle=False,
            mmap_mode="r",
            max_header_size=MAX_NPY_HEADER_BYTES,
        )
    except TypeError as exc:
        if "max_header_size" not in str(exc):
            raise
        return np.load(path, allow_pickle=False, mmap_mode="r")


def _expect_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _expect_exact_fields(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} fields must be exactly {sorted(expected)}")


def _numeric_list(value: Any, name: str, *, allowed_lengths: set[int]) -> list[float]:
    if not isinstance(value, list) or len(value) not in allowed_lengths:
        expected = ", ".join(str(item) for item in sorted(allowed_lengths))
        raise ValueError(f"{name} must contain {expected} numeric values")
    if any(isinstance(item, bool) or not isinstance(item, int | float) for item in value):
        raise ValueError(f"{name} must contain only numeric values")
    return [float(item) for item in value]


def _profile_payload(configuration: dict[str, Any]) -> dict[str, Any]:
    snapshot = _expect_object(configuration["target_profile"], "target_profile")
    _expect_exact_fields(snapshot, {"profile", "sha256"}, "target_profile")
    profile = _expect_object(snapshot["profile"], "target_profile.profile")
    encoded = json.dumps(profile, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    actual_digest = hashlib.sha256(encoded.encode()).hexdigest()
    if snapshot["sha256"] != actual_digest:
        raise ValueError("target_profile snapshot hash does not match its profile")
    return profile


def validate_configuration(configuration: Any) -> dict[str, Any]:
    config = _expect_object(configuration, "configuration")
    _expect_exact_fields(
        config,
        {
            "schema_version",
            "target_profile",
            "output_prefix",
            "inputs",
            "calibration",
            "compiler",
            "verification",
        },
        "configuration",
    )
    if config["schema_version"] != "1":
        raise ValueError("unsupported configuration schema_version")
    if not isinstance(config["output_prefix"], str) or not OUTPUT_PREFIX.fullmatch(
        config["output_prefix"]
    ):
        raise ValueError("output_prefix contains unsupported characters")

    profile = _profile_payload(config)
    platform = profile.get("platform")
    expected = {
        "s100": ("s100-oe-3.7.0", "nash-e", "j6em", [1]),
        "s600": ("s600-oe-3.7.0", "nash-p", "j6p", [1, 2]),
    }.get(platform)
    if expected is None:
        raise ValueError("target profile platform must be s100 or s600")
    _expect_exact_fields(
        profile,
        {
            "schema_version",
            "profile_id",
            "display_name",
            "toolchain_adapter",
            "platform",
            "march",
            "operator_catalog",
            "capabilities",
        },
        "target_profile.profile",
    )
    if (
        profile.get("schema_version") != "1"
        or profile.get("profile_id") != expected[0]
        or profile.get("toolchain_adapter") != ADAPTER_ID
        or profile.get("march") != expected[1]
        or profile.get("operator_catalog") != expected[2]
    ):
        raise ValueError("target profile is not compatible with this adapter")
    capabilities = _expect_object(profile["capabilities"], "target_profile.profile.capabilities")
    _expect_exact_fields(
        capabilities,
        {"core_num", "max_l2m_size", "compile_mode", "optimize_level"},
        "target_profile.profile.capabilities",
    )
    core_capability = _expect_object(capabilities["core_num"], "profile core_num capability")
    if core_capability != {"allowed": expected[3], "default": 1}:
        raise ValueError("target profile core_num capability was not recognized")
    if capabilities["compile_mode"] != {
        "allowed": ["latency", "bandwidth", "balance"],
        "default": "latency",
    }:
        raise ValueError("target profile compile_mode capability was not recognized")
    if capabilities["optimize_level"] != {
        "allowed": ["O0", "O1", "O2"],
        "default": "O2",
    }:
        raise ValueError("target profile optimize_level capability was not recognized")
    expected_l2m = (
        {
            "mode": "disabled",
            "default": 0,
            "allowed": [0],
            "supports_auto": False,
            "minimum_bytes": None,
            "maximum_bytes": None,
        }
        if platform == "s100"
        else {
            "mode": "optional",
            "default": 0,
            "allowed": None,
            "supports_auto": True,
            "minimum_bytes": 0,
            "maximum_bytes": 24 * 1024 * 1024,
        }
    )
    if capabilities["max_l2m_size"] != expected_l2m:
        raise ValueError("target profile max_l2m_size capability was not recognized")

    inputs = config["inputs"]
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 4:
        raise ValueError("OpenExplorer 3.7 supports one to four configured inputs")
    input_names: set[str] = set()
    input_geometries: list[tuple[int | None, int | None, int | None]] = []
    for index, raw_input in enumerate(inputs):
        field = f"inputs[{index}]"
        input_config = _expect_object(raw_input, field)
        _expect_exact_fields(
            input_config,
            {
                "name",
                "target_shape",
                "train_type",
                "train_layout",
                "runtime_type",
                "normalization",
            },
            field,
        )
        name = input_config["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{field}.name must be a non-empty string")
        if any(character in name for character in ";\r\n"):
            raise ValueError(f"{field}.name contains unsupported characters")
        if name in input_names:
            raise ValueError("input names must be unique")
        input_names.add(name)
        shape = input_config["target_shape"]
        if (
            not isinstance(shape, list)
            or not 1 <= len(shape) <= 4
            or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in shape
            )
        ):
            raise ValueError(f"{field}.target_shape must contain 1 to 4 positive integers")
        if shape[0] != 1:
            raise ValueError(f"{field}.target_shape currently requires batch 1")
        layout = input_config["train_layout"]
        if layout not in {"NCHW", "NHWC"}:
            raise ValueError(f"{field}.train_layout must be NCHW or NHWC")
        train_type = input_config["train_type"]
        if train_type not in SUPPORTED_TRAIN_TYPES:
            raise ValueError(f"{field}.train_type is unsupported")
        runtime_type = input_config["runtime_type"]
        if runtime_type not in SUPPORTED_RUNTIME_TYPES:
            raise ValueError(f"{field}.runtime_type is unsupported")
        channels = None
        height = None
        width = None
        if len(shape) == 4:
            channels = shape[1] if layout == "NCHW" else shape[3]
            height = shape[2] if layout == "NCHW" else shape[1]
            width = shape[3] if layout == "NCHW" else shape[2]
        if train_type != "featuremap":
            if len(shape) != 4:
                raise ValueError(f"{field} image-like input must have rank four")
            expected_channels = 1 if train_type == "gray" else 3
            if channels != expected_channels:
                raise ValueError(
                    f"{field} {train_type}/{layout} requires {expected_channels} channels"
                )
        if runtime_type == "nv12" and (
            channels != 3 or height is None or width is None or height % 2 or width % 2
        ):
            raise ValueError(f"{field} NV12 requires three channels and even height/width")
        normalization = _expect_object(input_config["normalization"], f"{field}.normalization")
        _expect_exact_fields(normalization, {"mean", "scale", "std"}, f"{field}.normalization")
        allowed_normalization_lengths = {0, 1}
        if channels is not None:
            allowed_normalization_lengths.add(channels)
        for normalization_field in ("mean", "scale", "std"):
            _numeric_list(
                normalization[normalization_field],
                f"{field}.normalization.{normalization_field}",
                allowed_lengths=allowed_normalization_lengths,
            )
        input_geometries.append((channels, height, width))

    calibration = _expect_object(config["calibration"], "calibration")
    _expect_exact_fields(
        calibration,
        {"source_type", "algorithm", "sample_limit", "recipe"},
        "calibration",
    )
    if calibration["source_type"] not in {"images", "npy", "npy_multi"}:
        raise ValueError("calibration.source_type must be images, npy, or npy_multi")
    if calibration["algorithm"] not in SUPPORTED_CALIBRATION_ALGORITHMS:
        raise ValueError("unsupported calibration algorithm")
    if (
        isinstance(calibration["sample_limit"], bool)
        or not isinstance(calibration["sample_limit"], int)
        or not 20 <= calibration["sample_limit"] <= 100
    ):
        raise ValueError("calibration.sample_limit must be between 20 and 100")
    if calibration["source_type"] == "images":
        if len(inputs) != 1:
            raise ValueError("image calibration supports exactly one model input")
        if inputs[0]["train_type"] not in {"rgb", "bgr", "gray"}:
            raise ValueError("image calibration requires rgb, bgr, or gray train_type")
        channels, height, width = input_geometries[0]
        if channels is None or height is None or width is None:
            raise ValueError("image calibration requires a four-dimensional input")
        recipe = _expect_object(calibration["recipe"], "calibration.recipe")
        _expect_exact_fields(
            recipe,
            {"id", "version", "resize_short", "crop_size", "mean", "std"},
            "calibration.recipe",
        )
        if (
            recipe["id"] not in {"imagenet-resnet18", "image-center-crop"}
            or recipe["version"] != "1"
        ):
            raise ValueError("unsupported calibration recipe")
        if (
            isinstance(recipe["resize_short"], bool)
            or not isinstance(recipe["resize_short"], int)
            or recipe["resize_short"] < 1
        ):
            raise ValueError("calibration.recipe.resize_short must be positive")
        if recipe["crop_size"] != [height, width]:
            raise ValueError("calibration crop_size must match the target input height and width")
        _numeric_list(recipe["mean"], "calibration.recipe.mean", allowed_lengths={channels})
        std = _numeric_list(recipe["std"], "calibration.recipe.std", allowed_lengths={channels})
        if any(item == 0 for item in std):
            raise ValueError("calibration.recipe.std cannot contain zero")
    elif calibration["source_type"] == "npy" and len(inputs) != 1:
        raise ValueError("direct NPY calibration supports exactly one model input")
    elif calibration["source_type"] == "npy_multi" and len(inputs) < 2:
        raise ValueError("multi-input NPY calibration requires at least two model inputs")
    elif calibration["recipe"] is not None:
        raise ValueError("direct NPY calibration must not include a recipe")

    compiler = _expect_object(config["compiler"], "compiler")
    _expect_exact_fields(
        compiler,
        {
            "compile_mode",
            "balance_factor",
            "core_num",
            "optimize_level",
            "max_l2m_size",
            "max_time_per_fc",
            "jobs",
            "cache_mode",
            "cache_key",
        },
        "compiler",
    )
    if compiler["compile_mode"] not in SUPPORTED_COMPILE_MODES:
        raise ValueError("unsupported compile_mode")
    balance_factor = compiler["balance_factor"]
    if compiler["compile_mode"] == "balance":
        if isinstance(balance_factor, bool) or not isinstance(balance_factor, int):
            raise ValueError("balance compile mode requires an integer balance_factor")
        if not 0 <= balance_factor <= 100:
            raise ValueError("balance_factor must be between 0 and 100")
    elif balance_factor is not None:
        raise ValueError("balance_factor must be null unless compile_mode is balance")
    if isinstance(compiler["core_num"], bool) or compiler["core_num"] not in expected[3]:
        raise ValueError(f"core_num is not supported by {platform}")
    if compiler["optimize_level"] not in SUPPORTED_OPTIMIZE_LEVELS:
        raise ValueError("unsupported optimize_level")
    l2m = compiler["max_l2m_size"]
    if platform == "s100" and (isinstance(l2m, bool) or l2m != 0):
        raise ValueError("S100 requires max_l2m_size=0")
    if (
        platform == "s600"
        and l2m != "auto"
        and (isinstance(l2m, bool) or not isinstance(l2m, int) or not 0 <= l2m <= 24 * 1024 * 1024)
    ):
        raise ValueError("S600 max_l2m_size must be auto or between 0 and 24 MiB")
    for field, minimum, maximum in (("max_time_per_fc", 0, 2**31 - 1), ("jobs", 1, 128)):
        value = compiler[field]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"compiler.{field} is outside the supported range")
    if compiler["cache_mode"] not in {"disable", "enable", "force_overwrite"}:
        raise ValueError("unsupported cache_mode")
    cache_key = compiler["cache_key"]
    if compiler["cache_mode"] == "disable":
        if cache_key is not None:
            raise ValueError("compiler.cache_key must be null when cache is disabled")
    elif not isinstance(cache_key, str) or not re.fullmatch(r"[a-f0-9]{64}", cache_key):
        raise ValueError("compiler.cache_key must be a lowercase SHA-256 digest")

    verification = _expect_object(config["verification"], "verification")
    _expect_exact_fields(verification, {"mode", "compare_digits"}, "verification")
    if verification["mode"] not in {"disabled", "basic"}:
        raise ValueError("verification.mode must be disabled or basic")
    compare_digits = verification["compare_digits"]
    if (
        isinstance(compare_digits, bool)
        or not isinstance(compare_digits, int)
        or not 1 <= compare_digits <= 12
    ):
        raise ValueError("verification.compare_digits must be between 1 and 12")
    return config


def _format_sequence(values: list[Any]) -> str:
    return " ".join(str(value) for value in values)


def _format_per_input(values: list[str]) -> str:
    return values[0] if len(values) == 1 else ";".join(values)


def render_openexplorer_config(
    configuration: dict[str, Any],
    *,
    model_path: Path,
    calibration_dir: Path,
    working_dir: Path,
) -> dict[str, Any]:
    config = validate_configuration(configuration)
    profile = config["target_profile"]["profile"]
    inputs = config["inputs"]
    compiler = config["compiler"]
    input_parameters: dict[str, Any] = {
        "input_name": _format_per_input([item["name"] for item in inputs]),
        "input_type_rt": _format_per_input([item["runtime_type"] for item in inputs]),
        "input_type_train": _format_per_input([item["train_type"] for item in inputs]),
        "input_layout_train": _format_per_input([item["train_layout"] for item in inputs]),
        "input_shape": _format_per_input(
            ["x".join(str(value) for value in item["target_shape"]) for item in inputs]
        ),
    }
    for source, target in (("mean", "mean_value"), ("scale", "scale_value"), ("std", "std_value")):
        values = [_format_sequence(item["normalization"][source]) for item in inputs]
        if any(values):
            input_parameters[target] = _format_per_input(values)

    compiler_parameters: dict[str, Any] = {
        "compile_mode": compiler["compile_mode"],
        "core_num": compiler["core_num"],
        "optimize_level": compiler["optimize_level"],
        "max_l2m_size": None if compiler["max_l2m_size"] == "auto" else compiler["max_l2m_size"],
        "max_time_per_fc": compiler["max_time_per_fc"],
        "jobs": compiler["jobs"],
        "cache_mode": compiler["cache_mode"],
    }
    if compiler["cache_key"] is not None:
        compiler_parameters["cache_path"] = f"/cache/compiler/{compiler['cache_key']}"
    if compiler["balance_factor"] is not None:
        compiler_parameters["balance_factor"] = compiler["balance_factor"]

    return {
        "model_parameters": {
            "onnx_model": str(model_path),
            "march": profile["march"],
            "working_dir": str(working_dir),
            "output_model_file_prefix": config["output_prefix"],
        },
        "input_parameters": input_parameters,
        "calibration_parameters": {
            "cal_data_dir": (
                _format_per_input([str(calibration_dir / item["name"]) for item in inputs])
                if config["calibration"]["source_type"] == "npy_multi"
                else str(calibration_dir)
            ),
            "calibration_type": config["calibration"]["algorithm"],
        },
        "compiler_parameters": compiler_parameters,
    }


def parse_static_metrics(payload: Any) -> dict[str, Any]:
    root = _expect_object(payload, "static performance payload")
    summary = _expect_object(root.get("summary"), "static performance summary")
    model_info = _expect_object(summary.get("model info"), "static performance model info")
    performance = _expect_object(summary.get("performance"), "static performance metrics")
    ddr = _expect_object(summary.get("DDR access data"), "static performance memory metrics")
    return {
        "march": model_info.get("BPU march"),
        "core_num": model_info.get("BPU core number"),
        "fps": performance.get("FPS"),
        "latency_us": performance.get("latency (us)"),
        "ddr_bytes_per_run": ddr.get("DDR bytes per run"),
        "l2m_bytes_per_run": ddr.get("L2M bytes per run"),
        "minimum_memory_bytes": ddr.get("min memory requirement"),
    }


def _table_fields(raw_line: str) -> list[str]:
    first = raw_line.find("|")
    last = raw_line.rfind("|")
    if first < 0 or last <= first:
        return []
    return [item.strip() for item in raw_line[first + 1 : last].split("|")]


def parse_quantized_cosines(log_text: str) -> dict[str, Any]:
    section: str | None = None
    nodes: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    for raw_line in log_text.splitlines():
        if "NodeType" in raw_line and "Quantized Cosine" in raw_line:
            section = "nodes"
            continue
        if "TensorName" in raw_line and "Quantized Cosine" in raw_line:
            section = "outputs"
            continue
        if section is None or "|" not in raw_line:
            continue
        fields = _table_fields(raw_line)
        try:
            if section == "nodes" and len(fields) == 7:
                cosine = float(fields[5])
                nodes.append(
                    {
                        "name": fields[0],
                        "type": fields[1],
                        "device": fields[2],
                        "quantized_cosine": cosine,
                    }
                )
            elif section == "outputs" and len(fields) == 3:
                outputs.append({"name": fields[0], "quantized_cosine": float(fields[2])})
        except ValueError:
            continue
    minimum_node = min(nodes, key=lambda item: item["quantized_cosine"]) if nodes else None
    return {
        "output_cosines": outputs,
        "minimum_node": minimum_node,
        "node_count": len(nodes),
        "reference_only": True,
    }


def parse_verifier_metrics(log_text: str) -> dict[str, Any]:
    cosines: list[dict[str, Any]] = []
    consistency: list[dict[str, Any]] = []
    for raw_line in log_text.splitlines():
        if "|" not in raw_line:
            continue
        fields = _table_fields(raw_line)
        if len(fields) == 3:
            try:
                cosine = float(fields[2])
            except ValueError:
                continue
            cosines.append(
                {
                    "node_name": fields[0],
                    "tensor_name": fields[1],
                    "cosine_similarity": cosine,
                }
            )
        elif len(fields) == 5 and fields[1].lower() in {"true", "false"}:
            try:
                max_abs_diff = float(fields[3])
                max_rel_diff = float(fields[4])
            except ValueError:
                continue
            consistency.append(
                {
                    "output_name": fields[0],
                    "consistent": fields[1].lower() == "true",
                    "mismatched_elements": fields[2],
                    "max_abs_diff": max_abs_diff,
                    "max_rel_diff": max_rel_diff,
                }
            )
    minimum = min(cosines, key=lambda item: item["cosine_similarity"]) if cosines else None
    return {
        "cosines": cosines,
        "minimum_cosine": minimum,
        "consistency": consistency,
    }


def _build_hb_verifier_command(
    *,
    optimized: Path,
    calibrated: Path,
    input_paths: list[Path],
    compare_digits: int,
) -> list[str]:
    command = [
        "hb_verifier",
        "--model",
        f"{optimized},{calibrated}",
    ]
    for input_path in input_paths:
        command.extend(["--input", str(input_path)])
    command.extend(["--compare_digits", str(compare_digits)])
    return command


class OpenExplorer370Adapter:
    implemented_steps = frozenset({"inspect", "check", "preprocess", "compile", "verify"})

    def __init__(
        self,
        *,
        request: dict[str, Any],
        model_path: Path,
        calibration_source: Path,
        attempt_root: Path,
        is_cancel_requested: Callable[[], bool],
    ) -> None:
        self.request = request
        try:
            self.configuration = validate_configuration(request["configuration"])
        except ValueError as exc:
            raise AdapterExecutionError(
                "CONFIG_INVALID", f"invalid normalized configuration: {exc}", step="runner"
            ) from exc
        self.model_path = model_path
        self.calibration_source = calibration_source
        self.attempt_root = attempt_root
        self.work_root = attempt_root / "work"
        self.check_root = self.work_root / "check"
        self.calibration_root = self.work_root / "calibration" / "input"
        self.output_root = self.work_root / "model_output"
        self.logs_root = attempt_root / "logs"
        self.generated_config = attempt_root / "generated.yaml"
        self.inspection_path = self.work_root / "model-inspection.json"
        self.calibration_manifest_path = self.work_root / "calibration-manifest.json"
        self.calibration_preview_path = self.work_root / "calibration-preview.png"
        self.is_cancel_requested = is_cancel_requested
        self.max_log_bytes = int(request["limits"]["max_log_bytes"])
        self.timeout_seconds = int(request["limits"]["timeout_seconds"])

    @property
    def output_prefix(self) -> str:
        return str(self.configuration["output_prefix"])

    def run_step(self, step: str) -> dict[str, Any]:
        operation = {
            "inspect": self.inspect,
            "check": self.check,
            "preprocess": self.preprocess,
            "compile": self.compile,
            "verify": self.verify,
        }.get(step)
        if operation is None:
            raise ValueError(f"unsupported OpenExplorer adapter step: {step}")
        return operation()

    def inspect(self) -> dict[str, Any]:
        try:
            details = inspect_onnx(self.model_path)
        except ValueError as exc:
            raise AdapterExecutionError("MODEL_PARSE_FAILED", str(exc), step="inspect") from exc
        atomic_write_json(self.inspection_path, details)
        blockers = details["blockers"]
        if blockers:
            blocker = blockers[0]
            raise AdapterExecutionError(
                str(blocker["code"]),
                str(blocker["message"]),
                step="inspect",
                details={"blockers": blockers},
            )
        return details

    def check(self) -> dict[str, Any]:
        self.check_root.mkdir(parents=True, exist_ok=True)
        profile = self.configuration["target_profile"]["profile"]
        command = [
            "hb_compile",
            "--model",
            str(self.model_path),
            "--march",
            profile["march"],
        ]
        for input_config in self.configuration["inputs"]:
            command.extend(
                [
                    "--input-shape",
                    input_config["name"],
                    "x".join(str(item) for item in input_config["target_shape"]),
                ]
            )
        result = self._run_command(command, cwd=self.check_root, log_name="check.log", step="check")
        return {"march": profile["march"], **result}

    def preprocess(self) -> dict[str, Any]:
        source_type = self.configuration["calibration"]["source_type"]
        if source_type == "npy_multi":
            return self._preprocess_npy_multi()
        if source_type == "npy":
            return self._preprocess_npy()
        return self._preprocess_images()

    def _preprocess_images(self) -> dict[str, Any]:
        try:
            import numpy as np
            from PIL import Image
        except ImportError as exc:
            raise AdapterExecutionError(
                "CALIBRATION_PREPROCESS_FAILED",
                f"preprocessing dependency unavailable: {exc}",
                step="preprocess",
            ) from exc

        if not self.calibration_source.is_dir() or self.calibration_source.is_symlink():
            raise AdapterExecutionError(
                "CALIBRATION_INVALID_SAMPLE",
                "calibration source must be a regular directory",
                step="preprocess",
            )
        candidates = []
        for path in sorted(self.calibration_source.iterdir(), key=lambda item: item.name):
            if path.is_symlink():
                raise AdapterExecutionError(
                    "CALIBRATION_INVALID_SAMPLE",
                    f"symbolic links are not allowed in calibration data: {path.name}",
                    step="preprocess",
                )
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES:
                candidates.append(path)
        sample_limit = int(self.configuration["calibration"]["sample_limit"])
        selected = candidates[:sample_limit]
        if len(selected) < 20:
            raise AdapterExecutionError(
                "CALIBRATION_INVALID_SAMPLE",
                f"at least 20 supported calibration images are required; found {len(selected)}",
                step="preprocess",
            )

        recipe = self.configuration["calibration"]["recipe"]
        input_config = self.configuration["inputs"][0]
        crop_height, crop_width = recipe["crop_size"]
        channels = 1 if input_config["train_type"] == "gray" else 3
        stat_shape = (
            (channels, 1, 1) if input_config["train_layout"] == "NCHW" else (1, 1, channels)
        )
        mean = np.asarray(recipe["mean"], dtype=np.float32).reshape(stat_shape)
        std = np.asarray(recipe["std"], dtype=np.float32).reshape(stat_shape)
        self.calibration_root.mkdir(parents=True, exist_ok=True)
        samples = []
        for index, source in enumerate(selected):
            if self.is_cancel_requested():
                raise InterruptedError("runner cancellation requested")
            try:
                with Image.open(source) as opened:
                    image = opened.convert("L" if input_config["train_type"] == "gray" else "RGB")
                    width, height = image.size
                    short = int(recipe["resize_short"])
                    if width <= height:
                        resized = (short, max(short, round(height * short / width)))
                    else:
                        resized = (max(short, round(width * short / height)), short)
                    image = image.resize(resized, Image.Resampling.BILINEAR)
                    left = (image.width - crop_width) // 2
                    top = (image.height - crop_height) // 2
                    if left < 0 or top < 0:
                        raise ValueError("resized image is smaller than the configured crop")
                    image = image.crop((left, top, left + crop_width, top + crop_height))
                    if index == 0:
                        image.save(self.calibration_preview_path, format="PNG")
                    array = np.asarray(image, dtype=np.float32)
                    if array.ndim == 2:
                        array = array[:, :, None]
                    if input_config["train_type"] == "bgr":
                        array = array[:, :, ::-1]
                    if input_config["train_layout"] == "NCHW":
                        array = array.transpose(2, 0, 1)
                    array = (array / np.float32(255.0) - mean) / std
            except Exception as exc:
                raise AdapterExecutionError(
                    "CALIBRATION_PREPROCESS_FAILED",
                    f"failed to preprocess calibration image {source.name}: {exc}",
                    step="preprocess",
                    details={"sample": source.name},
                ) from exc
            source_digest = sha256_file(source)
            output = self.calibration_root / (
                f"{index:04d}_{source_digest[:12]}.{input_config['train_type']}.npy"
            )
            np.save(output, array.astype(np.float32, copy=False), allow_pickle=False)
            samples.append(
                {
                    "index": index,
                    "source_name": source.name,
                    "source_sha256": source_digest,
                    "output_name": output.name,
                    "output_sha256": sha256_file(output),
                    "shape": list(array.shape),
                    "dtype": "float32",
                    "minimum": float(array.min()),
                    "maximum": float(array.max()),
                    "mean": float(array.mean()),
                }
            )

        manifest = {
            "schema_version": "1",
            "source_type": "images",
            "recipe": recipe,
            "sample_count": len(samples),
            "samples": samples,
        }
        atomic_write_json(self.calibration_manifest_path, manifest)
        generated = render_openexplorer_config(
            self.configuration,
            model_path=self.model_path,
            calibration_dir=self.calibration_root,
            working_dir=self.output_root,
        )
        self._validate_generated_paths(generated)
        try:
            import yaml

            rendered = yaml.safe_dump(generated, sort_keys=False, allow_unicode=True)
            if yaml.safe_load(rendered) != generated:
                raise ValueError("YAML round-trip changed the generated configuration")
        except Exception as exc:
            raise AdapterExecutionError(
                "CONFIG_INVALID", f"failed to render OpenExplorer YAML: {exc}", step="preprocess"
            ) from exc
        atomic_write_text(self.generated_config, rendered)
        return {
            "sample_count": len(samples),
            "calibration_dir": self.calibration_root.relative_to(self.attempt_root).as_posix(),
            "configuration": self.generated_config.relative_to(self.attempt_root).as_posix(),
            "preview": self.calibration_preview_path.relative_to(self.attempt_root).as_posix(),
            "first_sample_statistics": {
                key: samples[0][key] for key in ("shape", "dtype", "minimum", "maximum", "mean")
            },
        }

    def _preprocess_npy(self) -> dict[str, Any]:
        try:
            import numpy as np
        except ImportError as exc:
            raise AdapterExecutionError(
                "CALIBRATION_PREPROCESS_FAILED",
                f"NPY validation dependency unavailable: {exc}",
                step="preprocess",
            ) from exc
        if not self.calibration_source.is_dir() or self.calibration_source.is_symlink():
            raise AdapterExecutionError(
                "CALIBRATION_INVALID_SAMPLE",
                "calibration source must be a regular directory",
                step="preprocess",
            )
        candidates: list[Path] = []
        for path in sorted(self.calibration_source.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                raise AdapterExecutionError(
                    "CALIBRATION_INVALID_SAMPLE",
                    f"direct NPY calibration contains a non-regular file: {path.name}",
                    step="preprocess",
                )
            if path.suffix.lower() != ".npy":
                raise AdapterExecutionError(
                    "CALIBRATION_INVALID_SAMPLE",
                    f"direct NPY calibration contains a non-NPY file: {path.name}",
                    step="preprocess",
                )
            candidates.append(path)
        sample_limit = int(self.configuration["calibration"]["sample_limit"])
        selected = candidates[:sample_limit]
        if len(selected) < 20:
            raise AdapterExecutionError(
                "CALIBRATION_INVALID_SAMPLE",
                f"at least 20 NPY calibration samples are required; found {len(selected)}",
                step="preprocess",
            )
        expected_shape = self.configuration["inputs"][0]["target_shape"][1:]
        allowed_dtypes = {
            "bool",
            "int8",
            "int16",
            "int32",
            "uint8",
            "uint16",
            "uint32",
            "float16",
            "float32",
            "float64",
        }
        self.calibration_root.mkdir(parents=True, exist_ok=True)
        samples: list[dict[str, Any]] = []
        for index, source in enumerate(selected):
            if self.is_cancel_requested():
                raise InterruptedError("runner cancellation requested")
            try:
                array = _load_npy(np, source)
                if not isinstance(array, np.ndarray):
                    raise ValueError("file does not contain one array")
                dtype = array.dtype
                if (
                    dtype.name not in allowed_dtypes
                    or dtype.hasobject
                    or dtype.fields
                    or dtype.subdtype
                ):
                    raise ValueError(f"unsupported dtype {array.dtype}")
                if dtype.byteorder == ">" or (dtype.byteorder == "=" and not np.little_endian):
                    raise ValueError("big-endian arrays are not supported")
                if list(array.shape) != expected_shape:
                    raise ValueError(f"Shape {list(array.shape)} does not match {expected_shape}")
                if not array.flags.c_contiguous:
                    raise ValueError("Fortran-order arrays are not supported")
                offset = int(getattr(array, "offset", 0))
                if offset < 1 or offset + int(array.nbytes) != source.stat().st_size:
                    raise ValueError("payload size does not match the NPY header")
                flattened = array.reshape(-1)
                minimum = float("inf")
                maximum = float("-inf")
                total = 0.0
                for start in range(0, int(array.size), 1024 * 1024):
                    values = np.asarray(flattened[start : start + 1024 * 1024], dtype=np.float64)
                    if not np.isfinite(values).all():
                        raise ValueError("array contains NaN or infinite values")
                    minimum = min(minimum, float(values.min()))
                    maximum = max(maximum, float(values.max()))
                    with np.errstate(over="ignore", invalid="ignore"):
                        total += float(values.sum(dtype=np.float64))
                    if not np.isfinite(total):
                        raise ValueError("array values are too large for safe finite statistics")
            except (OSError, TypeError, ValueError) as exc:
                raise AdapterExecutionError(
                    "CALIBRATION_NPY_INVALID",
                    f"failed to validate direct NPY sample {source.name}: {exc}",
                    step="preprocess",
                    details={"sample": source.name},
                ) from exc
            source_digest = sha256_file(source)
            output = self.calibration_root / f"{index:04d}_{source_digest[:12]}.npy"
            shutil.copyfile(source, output, follow_symlinks=False)
            output_digest = sha256_file(output)
            if output_digest != source_digest:
                raise AdapterExecutionError(
                    "CALIBRATION_NPY_INVALID",
                    f"copied NPY sample failed hash validation: {source.name}",
                    step="preprocess",
                )
            samples.append(
                {
                    "index": index,
                    "source_name": source.name,
                    "source_sha256": source_digest,
                    "output_name": output.name,
                    "output_sha256": output_digest,
                    "shape": list(array.shape),
                    "dtype": array.dtype.name,
                    "minimum": minimum,
                    "maximum": maximum,
                    "mean": total / int(array.size),
                }
            )

        manifest = {
            "schema_version": "1",
            "source_type": "npy",
            "recipe": None,
            "sample_count": len(samples),
            "samples": samples,
        }
        atomic_write_json(self.calibration_manifest_path, manifest)
        generated = render_openexplorer_config(
            self.configuration,
            model_path=self.model_path,
            calibration_dir=self.calibration_root,
            working_dir=self.output_root,
        )
        self._validate_generated_paths(generated)
        try:
            import yaml

            rendered = yaml.safe_dump(generated, sort_keys=False, allow_unicode=True)
            if yaml.safe_load(rendered) != generated:
                raise ValueError("YAML round-trip changed the generated configuration")
        except Exception as exc:
            raise AdapterExecutionError(
                "CONFIG_INVALID",
                f"failed to render OpenExplorer YAML: {exc}",
                step="preprocess",
            ) from exc
        atomic_write_text(self.generated_config, rendered)
        return {
            "source_type": "npy",
            "sample_count": len(samples),
            "calibration_dir": self.calibration_root.relative_to(self.attempt_root).as_posix(),
            "configuration": self.generated_config.relative_to(self.attempt_root).as_posix(),
            "preview": None,
            "first_sample_statistics": {
                key: samples[0][key] for key in ("shape", "dtype", "minimum", "maximum", "mean")
            },
        }

    def _preprocess_npy_multi(self) -> dict[str, Any]:
        try:
            import numpy as np
        except ImportError as exc:
            raise AdapterExecutionError(
                "CALIBRATION_PREPROCESS_FAILED",
                f"NPY validation dependency unavailable: {exc}",
                step="preprocess",
            ) from exc
        if not self.calibration_source.is_dir() or self.calibration_source.is_symlink():
            raise AdapterExecutionError(
                "CALIBRATION_INVALID_SAMPLE",
                "multi-input calibration source must be a regular directory",
                step="preprocess",
            )
        configured_inputs = self.configuration["inputs"]
        expected_names = [item["name"] for item in configured_inputs]
        actual_names = sorted(
            path.name
            for path in self.calibration_source.iterdir()
            if path.is_dir() and not path.is_symlink()
        )
        if sorted(expected_names) != actual_names:
            raise AdapterExecutionError(
                "CALIBRATION_NPY_INVALID",
                "multi-input calibration directories do not match model input names",
                step="preprocess",
                details={"expected": expected_names, "actual": actual_names},
            )
        sample_limit = int(self.configuration["calibration"]["sample_limit"])
        input_files: dict[str, dict[str, Path]] = {}
        for name in expected_names:
            source_dir = self.calibration_source / name
            files: dict[str, Path] = {}
            for path in source_dir.iterdir():
                if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".npy":
                    raise AdapterExecutionError(
                        "CALIBRATION_INVALID_SAMPLE",
                        f"multi-input calibration contains an invalid file: {name}/{path.name}",
                        step="preprocess",
                    )
                files[path.name] = path
            input_files[name] = files
        sample_keys = set(next(iter(input_files.values())))
        if any(set(files) != sample_keys for files in input_files.values()):
            raise AdapterExecutionError(
                "CALIBRATION_NPY_INVALID",
                "every model input must provide the same named calibration samples",
                step="preprocess",
            )
        selected_keys = sorted(sample_keys)[:sample_limit]
        if len(selected_keys) < 20:
            raise AdapterExecutionError(
                "CALIBRATION_INVALID_SAMPLE",
                f"at least 20 aligned multi-input samples are required; found {len(selected_keys)}",
                step="preprocess",
            )

        self.calibration_root.mkdir(parents=True, exist_ok=True)
        allowed_dtypes = {
            "bool",
            "int8",
            "int16",
            "int32",
            "uint8",
            "uint16",
            "uint32",
            "float16",
            "float32",
            "float64",
        }
        inputs_manifest: list[dict[str, Any]] = []
        for input_config in configured_inputs:
            name = input_config["name"]
            expected_shape = input_config["target_shape"][1:]
            output_dir = self.calibration_root / name
            output_dir.mkdir()
            samples: list[dict[str, Any]] = []
            dtype_name: str | None = None
            for index, sample_key in enumerate(selected_keys):
                if self.is_cancel_requested():
                    raise InterruptedError("runner cancellation requested")
                source = input_files[name][sample_key]
                try:
                    array = _load_npy(np, source)
                    if not isinstance(array, np.ndarray):
                        raise ValueError("file does not contain one array")
                    dtype = array.dtype
                    if (
                        dtype.name not in allowed_dtypes
                        or dtype.hasobject
                        or dtype.fields
                        or dtype.subdtype
                    ):
                        raise ValueError(f"unsupported dtype {dtype}")
                    if dtype.byteorder == ">" or (dtype.byteorder == "=" and not np.little_endian):
                        raise ValueError("big-endian arrays are not supported")
                    if list(array.shape) != expected_shape:
                        raise ValueError(
                            f"Shape {list(array.shape)} does not match {expected_shape}"
                        )
                    if not array.flags.c_contiguous:
                        raise ValueError("Fortran-order arrays are not supported")
                    offset = int(getattr(array, "offset", 0))
                    if offset < 1 or offset + int(array.nbytes) != source.stat().st_size:
                        raise ValueError("payload size does not match the NPY header")
                    flattened = array.reshape(-1)
                    minimum = float("inf")
                    maximum = float("-inf")
                    total = 0.0
                    for start in range(0, int(array.size), 1024 * 1024):
                        values = np.asarray(
                            flattened[start : start + 1024 * 1024],
                            dtype=np.float64,
                        )
                        if not np.isfinite(values).all():
                            raise ValueError("array contains NaN or infinite values")
                        minimum = min(minimum, float(values.min()))
                        maximum = max(maximum, float(values.max()))
                        with np.errstate(over="ignore", invalid="ignore"):
                            total += float(values.sum(dtype=np.float64))
                        if not np.isfinite(total):
                            raise ValueError("array statistics are not finite")
                    if dtype_name is None:
                        dtype_name = dtype.name
                    elif dtype_name != dtype.name:
                        raise ValueError(
                            f"dtype {dtype.name} differs from {dtype_name} within input {name}"
                        )
                except (OSError, TypeError, ValueError) as exc:
                    raise AdapterExecutionError(
                        "CALIBRATION_NPY_INVALID",
                        f"failed to validate {name}/{sample_key}: {exc}",
                        step="preprocess",
                    ) from exc
                source_digest = sha256_file(source)
                output = output_dir / f"{index:04d}_{source_digest[:12]}.npy"
                shutil.copyfile(source, output, follow_symlinks=False)
                if sha256_file(output) != source_digest:
                    raise AdapterExecutionError(
                        "CALIBRATION_NPY_INVALID",
                        f"copied sample failed hash validation: {name}/{sample_key}",
                        step="preprocess",
                    )
                samples.append(
                    {
                        "index": index,
                        "sample_key": sample_key,
                        "source_sha256": source_digest,
                        "output_name": output.name,
                        "shape": list(array.shape),
                        "dtype": array.dtype.name,
                        "minimum": minimum,
                        "maximum": maximum,
                        "mean": total / int(array.size),
                    }
                )
            inputs_manifest.append(
                {
                    "name": name,
                    "shape": expected_shape,
                    "dtype": dtype_name,
                    "samples": samples,
                }
            )

        manifest = {
            "schema_version": "1",
            "source_type": "npy_multi",
            "recipe": None,
            "sample_count": len(selected_keys),
            "inputs": inputs_manifest,
        }
        atomic_write_json(self.calibration_manifest_path, manifest)
        generated = render_openexplorer_config(
            self.configuration,
            model_path=self.model_path,
            calibration_dir=self.calibration_root,
            working_dir=self.output_root,
        )
        self._validate_generated_paths(generated)
        try:
            import yaml

            rendered = yaml.safe_dump(generated, sort_keys=False, allow_unicode=True)
            if yaml.safe_load(rendered) != generated:
                raise ValueError("YAML round-trip changed the generated configuration")
        except Exception as exc:
            raise AdapterExecutionError(
                "CONFIG_INVALID",
                f"failed to render OpenExplorer YAML: {exc}",
                step="preprocess",
            ) from exc
        atomic_write_text(self.generated_config, rendered)
        return {
            "source_type": "npy_multi",
            "sample_count": len(selected_keys),
            "input_count": len(inputs_manifest),
            "inputs": [
                {
                    "name": item["name"],
                    "shape": item["shape"],
                    "dtype": item["dtype"],
                }
                for item in inputs_manifest
            ],
            "configuration": self.generated_config.relative_to(self.attempt_root).as_posix(),
            "preview": None,
        }

    def compile(self) -> dict[str, Any]:
        if not self.generated_config.is_file():
            raise AdapterExecutionError(
                "CONFIG_INVALID",
                "generated YAML is missing; preprocess must run first",
                step="compile",
            )
        self.output_root.mkdir(parents=True, exist_ok=True)
        cache_key = self.configuration["compiler"]["cache_key"]
        cache_root: Path | None = None
        cache_warm_before = False
        if cache_key is not None:
            cache_root = Path("/cache/compiler") / cache_key
            try:
                cache_root.mkdir(parents=True, exist_ok=True)
                cache_warm_before = any(path.is_file() for path in cache_root.rglob("*"))
            except OSError as exc:
                raise AdapterExecutionError(
                    "CACHE_UNAVAILABLE",
                    f"cannot prepare the compiler cache: {exc}",
                    step="compile",
                ) from exc
        command = ["hb_compile", "--config", str(self.generated_config)]
        command_result = self._run_command(
            command, cwd=self.attempt_root, log_name="compile.log", step="compile"
        )
        hbm = self.output_root / f"{self.output_prefix}.hbm"
        if not hbm.is_file() or hbm.is_symlink():
            raise AdapterExecutionError(
                "ARTIFACT_HBM_MISSING",
                "OpenExplorer did not produce the expected HBM",
                step="compile",
            )
        metrics = self._parse_static_metrics()
        quantization = parse_quantized_cosines(
            (self.logs_root / "compile.log").read_text(encoding="utf-8", errors="replace")
        )
        profile = self.configuration["target_profile"]["profile"]
        compiler = self.configuration["compiler"]
        if metrics.get("march") not in {None, profile["march"]}:
            raise AdapterExecutionError(
                "TOOL_COMPILE_FAILED",
                "compiled artifact march does not match the target profile",
                step="compile",
                details={"expected": profile["march"], "actual": metrics.get("march")},
            )
        if metrics.get("core_num") not in {None, compiler["core_num"]}:
            raise AdapterExecutionError(
                "TOOL_COMPILE_FAILED",
                "compiled artifact core count does not match the normalized configuration",
                step="compile",
                details={"expected": compiler["core_num"], "actual": metrics.get("core_num")},
            )
        compile_log = (self.logs_root / "compile.log").read_text(encoding="utf-8", errors="replace")
        cache_key = compiler["cache_key"]
        cache_hit = bool(
            cache_key
            and compiler["cache_mode"] == "enable"
            and (
                cache_warm_before
                or re.search(
                    r"(?:cache[^\n]{0,80}(?:hit|reuse|reused)|(?:hit|reuse)[^\n]{0,80}cache)",
                    compile_log,
                    re.IGNORECASE,
                )
            )
        )
        cache_files_after = (
            sum(1 for path in cache_root.rglob("*") if path.is_file())
            if cache_root is not None
            else 0
        )
        return {
            **command_result,
            "hbm_size_bytes": hbm.stat().st_size,
            "hbm_sha256": sha256_file(hbm),
            "static_performance": metrics,
            "quantization": quantization,
            "cache": {
                "mode": compiler["cache_mode"],
                "key": cache_key,
                "hit": cache_hit,
                "warm_before": cache_warm_before,
                "file_count_after": cache_files_after,
            },
        }

    def verify(self) -> dict[str, Any]:
        verification = self.configuration["verification"]
        if verification["mode"] == "disabled":
            return {"enabled": False, "mode": "disabled"}
        try:
            import numpy as np
            from horizon_tc_ui.hb_runtime import HBRuntime
        except ImportError as exc:
            raise AdapterExecutionError(
                "HBRUNTIME_UNAVAILABLE",
                f"HBRuntime dependency unavailable: {exc}",
                step="verify",
            ) from exc

        optimized = self.output_root / f"{self.output_prefix}_optimized_float_model.onnx"
        calibrated = self.output_root / f"{self.output_prefix}_calibrated_model.onnx"
        for model in (optimized, calibrated):
            if not model.is_file() or model.is_symlink():
                raise AdapterExecutionError(
                    "VERIFICATION_MODEL_MISSING",
                    f"verification model is missing: {model.name}",
                    step="verify",
                )

        verification_root = self.work_root / "verification"
        outputs_root = verification_root / "outputs"
        outputs_root.mkdir(parents=True, exist_ok=True)
        raw_input_paths: list[Path] = []
        feed_arrays: dict[str, Any] = {}
        for input_config in self.configuration["inputs"]:
            source_root = (
                self.calibration_root / input_config["name"]
                if self.configuration["calibration"]["source_type"] == "npy_multi"
                else self.calibration_root
            )
            candidates = sorted(source_root.glob("*.npy"))
            if not candidates:
                raise AdapterExecutionError(
                    "VERIFICATION_INPUT_MISSING",
                    f"no verification input is available for {input_config['name']}",
                    step="verify",
                )
            source = candidates[0]
            raw_input_paths.append(source)
            try:
                array = _load_npy(np, source)
            except (OSError, TypeError, ValueError) as exc:
                raise AdapterExecutionError(
                    "VERIFICATION_INPUT_INVALID",
                    f"cannot load verification input {source.name}: {exc}",
                    step="verify",
                ) from exc
            target_shape = input_config["target_shape"]
            if list(array.shape) == target_shape[1:]:
                array = np.expand_dims(array, axis=0)
            if list(array.shape) != target_shape:
                raise AdapterExecutionError(
                    "VERIFICATION_INPUT_INVALID",
                    f"verification input Shape {list(array.shape)} does not match {target_shape}",
                    step="verify",
                )
            if not np.isfinite(np.asarray(array, dtype=np.float64)).all():
                raise AdapterExecutionError(
                    "VERIFICATION_INPUT_INVALID",
                    "verification input contains NaN or infinite values",
                    step="verify",
                )
            feed_arrays[input_config["name"]] = array

        started = time.monotonic()
        try:
            session = HBRuntime(str(optimized))
            runtime_input_names = list(session.input_names)
            if set(runtime_input_names) != set(feed_arrays):
                raise ValueError(
                    f"runtime inputs {runtime_input_names} do not match {sorted(feed_arrays)}"
                )
            runtime_feed = {name: feed_arrays[name] for name in runtime_input_names}
            runtime_outputs = session.run(None, runtime_feed)
            output_names = list(session.output_names)
        except Exception as exc:
            raise AdapterExecutionError(
                "HBRUNTIME_INFERENCE_FAILED",
                f"HBRuntime single-sample inference failed: {exc}",
                step="verify",
            ) from exc
        if len(runtime_outputs) != len(output_names):
            raise AdapterExecutionError(
                "HBRUNTIME_INFERENCE_FAILED",
                "HBRuntime output count does not match model metadata",
                step="verify",
            )
        output_summaries: list[dict[str, Any]] = []
        for index, (name, output) in enumerate(zip(output_names, runtime_outputs, strict=True)):
            values = np.asarray(output)
            if values.size < 1:
                raise AdapterExecutionError(
                    "HBRUNTIME_INFERENCE_FAILED",
                    f"HBRuntime output is empty: {name}",
                    step="verify",
                )
            finite_values = np.asarray(values, dtype=np.float64)
            if not np.isfinite(finite_values).all():
                raise AdapterExecutionError(
                    "HBRUNTIME_INFERENCE_FAILED",
                    f"HBRuntime output contains non-finite values: {name}",
                    step="verify",
                )
            destination = outputs_root / f"{index:03d}.npy"
            np.save(destination, values, allow_pickle=False)
            output_summaries.append(
                {
                    "name": name,
                    "shape": list(values.shape),
                    "dtype": values.dtype.name,
                    "minimum": float(finite_values.min()),
                    "maximum": float(finite_values.max()),
                    "mean": float(finite_values.mean()),
                    "output_file": destination.relative_to(self.attempt_root).as_posix(),
                    "sha256": sha256_file(destination),
                }
            )
        runtime_summary = {
            "schema_version": "1",
            "model": optimized.name,
            "input_names": runtime_input_names,
            "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
            "outputs": output_summaries,
        }
        runtime_summary_path = verification_root / "hbruntime-summary.json"
        atomic_write_json(runtime_summary_path, runtime_summary)

        verifier_command = _build_hb_verifier_command(
            optimized=optimized,
            calibrated=calibrated,
            input_paths=raw_input_paths,
            compare_digits=verification["compare_digits"],
        )
        command_result = self._run_command(
            verifier_command,
            cwd=verification_root,
            log_name="verify.log",
            step="verify",
        )
        verifier_metrics = parse_verifier_metrics(
            (self.logs_root / "verify.log").read_text(encoding="utf-8", errors="replace")
        )
        if not verifier_metrics["cosines"] and not verifier_metrics["consistency"]:
            raise AdapterExecutionError(
                "HB_VERIFIER_RESULT_INVALID",
                "hb_verifier completed without parseable comparison metrics",
                step="verify",
            )
        verifier_summary = {
            "schema_version": "1",
            "model_pair": [optimized.name, calibrated.name],
            "compare_digits": verification["compare_digits"],
            **verifier_metrics,
        }
        verifier_summary_path = verification_root / "hb-verifier-summary.json"
        atomic_write_json(verifier_summary_path, verifier_summary)
        return {
            "enabled": True,
            "mode": verification["mode"],
            "hbruntime": runtime_summary,
            "hb_verifier": verifier_summary,
            "command": command_result,
        }

    def collect(self, *, require_hbm: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        artifacts_root = self.attempt_root / "artifacts"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        candidates: list[tuple[str, Path, str, bool, str | None]] = [
            (
                "generated_config",
                self.generated_config,
                "application/yaml",
                require_hbm,
                "generated.yaml",
            ),
            (
                "model_inspection",
                self.inspection_path,
                "application/json",
                False,
                "model-inspection.json",
            ),
            (
                "calibration_manifest",
                self.calibration_manifest_path,
                "application/json",
                False,
                "calibration-manifest.json",
            ),
            (
                "calibration_preview",
                self.calibration_preview_path,
                "image/png",
                False,
                "calibration-preview.png",
            ),
            ("tool_log", self.logs_root / "check.log", "text/plain", False, None),
            ("tool_log", self.logs_root / "compile.log", "text/plain", False, None),
            ("tool_log", self.logs_root / "verify.log", "text/plain", False, None),
            (
                "hbruntime_summary",
                self.work_root / "verification" / "hbruntime-summary.json",
                "application/json",
                False,
                "hbruntime-summary.json",
            ),
            (
                "hb_verifier_summary",
                self.work_root / "verification" / "hb-verifier-summary.json",
                "application/json",
                False,
                "hb-verifier-summary.json",
            ),
            (
                "hbm",
                self.output_root / f"{self.output_prefix}.hbm",
                "application/octet-stream",
                require_hbm,
                f"{self.output_prefix}.hbm",
            ),
            (
                "quant_info",
                self.output_root / f"{self.output_prefix}_quant_info.json",
                "application/json",
                False,
                f"{self.output_prefix}_quant_info.json",
            ),
            (
                "advice_csv",
                self.output_root / f"{self.output_prefix}_advice.csv",
                "text/csv",
                False,
                f"{self.output_prefix}_advice.csv",
            ),
            (
                "advice_json",
                self.output_root / f"{self.output_prefix}_advice.json",
                "application/json",
                False,
                f"{self.output_prefix}_advice.json",
            ),
            (
                "static_perf_json",
                self.output_root / f"{self.output_prefix}.json",
                "application/json",
                False,
                f"{self.output_prefix}.json",
            ),
            (
                "static_perf_html",
                self.output_root / f"{self.output_prefix}.html",
                "text/html",
                False,
                f"{self.output_prefix}.html",
            ),
        ]
        candidates.extend(
            [
                (kind, self.output_root / filename, mime_type, False, filename)
                for kind, filename, mime_type in (
                    (
                        "original_onnx",
                        f"{self.output_prefix}_original_float_model.onnx",
                        "application/onnx",
                    ),
                    (
                        "optimized_onnx",
                        f"{self.output_prefix}_optimized_float_model.onnx",
                        "application/onnx",
                    ),
                    (
                        "calibrated_onnx",
                        f"{self.output_prefix}_calibrated_model.onnx",
                        "application/onnx",
                    ),
                    (
                        "ptq_onnx",
                        f"{self.output_prefix}_ptq_model.onnx",
                        "application/onnx",
                    ),
                    (
                        "quantized_bc",
                        f"{self.output_prefix}_quantized_model.bc",
                        "application/octet-stream",
                    ),
                    (
                        "quantized_removed_bc",
                        f"{self.output_prefix}_quantized_removed_model.bc",
                        "application/octet-stream",
                    ),
                    (
                        "node_info",
                        f"{self.output_prefix}_node_info.csv",
                        "text/csv",
                    ),
                    ("tool_log", "hb_compile.log", "text/plain"),
                )
            ]
        )
        verification_outputs = self.work_root / "verification" / "outputs"
        if verification_outputs.is_dir() and not verification_outputs.is_symlink():
            candidates.extend(
                (
                    "hbruntime_output",
                    path,
                    "application/x-npy",
                    False,
                    f"hbruntime-output-{path.name}",
                )
                for path in sorted(verification_outputs.glob("*.npy"))
            )
        seen_sources: set[Path] = set()
        artifacts: list[dict[str, Any]] = []
        for kind, source, mime_type, required, destination_name in candidates:
            if source in seen_sources:
                continue
            seen_sources.add(source)
            if not source.exists():
                if required:
                    raise AdapterExecutionError(
                        "ARTIFACT_HBM_MISSING" if kind == "hbm" else "RUN_RESULT_INVALID",
                        f"required artifact is missing: {source.name}",
                        step="collect",
                    )
                continue
            if not source.is_file() or source.is_symlink():
                raise AdapterExecutionError(
                    "RUN_RESULT_INVALID",
                    f"artifact is not a regular file: {source.name}",
                    step="collect",
                )
            destination = source
            if destination_name is not None:
                destination = artifacts_root / destination_name
                if source != destination:
                    shutil.copy2(source, destination, follow_symlinks=False)
            relative = destination.relative_to(self.attempt_root).as_posix()
            artifacts.append(
                {
                    "kind": kind,
                    "relative_path": relative,
                    "size_bytes": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                    "mime_type": mime_type,
                    "required": required,
                }
            )
        manifest = {"schema_version": "1", "artifacts": artifacts}
        return manifest, {"artifact_count": len(artifacts)}

    def _validate_generated_paths(self, generated: dict[str, Any]) -> None:
        model_parameters = generated["model_parameters"]
        expected = {
            "onnx_model": self.model_path,
            "working_dir": self.output_root,
        }
        for field, path in expected.items():
            if Path(model_parameters[field]) != path:
                raise AdapterExecutionError(
                    "CONFIG_INVALID",
                    f"generated {field} escaped the controlled path",
                    step="preprocess",
                )
        calibration_value = generated["calibration_parameters"]["cal_data_dir"]
        calibration_paths = [Path(item) for item in str(calibration_value).split(";")]
        expected_calibration_paths = (
            [self.calibration_root / item["name"] for item in self.configuration["inputs"]]
            if self.configuration["calibration"]["source_type"] == "npy_multi"
            else [self.calibration_root]
        )
        if calibration_paths != expected_calibration_paths:
            raise AdapterExecutionError(
                "CONFIG_INVALID",
                "generated calibration path escaped the attempt",
                step="preprocess",
            )
        cache_key = self.configuration["compiler"]["cache_key"]
        cache_path = generated["compiler_parameters"].get("cache_path")
        expected_cache = None if cache_key is None else f"/cache/compiler/{cache_key}"
        if cache_path != expected_cache:
            raise AdapterExecutionError(
                "CONFIG_INVALID",
                "generated cache path escaped the controlled cache root",
                step="preprocess",
            )

    def _run_command(
        self, command: list[str], *, cwd: Path, log_name: str, step: str
    ) -> dict[str, Any]:
        cwd.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_root / log_name
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            code = {
                "check": "TOOL_CHECK_FAILED",
                "compile": "TOOL_COMPILE_FAILED",
                "verify": "TOOL_VERIFY_FAILED",
            }.get(step, "TOOL_EXECUTION_FAILED")
            raise AdapterExecutionError(
                code,
                f"failed to start {command[0]}: {exc}",
                step=step,
                details={"executable": command[0]},
            ) from exc
        assert process.stdout is not None
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        written = 0
        truncated = False
        try:
            with log_path.open("wb") as log:
                while selector.get_map():
                    if self.is_cancel_requested():
                        self._terminate_process(process)
                        raise InterruptedError("runner cancellation requested")
                    if time.monotonic() - started > self.timeout_seconds:
                        self._terminate_process(process)
                        raise AdapterExecutionError(
                            "RUN_TIMEOUT",
                            f"{step} exceeded {self.timeout_seconds} seconds",
                            step=step,
                        )
                    for key, _mask in selector.select(timeout=0.2):
                        chunk = os.read(key.fd, 64 * 1024)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        remaining = max(0, self.max_log_bytes - written)
                        accepted = chunk[:remaining]
                        if accepted:
                            log.write(accepted)
                            output = (
                                sys.stdout.buffer if key.data == "stdout" else sys.stderr.buffer
                            )
                            output.write(accepted)
                            output.flush()
                            written += len(accepted)
                        if len(accepted) < len(chunk):
                            truncated = True
                while process.poll() is None:
                    if self.is_cancel_requested():
                        self._terminate_process(process)
                        raise InterruptedError("runner cancellation requested")
                    if time.monotonic() - started > self.timeout_seconds:
                        self._terminate_process(process)
                        raise AdapterExecutionError(
                            "RUN_TIMEOUT",
                            f"{step} exceeded {self.timeout_seconds} seconds",
                            step=step,
                        )
                    time.sleep(0.2)
                return_code = process.returncode
                assert return_code is not None
                log.flush()
                os.fsync(log.fileno())
        finally:
            selector.close()
            if process.poll() is None:
                self._terminate_process(process)
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if return_code != 0:
            code = {
                "check": "TOOL_CHECK_FAILED",
                "compile": "TOOL_COMPILE_FAILED",
                "verify": "TOOL_VERIFY_FAILED",
            }.get(step, "TOOL_EXECUTION_FAILED")
            tail = log_path.read_bytes()[-4000:].decode(errors="replace")
            raise AdapterExecutionError(
                code,
                f"{command[0]} exited with code {return_code}",
                step=step,
                details={"exit_code": return_code, "log_tail": tail, "log_truncated": truncated},
            )
        return {
            "exit_code": return_code,
            "duration_ms": duration_ms,
            "log": log_path.relative_to(self.attempt_root).as_posix(),
            "log_truncated": truncated,
        }

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)

    def _parse_static_metrics(self) -> dict[str, Any]:
        path = self.output_root / f"{self.output_prefix}.json"
        if not path.is_file() or path.is_symlink():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return parse_static_metrics(payload)
        except (OSError, ValueError, TypeError) as exc:
            raise AdapterExecutionError(
                "RUN_RESULT_INVALID",
                f"failed to parse OpenExplorer performance JSON: {exc}",
                step="compile",
            ) from exc
