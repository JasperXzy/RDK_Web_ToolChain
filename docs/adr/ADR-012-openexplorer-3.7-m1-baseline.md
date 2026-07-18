# ADR-012：OpenExplorer 3.7.0 M1 ResNet18 基线

| 属性 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-19 |
| 关联阶段 | M1 OpenExplorer 纵向闭环 |

## 背景

M1 需要在与生产 Runner 相同的安全限制下验证完整纵向链路：ONNX 检查、校准预处理、
受控 YAML 生成、PTQ 编译和产物归集。验证必须同时覆盖 S100 与 S600，且不得向公开仓库
提交受限制模型、校准图片或专有二进制。

Horizon 样例下载器记录的历史 ResNet18 MD5 为
`62de4ff68317c65ab4bb6a451e719e6d`，但官方 FTP 在本次验证中持续 120 秒零字节超时。
因此基线使用 ONNX Model Zoo 发布的 `resnet18-v1-7.onnx`，文件大小为 46,820,737 字节，
SHA-256 为 `4e8f8653e7a2222b3904cc3fe8e304cd8b339ce1d05fd24688162f86fb6df52c`。
该模型只保存在被 Git 忽略的本地参考目录。

## 环境

- OpenExplorer 基础镜像：`ai_toolchain_ubuntu_22_s100_s600_cpu:v3.7.0`；
- 基础镜像 digest：
  `sha256:c6b9dc0a061844245dafd661cc23cba67208d50942a0dc2717f2fd3f24f5bf01`；
- HBDK：4.7.5；
- HMCT：2.6.5；
- `hb_compile`：3.5.3；
- 校准集：本地 OpenExplorer 样例携带的 20 张 ImageNet 图片；
- 容器限制：禁网、只读根文件系统、只读 Assets、`cap_drop=ALL`、
  `no-new-privileges`、8 GiB 内存和 4 CPU。

## 实测结果

以下性能值是编译器静态估算，不是开发板实测值。

| 项目 | S100 | S600 |
|---|---:|---:|
| `march` | `nash-e` | `nash-p` |
| `core_num` | 1 | 2 |
| `max_l2m_size` | 0 | 自动（YAML `null`） |
| 优化等级 | O2 | O0 |
| 校准样本 | 20 | 20 |
| 最终量化余弦相似度 | 0.994883 | 0.994565 |
| HBM 大小 | 12,203,840 B | 13,137,104 B |
| 静态延迟 | 411 µs | 278.4 µs |
| 静态 FPS | 2433.29 | 3592.52 |
| 每次运行 DDR 访问 | 12,173,312 B | 12,405,632 B |
| 每次运行 L2M 访问 | 0 B | 9,229,312 B |
| Manifest 产物数 | 11 | 11 |

两次完整链路均生成有效 HBM、YAML、模型检查 JSON、校准 Manifest、工具日志、量化信息、
建议 CSV/JSON 和静态性能 JSON/HTML。所有 Artifact Manifest 中的大小与 SHA-256 已重新核验。

## 决策

1. OpenExplorer 3.7.0 的版本相关逻辑放入 `openexplorer-3.7.0` Adapter，不进入
   Controller 领域逻辑。
2. M1 只支持单个 RGB/NCHW 图像输入和固定 `imagenet-resnet18` Recipe；支持多输入和
   自定义 Recipe 前必须扩展规范化配置 schema 与测试矩阵。
3. 标准转换要求 20～100 张真实校准图片。Runner 按稳定顺序生成 NPY，并记录每个源文件
   和输出文件的 SHA-256。
4. S100 强制 `nash-e`、单核和 L2M=0；S600 强制 `nash-p`，允许双核。
5. S600 的“自动 L2M”规范化值为字符串 `auto`，Adapter 渲染为 YAML `null`。真实输出确认
   OpenExplorer 将其解析为 `None` 并自动分配 L2M。
6. 只归集最终 HBM、配置、日志、报告和结构化 JSON/CSV；中间 ONNX/BC 保留在 Attempt
   工作目录，但不进入默认 Artifact Manifest。
7. JSON/HTML 报告均视为不可信产物。当前只提供下载和哈希校验，后续 UI 不得直接以
   同源权限执行其中脚本。

## 已知限制

- 当前基线模型不是 Horizon FTP 包中的历史字节副本；二者不得混用 MD5 或精度结论。
- `hb_compile --model` 的“检查”模式会执行一次无真实校准数据的快速编译，因此完整任务会
  产生额外中间文件和时间开销。
- 当前只解析静态性能 summary；节点级表格、建议内容和量化相似度仍保留在原始产物中。
- 尚未覆盖板端运行、HBRuntime、`hb_verifier`、多输入、动态 Shape 和 GPU Runner。

## 自动化证据

- `tests/unit/test_openexplorer_adapter.py`：平台约束、YAML 映射、预处理和性能 Parser。
- `tests/fixtures/openexplorer-3.7.0/`：S100/S600 脱敏静态性能 Golden fixture。
- `tests/integration/test_docker_runner.py`：受限 Runner 合约回归。
- `tests/integration/test_openexplorer_resnet18.py`：真实模型与校准数据的 S100/S600
  端到端编译 Golden 回归；默认关闭，需通过环境变量显式启用。
- 本 ADR 中的完整编译结果来自本机隔离 Named Volume；模型、图片和 HBM 未提交到 Git。
