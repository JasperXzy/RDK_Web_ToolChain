# RDK WebToolChain 文档索引

本目录记录 RDK WebToolChain 的产品范围、系统架构和工程约束。项目定位为运行在开发者本机的 S100/S600 模型转换工作台，不面向公网或多租户部署。

## 核心文档

- [产品需求文档（PRD）](./PRD.md)：定义产品目标、用户流程、功能范围、优先级、非功能要求和验收标准。
- [项目设计文档](./PROJECT_DESIGN.md)：定义本地部署架构、Docker 调度方式、组件职责、数据模型、API、任务协议、安全边界、测试和实施方案。
- [ADR-011](./adr/ADR-011-runner-runtime-boundary.md)：记录 M0 Runner 运行时、Named Volume 与受限容器 Spike 结论。

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
