# ADR-016：M3 数值验证、多输入、比较、缓存与可选 GPU

| 属性 | 内容 |
|---|---|
| 状态 | Accepted（GPU 实机验收延后） |
| 日期 | 2026-07-19 |
| 关联阶段 | M3：验证、比较与 GPU |

## 背景

M2.1 已证明单输入图片/NPY 能通过完整 Controller 路径生成 S100/S600 HBM，但转换成功尚未
证明浮点中间模型可实际推理，也缺少不同配置间的结构化回归、多输入校准和工具链缓存。M3
还要求 GPU Worker；本开发机显卡代际新于 OpenExplorer 3.7 的支持范围，不能把本机 GPU
失败当成产品 CPU 路径失败，也不能伪造 GPU 成功证据。

## 决策

1. CPU Runner 是 M3 的规范发布路径。转换默认执行 `inspect → check → preprocess → compile →
   verify → collect`。验证可显式关闭；开启时先用 HBRuntime 对 `optimized_float_model.onnx`
   执行首组真实预处理输入，再用 `hb_verifier` 比较 optimized/calibrated ONNX。输出 NPY、
   两份结构化摘要和原始日志均进入 Artifact Manifest。
2. 输入配置扩展为一至四个、Rank 1～4，当前仍固定 batch=1。静态维度不得修改；动态维度
   必须填写正整数目标值。图片校准仅支持单个 Rank-4 输入；直接 NPY 支持单输入；多输入
   使用 `npy_multi` 冻结源类型。
3. 多输入 ZIP 必须严格为 `<input_name>/<sample>.npy`。只允许 2～4 个输入目录，目录名必须
   与 ONNX 输入完全一致，各目录样本 basename 集合完全相同，逻辑样本数为对齐后的文件名
   数。Controller 原子登记，Runner 再按输入名、Shape、dtype、哈希和样本键复核，不依赖
   文件遍历顺序。
4. 任务比较只接受同一模型版本的 2～4 个成功转换任务，返回公共字段、差异字段和逐任务
   行。比较项包括 Profile、Runner、校准、编译参数、HBM/性能、量化与 verifier 指标、
   HBRuntime 耗时、总耗时和缓存命中；比较结果不改写历史 Run。
5. 编译缓存使用独立 `rdkwt-cache` Named Volume。Cache Key 绑定模型和校准哈希、规范化配置、
   Profile、Adapter 与冻结 Runner image ID，并传给 OE `cache_path`。缓存可关闭、启用或强制
   重建；每次仍创建独立 Run，M3 不直接复用最终 HBM。
6. CPU/GPU 镜像分别由部署设置固定，普通 API 只能选择 `runner_mode=cpu|gpu`，不能传入镜像、
   Device ID、共享内存或挂载。GPU 容器使用管理员配置的 NVIDIA DeviceRequest 与 shm size；
   Preflight 中 GPU 为可选检查，`DISABLED/UNAVAILABLE` 不阻断 CPU。
7. GPU 默认关闭。本开发机不构建、不启动 OE GPU Runner；只通过单元测试验证固定镜像解析、
   DeviceRequest、设备 Allowlist、共享内存和不可用时拒绝创建。必须在 OpenExplorer 3.7
   明确支持的 NVIDIA GPU、Driver 与 Container Toolkit 主机上补跑 GPU Smoke/转换，才能把
   GPU 实机状态从“待验收”改为“已通过”。
8. 迁移 `0005_m3_execution_metadata` 为 Run 冻结 `runner_mode`、`cache_key` 和 `cache_hit`。
   Retry 继续使用原 Runner image ID、运行模式、Cache Key 与完整请求快照。

## API 与部署增量

```text
POST /api/v1/conversion-runs
  input: {...}                         # 单输入
  inputs: [{...}, {...}]               # 多输入，二者互斥
  runner_mode: cpu|gpu
  cache_mode: disable|enable|force_overwrite
  verification: {mode: disabled|basic, compare_digits: 1..12}

POST /api/v1/run-comparisons
  {"run_ids": ["...", "..."]}
```

新增部署设置为 `RDKWT_CACHE_DIR`、`RDKWT_CACHE_VOLUME`、`RDKWT_GPU_ENABLED`、
`RDKWT_GPU_RUNNER_IMAGE`、`RDKWT_GPU_DEVICE_IDS` 和 `RDKWT_GPU_SHM_SIZE`。

## 验收证据

- Ruff、JavaScript 语法检查和普通 Pytest：通过（68 项通过，6 项按环境条件跳过）。
- 多输入对齐/回滚、动态 Shape、缓存键、比较 API、迁移、GPU 容器参数与解析器均有单元覆盖。
- 受限 Runner 合约与 Controller Docker E2E：2 项通过。
- 真实 Controller ResNet18 S100/S600 CPU 发布门禁：1 项通过，最终镜像用时 3 分 35 秒；门禁额外
  重复提交相同 S100 配置，并确认缓存已预热、命中且缓存文件持续存在。
- 两个平台均实际执行 HBRuntime 并生成输出 NPY，`hb_verifier` 返回可解析 Cosine；HBM 下载、
  SHA-256 和可复现 ZIP 同时通过。
- 验收审查发现并修复 OE 内旧版 NumPy 不支持 `max_header_size`、OE 日志前缀影响表格解析，
  以及多输入 `hb_verifier` 必须重复传入 `--input` 三项兼容问题；NPY 回退前仍手工限制
  Header 为 16 KiB。
- GPU 实机：未执行，原因是本开发机 GPU 不在 OE 3.7 支持范围；这不是 CPU 发布门禁的跳过项。

## 后果

- M3 的 CPU 数值验证、多输入数据模型、动态维度、任务比较和缓存形成完整 Web/API/Runner
  闭环。
- HBRuntime/hb_verifier 是数值冒烟和阶段回归，不等同于带标签业务数据集上的最终精度。
- GPU 控制面已经实现且默认安全关闭；在兼容主机完成实机门禁前，不得宣称 GPU 转换已验证。
