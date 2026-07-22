# ADR-018：M5 发布维护、备份恢复与非 root 边界

| 属性 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-22 |
| 关联阶段 | M5：发布与维护加固 |

## 背景

M4 已形成从项目资产、CPU OE 转换到开发板验证的软件闭环，但“能运行”仍不等于“可长期
使用”。Named Volume 中的数据库、模型、校准数据、HBM 和加密凭据需要可观测、可迁移和可
恢复；清理若缺少边界会直接威胁成功产物；运行中的 Controller 也不能安全替换自身 SQLite
或 Secret Store。

## 决策

1. 系统备份格式为 `*.rdkwt-backup.zip`。SQLite 使用 `sqlite3.Connection.backup()` 生成一致
   快照；模型和校准资产始终包含，任务目录与加密凭据可选，编译缓存始终排除。Manifest 为
   每个成员记录路径、字节数和 SHA-256，并记录应用版本、时间和 Scope。
   系统备份、SQLite 临时快照和项目便携包均以 `0600` 保存，避免同机其他普通用户读取模型或
   随备份保存的认证材料。
2. 校验拒绝绝对路径、`..`、反斜线、重复成员、符号链接、加密成员、未知压缩算法、过量
   Entry、超出展开上限和异常压缩比；随后逐文件读取并核对大小与哈希，再对 SQLite 执行
   `PRAGMA integrity_check` 和必要表检查。上传的备份只有全部通过后才进入备份目录。
3. Web 不在运行中执行恢复。恢复必须通过 `scripts/rdkwt.sh restore <filename>`：先停止
   Controller，再用同一镜像运行 `rdkwt-maintenance restore --confirm RESTORE`。恢复程序先生成
   完整 `pre-restore` 安全备份，把新内容放到每个目标卷内的临时目录，再交换数据库、Secret
   Store、资产和可选任务目录；异常时反向恢复已交换路径。成功后启动 Controller 并执行迁移。
4. 维护页清理只接受固定四类：超过 24 小时的上传暂存、可重新生成的导出 ZIP、数据库中已
   不存在的 UUID 任务目录、编译缓存。服务先返回候选数量、大小和五分钟一次性随机令牌；执行
   时类别、路径、大小、mtime 和活动任务状态必须与预览一致。成功 HBM、备份和登记资产不在
   清理集合中。
5. 项目便携包 `rdkwt-project` 只保存项目名称/备注、模型和校准版本及内容寻址文件，不保存
   转换历史、HBM、设备或凭据。导入复用与上传相同的文件格式验证并重建不可变校准目录；旧
   模型检查结果不受信任，所有模型重新进入隔离检查。
6. Controller 镜像创建专用 `rdkwt` 用户。一次性 `volume-init` 服务仅保留 `CHOWN`、
   `DAC_OVERRIDE`、`FOWNER` 三项能力，按构建时 UID/GID 迁移 Named Volume 后退出；Controller
   从第一个进程起就以 `rdkwt` 运行，并启用只读根文件系统、零 Linux Capability 和
   `no-new-privileges`。运维脚本从临时只读容器内探测实际 Docker Socket GID，避免 user
   namespace/remap 令宿主所见 GID 失真，再由 Compose 提供补充组。UID/GID 默认
   `10001:10001`。这不降低 Docker Socket 本身等价宿主 root 的能力；因此服务仍仅监听本机并
   保持固定镜像、固定挂载和白名单 API。
7. 诊断包只包含版本、非敏感设置、预检与存储统计；不包含环境变量、凭据、日志或业务文件。
   安装脚本不删除 Volume，`down` 也不带 `--volumes`。

## API 与运维增量

```text
POST /api/v1/projects/import
GET  /api/v1/projects/{project_id}/export

GET  /api/v1/maintenance/storage
POST /api/v1/maintenance/cleanup-preview
POST /api/v1/maintenance/cleanup
GET/POST /api/v1/maintenance/backups
POST /api/v1/maintenance/backups/import
GET  /api/v1/maintenance/backups/{filename}
GET  /api/v1/maintenance/diagnostics
```

新增 `rdkwt-maintenance backup/list/verify/restore` 和 `scripts/rdkwt.sh`。备份接收的压缩大小、
展开大小和成员数分别由 `RDKWT_MAX_BACKUP_BYTES`、
`RDKWT_MAX_BACKUP_UNCOMPRESSED_BYTES`、`RDKWT_MAX_BACKUP_ENTRIES` 控制。

## 后果

- 恢复需要短暂停机和足够的临时空间；这是获得数据库、Secret Store 和多卷一致边界的必要
  代价。关键部署仍应把下载后的备份复制到另一块受控介质，不能只留在同一主机。
- 备份包含凭据时同时包含 `master.key`，持有该备份等价于持有已保存的板端认证材料；默认
  本地 UI 明确警告，但最终保管责任属于操作者。
- 项目包适合迁移可再次转换的输入资产；任务复现与 HBM 仍使用已有 Run Export，避免一个
  “项目导出”隐式携带大量产物或敏感设备上下文。
