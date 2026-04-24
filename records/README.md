# Siemens RFID 读取日志脚本

## 推荐：OPC UA 自动过站记录（功能 1 + 2）

`rfid_opcua_logger.py` 通过 OPC UA 协议同时实现两个核心功能：

**功能 1 — 自动开始/停止扫描**
- 实时轮询 `Diagnostics > Presence` 变量（手册 Section 3.3.3）
- 小车到来（Presence: 0 → >0）→ 自动调用 `ScanStart`（手册 3.1.2）
- 小车离开（Presence: >0 → 0）→ 自动调用 `ScanStop`

**功能 2 — 标签自动写入 CSV**
- 订阅 `RfidScanEventType` 事件，实时接收标签数据
- 每次过站结束后将本次所有标签写入每日 CSV

### WBM 前置配置（Settings > Communication > OPC UA）

| 选项 | 要求 |
|------|------|
| OPC UA 模式 | Main application |
| Presence events | **必须启用** |
| Last access events | 建议启用 |
| OPC UA 端口 | 默认 4840 |

### 快速启动

1. 首次安装依赖（只需一次）：
   ```powershell
   pip install asyncua
   ```
2. 双击 `run_opcua_logger.bat`（或手动运行 `python rfid_opcua_logger.py`）

### 关键配置项（`rfid_opcua_logger.py` 顶部）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OPCUA_URL` | `opc.tcp://192.168.0.254:4840` | OPC UA 服务器地址 |
| `OPCUA_USER` / `OPCUA_PASS` | 空 | WBM 认证用户名/密码 |
| `OUTPUT_DIR` | `C:\rfid_logger\records` | CSV 输出目录 |
| `READ_POINT` | `1` | 读点编号（RF695R 最多 4 个） |
| `POLL_INTERVAL` | `0.5` | Presence 轮询间隔（秒） |

---

这个脚本用于把 RF695R 读写器（配 RF662A 天线）每次读取到的数据自动记录到本地文件，方便工人查看历史记录。

默认读取地址是：`https://192.168.0.254/`

## 生成的日志文件

脚本运行后会在 `logs/` 目录下生成：

- `rfid_reads.csv`：适合 Excel 打开查看
- `rfid_reads.jsonl`：完整原始数据（便于后续分析）

## 使用方法（推荐）

1. 安装 Python 3（Windows 安装时勾选 Add Python to PATH）
2. 双击运行 `run_logger.bat`
3. 读写器有新数据时，会自动追加到日志文件

如果你是在 PowerShell 手动运行，请使用：

```powershell
.\run_logger.bat
```

不要直接输入 `run_logger.bat`，否则不会执行当前目录脚本。

建议先做一次连通性测试（会在控制台打印成功或超时信息）：

```powershell
python -u .\rfid_logger.py --url "https://192.168.0.254/" --interval 1 --timeout 8 --insecure
```

## 命令行运行（可选）

```bash
python rfid_logger.py --url "https://192.168.0.254/" --interval 1 --insecure
```

常用参数：

- `--url`：读写器页面/API地址（默认 `https://192.168.0.254/`）
- `--interval`：轮询间隔秒数（默认 `1`）
- `--timeout`：请求超时秒数（默认 `3`）
- `--log-all`：每次轮询都记录；默认只在数据变化时记录
- `--insecure`：跳过 HTTPS 证书校验（设备自签名证书常用）
- `--cookie`：附带浏览器登录态 Cookie（页面需要登录时）
- `--header`：自定义请求头（可重复添加）
- `--debug-payload`：把每次响应原文写入指定文件，便于排查 `tags=0`
- `--basic-auth`：使用 HTTP Basic 认证（格式 `用户:密码`）
- `--discover-endpoints`：自动扫描并测试可能的数据接口 URL（推荐在 `tags=0` 时使用）
- `--mode xml`：使用 RF69xR 官方 XML 通道（推荐）
- `--xml-port`：XML 通道端口（在 WBM 的 `Settings > Communication > XML` 中配置）

## CSV 字段说明

- `timestamp`：记录时间（本地时区）
- `source_url`：读取地址
- `host_name`：运行脚本的电脑名
- `tag_count`：识别出的标签数量
- `tags`：识别出的标签值（用 `|` 分隔）
- `payload_hash`：原始数据哈希（用于判断变化）
- `raw_preview`：原始数据简要预览

## 注意事项

- 如果你实际读取接口不是根路径 `/`，请把 `--url` 改成真实接口，比如：
  - `http://192.168.0.254/api/read`
  - `http://192.168.0.254/rfid`
- `https://192.168.0.254/#page=9` 里的 `#page=9` 只是浏览器前端路由，脚本里不要带 `#...`，直接用 `https://192.168.0.254/` 即可。
- 若终端是 `saved | tags=0`，常见原因是接口需要登录态。可在浏览器开发者工具里复制请求头里的 `Cookie`，再这样运行：
  - `python -u .\rfid_logger.py --url "https://192.168.0.254/实际数据接口" --insecure --cookie "你的Cookie" --debug-payload ".\logs\last_payload.html" --log-all`
- 如果无法使用 F12，可先测试设备是否支持 Basic 认证：
  - `python -u .\rfid_logger.py --url "https://192.168.0.254/" --insecure --basic-auth "admin:你的密码" --log-all --debug-payload ".\logs\last_payload.html"`
- 如果一直 `tags=0`，可自动探测候选接口：
  - `python -u .\rfid_logger.py --url "https://192.168.0.254/" --insecure --discover-endpoints`
  - 若需要带登录态：`python -u .\rfid_logger.py --url "https://192.168.0.254/" --insecure --cookie "你的Cookie" --discover-endpoints`

## 推荐：XML 官方接口采集（不用抓网页）

在设备 WBM 中先配置：

1. `Settings > Communication > XML`：启用 XML
2. 启用至少一种事件（建议先开 `Observed events`）
3. 选择 `Transmitting read point`（至少一个读点）
4. 设置 `XML channel (1-4)` 的端口号（记下这个端口）

然后运行：

```powershell
python -u .\rfid_logger.py --mode xml --url "https://192.168.0.254/" --xml-port 这里填XML端口 --timeout 8 --log-all
```

可选调试：

```powershell
python -u .\rfid_logger.py --mode xml --url "https://192.168.0.254/" --xml-port 这里填XML端口 --timeout 8 --log-all --debug-payload ".\logs\last_xml_payload.xml"
```
- 电脑与读写器要在同一网段，能互相访问。
- 如需 24 小时运行，建议放到一台固定工位机并设置开机自启动。
