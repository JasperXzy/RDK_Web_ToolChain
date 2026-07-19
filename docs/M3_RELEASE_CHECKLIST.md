# M3 发布检查表

## 1. CPU 前置条件

- Docker Engine 为 `linux/amd64`。
- 本机已加载 OpenExplorer 3.7 CPU 基础镜像。
- 固定 ResNet18 基线 SHA-256 为
  `4e8f8653e7a2222b3904cc3fe8e304cd8b339ce1d05fd24688162f86fb6df52c`。
- 至少 20 份本地 ImageNet JPEG/PNG/BMP 样本。

## 2. 构建

```bash
docker build -f runner/Dockerfile.cpu \
  -t rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1 .
docker build -f infra/Dockerfile.controller \
  -t rdk-webtoolchain/controller:0.1-dev .
```

本机显卡不兼容 OE 3.7 时，不构建也不运行 `runner/Dockerfile.gpu`，保持
`RDKWT_GPU_ENABLED=false`。

## 3. 快速门禁

```bash
.venv/bin/python -m ruff check .
node --check services/controller/rdkwt_controller/web/app.js
.venv/bin/python -m pytest -q

RDKWT_RUN_DOCKER_TESTS=1 .venv/bin/python -m pytest \
  tests/integration/test_docker_runner.py \
  tests/integration/test_controller_docker_e2e.py -q
```

## 4. 真实 CPU/OE 门禁

```bash
RDKWT_RUN_OE_RELEASE_TESTS=1 \
RDKWT_RESNET18_ONNX=/path/to/resnet18.onnx \
RDKWT_IMAGENET_CALIBRATION_DIR=/path/to/imagenet \
.venv/bin/python -m pytest \
  tests/integration/test_controller_openexplorer_release.py -q
```

通过标准：

- S100 `nash-e`/单 Core 和 S600 `nash-p`/双 Core 均生成 HBM。
- 两个平台均产生 HBRuntime 输出 NPY 与 hb_verifier Cosine 摘要。
- 编译结果记录 Cache Key/命中状态，缓存卷与资产/任务卷相互隔离；相同 S100 配置再次提交时
  必须报告缓存已预热且命中。
- HBM 下载大小与哈希匹配，导出 ZIP 包含冻结请求、结果、YAML、模型和校准 Manifest。
- 测试结束后受管 Runner 和临时卷全部清理。

## 5. 多输入与比较门禁

- `npy_multi` ZIP 使用 `<input_name>/<sample>.npy`，输入目录与 ONNX 完全一致。
- 2～4 个目录的样本名集合、每输入 Shape/dtype 都一致；任意不一致时整个导入回滚。
- 动态维度未填写时转换预览失败；填写后静态维度仍不可改变，batch 必须为 1。
- 只允许比较同一模型的 2～4 个成功转换；失败、不同模型、重复 ID 均被拒绝。

这些条件由普通 Pytest 中的 Controller/Adapter 契约测试覆盖。

## 6. GPU 兼容主机补充门禁

仅在 OE 3.7 明确支持的 NVIDIA GPU 主机上执行：

1. 安装兼容 Driver 与 NVIDIA Container Toolkit，确认 Docker 注册 `nvidia` runtime。
2. 用合规 GPU 基础镜像构建固定 Runner：

   ```bash
   docker build -f runner/Dockerfile.gpu \
     --build-arg OE_BASE_IMAGE=<approved-oe-3.7-gpu-image> \
     -t rdk-webtoolchain/oe-runner-gpu:oe3.7.0-app0.1 .
   ```

3. 配置 `RDKWT_GPU_ENABLED=true`、固定 `RDKWT_GPU_RUNNER_IMAGE`，按需设置设备 ID 与 shm。
4. Preflight 必须为 `READY`，再执行 GPU Smoke 和至少一次真实转换/验证。
5. 记录 GPU、Driver、Toolkit、镜像 ID 和结果；失败不得影响 CPU 可用性。

本开发机因 GPU 代际不受 OE 3.7 支持，GPU 实机门禁明确标记为“待兼容主机执行”。
