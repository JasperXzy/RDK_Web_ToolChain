# RDK WebToolChain

RDK WebToolChain 是面向 S100/S600 与 OpenExplorer 3.7.0 的本地模型转换工作台。

当前实现已进入 M2 Web 产品阶段：在 M1 OpenExplorer 3.7.0 真实编译闭环之上，已提供
项目工作台、ONNX 流式上传与内容去重、图片校准集草稿/定稿、资源版本 ID 转换提交，
以及 Host Allowlist、CSRF Token 和安全删除边界。

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest
```

构建基于本机 OpenExplorer CPU 镜像的固定 Runner：

```bash
docker build \
  --file runner/Dockerfile.cpu \
  --tag rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1 \
  .
```

Controller 的最小本地入口：

```bash
.venv/bin/rdkwt-controller
```

浏览器访问 `http://127.0.0.1:8080/`，即可创建项目、上传 ONNX、登记 20～100 张校准
图片并提交 S100/S600 转换。

API 修改操作需要先从 `GET /api/v1/session` 获取 CSRF Token，并通过
`X-RDKWT-CSRF` 请求头回传。提交转换时只接受已登记的资源版本 ID：

```http
POST /api/v1/conversion-runs
Content-Type: application/json
X-RDKWT-CSRF: <token>

{
  "profile_id": "s100-oe-3.7.0",
  "model_version_id": "<model-version-uuid>",
  "calibration_version_id": "<calibration-version-uuid>",
  "core_num": 1,
  "max_l2m_size": 0
}
```

上传 API 使用流式原始请求体，文件名通过 `X-Filename` 或 UTF-8 Base64 编码的
`X-Filename-B64` 传递。默认单文件上限为 2 GiB，可通过 `RDKWT_MAX_UPLOAD_BYTES` 调整。
M1 转换仍固定使用 `imagenet-resnet18` Recipe。

产品范围与工程边界见 [PRD](docs/PRD.md) 和
[项目设计文档](docs/PROJECT_DESIGN.md)。
