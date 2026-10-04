# Muse Display MCP

树莓派上的显示设备统一控制 MCP 服务：Muse / 任意 MCP 客户端 → `display.*` 工具 → Adapter → 设备。

V0.1 设备：

| 设备 | 类型 | 传输 | 能力 |
| --- | --- | --- | --- |
| BOOX NoteX（1404×1872 墨水屏） | `boox` | ADB（网络 5555） | text / card / image / clear / status 全套 |
| HoloCubic（240×240 ESP32 魔方屏） | `holocubic` | MQTT + HTTP 视频拉取 | text / image / video / clear / status |

## MCP 工具（需求 §4）

| 工具 | BOOX | HoloCubic |
| --- | --- | --- |
| `display.list_devices` | ✅ | ✅ |
| `display.show_text` | ✅ 渲染 JPEG 推送 | ✅ MQTT 命令 |
| `display.show_card` | ✅ 卡片布局 | ❌ `CAPABILITY_NOT_SUPPORTED` |
| `display.show_image` | ✅ prompt≈240s / url·path≈15s 双超时（评审 D6） | ✅ HTTP 图片 URL |
| `display.show_video` | ❌ `CAPABILITY_NOT_SUPPORTED` | ✅ 本地视频静音播放，1–30 秒 |
| `display.clear` | ✅ home 键 | ✅ |
| `display.get_status` | ✅ 电量/屏幕/前台应用 | ✅ presence 级 online |

错误一律 `{"success": false, "error": {"code", "message"}}`（需求 §11），八类错误码见 [src/muse_display/errors.py](src/muse_display/errors.py)。

## MQTT 契约（HoloCubic 固件对接）

Topic（需求 §7，ack 为 V0.1 补定，固件按此实现）：

```
muse/display/{device_id}/command   下行 QoS1  {"command_id","command","payload","ttl"?}
muse/display/{device_id}/ack       上行 QoS1  {"command_id","status":"displayed|failed","error"?}
muse/display/{device_id}/status    上行 QoS0 retain  {"online":true} 30s 心跳
                                   LWT 同 topic retain  {"online":false}
```

规则（评审 D3/D4）：MCP 先订阅 ack 再发布；普通命令超时 5s；重复 `command_id` 忽略（设备端 LRU 8–16 条去重）；90s 无心跳判离线。

### HoloCubic 图片与视频

- `display.show_image` 需要设备可访问的 HTTP(S) `image_url`。设备通过 HTTP 下载 RGB565 图片。
- `display.show_video(device_id, video_path, seconds=10)` 在 MCP 服务所在机器读取本地视频，调用 ffmpeg 转为 240×240、12fps MJPEG；HoloCubic 从 Pi 的临时 HTTP 服务拉流并静音播放。`seconds` 范围为 1–30。
- Pi 设备配置中的 `stream_host` 必须是 Pi 与设备均可达、且已绑定在 Pi 上的 LAN 地址。本仓库示例使用 `192.0.2.10`；部署到其他网络时请替换。
- 视频准备期间一次只允许一个请求；输入文件上限 100 MiB。临时视频文件在 MCP 服务生命周期结束时清理，HTTP 端口默认为 8124。
- 播放任务保留 MQTT 主循环。播放中收到新显示命令时固件会请求中断，并在当前播放任务退出后处理新命令。
- BOOX 不声明视频能力。HoloCubic 无音频输出，因此视频无声；当前固件视频解码与播放仍需在目标设备上实测。

## 本地开发（Windows）

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m pytest tests/ -q
```

测试全 mock：无树莓派、无设备、无 broker、无字体也能跑（渲染层回退 PIL 内置字体）。

> mcp 版本：已钉 `mcp>=2`（2026-10-04 实测 mcp 2.3.0）。注意 2.x 是破坏性改名：`mcp.server.fastmcp.FastMCP` → `mcp.server.mcpserver.MCPServer`，host/port 改经 `run(transport=..., host=..., port=...)` 传参——server.py 已适配。

## 树莓派部署（Pi 3B+ / Python 3.11）

```bash
# 1. 代码与虚拟环境
ssh user@192.0.2.10
sudo mkdir -p /opt/muse-display && sudo chown user /opt/muse-display
rsync -a --exclude .venv --exclude tests ./ user@192.0.2.10:/opt/muse-display/
cd /opt/muse-display && python3 -m venv .venv
.venv/bin/pip install -e .

# 2. MQTT broker
sudo apt install -y mosquitto
sudo cp deploy/mosquitto.conf /etc/mosquitto/conf.d/muse.conf
sudo cp deploy/acl /etc/mosquitto/acl
sudo mosquitto_passwd -c /etc/mosquitto/passwd muse   # 之后 -b 追加设备账号
sudo systemctl restart mosquitto

# 3. 凭据 + systemd
sudo mkdir -p /etc/muse-display && sudo cp deploy/env.example /etc/muse-display/env
sudo nano /etc/muse-display/env && sudo chmod 600 /etc/muse-display/env
sudo cp deploy/muse-display.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now muse-display
journalctl -u muse-display -f
```

Windows 侧接入：`claude mcp add --transport http muse-display http://192.0.2.10:8080/mcp`（tailnet）。

## 安全（需求 §13/§14）

- 服务只暴露 LAN + Tailscale，永不上公网；MQTT 禁匿名，ACL 每账号只碰自己的 topic 子树
- 凭据只进 `/etc/muse-display/env`（0600）：`MUSE_DISPLAY_TOKEN` / `MQTT_USERNAME` / `MQTT_PASSWORD`
- `MUSE_DISPLAY_TOKEN` 未设置 → 服务拒绝启动（评审 T1 Day-1 强制）
- 日志不落敏感内容：带 token 的图片 URL 过 `redact_url` 后才写日志

## 目录

```
src/muse_display/
├── server.py          # MCPServer（mcp 2.x）装配 + 入口（stdio | streamable-http）
├── tools.py           # DisplayService：能力门 + 错误映射 + 派生状态 + redact 日志
├── registry.py        # devices.yaml 严格加载（fail-fast）
├── base.py            # DisplayAdapter 抽象
├── errors.py          # 八类错误码 + §11 错误体
├── render.py          # PIL 文本/卡片渲染 + 图片适配（e-ink 黑白）
├── util.py            # command_id / redact_url / now_iso
├── mqtt_transport.py  # PahoTransport（paho v2；先订阅后发布）
└── adapters/
    ├── boox.py        # copy-adapt 自 executor.py 的已验证 ADB 链
    └── holocubic.py   # MQTT 命令/ack/状态（transport 可注入，测试零 broker）
config/devices.yaml   # 设备注册表
deploy/               # mosquitto / acl / systemd / env 模板
```

新增设备类型三步：adapter 模块 + `registry.ADAPTER_TYPES` + server 工厂（需求 §17）。

## 已知边界（V0.1 → 补齐轮）

- HoloCubic `show_card` / `show_image`、TTL 持久化、SQLite 状态、get_status 完整版 → 补齐轮（评审 c8009bd4）
- HTTP 模式 bearer 中间件未做，V0.1 靠网络边界（Tailscale ACL / LAN）
- 固件（HoloCubic MuseDisplay，C++）：`clear` + `show_text` + 心跳 + LWT + 去重，另仓开发
