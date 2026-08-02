# M4 板端发布检查表

## 1. 前置条件

- 已有一个 M3 CPU Runner 生成并成功验证的 S100 或 S600 HBM；开发机 GPU 保持
  `RDKWT_GPU_ENABLED=false` 即可。
- 目标板可从 Controller 所在网络访问，SSH/SFTP 可用，磁盘空间足够。
- 板端安装 `hrt_model_exec`，且 HBM 目标平台与板卡一致。
- 从设备控制台或可信运维渠道取得 SSH Host Key 的 OpenSSH `SHA256:` 指纹；不要仅依据网页
  首次观察值完成核对。
- 使用专用低权限账号；若板端 Runtime 必须 root，限制该账号的网络来源并保护凭据。

## 2. 自动化门禁（无需板卡）

```bash
.venv/bin/ruff check .
node --check services/controller/rdkwt_controller/web/app.js
.venv/bin/pytest -q
```

必须覆盖：密文不含明文凭据、`0600/0700` 权限、API 不回显、Host Key 未信任/变化、工具缺失、
平台不匹配、S100/S600 Core 选择器限制、官方命令参数、`model_info/infer/perf` 解析、取消、
重启状态和产物哈希。

## 3. UI 实机门禁

1. 打开“开发板 → 添加开发板”，录入平台、地址、用户和密码/私钥。保存后确认列表及 API
   均不显示凭据。
2. 首次探测核对弹出的 Host Key 指纹；拒绝后不应连接，确认并保存后再次探测。
3. 探测必须显示正确平台、OS、`hrt_model_exec` 版本、SFTP 和 `/tmp` 空间。
4. 选择同平台成功转换，运行 `model_info`；核对模型名、输入和输出，并下载原始日志。
5. 对 NV12 图像模型上传一张 JPG/PNG 运行 `infer`；确认图片按转换配置缩放和中心裁剪，并自动
   映射为 Y、UV 两个物理输入。核对推理延迟，下载并检查 dump BIN。
6. 分别按帧数和按分钟运行 `perf`；核对平均/最低/最高延迟、FPS 和 profile 文件。
7. 启动长时间 `perf` 后取消；任务应进入 `CANCELLED`，SSH Channel 关闭，远端临时目录清理。
8. 用错误平台 HBM、错误 Host Key、错误凭据和缺失工具各执行一次负向测试；必须在上传/执行前
   或受控阶段失败，并给出稳定错误码。
9. 检查 `/tmp/rdkwt` 无遗留任务目录（除非显式启用 `RDKWT_BOARD_KEEP_REMOTE=true`）。

## 4. 可选直连自动门禁

仓库提供默认跳过的真实板卡测试。以下变量必须来自本机安全环境，不要提交到 `.env` 或 Git：

```bash
RDKWT_RUN_BOARD_RELEASE_TESTS=1 \
RDKWT_BOARD_HOST=192.168.1.10 \
RDKWT_BOARD_USER=root \
RDKWT_BOARD_PLATFORM=s100 \
RDKWT_BOARD_HOST_KEY='SHA256:...' \
RDKWT_BOARD_AUTH_TYPE=password \
RDKWT_BOARD_PASSWORD='...' \
RDKWT_BOARD_HBM=/absolute/path/model.hbm \
.venv/bin/pytest tests/integration/test_board_release.py -q
```

私钥认证改用 `RDKWT_BOARD_AUTH_TYPE=private_key` 和
`RDKWT_BOARD_PRIVATE_KEY_FILE=/absolute/path/key`。设置 `RDKWT_BOARD_INPUT` 后额外执行 infer。

## 5. 发布判定

- 自动化门禁全部通过。
- 要声明某个平台“板端已验证”，必须在该平台真实设备上完成第 3 节，并保存板型、OS、Runtime
  版本、HBM SHA-256、固定参数、结构化指标、profile 和原始日志。
- 当前没有板卡时，M4 软件实现可标记为完成，但实机状态必须标记为“待板卡执行”，不得宣称
  `model_info/infer/perf` 已在真实 S100/S600 上通过。
- 本开发机新 GPU 不受 OE 支持与本门禁无关；M4 不要求启用 GPU。
