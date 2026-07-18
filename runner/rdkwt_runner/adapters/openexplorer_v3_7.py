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
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..filesystem import atomic_write_json, atomic_write_text, sha256_file

ADAPTER_ID = "openexplorer-3.7.0"
OUTPUT_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SUPPORTED_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png"}
SUPPORTED_CALIBRATION_ALGORITHMS = {"default", "mix", "kl", "max"}
SUPPORTED_COMPILE_MODES = {"latency", "bandwidth", "balance"}
SUPPORTED_OPTIMIZE_LEVELS = {"O0", "O1", "O2"}


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
    if not isinstance(inputs, list) or len(inputs) != 1:
        raise ValueError("OpenExplorer 3.7 M1 supports exactly one input")
    input_config = _expect_object(inputs[0], "inputs[0]")
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
        "inputs[0]",
    )
    if not isinstance(input_config["name"], str) or not input_config["name"].strip():
        raise ValueError("inputs[0].name must be a non-empty string")
    if any(character in input_config["name"] for character in ";\r\n"):
        raise ValueError("inputs[0].name contains unsupported characters")
    shape = input_config["target_shape"]
    if (
        not isinstance(shape, list)
        or len(shape) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in shape)
    ):
        raise ValueError("inputs[0].target_shape must contain four positive integers")
    if shape[0] != 1 or shape[1] != 3:
        raise ValueError("the M1 ImageNet recipe requires target shape [1, 3, H, W]")
    if input_config["train_type"] != "rgb" or input_config["train_layout"] != "NCHW":
        raise ValueError("the M1 ImageNet recipe requires rgb/NCHW training input")
    if input_config["runtime_type"] not in {"nv12", "featuremap"}:
        raise ValueError("runtime_type must be nv12 or featuremap")
    if input_config["runtime_type"] == "nv12" and (shape[2] % 2 or shape[3] % 2):
        raise ValueError("NV12 target height and width must be even")
    normalization = _expect_object(input_config["normalization"], "inputs[0].normalization")
    _expect_exact_fields(normalization, {"mean", "scale", "std"}, "inputs[0].normalization")
    _numeric_list(normalization["mean"], "normalization.mean", allowed_lengths={0, 1, 3})
    _numeric_list(normalization["scale"], "normalization.scale", allowed_lengths={0, 1, 3})
    _numeric_list(normalization["std"], "normalization.std", allowed_lengths={0, 1, 3})

    calibration = _expect_object(config["calibration"], "calibration")
    _expect_exact_fields(calibration, {"algorithm", "sample_limit", "recipe"}, "calibration")
    if calibration["algorithm"] not in SUPPORTED_CALIBRATION_ALGORITHMS:
        raise ValueError("unsupported calibration algorithm")
    if (
        isinstance(calibration["sample_limit"], bool)
        or not isinstance(calibration["sample_limit"], int)
        or not 20 <= calibration["sample_limit"] <= 100
    ):
        raise ValueError("calibration.sample_limit must be between 20 and 100")
    recipe = _expect_object(calibration["recipe"], "calibration.recipe")
    _expect_exact_fields(
        recipe,
        {"id", "version", "resize_short", "crop_size", "mean", "std"},
        "calibration.recipe",
    )
    if recipe["id"] != "imagenet-resnet18" or recipe["version"] != "1":
        raise ValueError("unsupported calibration recipe")
    if (
        isinstance(recipe["resize_short"], bool)
        or not isinstance(recipe["resize_short"], int)
        or recipe["resize_short"] < 1
    ):
        raise ValueError("calibration.recipe.resize_short must be positive")
    if recipe["crop_size"] != [shape[2], shape[3]]:
        raise ValueError("calibration crop_size must match the target input height and width")
    _numeric_list(recipe["mean"], "calibration.recipe.mean", allowed_lengths={3})
    std = _numeric_list(recipe["std"], "calibration.recipe.std", allowed_lengths={3})
    if any(item == 0 for item in std):
        raise ValueError("calibration.recipe.std cannot contain zero")

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
    if platform == "s600" and l2m != "auto" and (
        isinstance(l2m, bool)
        or not isinstance(l2m, int)
        or not 0 <= l2m <= 24 * 1024 * 1024
    ):
        raise ValueError("S600 max_l2m_size must be auto or between 0 and 24 MiB")
    for field, minimum, maximum in (("max_time_per_fc", 0, 2**31 - 1), ("jobs", 1, 128)):
        value = compiler[field]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"compiler.{field} is outside the supported range")
    if compiler["cache_mode"] not in {"disable", "enable", "force_overwrite"}:
        raise ValueError("unsupported cache_mode")
    return config


