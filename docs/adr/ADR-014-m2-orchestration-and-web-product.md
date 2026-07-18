# ADR-014：M2 持久任务编排与 Web 产品闭环

| 属性 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-19 |
| 关联阶段 | 项目设计 M2：P0 Web 产品 |

## 背景

ADR-013 建立了项目与内容寻址资产目录，但转换仍缺少独立模型检查、持久队列、实时日志、
取消/重试、Controller 重启对账、结果解释和可复现导出。仅靠同步 API 或内存中的后台任务
无法保证长时间 OpenExplorer 编译的可追溯性，也无法在浏览器刷新或 Controller 重启后给出
可信状态。

## 决策

1. ONNX 上传后立即创建 `MODEL_INSPECTION` Run，由同一受限 Runner 镜像在隔离容器中解析
   ONNX。检查记录文件哈希、IR、opset、输入输出、算子统计、动态维度和 external data；标准
   转换只接受哈希仍匹配且状态为 `READY` 的模型版本。
2. SQLite 是队列和状态事实来源。Controller 生命周期内只启动一个后台 Worker，默认只领取
   一个 `QUEUED` Attempt；所有状态变化先持久化，再执行外部 Docker 操作。
3. Run 冻结规范化 JSON、生成 YAML、模型/校准清单哈希、完整 Target Profile 快照、应用和
   Runner 合约版本、Runner 镜像引用及不可变镜像 ID。修改配置创建新 Run；相同快照重试只
   创建新的 Attempt 目录和记录，不覆盖历史数据。
4. Runner 容器继续使用固定 entrypoint、Named Volume、禁网、只读根文件系统、cap drop、
   `no-new-privileges`、资源限制和系统 Label。普通 API 不暴露镜像、命令、挂载、网络或
   privileged 参数。
5. 每个 Attempt 同时保存 `events.jsonl`、原始 `runner.log` 和带 sequence/stream/timestamp 的
   `logs/stream.jsonl`。事件与日志分别通过 SSE 输出，支持 `Last-Event-ID`/`after` 从断点续接；
   stdout 和 stderr 在采集与展示中保持区分。
6. 取消先写入 `cancel_requested_at`。排队任务原子转为 `CANCELLED`；运行任务按精确
   run/attempt Label 校验容器后发送 stop，超过配置的宽限时间由 Docker 强制终止。已有日志和
   可识别产物继续保留。
7. Controller 启动时对 SQLite 活跃 Attempt 和 Docker 管理容器做双向对账：匹配容器恢复
   监控，丢失或无法确认的任务标记为 `INTERRUPTED`，排队任务重新进入 Worker，孤儿管理容器
   被精确清理。系统不会从日志字符串推断成功。
8. 成功条件同时要求 Runner/`hb_compile` 退出正常、非空 HBM、结果和产物清单存在且通过
   哈希/大小校验。Controller 对 HBM、配置、日志、量化信息、静态报告和中间产物分类，并提供
   单文件下载、HBM 快速下载和可复现 ZIP。
9. Web 采用项目工作台、自动模型检查、图片校准集和六步转换向导。任务页展示阶段、断线可续
   日志、取消/重试、冻结配置、指标、产物与 sandbox 报告。未提交草稿保存在项目级
   `localStorage`。
10. 本地 Web 使用 HttpOnly SameSite 会话 Cookie、会话绑定 CSRF、Host Allowlist、同源
    Origin 检查、无通配 CORS、CSP、`nosniff` 和 `no-referrer`。安全响应头由纯 ASGI 中间件
    添加，避免缓冲 SSE 和大文件响应。

## 数据与 API 增量

迁移 `0003_m2_task_orchestration` 为模型版本增加检查关联，为 Run/Attempt 增加任务类型、冻结
镜像、YAML、阶段、取消时间和恢复标记。主要 API 增量如下：

```text
POST /api/v1/model-versions/{version_id}/inspect
POST /api/v1/conversion-previews
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/cancel
POST /api/v1/runs/{run_id}/retry
GET  /api/v1/runs/{run_id}/events
GET  /api/v1/runs/{run_id}/log-stream
GET  /api/v1/runs/{run_id}/logs/download
GET  /api/v1/runs/{run_id}/artifacts/{artifact_index}
GET  /api/v1/runs/{run_id}/export
POST /api/v1/system/preflight/runner-smoke-test
```

## M2 验收映射

| PRD 场景 | 实现与证据 |
|---|---|
| AC-001 首次预检 | `SystemService`、受控 Smoke Test、浏览器首次启动自动执行；Controller 单元与 Docker E2E 覆盖 |
| AC-002 S100 转换 | S100 Profile 锁定 `nash-e`/单核/L2M=0；OpenExplorer ResNet18 Golden 覆盖 |
| AC-003 S600 转换 | S600 Profile 锁定 `nash-p`，允许双核与受限 L2M；Adapter 单元与 Golden 覆盖 |
| AC-004 配置阻断 | 浏览器与 Pydantic/Adapter 双层校验，创建 Docker 容器前拒绝非法字段 |
| AC-005 任务失败 | Runner 失败结果、阶段、错误码、日志和已有产物保留；Runner 单元覆盖 |
| AC-006 任务取消 | SQLite 取消标记、受控 stop、终态保留日志、容器精确清理；Run Service 单元覆盖 |
| AC-007 Controller 重启 | 启动对账、容器恢复、队列恢复和 `INTERRUPTED` 兜底；Orchestrator 单元覆盖 |
| AC-008 Docker 参数保护 | 类型化 API 不接收 Docker 参数；固定安全容器 spec 与真实 Docker E2E 覆盖 |

`tests/integration/test_openexplorer_resnet18.py` 是需要本地官方模型和校准图片的显式 Golden；
普通 CI 不伪造 OpenExplorer 成功结果。

## 后果与范围

- M2 转换具备可恢复、可审计和可复现的本地产品闭环；Controller 重启不再丢失排队状态，
  浏览器刷新不再丢失任务日志位置。
- 项目删除可在没有活跃任务时级联删除终态 Run 目录和元数据；内容相同且仍被其他项目引用的
  Blob 保留。
- M2 原始正式路径限定为 CPU Runner、单个四维 ONNX 输入和图片校准；直接 NPY 与 ZIP
  导入已由 [ADR-015](./ADR-015-m2-1-calibration-and-release-gate.md) 在 M2.1 补齐。多输入、
  HBRuntime、`hb_verifier`、比较、GPU 和板端能力继续按后续阶段实现。
- CSRF 会话属于本机 Controller 进程；进程重启后页面会重新获取会话。产品仍不支持公网、
  多租户或任意网络暴露。
