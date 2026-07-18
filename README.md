# RDK WebToolChain

RDK WebToolChain 是面向 S100/S600 与 OpenExplorer 3.7.0 的本地模型转换工作台。
当前代码已完成《项目设计文档》定义的 M2 P0 Web 产品闭环与 M2.1 发布加固：用户可以在浏览器中完成
环境预检、ONNX 检查、校准数据管理、六步配置、队列执行、实时日志、失败重试、结果查看
和可复现导出。

## M2 能力

- 首次启动预检 Docker Engine、`linux/amd64`、Runner 固定镜像、三个数据目录和剩余空间，
  并运行受控 Runner Smoke Test 展示工具链版本。
- ONNX 上传后自动创建隔离检查任务，解析 IR/opset、输入输出、算子统计和 external data；
  只有检查为 `READY` 且哈希未变化的版本可以提交转换。
- 校准集按内容寻址保存，支持 JPEG/PNG/BMP 图片或直接 NPY、单文件/ZIP 批量导入、20～100
  份样本和不可变清单。图片路径提供 Resize、Center Crop 与归一化预览；直接 NPY 路径冻结
  Shape、dtype、布局、有限数值统计，并在提交前与模型的去 batch 输入 Shape 交叉校验。
- 六步向导根据模型结构和 S100/S600 Target Profile 生成类型化配置与 YAML 预览；草稿保存
  在当前浏览器，NV12、通道、归一化、Core 和 L2M 等交叉约束在创建容器前校验。
- SQLite 持久队列默认只运行一个 Runner。Run 冻结模型、校准清单、Profile、生成 YAML、
  应用/合约版本和不可变镜像 ID；每次重试创建独立 Attempt。
- 任务详情通过 SSE 展示阶段和区分 stdout/stderr 的日志，支持断线续接、暂停、搜索、下载、
  排队/运行中取消，以及 Controller 重启后的容器对账与恢复。
- 结果页展示 HBM、Quantized Cosine、静态性能、阶段耗时、警告和分类产物；可下载单个产物、
  快速下载 HBM，或导出包含清单、快照、日志与产物校验信息的可复现 ZIP。
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
磁盘、Runner 内存、CPU、PID 上限和取消宽限时间。修改后需重建或重启 Controller。

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
HBM 哈希下载和可复现 ZIP 校验。更轻量的 Adapter Golden 说明见
`tests/integration/test_openexplorer_resnet18.py`；完整发布步骤见
[M2.1 发布检查表](docs/M2_1_RELEASE_CHECKLIST.md)。

## API 边界

浏览器以外的 API 客户端应先调用 `GET /api/v1/session`，保存返回的会话 Cookie，并在每个
修改请求中通过 `X-RDKWT-CSRF` 回传 Token。上传 API 使用流式原始请求体，文件名通过
`X-Filename` 或 UTF-8 Base64 编码的 `X-Filename-B64` 传递。转换 API 只接受已登记的资源
版本 ID 和白名单配置，不接受镜像、命令、宿主机路径、挂载或 `privileged` 参数。

当前正式转换路径是单个四维 ONNX 输入（动态维度需提供显式正整数目标 Shape）、图片或直接
NPY 校准、CPU Runner 与 PTQ。多输入、HBRuntime、`hb_verifier`、任务比较、GPU 和板端闭环
属于后续阶段。

产品范围与工程边界见 [PRD](docs/PRD.md)、[项目设计文档](docs/PROJECT_DESIGN.md) 和
[M2 编排决策](docs/adr/ADR-014-m2-orchestration-and-web-product.md) 和
[M2.1 发布加固决策](docs/adr/ADR-015-m2-1-calibration-and-release-gate.md)。
