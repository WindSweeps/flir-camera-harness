# FLIR Camera Harness 1.0

## DeepSeek Harness（DSH）

### 桌面版插件安装

在 DSH 的「插件」页面选择从 Git 仓库安装，输入
`https://github.com/WindSweeps/flir-camera-harness`。

连接清单服务需要 Python 3.11 或更新版本。用 DSH 将要调用的 Python 安装依赖：

```sh
python3 -m pip install -r requirements.txt
```

Windows 默认使用 `python`，在上述命令中相应替换。使用虚拟环境时，将
`FLIR_INVENTORY_PYTHON` 设为该环境的 Python 绝对路径，确保从继承该变量的
环境启动 DSH。

在 Windows x64 相机工作站，将原发行包完整解压至
`C:/Data/FLIR_Camera_Harness`，或在启动 DSH 前用 `FLIR_RUNTIME_DIR`
指定实际解压目录。可使用 `FLIR_OUTPUT_DIR` 指定输出目录，使用
`FLIR_REPLAY=1` 启用内置回放。macOS 和 Linux 会加载连接清单服务。

### CLI 手动配置

相机服务使用已有的 Windows x64 发行包。将发行包完整解压到
`C:/Data/FLIR_Camera_Harness`，然后将本仓库放到
`C:/Data/flir-camera-harness`。在仓库目录中使用 Python 3.11 或更新版本执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
dsh --profile headless --patch "$PWD/examples/dsh.patch.yml" "调用 flir_status 和 get_connection_inventory"
```

目录有变化时，先编辑 [DSH 配置](examples/dsh.patch.yml) 中的两个
`command` 和对应的 `args`。桌面版可将两个 `insert` 条目加入该
profile 的 `cordis.patch.yml`。接入内置回放时，在相机服务的参数中追加
`--replay`。

## 设备中台接口

中台将探测结果交给 DSH 主 Agent，主 Agent 在同一个 DSH 会话中调用
`flir-camera-inventory` 服务的 `set_connection_inventory`。如果中台通过
MCP 路由直接提交，它也应调用同一个服务实例。参数与 Rigol 的连接清单一致。

```json
{
  "inventory": {
    "schema_version": 1,
    "devices": [{"resource": "flir:serial:12345678", "label": "FLIR 相机"}]
  }
}
```

`resource` 是中台提供的唯一连接标识。每次成功提交会替换整份清单，返回
`schema_version` 和 `device_count`。空数组可清空清单。主 Agent 调用
`get_connection_inventory` 读取当前报告。清单保存在该 MCP 进程内，进程
重启后中台需要重新提交。实际相机发现和连接状态可由 `flir_list_cameras`
和 `flir_status` 查询。

仓库提供 DSH 配置、连接清单服务以及发行包中的封装源码。相机可执行文件、
PySpin 和驱动来自现有 Windows 发行包和相机工作站。

## Windows 相机服务

把现有 FLIR ROI Camera 1.9 封装为 **31 个通用 MCP 工具**，让支持本地
stdio MCP 的 AI 客户端直接操作相机、记录实验并检查保存结果。
相机采集、触发照片、精简参数 TXT、连续记录和手机监控仍使用原软件的实现。

## 三步接入

1. 完整解压此目录到 Windows x64 电脑，例如 `C:\Data\FLIR_Camera_Harness`。
   保留 `runtime` 及其 `_internal`，无需另装 Python、Node.js，也不需要 API Key。
   实机采集使用这台电脑已有的 Spinnaker/USB 相机驱动；先前 1.9 能连接相机的环境可继续使用。
2. 双击 `make_config.cmd`。它按**当前解压路径**生成 `.mcp.json` 和
   `configs/hardware.mcp.json`、`configs/replay.mcp.json`。移动文件夹后重新运行。
   脚本只写本插件目录，不会修改任何 AI 客户端的全局配置。
3. 在 AI 客户端添加一个**本地 stdio MCP 服务器**，名称 `flir-camera`，复制
   `configs/hardware.mcp.json` 中的 `command` 和 `args`。若客户端接受
   `mcpServers` JSON，可合并该条目；不要整文件覆盖已有服务器配置。重连后应看到 31 个 `flir_*` 工具。

连接方式示例（实际以生成文件为准）：

```json
{
  "mcpServers": {
    "flir-camera": {
      "command": "C:\\Data\\FLIR_Camera_Harness\\runtime\\FLIR_ROI_Camera.exe",
      "args": ["--mcp", "--output-dir", "C:\\Data\\FLIR_Camera_Harness\\data"]
    }
  }
}
```

`command` 填可执行文件的完整路径，不要在路径内容中额外加引号；`args` 每项独立。
这是程序启动配置，**不是一个 HTTP URL**。只支持远程 HTTP MCP、不能启动本地进程的
云端客户端无法直接使用此版本。手机监控网址也不能当作 MCP 控制地址。

先想无相机试用时，改用 `configs/replay.mcp.json`。两份配置使用相同服务器名，
选择一份接入即可。回放参数为模拟值，不改变回放图像，不支持外部触发和触发照片。

MCP 启动后默认隐藏窗口、不开手机监控、不开相机采集。可让 AI 调用
`flir_show_window` 显示完整相机窗口。一个 MCP 进程管理一台相机；原软件或其他
客户端已占用同一相机时，先关闭其相机连接。此插件不会接管另一个正在运行的 1.9 进程。

## 可以直接对 AI 说

- “列出相机并连接唯一的一台，先不开始采集，告诉我参数范围。”
- “设曝光 5000 微秒、增益 3 dB，连续采集，并显示相机完整画面。”
- “统计 x=100、y=100、宽 180、高 140 的选区，从现在开始一直记录，等我说停止。”
- “停止记录并导出为 experiment01.csv，告诉我 CSV 和参数 TXT 的完整路径。”
- “先停止采集，把触发照片命名为 test_A，存入 photos，然后用 Line0 上升沿、延迟 9 微秒外部触发采集。”
- “打开自动曝光和自动增益，把自动增益上限设为 12 dB，读回确认。”
- “开启手机只读监控，把本机局域网访问链接给我。”

支持技能的客户端还可加载 `skills/flir-camera-harness/SKILL.md`。
其他客户端无需技能也能发现工具；也可将该文件作为相机操作说明提供给 AI。
技能说明不会自动建立 MCP 连接。

## 功能与工具

| 功能 | 工具 |
|---|---|
| 相机发现、连接、状态、采集 | `flir_status`、`flir_list_cameras`、`flir_open_camera`、`flir_close_camera`、`flir_start_capture`、`flir_stop_capture` |
| 全局参数、曝光、增益、触发 | `flir_parameters`、`flir_set_exposure`、`flir_set_gain`、`flir_set_auto_brightness`、`flir_set_trigger` |
| ROI、曲线、峰值、整幅预览、导出 | `flir_set_roi`、`flir_clear_curve`、`flir_clear_peak`、`flir_get_curve`、`flir_preview`、`flir_export_curve` |
| 不限 60 秒的记录及恢复 | `flir_record_start`、`flir_record_stop`、`flir_record_status`、`flir_record_export`、`flir_record_recover` |
| 后台任务查询 | `flir_job_status` |
| 触发照片设置与保存状态 | `flir_photo_configure`、`flir_photo_status` |
| 实验/标定关联信息 | `flir_context_get`、`flir_context_set` |
| 手机只读监控 | `flir_monitor_addresses`、`flir_monitor_start`、`flir_monitor_stop` |
| 显示/隐藏本机相机窗口 | `flir_show_window` |

全部参数约束来自 `tools.json`，与运行时 `tools/list` 使用同一份定义。
没有任意执行代码、任意写相机节点、删除文件目录的通用工具。
本版会读取/记录设备可用的 binning、硬件 ROI、像素格式、ADC 等节点，但**没有新增这些参数的设置器**。

## 数据放在哪里

生成配置默认使用本插件的 `data` 目录，可改 `--output-dir`。
相对路径以该目录为基准，也可在工具中指定绝对路径。

| 数据 | 默认位置/规则 |
|---|---|
| 当前曲线/完整记录 | AI 指定的 `.csv`，同位置自动生成同名 `.txt` |
| 未导出记录缓存 | `data/cache/record_*/session.jsonl` |
| 外部触发照片 | `data/photos/唯一批次目录/`，可由 `photo_configure` 更改 |
| 自检结果 | `_harness_test_output/report.json`、`verification.log` |

照片名为 `YYYYMMDD-HHMMSS-mmm_拍摄名称_000001.tif`，同名 TXT 记录参数。
时间取主机收到图像时的 UTC+08:00 系统时间；设备时间戳另记，不能当作相同时间。
照片是完整原始 8/16 位灰度 TIFF，曲线则是选区的显示灰度 DN 总和。
预览工具返回的 JPEG 用于查看，不替代原始 TIFF。

TXT **只记初始参数、参数变化、ROI/操作及标定关联信息**，不放原始像素或重复光强数据。
当前没有标定系数，所有实际功率/辐射量保持“未标定”。填入波长、倍率、ND 透过率、
暗场/平场文件等，只记录来源和文件哈希，不会自动应用校正或计算真实功率。

## 保存、过曝和性能

- 当前曲线只含最近 60 秒。长时间实验要先 `record_start`，结束时 `record_stop`，
  等到 `done=true` 后导出；清空曲线/重画 ROI 不清除已开始的完整记录。
- 保存返回 `job_id`，查询到 `status=succeeded` 才算完成。默认不覆盖既有 CSV/TXT。
  成功导出后清理本进程拥有的记录缓存；失败、取消或恢复的外部缓存会保留。
- 如果工作区/完整记录曾检测到过曝，保存默认返回 `SATURATION_WARNING`。
  AI 应先说明风险，得到你同意后才传 `allow_saturated=true` 继续保存。
  对异常中断的不完整缓存，另有 `allow_incomplete`。
- 保留原有低频过曝检测：重新框选、清除、修改参数时检查，其余约每 2 秒抽检。
  “未检测到”不保证每个瞬时峰值都未过曝。全局节点同样是采样读回；随帧 Chunk 可用时
  记录对应曝光/增益。记录全部**观察到的**变化，不声称捕获硬件的每个瞬时变化。
- 连续记录队列上限 512 条，后台批量落盘；照片上限 8 张/64 MiB；导出/哈希最多
  2 个后台任务，保留 64 个任务摘要及 32 个记录句柄。不会把整个长记录读进内存。
  文件本身随实验增长，请预留磁盘空间。缓存句柄淘汰后仍可按路径恢复文件。
- AI 应低频查询（约 0.5–2 秒），无需每次读图或刷新节点。预览完整视野且尺寸受限。
  31 个工具都在同一 Qt 线程串行操作设备；磁盘导出和哈希使用工作线程。

客户端正常关闭 stdin 后，服务会停止采集、结束记录、关闭监控并等待后台写入完成。
若客户端强制杀进程/电脑断电，无法保证最后一批数据落盘；可用 `record_recover`
恢复已有 `session.jsonl` 的完整行。旧 `record_id`、`job_id` 在重启后无效。

## 自检和技术边界

双击 `verify.cmd` 可运行内置 Qt/模拟 SDK 自检，不接触真实相机，报告在
`_harness_test_output/report.json`。`open_camera_gui.cmd` 启动普通图形版。
MCP 模式中弹窗提示写入 `status.recent_events`，保存确认默认拒绝；通过工具处理确认即可。
关闭 MCP 相机窗口会停止采集并结束该 MCP 进程；只想收起窗口时使用 `flir_show_window` 的 `visible=false`。
普通图形版仍保留正常交互弹窗。

原 Windows 发行包的验证结果见包内 `verification/verification.json`，包含协议、真实 EXE stdio 回放、
触发节点模拟、低频缓存、导出、记录恢复、原界面及手机监控回归。
已使用官方 MCP Python SDK **2.3.0** 的自动协商和 legacy 模式实际接入，验证
31 个工具的 schema、结构化结果、图片、错误及采集导出。测试依赖只用于开发验证，
使用插件无需安装这个 SDK。
真实相机的 Line0 电信号触发、设备自动曝光/增益行为和手机跨设备联网，需要接实机核验；
模拟与回放通过不等于已验证这些硬件行为。

实现支持 initialize 式 MCP 协议版本 `2025-11-25`、`2025-06-18`、`2025-03-26`、
`2024-11-05`。较新客户端需支持旧版协商/回退（官方 SDK 2.3.0 自动回退已验证）；
本版不宣称实现 2026 无状态协议。
stdio 仅写 JSON-RPC，诊断走 stderr，不向公网提供相机控制接口。
参考：[MCP 生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、
[MCP 工具协议](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。

`source` 提供封装源码、构建入口和测试，`manifest.json` 标识版本与原软件来源，
`tools.json` 提供工具契约；这三部分配合技能说明，构成可检查、可运行的 AI harness。
