# ADR-015：M2.1 校准输入与真实发布门禁

| 属性 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-19 |
| 关联阶段 | M2.1：发布加固与校准输入补齐 |

## 背景

M2 已形成图片校准的 Web 产品闭环，但 PRD P0 还要求直接 NPY、多文件导入和可重复的真实
S100/S600 发布验收。仅在 Adapter 层运行 Golden 无法证明模型上传、隔离检查、资产冻结、
持久队列、产物下载与导出包这一整条 Controller 路径可用。现有检查器还限定 opset 10～19，
与 ADR-012 固定且已由 OpenExplorer 3.7 成功编译的 opset 8 ResNet18 基线冲突。

## 决策

1. 校准版本创建时冻结 `source_type=images|npy`，定稿后不可修改。样本、Manifest、校验报告
   和 Run 快照都记录该类型，Controller 与 Runner 在执行前再次核对。
2. 直接 NPY 使用 `numpy.load(allow_pickle=False, mmap_mode="r")` 读取；拒绝对象、结构化、
   子数组、大端、Fortran 布局、非 1～4 维、非正维度、Header/Payload 大小不符，以及 NaN/
   Inf。允许的标量 dtype 为 bool、8/16/32 位有符号或无符号整数和 16/32/64 位浮点数。
3. NPY 定稿要求所有样本 Shape 与 dtype 完全一致。创建转换时，其 Shape 必须等于模型目标
   Shape 去除 batch 后的部分；直接 NPY 不接受图片 Recipe。Runner 在只读 Assets 上重复
   dtype、字节序、布局、大小、Shape、有限数值和哈希检查，再复制到 Attempt 工作目录。
4. ZIP 导入只接受 Stored/Deflate、非加密普通文件。拒绝绝对路径、`..`、反斜杠、符号链接、
   重名 basename、类型不匹配、异常压缩比、单项或总解压大小越界和超过版本剩余容量。先验证
   全部条目，再在一个数据库事务中登记；任意条目失败时版本样本数保持不变。
5. Web 根据版本类型切换扩展名、文案和预览。图片显示 Resize/Center Crop/归一化结果；
   NPY 隐藏 Recipe 与图片画布，显示冻结 Shape、dtype 和首样本统计，并在进入下一步前检查
   模型 Shape。
6. ONNX 检查的实测支持范围调整为 opset 8～19。下界来自固定 SHA-256 的 ResNet18 opset 8
   基线；发布门禁确认 OpenExplorer 3.7 会先转换到 opset 19，再完成 S100/S600 编译。范围外
   仍在创建转换容器前阻断。
7. 新增显式发布门禁 `test_controller_openexplorer_release.py`。它通过真实 HTTP API 上传模型、
   启动隔离检查、用 ZIP 导入 20 份 ImageNet 图片并定稿，然后在同一 Controller 队列中依次
   运行 S100 `nash-e`/单 Core/L2M=0 与 S600 `nash-p`/双 Core/自动 L2M。成功还必须满足
   HBM 非空、下载内容与 Manifest 哈希一致、导出 ZIP 包含冻结请求、结果、YAML、模型和校准
   Manifest，并且所有受管 Runner 容器已清理。

## 数据与 API 增量

迁移 `0004_m2_1_calibration_sources` 为校准样本增加 JSON 校验元数据。校准版本已有的
`source_type` 由本增量正式进入 API 与执行合约。

```text
POST /api/v1/projects/{project_id}/calibration-sets
  {"name":"...","source_type":"images|npy"}

POST /api/v1/calibration-versions/{version_id}/samples
POST /api/v1/calibration-versions/{version_id}/archives
POST /api/v1/calibration-versions/{version_id}/finalize
```

上传继续使用原始请求体和 `X-Filename` / `X-Filename-B64`，不接受客户端路径、解压目录或
其他 Docker 参数。

## 验收证据

- Ruff、JavaScript 语法检查和普通 Pytest：通过。
- NPY/ZIP、迁移、配置联动与 Runner 单元覆盖：通过。
- 受限 Runner 合约与 Controller Docker E2E：2 项通过。
- 真实 Controller ResNet18 S100/S600 发布门禁：1 项通过，用时 7 分 40 秒。
- S100 最终量化余弦相似度为 0.994883，与 ADR-012 固定基线一致。

普通 CI 不伪造专有 OpenExplorer 成功结果；真实门禁需在具有本地 Runner 镜像、固定模型和
校准图片的发布主机上显式启用。

## 后果与范围

- 图片与直接 NPY 都具备内容寻址、不可变清单、双层校验和 Web 配置闭环；ZIP 不会绕过单
  文件校验。
- Controller 新增 NumPy 运行依赖，用于在登记不可信 NPY 前执行受限读取与统计。
- M2.1 仍限定 CPU Runner、PTQ 和单个四维模型输入。多输入、HBRuntime、`hb_verifier`、
  任务比较、GPU 与板端闭环不在本决策范围。
