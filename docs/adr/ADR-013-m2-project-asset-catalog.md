# ADR-013：M2 项目与资产目录纵向闭环

| 属性 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-19 |
| 关联阶段 | M2 P0 Web 产品（第一增量） |

## 背景

M1 转换 API 接受 Assets Volume 内的逻辑路径，足以验证 Runner，但不适合作为浏览器产品
边界。浏览器不得选择容器路径；任务也必须冻结模型和校准版本，避免同一路径内容变化后
无法复现。与此同时，上传文件属于不可信输入，Controller 又持有 Docker Socket，因此
本地 Web 修改接口必须具备 Host 与 CSRF 保护。

## 决策

1. 新增 `projects`、`assets`、`models/model_versions`、
   `calibration_sets/calibration_versions/calibration_samples` 表，并通过 Alembic
   `0002_m2_catalog` 从 M0 schema 原位升级。
2. Blob 使用 `SHA-256 + size + kind` 去重，文件写入 staging 后 `fsync`，再原子移动到
   `/assets/blobs/sha256/<prefix>/`。模型 Blob 保留系统生成的 `.onnx` 后缀，以兼容工具链；
   原始文件名只保存在元数据中。
3. 上传使用原始流式请求体，不信任 `Content-Type`。模型首期要求 `.onnx` 文件名；校准
   图片根据 JPEG/PNG/BMP 文件签名识别，并校验扩展名与内容一致。
4. 图片校准版本先处于 `DRAFT`，可登记最多 100 个样本。定稿时生成稳定顺序的只读源
   目录与 Manifest，记录每个样本的原名、哈希、大小和 MIME，并将版本切换为 `READY`。
5. 少于 20 张图片允许定稿并显示警告，但 M1 标准转换仍拒绝少于 20 张的版本。
6. `POST /api/v1/conversion-runs` 只接受 `model_version_id` 与
   `calibration_version_id`。Controller 从数据库解析受控路径，确认两者属于同一项目，并在
   提交前重新核对模型、校准 Manifest 和全部物化样本的哈希。
7. 项目删除先提供影响预览，并要求 `X-Confirm-Project` 精确匹配项目 ID。存在任何转换
   历史时暂时阻止删除；无引用的 Blob 才会从文件系统移除，共享 Blob 必须保留。
8. 所有 `/api/` 修改请求要求启动时随机生成的 `X-RDKWT-CSRF` Token；HTTP Host 使用
   Allowlist，带 `Origin` 的请求必须与当前 Host 同源。应用不配置通配 CORS，并为产品页面
   设置 CSP、`nosniff` 与 `no-referrer` 响应头。
9. Docker Client 改为首次 Preflight 或任务操作时惰性连接。Docker 暂不可用不再阻止
   Controller 和项目页面启动，页面通过 Preflight 呈现故障。

## API 增量

```text
GET    /api/v1/session
GET    /api/v1/projects
POST   /api/v1/projects
GET    /api/v1/projects/{project_id}
PATCH  /api/v1/projects/{project_id}
GET    /api/v1/projects/{project_id}/deletion-preview
DELETE /api/v1/projects/{project_id}

GET    /api/v1/projects/{project_id}/models
POST   /api/v1/projects/{project_id}/models
GET    /api/v1/model-versions/{version_id}

GET    /api/v1/projects/{project_id}/calibration-sets
POST   /api/v1/projects/{project_id}/calibration-sets
GET    /api/v1/calibration-versions/{version_id}
POST   /api/v1/calibration-versions/{version_id}/samples
POST   /api/v1/calibration-versions/{version_id}/finalize
```

浏览器工作台直接调用以上 API，提供项目总览、上传进度、模型与校准版本选择、平台选择、
转换提交和最近任务轮询。

## 已知限制

- Controller 暂不直接解析 ONNX；模型版本标记为 `PENDING_INSPECTION`，真实结构检查仍由
  隔离 Runner 在转换开始时完成。独立的 Model Inspection 任务属于下一增量。
- 校准上传当前只支持单张图片流；NPY、ZIP 安全解包、断点续传和分片上传尚未实现。
- 校准 Recipe 仍锁定为 M1 `imagenet-resnet18`，尚未提供声明式 Recipe 编辑与预览。
- Web 当前轮询任务摘要；SSE 日志、取消、重试与重启恢复属于后续 M2 增量。
- CSRF Token 保存在 Controller 进程内，重启后浏览器需重新获取。
- 有历史任务的项目尚不能级联删除，避免在产物导出与引用计数未完成前丢失审计记录。

## 自动化与验证证据

- `tests/unit/test_controller.py` 覆盖 CSRF、上传去重、校准定稿、ID 化转换和安全删除。
- `tests/unit/test_migrations.py` 从 M0 schema 升级并确认既有任务记录保持不变。
- `tests/unit/test_docker_gateway.py` 覆盖 Docker Client 惰性连接。
- ResNet18 S100/S600 Golden 使用与 Catalog 相同的内容寻址 `.onnx` 路径。
- 本地临时实例已通过无头 Chrome 以 1440×1000 视口检查；HTML、CSS、JS 与所有初始化
  API 均返回成功。

## 后续决策

[ADR-014](./ADR-014-m2-orchestration-and-web-product.md) 已完成本 ADR 所列的独立模型检查、
声明式图片预处理预览、SSE、取消、重试、重启恢复、产物导出和终态项目级联删除。直接
NPY/ZIP 校准导入仍保留为后续范围。
