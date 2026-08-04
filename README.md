# RDK WebToolChain

RDK WebToolChain 是面向 D-Robotics S100/S600 的本地模型转换与板端验证工作台
通过浏览器组织 ONNX 检查、PTQ 转换、结果分析和 HBM 上板测试，底层使用 OpenExplorer 3.7.0

## 前置条件

- Linux `amd64` 主机
- Docker Engine 和 Docker Compose v2
- 当前用户可访问 `/var/run/docker.sock`
- 已加载 OpenExplorer 3.7.0 CPU 基础镜像 `ai_toolchain_ubuntu_22_s100_s600_cpu:v3.7.0`
- 板端验证需要可通过 SSH 访问的 S100 或 S600

## 快速开始

### 1. 构建 CPU Runner

```bash
docker build \
  --file runner/Dockerfile.cpu \
  --tag rdk-webtoolchain/oe-runner-cpu:oe3.7.0-app0.1 \
  .
```

### 2. 启动应用

```bash
./scripts/rdkwt.sh install
```

脚本会创建 `infra/.env`、检查 Docker、写入 Docker Socket GID、构建 Controller 并启动服务
如需调整端口或资源限制，可先复制 `infra/.env.example` 为 `infra/.env` 再编辑

### 3. 打开页面

脚本会根据 `infra/.env` 中的 `RDKWT_PORT` 输出访问地址，默认地址为
[http://127.0.0.1:8080/](http://127.0.0.1:8080/)

## 使用流程

1. 创建项目并上传 ONNX，等待模型检查完成
2. 导入并定稿 20～100 组校准样本
3. 选择 S100 或 S600，配置输入与编译参数并提交转换
4. 查看验证和性能结果，下载 HBM 或可复现导出包
5. 添加开发板并核对 SSH Host Key
6. 运行模型信息、单图推理或性能测试

## 常用命令

| 命令 | 用途 |
|---|---|
| `./scripts/rdkwt.sh doctor` | 检查 Docker、配置和 Runner 镜像 |
| `./scripts/rdkwt.sh up` | 构建并启动应用 |
| `./scripts/rdkwt.sh down` | 停止应用，不删除数据卷 |
| `./scripts/rdkwt.sh backup` | 创建完整备份 |
| `./scripts/rdkwt.sh upgrade` | 备份、拉取更新并重建 |
| `./scripts/rdkwt.sh diagnostics` | 生成脱敏诊断文件 |

恢复备份前，先在维护页上传并校验文件

```bash
./scripts/rdkwt.sh restore <维护页显示的备份文件名>
```

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest -q
```

需要真实 OpenExplorer 镜像或开发板的集成测试默认跳过
