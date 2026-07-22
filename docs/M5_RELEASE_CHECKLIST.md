# M5 发布与维护检查表

## 1. 静态、单元与普通回归

```bash
.venv/bin/ruff check .
node --check services/controller/rdkwt_controller/web/app.js
bash -n scripts/rdkwt.sh
sh -n infra/controller-entrypoint.sh
sh -n infra/volume-init.sh
.venv/bin/pytest -q
```

重点覆盖清理令牌与计划变化、备份清单/hash/SQLite、篡改成员拒绝、恢复前安全备份、项目包
往返及模型重新检查状态。

## 2. Headless Chrome 门禁

本门禁启动临时 Controller，并用本机 Chrome/Chromium 执行真实页面脚本和 API 初始化：

```bash
RDKWT_RUN_BROWSER_TESTS=1 \
.venv/bin/pytest tests/integration/test_browser_release.py -q
```

必须看到 M5 维护入口、完成 Session/API 初始化，并把“正在检查环境”更新为实际预检状态。

## 3. Docker 与非 root 门禁

```bash
./scripts/rdkwt.sh up
docker compose --env-file infra/.env -f infra/compose.yaml exec -T controller id
docker compose --env-file infra/.env -f infra/compose.yaml exec -T controller \
  sh -c 'test "$(id -u)" != 0 && test -w /state && test -w /assets && test -w /runs && test -w /cache'
```

`id` 必须显示非 root `rdkwt`，Controller 必须保持只读根文件系统和零 Capability，同时能通过
实际 Socket GID 访问 Docker。`volume-init` 只能保留卷迁移所需三项能力且正常退出。若 CPU
Runner 镜像尚未准备，doctor 可以报告缺失，但维护页、项目管理和备份仍应可用；转换发布仍
必须补齐 CPU 镜像。

## 4. 维护 UI 与安全清理

1. 打开“维护”，核对 state/assets/runs/cache 占用和文件系统余量。
2. 在测试项目生成项目导出与 Run Export，只选择“生成的导出包”，先预览再执行。
3. 预览后新增/修改候选文件，旧令牌必须返回 `CLEANUP_PLAN_STALE`。
4. 有排队或运行任务时，所有清理执行必须返回 `CLEANUP_ACTIVE_TASKS`。
5. 清理缓存后确认成功 HBM、项目原始模型、校准数据和备份仍存在；下一次启用缓存的转换应
   重新构建而不是引用缺失数据。

## 5. 项目迁移门禁

1. 导出包含模型、图片/NPY/多输入 NPY 校准的项目。
2. 在同一实例导入，核对新 Project ID、资产哈希和校准 `READY/DRAFT` 状态。
3. 确认所有模型为 `PENDING_INSPECTION/INSPECTING`，而不是继承原项目的 `READY`。
4. 修改 ZIP 中一个字节、加入 `../`、符号链接或未登记成员，各自必须在创建项目之前拒绝。
5. 确认项目包不含 Conversion Run、HBM、Device 或凭据。

## 6. 备份与恢复演练

只在一次性测试部署执行完整恢复，不要用首次演练覆盖唯一的生产数据：

1. 在维护页创建“包含任务和加密凭据”的完整备份并下载一份到另一存储介质。
2. 重新上传该文件，必须显示逐文件校验通过；篡改 ZIP 后上传必须失败。
3. 记录当前项目、设备、Run、HBM 哈希，然后创建一组只用于确认回滚的额外数据。
4. 执行 `./scripts/rdkwt.sh restore <页面显示的完整文件名>`。
5. 服务重启后，备份时的数据必须恢复，额外数据必须消失；模型/校准/Run/HBM/设备凭据可用。
6. 备份列表必须多出 `pre-restore-*.rdkwt-backup.zip`，并可独立通过 verify。

## 7. 发布判定

- 全部普通测试、浏览器门禁、Docker 非 root/卷写入检查通过。
- 在一次性部署完成项目迁移与完整备份恢复演练，并把备份复制到独立介质。
- M5 不要求 GPU；本开发机继续保持 `RDKWT_GPU_ENABLED=false`。CPU OE 转换与真实板卡状态仍按
  M3/M4 各自检查表判定，不能由维护测试替代。
