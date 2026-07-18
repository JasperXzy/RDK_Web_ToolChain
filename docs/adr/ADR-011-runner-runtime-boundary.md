# ADR-011：OpenExplorer Runner 运行时与存储边界

| 属性 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-19 |
| 关联阶段 | M0 基础骨架与合约 |

## 背景

M0 需要确认 Controller 能够在不开放任意 Docker 参数的前提下启动 OpenExplorer
Runner，并在禁网、只读资产和只读容器根文件系统条件下收集结构化结果。

## 验证结果

1. 本机 Docker Engine 29.6.1、API 1.55、`linux/amd64` 可用。
2. OpenExplorer CPU 基础镜像
   `ai_toolchain_ubuntu_22_s100_s600_cpu:v3.7.0` 可用，镜像 digest 为
   `sha256:c6b9dc0a061844245dafd661cc23cba67208d50942a0dc2717f2fd3f24f5bf01`。
3. 基础镜像内 Python 版本为 3.10.12，因此 Runner 源码必须保持 Python 3.10 兼容；
   Controller 仍以 Python 3.11 为目标。
4. `hb_compile` 可执行，但不支持 `--version`。Runner Probe 使用 `hb_compile --help`
   验证工具可用性，HMCT/HBDK 版本通过 Python distribution metadata 与最终版本清单采集。
5. 以下容器限制下，Runner Probe 可以读取资产、运行 `hb_compile --help`、写入事件、
   结果和 Artifact Manifest：

   - `network=none`；
   - 容器根文件系统只读；
   - Assets Volume 只读；
   - Runs Volume 可写；
   - `cap_drop=ALL`；
   - `no-new-privileges`；
   - 受限 `/tmp` tmpfs。

6. 宿主机临时 bind mount 在当前 Docker UID 映射下不可由 Runner 写入；Docker Named
   Volume 可正常写入。这验证了项目设计中“子容器只接收固定 Named Volume”的选择。
7. 派生 Runner 使用固定 JSON entrypoint，任务请求无法指定镜像、entrypoint、宿主机路径、
   `privileged`、网络模式或 Docker Socket。
8. 真实 Controller 容器已完成一次端到端 Probe：创建 Runner、持续读取日志、验证
   `result.json` 与 Artifact Manifest、更新 SQLite，并精确删除对应 Runner 容器。

## 决策

- Runner 代码以 Python 3.10 为最低运行版本，并在真实 OpenExplorer CPU 镜像中测试。
- Controller 与 Runner 之间继续使用版本化 JSON 合约；Runner 不依赖 Controller 的
  FastAPI、SQLAlchemy 等依赖。
- Runner 只挂载预配置的 Assets/Runs Named Volume，不支持任务级 bind mount。
- Runner 将 `HOME`、Matplotlib 和 XDG Cache 指向受限 `/tmp`，避免只读根文件系统告警
  或写入失败。
- Controller 创建任务时使用解析后的不可变 image ID，并自行生成容器名、Label、命令参数
  和挂载配置。

## 尚未覆盖

本次 Probe 没有执行真实 ONNX 检查或 PTQ 编译。以下内容必须在 M1 ResNet18
S100/S600 Golden 测试中继续验证：

- `hb_compile check/compile` 在禁网、只读 Assets 和 `cap_drop=ALL` 下的完整兼容性；
- SIGTERM、超时、OOM 和大体积中间产物行为；
- OpenExplorer 实际产物命名、结构化字段和文件所有权；
- S600 双 Core 与 L2M 参数。

## 自动化证据

- `tests/integration/test_docker_runner.py`：受限 Runner 合约闭环。
- `tests/integration/test_controller_docker_e2e.py`：Controller 到 Runner 的端到端闭环。
- 默认测试覆盖 JSON Schema、Target Profile、路径边界、容器参数白名单和 API。

