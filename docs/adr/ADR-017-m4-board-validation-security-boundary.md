# ADR-017：M4 板端验证与 SSH 安全边界

| 属性 | 内容 |
|---|---|
| 状态 | Accepted（真实板卡门禁待有设备时执行） |
| 日期 | 2026-07-22 |
| 关联阶段 | M4：板端闭环 |

## 背景

M3 已在 CPU OpenExplorer 3.7 Runner 中完成 S100/S600 HBM 生成、数值验证、比较和缓存，但
静态编译指标不能代替目标开发板上的真实运行。当前开发机 GPU 代际不受 OE 3.7 支持；板端
验证不能依赖该 GPU，也不能把 SSH 凭据或任意远端命令带入转换 Runner。

## 决策

1. Controller 直接建立 SSH/SFTP 连接，Runner 继续以 `network=none` 运行。板端路径不调用
   本机 GPU，也不在开发机加载 OE GPU Runtime；它只上传已成功转换且哈希复核过的 HBM。
2. `devices` 只保存设备元数据、加密凭据引用和固定 Host Key 指纹。密码/私钥使用 Fernet
   加密后写入 `/state/secrets/<opaque-ref>.secret`；目录为 `0700`，主密钥和密文为 `0600`。
   SQLite、API 响应、任务快照、日志和导出均不包含凭据。该机制防止普通误读和备份泄漏，
   不声称能抵抗已取得 Controller 进程权限或主机 root 权限的攻击者。
3. SSH Client 不读取用户 `known_hosts`、SSH Agent 或默认私钥。每次连接都计算 OpenSSH
   `SHA256:` 指纹并与设备记录做常量时间比较。首次连接只返回观察到的指纹，必须由用户通过
   可信渠道核对并显式保存；指纹变化时连接失败，不自动接受。
4. 远端工作区固定为 `/tmp/rdkwt/<board-run-uuid>`。目录、上传、下载与清理由 SFTP API
   完成，拒绝中间符号链接；不接受来自 API 的远端路径。完成、失败或取消后默认清理，可由
   管理员用 `RDKWT_BOARD_KEEP_REMOTE=true` 暂时保留用于受控排障。
5. 唯一允许的远端可执行程序是 `uname` 和 `hrt_model_exec`。探测固定调用
   `hrt_model_exec --version`；任务固定调用 `model_info`、`infer` 或 `perf`。参数从 UUID、受限
   文件名、平台 Core 白名单和有界整数生成，再用 `shlex.join` 编码；API 不接受命令、Shell、
   环境变量、工具路径或额外参数。
6. `model_info` 保存模型输入输出结构和原始日志；`infer` 保存延迟和 dump NPY；`perf` 保存
   平均/最低/最高延迟、FPS 和 profile 文件。下载时再次核对路径、类型、大小和 SHA-256。
   命令输出、上传和下载都有上限。
7. 板端任务使用独立 SQLite 持久队列和单并发 Worker。排队任务在重启后继续；正在运行的
   SSH 命令无法安全接管，重启时标记 `INTERRUPTED`。取消会记录状态并关闭活动 Channel/Client。
8. 按 OE 3.7 契约，`core_id=0` 表示自动调度、`1` 表示 Core 0、`2` 表示 Core 1；S100
   允许选择器 0/1，S600 允许 0/1/2。`thread_num` 限制为 1～32，`perf_time` 的单位为分钟。
   设备必须先通过 SFTP、OS/板型、磁盘、`hrt_model_exec` 和平台一致性探测，且 HBM Profile
   平台必须与设备一致，才可提交任务。

## API 与部署增量

```text
GET/POST                 /api/v1/devices
GET/PATCH/DELETE         /api/v1/devices/{device_id}
POST                     /api/v1/devices/{device_id}/probe
POST                     /api/v1/devices/{device_id}/board-runs
GET/POST                 /api/v1/board-runs
POST                     /api/v1/board-runs/infer
GET                      /api/v1/board-runs/{board_run_id}
GET                      /api/v1/board-runs/{board_run_id}/events
GET                      /api/v1/board-runs/{board_run_id}/logs
GET                      /api/v1/board-runs/{board_run_id}/artifacts/{index}
POST                     /api/v1/board-runs/{board_run_id}/cancel
```

新增部署设置为 `RDKWT_BOARD_CONNECT_TIMEOUT_SECONDS`、
`RDKWT_BOARD_COMMAND_TIMEOUT_SECONDS`、`RDKWT_BOARD_MAX_UPLOAD_BYTES` 和
`RDKWT_BOARD_KEEP_REMOTE`。迁移 `0006_m4_board_validation` 新增 `devices` 与 `board_runs`。

## 验收证据

- 凭据密文/权限、API 不回显、Host Key 首次信任、平台不匹配、解析器、三种任务持久化、
  产物哈希和迁移已有无板卡自动化测试。
- 原有 Controller 测试与 JavaScript 语法检查通过。
- 真实 S100/S600 SSH、SFTP、`model_info/infer/perf` 仍必须按
  [M4 发布检查表](../M4_RELEASE_CHECKLIST.md) 在对应板卡上执行；当前没有板卡或凭据时不得
  把模拟测试表述为实机通过。
- 开发机 GPU/OE 不兼容不阻塞 M4，因为板端命令在 RDK 设备上执行；生成 HBM 仍沿用已通过
  的 CPU Runner 路径。

## 后果

- 转换 Run 与板端实测 Run 通过不可变 HBM 哈希关联，静态性能和真实设备数据可分别追溯。
- 用户必须维护 Host Key 和 Secret Store 备份；丢失 `/state/secrets/master.key` 后无法恢复
  已保存凭据，需要重新录入。
- Controller 获得对已登记开发板的有限出站访问能力，因此仍只能作为本机单用户应用部署。
