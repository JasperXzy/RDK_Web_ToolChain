# RDK WebToolChain

RDK WebToolChain 是面向 S100/S600 与 OpenExplorer 3.7.0 的本地模型转换工作台。

当前实现处于 M0：提供版本化 Controller/Runner 合约、S100/S600 Target Profile、最小
FastAPI Controller、SQLite 状态和受控 Docker Runner 探针。

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

产品范围与工程边界见 [PRD](docs/PRD.md) 和
[项目设计文档](docs/PROJECT_DESIGN.md)。