def _format_sequence(values: list[Any]) -> str:
    return " ".join(str(value) for value in values)


def render_openexplorer_config(
    configuration: dict[str, Any],
    *,
    model_path: Path,
    calibration_dir: Path,
    working_dir: Path,
) -> dict[str, Any]:
    config = validate_configuration(configuration)
    profile = config["target_profile"]["profile"]
    input_config = config["inputs"][0]
    normalization = input_config["normalization"]
    compiler = config["compiler"]
    input_parameters: dict[str, Any] = {
        "input_name": input_config["name"],
        "input_type_rt": input_config["runtime_type"],
        "input_type_train": input_config["train_type"],
        "input_layout_train": input_config["train_layout"],
        "input_shape": "x".join(str(item) for item in input_config["target_shape"]),
    }
    for source, target in (("mean", "mean_value"), ("scale", "scale_value"), ("std", "std_value")):
        values = normalization[source]
        if values:
            input_parameters[target] = _format_sequence(values)

    compiler_parameters: dict[str, Any] = {
        "compile_mode": compiler["compile_mode"],
        "core_num": compiler["core_num"],
        "optimize_level": compiler["optimize_level"],
        "max_l2m_size": None if compiler["max_l2m_size"] == "auto" else compiler["max_l2m_size"],
        "max_time_per_fc": compiler["max_time_per_fc"],
        "jobs": compiler["jobs"],
        "cache_mode": compiler["cache_mode"],
    }
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
            "cal_data_dir": str(calibration_dir),
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


