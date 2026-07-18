# M2.1 发布检查表

## 1. 前置资产

- Docker Engine 可用且宿主机架构为 `linux/amd64`。
- OpenExplorer 基础镜像 `ai_toolchain_ubuntu_22_s100_s600_cpu:v3.7.0` 已加载。
- ResNet18 ONNX 与至少 20 份 ImageNet JPEG/PNG/BMP 样本只保存在本机。
- 默认发布基线模型 SHA-256：
  `4e8f8653e7a2222b3904cc3fe8e304cd8b339ce1d05fd24688162f86fb6df52c`。

## 2. 构建固定镜像

```bash
docker build -f runner/Dockerfile.cpu \
  -t rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1 .
docker build -f infra/Dockerfile.controller \
  -t rdk-webtoolchain/controller:0.1-dev .
```

## 3. 快速门禁

```bash
.venv/bin/python -m ruff check .
node --check services/controller/rdkwt_controller/web/app.js
.venv/bin/python -m pytest -q

RDKWT_RUN_DOCKER_TESTS=1 .venv/bin/python -m pytest \
  tests/integration/test_docker_runner.py \
  tests/integration/test_controller_docker_e2e.py -q
```

## 4. 真实 S100/S600 发布门禁

```bash
RDKWT_RUN_OE_RELEASE_TESTS=1 \
RDKWT_RESNET18_ONNX=/path/to/resnet18.onnx \
RDKWT_IMAGENET_CALIBRATION_DIR=/path/to/imagenet \
.venv/bin/python -m pytest \
  tests/integration/test_controller_openexplorer_release.py -q
```

需要替换模型基线时，先完成独立审查，再显式传入
`RDKWT_RESNET18_SHA256=<reviewed-sha256>`；不得仅为通过门禁而跳过哈希。

## 5. 通过标准

- 模型隔离检查为 `READY`，动态 batch 只产生需显式 Target Shape 的警告。
- ZIP 原子导入 20 份真实图片，校准 Manifest 定稿并冻结。
- S100 生成 `nash-e`、单 Core、L2M=0 的非空 HBM。
- S600 生成 `nash-p`、双 Core、自动 L2M 的非空 HBM。
- 两个平台的 HBM 下载大小与 SHA-256 均匹配 Artifact Manifest。
- 两个平台的导出 ZIP 均包含版本清单、冻结请求、结果、YAML、模型与校准 Manifest。
- 测试结束后不存在属于本次 Run ID 的受管 Runner 容器，临时 Named Volume 已删除。

任一项失败都不得标记 M2.1 可发布；保留失败 Attempt 的日志与结果进行定位。
