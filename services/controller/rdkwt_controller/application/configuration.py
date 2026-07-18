from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from rdkwt_runner.adapters.openexplorer_v3_7 import (
    render_openexplorer_config,
    validate_configuration,
)

from rdkwt_controller.profiles import TargetProfile


def _positive_shape(value: Any) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value)
    ):
        raise ValueError("target_shape must contain four positive integers")
    return value


def _number_list(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int | float) for item in value
    ):
        raise ValueError(f"{name} must be a list of numbers")
    return [float(item) for item in value]


def normalize_configuration(
    *,
    profile: TargetProfile,
    inspection: dict[str, Any],
    output_prefix: str,
    input_options: dict[str, Any] | None,
    calibration_options: dict[str, Any] | None,
    compiler_options: dict[str, Any],
    sample_count: int,
    calibration_source_type: str = "images",
    calibration_validation_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if inspection.get("compatibility_status") != "READY":
        raise ValueError("model inspection contains blocking compatibility errors")
    inspected_inputs = inspection.get("inputs")
    if not isinstance(inspected_inputs, list) or len(inspected_inputs) != 1:
        raise ValueError("M2 conversion requires exactly one inspected model input")
    inspected = inspected_inputs[0]
    inspected_shape = inspected.get("shape")
    if not isinstance(inspected_shape, list) or len(inspected_shape) != 4:
        raise ValueError("inspected model input must have rank four")

    supplied_input = input_options or {}
    target_shape_raw = supplied_input.get("target_shape")
    if target_shape_raw is None:
        if any(not isinstance(item, int) or isinstance(item, bool) for item in inspected_shape):
            raise ValueError("dynamic model input requires an explicit target_shape")
        target_shape_raw = inspected_shape
    target_shape = _positive_shape(target_shape_raw)
    for index, inspected_dimension in enumerate(inspected_shape):
        if isinstance(inspected_dimension, int) and inspected_dimension != target_shape[index]:
            raise ValueError(
                f"target_shape[{index}] must remain {inspected_dimension} "
                "for the static model input"
            )

    input_name = str(supplied_input.get("name") or inspected.get("name") or "")
    if input_name != inspected.get("name"):
        raise ValueError("input name must match the inspected ONNX graph")
    train_layout = str(supplied_input.get("train_layout") or "NCHW")
    train_type = str(supplied_input.get("train_type") or "rgb")
    runtime_type = str(supplied_input.get("runtime_type") or "nv12")
    channels = target_shape[1] if train_layout == "NCHW" else target_shape[3]
    default_mean = [123.675, 116.28, 103.53] if channels == 3 else [0.0]
    default_scale = [0.01712475, 0.017507, 0.01742919] if channels == 3 else [1.0]
    normalization_options = supplied_input.get("normalization") or {}
    if not isinstance(normalization_options, dict):
        raise ValueError("input normalization must be an object")
    normalization = {
        "mean": _number_list(
            normalization_options.get("mean", default_mean), "normalization.mean"
        ),
        "scale": _number_list(
            normalization_options.get("scale", default_scale), "normalization.scale"
        ),
        "std": _number_list(
            normalization_options.get("std", []), "normalization.std"
        ),
    }

    supplied_calibration = calibration_options or {}
    if not isinstance(supplied_calibration, dict):
        raise ValueError("calibration configuration must be an object")
    if not 20 <= sample_count <= 100:
        raise ValueError("standard conversion requires 20 to 100 calibration samples")
    sample_limit = supplied_calibration.get("sample_limit", sample_count)
    if isinstance(sample_limit, bool) or not isinstance(sample_limit, int):
        raise ValueError("calibration sample_limit must be an integer")
    if sample_limit > sample_count:
        raise ValueError("calibration sample_limit exceeds the finalized sample count")
    height = target_shape[2] if train_layout == "NCHW" else target_shape[1]
    width = target_shape[3] if train_layout == "NCHW" else target_shape[2]
    if calibration_source_type == "images":
        recipe_options = supplied_calibration.get("recipe") or {}
        if not isinstance(recipe_options, dict):
            raise ValueError("calibration recipe must be an object")
        recipe_mean = [0.485, 0.456, 0.406] if channels == 3 else [0.0]
        recipe_std = [0.229, 0.224, 0.225] if channels == 3 else [1.0]
        recipe: dict[str, Any] | None = {
            "id": str(recipe_options.get("id") or "image-center-crop"),
            "version": "1",
            "resize_short": int(recipe_options.get("resize_short") or max(height, width)),
            "crop_size": [height, width],
            "mean": _number_list(recipe_options.get("mean", recipe_mean), "recipe.mean"),
            "std": _number_list(recipe_options.get("std", recipe_std), "recipe.std"),
        }
    elif calibration_source_type == "npy":
        if supplied_calibration.get("recipe") is not None:
            raise ValueError("direct NPY calibration must not include an image recipe")
        report = calibration_validation_report or {}
        npy_shape = report.get("shape")
        expected_shape = target_shape[1:]
        if npy_shape != expected_shape:
            raise ValueError(
                f"NPY sample Shape {npy_shape} must match the batch-free model input "
                f"Shape {expected_shape}"
            )
        if not isinstance(report.get("dtype"), str):
            raise ValueError("NPY calibration validation report is incomplete")
        recipe = None
    else:
        raise ValueError("calibration source_type must be images or npy")

    core_num = compiler_options.get("core_num")
    if core_num is None:
        core_num = int(profile.capabilities.core_num.default)
    max_l2m_size = compiler_options.get("max_l2m_size")
    if max_l2m_size is None:
        max_l2m_size = profile.capabilities.max_l2m_size.default
    profile.validate_compile_options(
        core_num=core_num, max_l2m_size=max_l2m_size
    )
    configuration = {
        "schema_version": "1",
        "target_profile": profile.snapshot(),
        "output_prefix": output_prefix,
        "inputs": [
            {
                "name": input_name,
                "target_shape": target_shape,
                "train_type": train_type,
                "train_layout": train_layout,
                "runtime_type": runtime_type,
                "normalization": normalization,
            }
        ],
        "calibration": {
            "source_type": calibration_source_type,
            "algorithm": str(supplied_calibration.get("algorithm") or "default"),
            "sample_limit": sample_limit,
            "recipe": recipe,
        },
        "compiler": {
            "compile_mode": str(compiler_options.get("compile_mode") or "latency"),
            "balance_factor": compiler_options.get("balance_factor"),
            "core_num": core_num,
            "optimize_level": str(compiler_options.get("optimize_level") or "O2"),
            "max_l2m_size": max_l2m_size,
            "max_time_per_fc": int(compiler_options.get("max_time_per_fc") or 0),
            "jobs": int(compiler_options.get("jobs") or 8),
            "cache_mode": str(compiler_options.get("cache_mode") or "disable"),
        },
    }
    return validate_configuration(configuration)


def render_configuration_preview(
    configuration: dict[str, Any],
    *,
    model_logical_path: str,
) -> dict[str, Any]:
    generated = render_openexplorer_config(
        configuration,
        model_path=Path("/assets") / model_logical_path,
        calibration_dir=Path("/runs/<run-id>/attempts/1/work/calibration/input"),
        working_dir=Path("/runs/<run-id>/attempts/1/work/model_output"),
    )
    rendered = yaml.safe_dump(generated, sort_keys=False, allow_unicode=True)
    return {
        "configuration": configuration,
        "yaml": rendered,
        "field_sources": {
            "model_parameters.onnx_model": "system",
            "model_parameters.march": "target_profile",
            "model_parameters.working_dir": "system",
            "model_parameters.output_model_file_prefix": "user",
            "input_parameters": "user_and_model_inspection",
            "calibration_parameters.cal_data_dir": "system",
            "calibration_parameters.calibration_type": "user",
            "compiler_parameters": "user_and_target_profile",
        },
        "warnings": inspection_warnings(configuration),
    }


def inspection_warnings(configuration: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    compiler = configuration["compiler"]
    if compiler["optimize_level"] == "O0":
        warnings.append("O0 会关闭大部分编译优化，仅建议用于调试。")
    if configuration["calibration"]["sample_limit"] < 50:
        warnings.append("校准样本少于 50 张，量化稳定性可能下降。")
    return warnings
