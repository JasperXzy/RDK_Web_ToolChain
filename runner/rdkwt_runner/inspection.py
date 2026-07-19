from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .filesystem import sha256_file


def inspect_onnx(model_path: Path) -> dict[str, Any]:
    try:
        import onnx
        from onnx.external_data_helper import uses_external_data

        model = onnx.load(str(model_path), load_external_data=False)
    except Exception as exc:
        raise ValueError(f"failed to parse ONNX model: {exc}") from exc

    external_tensors = [item.name for item in model.graph.initializer if uses_external_data(item)]
    checker_error = None
    if not external_tensors:
        try:
            onnx.checker.check_model(model)
        except Exception as exc:
            checker_error = str(exc)

    initializer_names = {item.name for item in model.graph.initializer}

    def tensor_info(value_info: Any) -> dict[str, Any]:
        tensor_type = value_info.type.tensor_type
        dimensions: list[int | str | None] = []
        dynamic = False
        for dimension in tensor_type.shape.dim:
            if dimension.HasField("dim_value") and dimension.dim_value > 0:
                dimensions.append(int(dimension.dim_value))
            elif dimension.HasField("dim_param") and dimension.dim_param:
                dimensions.append(str(dimension.dim_param))
                dynamic = True
            else:
                dimensions.append(None)
                dynamic = True
        return {
            "name": value_info.name,
            "shape": dimensions,
            "dtype": onnx.TensorProto.DataType.Name(tensor_type.elem_type),
            "dynamic": dynamic,
        }

    inputs = [tensor_info(item) for item in model.graph.input if item.name not in initializer_names]
    outputs = [tensor_info(item) for item in model.graph.output]
    opsets = [
        {"domain": item.domain or "ai.onnx", "version": int(item.version)}
        for item in model.opset_import
    ]
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if checker_error is not None:
        blockers.append({"code": "MODEL_CHECKER_FAILED", "message": checker_error[:2000]})
    if external_tensors:
        blockers.append(
            {
                "code": "MODEL_EXTERNAL_DATA_UNSUPPORTED",
                "message": (
                    f"ONNX references {len(external_tensors)} external tensor files; "
                    "M3 accepts self-contained models only"
                ),
            }
        )
    if int(model.ir_version) > 9:
        blockers.append(
            {
                "code": "MODEL_IR_UNSUPPORTED",
                "message": f"IR version {int(model.ir_version)} exceeds the supported maximum 9",
            }
        )
    default_opset = next((item["version"] for item in opsets if item["domain"] == "ai.onnx"), None)
    if default_opset is None or not 8 <= default_opset <= 19:
        blockers.append(
            {
                "code": "MODEL_OPSET_UNSUPPORTED",
                "message": f"ai.onnx opset must be between 8 and 19; found {default_opset}",
            }
        )
    if not 1 <= len(inputs) <= 4:
        blockers.append(
            {
                "code": "MODEL_INPUT_COUNT_UNSUPPORTED",
                "message": f"M3 supports one to four model inputs; found {len(inputs)}",
            }
        )
    else:
        for item in inputs:
            if not 1 <= len(item["shape"]) <= 4:
                blockers.append(
                    {
                        "code": "MODEL_INPUT_RANK_UNSUPPORTED",
                        "message": (
                            f"input {item['name']} has rank {len(item['shape'])}; "
                            "M3 supports rank one to four"
                        ),
                    }
                )
            elif item["dynamic"]:
                warnings.append(
                    {
                        "code": "MODEL_DYNAMIC_SHAPE",
                        "message": (
                            f"input {item['name']} has dynamic dimensions and requires "
                            "explicit positive target values"
                        ),
                    }
                )

    return {
        "schema_version": "1",
        "format": "onnx",
        "size_bytes": model_path.stat().st_size,
        "sha256": sha256_file(model_path),
        "ir_version": int(model.ir_version),
        "opsets": opsets,
        "inputs": inputs,
        "outputs": outputs,
        "operators": dict(sorted(Counter(node.op_type for node in model.graph.node).items())),
        "external_data": bool(external_tensors),
        "external_tensor_count": len(external_tensors),
        "compatibility_status": "BLOCKED" if blockers else "READY",
        "blockers": blockers,
        "warnings": warnings,
    }
