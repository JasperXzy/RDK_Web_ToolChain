# RDK WebToolChain

RDK WebToolChain 是面向 S100/S600 与 OpenExplorer 3.7.0 的本地模型转换工作台。
当前代码已完成 M3 的 CPU 发布路径：用户可在浏览器中完成环境预检、ONNX 检查、单/多输入
校准、动态 Shape 配置、队列执行、HBRuntime/`hb_verifier` 验证、任务比较、缓存、结果查看和
可复现导出。GPU 控制面已实现但默认关闭，需在 OpenExplorer 支持的 GPU 主机上另行验收。

## 当前能力（M3）

- 首次启动预检 Docker Engine、`linux/amd64`、CPU 固定镜像、状态/资产/任务/缓存存储和剩余
  空间，并运行受控 Runner Smoke Test；GPU 是可选检查，不可用时不会阻断 CPU。
- ONNX 上传后自动创建隔离检查任务，解析 IR/opset、输入输出、算子统计和 external data；
  只有检查为 `READY` 且哈希未变化的版本可以提交转换。
- 校准集按内容寻址保存，支持图片、直接 NPY 和多输入 NPY ZIP、20～100 组逻辑样本和不可变
  清单。多输入 ZIP 严格使用 `<input_name>/<sample>.npy` 并原子核对样本名、Shape 与 dtype。
- 六步向导按 ONNX 自动生成 1～4 个 Rank 1～4 输入；动态维度必须填写，静态维度不可更改，
  batch 当前固定为 1。NV12、通道、归一化、Core、L2M 和校准源在创建容器前交叉校验。
- SQLite 持久队列默认只运行一个 Runner。Run 冻结模型、校准清单、Profile、生成 YAML、
  应用/合约版本和不可变镜像 ID；每次重试创建独立 Attempt。
- 任务详情通过 SSE 展示阶段和区分 stdout/stderr 的日志，支持断线续接、暂停、搜索、下载、
  排队/运行中取消，以及 Controller 重启后的容器对账与恢复。
- 转换默认用 HBRuntime 对 optimized ONNX 做真实单样本推理，再由 `hb_verifier` 比较
  optimized/calibrated ONNX；输出 NPY、摘要和日志均可下载。结果页展示验证、HBM、性能、
  缓存和阶段耗时，并可对比同一模型的 2～4 个成功任务。
- 编译缓存使用独立 Named Volume 和绑定模型、校准、配置、Profile、Adapter、镜像的 Cache
  Key；支持启用、关闭或强制重建，每次仍保留独立 Run 历史。
- 默认只监听本机，Runner 禁网、只读根文件系统、无特权、无 Docker Socket；Web 修改请求
  使用 HttpOnly SameSite 会话、CSRF、Host/Origin 校验，HTML 报告在 sandbox iframe 中展示。

## 本地运行

先准备本地 OpenExplorer 3.7.0 基础镜像，再构建固定 Runner：

```bash
docker build \
  --file runner/Dockerfile.cpu \
  --tag rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1 \
  .
```

准备 Compose 环境文件，将 `DOCKER_GID` 改为本机
`stat -c '%g' /var/run/docker.sock` 的结果，然后启动：

```bash
cp infra/.env.example infra/.env
docker compose --env-file infra/.env -f infra/compose.yaml up -d --build
```

浏览器访问 `http://127.0.0.1:8080/`。首次打开会自动执行预检；环境可用后，按“项目 →
上传 ONNX → 等待模型检查 → 创建并定稿校准集 → 新建转换”的顺序操作。

常用部署参数位于 `infra/.env`，包括 Runner 镜像、任务超时、日志/上传上限、最低剩余
磁盘、Runner 内存、CPU、PID 上限、缓存和可选 GPU 设置。修改后需重建或重启 Controller。

### GPU 注意事项

默认 `RDKWT_GPU_ENABLED=false`。只有在 GPU、Driver、NVIDIA Container Toolkit 和 OE 3.7
版本明确兼容时，才构建 `runner/Dockerfile.gpu` 并配置固定镜像。当前开发机显卡代际过新，
没有运行 GPU OE；CPU 转换和真实发布门禁不依赖 GPU。兼容主机的步骤见
[M3 发布检查表](docs/M3_RELEASE_CHECKLIST.md)。

## 本地开发与验证

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
```

Docker 合约与 Controller 端到端测试需要预先构建上述两个镜像：

```bash
RDKWT_RUN_DOCKER_TESTS=1 .venv/bin/python -m pytest \
  tests/integration/test_docker_runner.py \
  tests/integration/test_controller_docker_e2e.py
```

真实 S100/S600 发布门禁为显式启用项，需要本机 ResNet18 ONNX 与至少 20 张校准图片：

```bash
RDKWT_RUN_OE_RELEASE_TESTS=1 \
RDKWT_RESNET18_ONNX=/path/to/resnet18.onnx \
RDKWT_IMAGENET_CALIBRATION_DIR=/path/to/imagenet \
.venv/bin/python -m pytest \
  tests/integration/test_controller_openexplorer_release.py -q
```

该门禁通过完整 Controller API 执行模型上传与隔离检查、ZIP 导入与冻结、S100/S600 编译、
HBRuntime、`hb_verifier`、HBM 哈希下载、可复现 ZIP 和重复 S100 缓存命中校验。更轻量的
Adapter Golden 说明见
`tests/integration/test_openexplorer_resnet18.py`；完整发布步骤见
[M3 发布检查表](docs/M3_RELEASE_CHECKLIST.md)。

## API 边界

浏览器以外的 API 客户端应先调用 `GET /api/v1/session`，保存返回的会话 Cookie，并在每个
修改请求中通过 `X-RDKWT-CSRF` 回传 Token。上传 API 使用流式原始请求体，文件名通过
`X-Filename` 或 UTF-8 Base64 编码的 `X-Filename-B64` 传递。转换 API 只接受已登记的资源
版本 ID 和白名单配置，不接受镜像、命令、宿主机路径、挂载或 `privileged` 参数。

当前正式发布路径是 CPU Runner 与 PTQ，支持 1～4 输入、动态目标 Shape、图片/直接 NPY/
多输入 NPY 校准、数值验证、比较和缓存。GPU 控制面已实现但尚未在兼容硬件上完成真实门禁；
板端 SSH/SFTP、`model_info/infer/perf` 属于 M4。

产品范围与工程边界见 [PRD](docs/PRD.md)、[项目设计文档](docs/PROJECT_DESIGN.md) 和
[M2 编排决策](docs/adr/ADR-014-m2-orchestration-and-web-product.md) 和
[M2.1 发布加固决策](docs/adr/ADR-015-m2-1-calibration-and-release-gate.md)，以及
[M3 验证与可选 GPU 决策](docs/adr/ADR-016-m3-verification-comparison-and-optional-gpu.md)。
