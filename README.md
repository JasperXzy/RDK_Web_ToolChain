# RDK WebToolChain

RDK WebToolChain 是面向 S100/S600 与 OpenExplorer 3.7.0 的本地模型转换工作台。
当前代码已完成 M5 软件闭环：用户可在浏览器中完成环境预检、ONNX 检查、单/多输入
校准、动态 Shape 配置、队列执行、HBRuntime/`hb_verifier` 验证、任务比较、缓存、结果查看和
可复现导出，并把成功 HBM 关联到 S100/S600 开发板执行 `model_info/infer/perf`。GPU 控制面
已实现但默认关闭；本机新 GPU 不受 OE 支持，但这不影响 CPU 转换和板端验证。真实板卡门禁
需在有对应设备与凭据时执行。M5 另提供项目便携包、存储安全清理、带 SHA-256 清单的系统
备份与停服恢复、脱敏诊断和非 root Controller。

## 当前能力（M5）

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
- 开发板凭据使用本机 Fernet Secret Store，SQLite 和 API 只保存/返回非敏感引用或元数据；
  SSH 禁用 Agent/默认密钥并强制固定 Host Key，首次指纹必须显式确认，变化时拒绝连接。
- 板端任务只允许同平台成功 HBM 和固定 `hrt_model_exec model_info/infer/perf`，远端目录固定在
  `/tmp/rdkwt/<run-id>`，支持 SSE 状态、取消、原始日志、推理输出、profile 和结构化实测指标。
- 项目可导出/导入模型与校准输入；ZIP 路径、大小、压缩比和逐文件哈希严格校验，导入模型
  必须重新检查，不携带任务、设备或凭据。
- “维护”页展示四个持久卷的占用与余量；清理必须先预览并回传一次性令牌，活动任务期间拒绝
  执行，且只触及上传暂存、可再生导出、孤立任务目录和缓存，不自动删除成功 HBM。
- 系统备份使用 SQLite 一致快照和逐文件 SHA-256，可选任务与加密凭据、始终排除缓存；恢复
  必须停服执行并先创建恢复前安全备份。诊断 JSON 不包含凭据、环境变量、日志或业务文件。
- 一次性 `volume-init` 服务在首次升级时迁移 Named Volume 所有权；Controller 随后直接以
  专用非 root `rdkwt` 用户、只读根文件系统和零 Linux Capability 运行。Docker Socket 仍属于
  高权限边界，因此服务保持本机监听和白名单参数。

## 本地运行

先准备本地 OpenExplorer 3.7.0 基础镜像，再构建固定 Runner：

```bash
docker build \
  --file runner/Dockerfile.cpu \
  --tag rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1 \
  .
```

推荐使用 M5 运维入口，它会检查 Docker、自动写入实际 Socket GID、保护 `.env` 权限并启动：

```bash
./scripts/rdkwt.sh install
```

也可手动准备 Compose 环境文件；但在启用 user namespace/remap 的 Docker 上，宿主看到的
Socket GID 可能不同于容器内 GID，因此仍应先执行 `./scripts/rdkwt.sh doctor` 自动探测，再启动：

```bash
cp --no-clobber infra/.env.example infra/.env
docker compose --env-file infra/.env -f infra/compose.yaml build controller
./scripts/rdkwt.sh doctor
docker compose --env-file infra/.env -f infra/compose.yaml up -d controller
```

浏览器访问 `http://127.0.0.1:8080/`。首次打开会自动执行预检；环境可用后，按“项目 →
上传 ONNX → 等待模型检查 → 创建并定稿校准集 → 新建转换”的顺序生成 HBM。随后打开
“开发板”，添加并探测设备、通过可信渠道核对 Host Key，再创建板端验证。

常用部署参数位于 `infra/.env`，包括 Runner 镜像、任务超时、日志/上传上限、最低剩余
磁盘、Runner 内存、CPU、PID 上限、缓存、板端连接/命令超时和可选 GPU 设置。修改后需重建
或重启 Controller。Secret Store 位于状态卷 `/state/secrets`；备份时必须同时保存
`master.key`，但不得把该目录提交到 Git 或普通诊断包。

常用维护命令：

```bash
./scripts/rdkwt.sh doctor
./scripts/rdkwt.sh backup
./scripts/rdkwt.sh diagnostics
./scripts/rdkwt.sh upgrade
# 恢复文件先在“维护”页上传校验，然后：
./scripts/rdkwt.sh restore backup-YYYYMMDDTHHMMSSZ-xxxxxxxx.rdkwt-backup.zip
```

恢复会短暂停服，并在覆盖前自动创建 `pre-restore` 安全备份。重要备份仍应下载到另一块受控
存储介质；包含凭据的备份同时包含主密钥，必须按认证材料保护。

### GPU 注意事项

默认 `RDKWT_GPU_ENABLED=false`。只有在 GPU、Driver、NVIDIA Container Toolkit 和 OE 3.7
版本明确兼容时，才构建 `runner/Dockerfile.gpu` 并配置固定镜像。当前开发机显卡代际过新，
没有运行 GPU OE；CPU 转换和真实发布门禁不依赖 GPU。兼容主机的步骤见
[M3 发布检查表](docs/M3_RELEASE_CHECKLIST.md)。

### 板端注意事项

板端运行由 Controller 通过 SSH/SFTP 直接完成，不依赖开发机 GPU。先在设备控制台或可信
运维渠道取得 Host Key 指纹；网页首次观察到的值只能用于比对，不能替代带外核验。完整的
正向、负向和清理验收见 [M4 板端发布检查表](docs/M4_RELEASE_CHECKLIST.md)。

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

当前正式转换路径是 CPU Runner 与 PTQ，支持 1～4 输入、动态目标 Shape、图片/直接 NPY/
多输入 NPY 校准、数值验证、比较和缓存。板端 SSH/SFTP、`model_info/infer/perf` 的软件闭环
已完成；只有在对应真实设备上跑完 M4 门禁后，才可宣称该平台已完成实机验证。GPU 控制面
尚未在兼容硬件上完成真实门禁。

产品范围与工程边界见 [PRD](docs/PRD.md)、[项目设计文档](docs/PROJECT_DESIGN.md) 和
[M2 编排决策](docs/adr/ADR-014-m2-orchestration-and-web-product.md) 和
[M2.1 发布加固决策](docs/adr/ADR-015-m2-1-calibration-and-release-gate.md)，以及
[M3 验证与可选 GPU 决策](docs/adr/ADR-016-m3-verification-comparison-and-optional-gpu.md) 和
[M4 板端安全边界](docs/adr/ADR-017-m4-board-validation-security-boundary.md)，以及
[M5 发布维护决策](docs/adr/ADR-018-m5-release-maintenance.md) 和
[M5 发布检查表](docs/M5_RELEASE_CHECKLIST.md)。