class OpenExplorer370Adapter:
    implemented_steps = frozenset({"inspect", "check", "preprocess", "compile"})

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
        }.get(step)
        if operation is None:
            raise ValueError(f"unsupported OpenExplorer adapter step: {step}")
        return operation()

    def inspect(self) -> dict[str, Any]:
        try:
            import onnx
            from onnx.external_data_helper import uses_external_data

            model = onnx.load(str(self.model_path), load_external_data=False)
        except Exception as exc:
            raise AdapterExecutionError(
                "MODEL_PARSE_FAILED", f"failed to parse ONNX model: {exc}", step="inspect"
            ) from exc
        external_tensors = [
            item.name for item in model.graph.initializer if uses_external_data(item)
        ]
        if external_tensors:
            raise AdapterExecutionError(
                "MODEL_EXTERNAL_DATA_UNSUPPORTED",
                "ONNX external data is not supported in M1",
                step="inspect",
                details={"tensor_count": len(external_tensors)},
            )
        try:
            onnx.checker.check_model(model)
        except Exception as exc:
            raise AdapterExecutionError(
                "MODEL_PARSE_FAILED", f"ONNX checker rejected the model: {exc}", step="inspect"
            ) from exc

        initializer_names = {item.name for item in model.graph.initializer}

        def tensor_info(value_info: Any) -> dict[str, Any]:
            tensor_type = value_info.type.tensor_type
            dimensions: list[int | str | None] = []
            for dimension in tensor_type.shape.dim:
                if dimension.HasField("dim_value") and dimension.dim_value > 0:
                    dimensions.append(int(dimension.dim_value))
                elif dimension.HasField("dim_param") and dimension.dim_param:
                    dimensions.append(str(dimension.dim_param))
                else:
                    dimensions.append(None)
            return {
                "name": value_info.name,
                "shape": dimensions,
                "dtype": onnx.TensorProto.DataType.Name(tensor_type.elem_type),
            }

        details = {
            "format": "onnx",
            "size_bytes": self.model_path.stat().st_size,
            "sha256": sha256_file(self.model_path),
            "ir_version": int(model.ir_version),
            "opsets": [
                {"domain": item.domain or "ai.onnx", "version": int(item.version)}
                for item in model.opset_import
            ],
            "inputs": [
                tensor_info(item)
                for item in model.graph.input
                if item.name not in initializer_names
            ],
            "outputs": [tensor_info(item) for item in model.graph.output],
            "operators": dict(sorted(Counter(node.op_type for node in model.graph.node).items())),
            "external_data": False,
        }
        atomic_write_json(self.inspection_path, details)
        return details

    def check(self) -> dict[str, Any]:
        self.check_root.mkdir(parents=True, exist_ok=True)
        profile = self.configuration["target_profile"]["profile"]
        input_config = self.configuration["inputs"][0]
        command = [
            "hb_compile",
            "--model",
            str(self.model_path),
            "--march",
            profile["march"],
            "--input-shape",
            input_config["name"],
            "x".join(str(item) for item in input_config["target_shape"]),
        ]
        result = self._run_command(command, cwd=self.check_root, log_name="check.log", step="check")
        return {"march": profile["march"], **result}

    def preprocess(self) -> dict[str, Any]:
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
        crop_height, crop_width = recipe["crop_size"]
        mean = np.asarray(recipe["mean"], dtype=np.float32).reshape(3, 1, 1)
        std = np.asarray(recipe["std"], dtype=np.float32).reshape(3, 1, 1)
        self.calibration_root.mkdir(parents=True, exist_ok=True)
        samples = []
        for index, source in enumerate(selected):
            if self.is_cancel_requested():
                raise InterruptedError("runner cancellation requested")
            try:
                with Image.open(source) as opened:
                    image = opened.convert("RGB")
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
                    array = np.asarray(image, dtype=np.float32).transpose(2, 0, 1)
                    array = (array / np.float32(255.0) - mean) / std
            except Exception as exc:
                raise AdapterExecutionError(
                    "CALIBRATION_PREPROCESS_FAILED",
                    f"failed to preprocess calibration image {source.name}: {exc}",
                    step="preprocess",
                    details={"sample": source.name},
                ) from exc
            source_digest = sha256_file(source)
            output = self.calibration_root / f"{index:04d}_{source_digest[:12]}.bgr.npy"
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
                }
            )

        manifest = {
            "schema_version": "1",
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
        }

    def compile(self) -> dict[str, Any]:
        if not self.generated_config.is_file():
            raise AdapterExecutionError(
                "CONFIG_INVALID",
                "generated YAML is missing; preprocess must run first",
                step="compile",
            )
        self.output_root.mkdir(parents=True, exist_ok=True)
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
        return {
            **command_result,
            "hbm_size_bytes": hbm.stat().st_size,
            "hbm_sha256": sha256_file(hbm),
            "static_performance": metrics,
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
            ("tool_log", self.logs_root / "check.log", "text/plain", False, None),
            ("tool_log", self.logs_root / "compile.log", "text/plain", False, None),
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
        artifacts: list[dict[str, Any]] = []
        for kind, source, mime_type, required, destination_name in candidates:
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
        calibration = Path(generated["calibration_parameters"]["cal_data_dir"])
        if calibration != self.calibration_root:
            raise AdapterExecutionError(
                "CONFIG_INVALID",
                "generated calibration path escaped the attempt",
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
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            code = "TOOL_CHECK_FAILED" if step == "check" else "TOOL_COMPILE_FAILED"
            raise AdapterExecutionError(
                code,
                f"failed to start hb_compile: {exc}",
                step=step,
                details={"executable": command[0]},
            ) from exc
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
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
                            sys.stdout.buffer.write(accepted)
                            sys.stdout.buffer.flush()
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
            code = "TOOL_CHECK_FAILED" if step == "check" else "TOOL_COMPILE_FAILED"
            tail = log_path.read_bytes()[-4000:].decode(errors="replace")
            raise AdapterExecutionError(
                code,
                f"hb_compile exited with code {return_code}",
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
