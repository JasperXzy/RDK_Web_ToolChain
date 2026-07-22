# RDK WebToolChain 文档索引

本目录记录 RDK WebToolChain 的产品范围、系统架构和工程约束。项目定位为运行在开发者本机的 S100/S600 模型转换工作台，不面向公网或多租户部署。

## 核心文档

- [产品需求文档（PRD）](./PRD.md)：定义产品目标、用户流程、功能范围、优先级、非功能要求和验收标准。
- [项目设计文档](./PROJECT_DESIGN.md)：定义本地部署架构、Docker 调度方式、组件职责、数据模型、API、任务协议、安全边界、测试和实施方案。
- [ADR-011](./adr/ADR-011-runner-runtime-boundary.md)：记录 M0 Runner 运行时、Named Volume 与受限容器 Spike 结论。
- [ADR-012](./adr/ADR-012-openexplorer-3.7-m1-baseline.md)：记录 M1 ResNet18 S100/S600 真实校准编译基线与 Adapter 决策。
- [ADR-013](./adr/ADR-013-m2-project-asset-catalog.md)：记录 M2 项目/资产目录、内容寻址存储、校准版本和本地 Web 安全边界。
- [ADR-014](./adr/ADR-014-m2-orchestration-and-web-product.md)：记录 M2 独立模型检查、持久队列、SSE、取消/恢复、结果导出和六步 Web 闭环。
- [ADR-015](./adr/ADR-015-m2-1-calibration-and-release-gate.md)：记录 M2.1 直接 NPY、ZIP 原子导入、真实 opset 边界和 Controller 级 S100/S600 发布门禁。
- [ADR-016](./adr/ADR-016-m3-verification-comparison-and-optional-gpu.md)：记录 M3 数值验证、多输入、比较、缓存和默认关闭的 GPU 控制面。
- [ADR-017](./adr/ADR-017-m4-board-validation-security-boundary.md)：记录 M4 加密凭据、固定 SSH Host Key、受限板端命令、任务与清理边界。
- [M2.1 发布检查表](./M2_1_RELEASE_CHECKLIST.md)：记录镜像构建、自动化测试、真实资产门禁与发布判定命令。
- [M3 发布检查表](./M3_RELEASE_CHECKLIST.md)：记录 CPU 验证/比较/缓存门禁与兼容 GPU 主机补充步骤。
- [M4 板端发布检查表](./M4_RELEASE_CHECKLIST.md)：记录 S100/S600 SSH/SFTP、`model_info/infer/perf`、负向测试和实机发布判定。

## 已确认的核心决策

1. 产品仅在本机运行，默认只监听 `127.0.0.1`。
2. 使用 Docker Compose 交付 WebToolChain 控制容器。
3. 控制容器通过宿主机 Docker Socket 调用 Docker Engine，即 Docker-outside-of-Docker。
4. 每次模型转换创建一个短生命周期 OpenExplorer 任务容器。
5. 第一阶段聚焦 ONNX、PTQ、S100/S600 和 OpenExplorer 3.7.0。
6. 使用 SQLite 保存元数据，使用 Docker Named Volume 保存任务数据和产物。
7. 不依赖云服务，不主动上传模型、校准数据、日志或使用数据。

## 参考资料

- [OpenExplorer 目录说明](../references/open_explorer_v3.7.0/README-CN)
- [OpenExplorer Docker 启动脚本](../references/open_explorer_v3.7.0/run_docker.sh)
- [PTQ 转换流程](../references/oe-doc-3.7.0-s100-s600/guide/ptq/ptq_workflow.html)
- [模型量化与编译](../references/oe-doc-3.7.0-s100-s600/guide/ptq/ptq_usage/quantize_compile.html)
- [hb_compile 配置说明](../references/oe-doc-3.7.0-s100-s600/guide/ptq/ptq_tool/hb_compile/convert.html)
- [hrt_model_exec](../references/oe-doc-3.7.0-s100-s600/guide/ucp/runtime/tool_introduction/hrt_model_exec.html)
