# RDK WebToolChain

RDK WebToolChain 是面向 S100/S600 与 OpenExplorer 3.7.0 的本地模型转换工作台。

当前实现已进入 M1 纵向闭环：除版本化 Controller/Runner 合约、S100/S600 Target Profile、
SQLite 状态和受控 Docker 调度外，已提供 OpenExplorer 3.7.0 ONNX 检查、ResNet18 校准
预处理、平台锁定 YAML 生成、PTQ 编译和产物归集。

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest
```

构建基于本机 OpenExplorer CPU 镜像的固定 Runner：

```bash
docker build \
  --file runner/Dockerfile.cpu \
  --tag rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1 \
  .
```

Controller 的最小本地入口：

```bash
.venv/bin/rdkwt-controller
```

提交当前 M1 支持的 ResNet18 转换：

```http
POST /api/v1/conversion-runs
Content-Type: application/json

{
  "profile_id": "s100-oe-3.7.0",
  "model_path": "models/resnet18.onnx",
  "calibration_path": "calibration/imagenet",
  "core_num": 1,
  "max_l2m_size": 0
}
```

`model_path` 与 `calibration_path` 是 Assets Volume 内的受控逻辑路径。M1 固定使用
`imagenet-resnet18` Recipe，并要求 20～100 张校准图片。

产品范围与工程边界见 [PRD](docs/PRD.md) 和
[项目设计文档](docs/PROJECT_DESIGN.md)。
