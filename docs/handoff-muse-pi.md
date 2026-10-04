# Muse Display MCP 联调交接文档（树莓派部署）

- 日期：2026-10-04（v0.2 更新）
- 部署版本：v0.2（review R1–R3 已落地），前一版 v0.1 @ `2cca67b`
- 部署目标：Raspberry Pi 3B+ / Raspbian 12 (bookworm) / Python 3.11，SSH `zooman@192.168.50.207`
- 服务状态：systemd `muse-display`，已验证 active / enabled

---

## 1. 接入信息（联调必读）

| 项 | 值 |
|---|---|
| MCP 端点 | `http://100.77.242.40:8080/mcp` |
| 传输方式 | Streamable HTTP（MCP 2025-03-26 协议） |
| 鉴权 | **v0.2 起逐请求 Bearer（必带）**：每个请求头 `Authorization: Bearer <token>`，缺失/错误返回 HTTP 401。token 与服务启动门同源 `MUSE_DISPLAY_TOKEN` |
| 网络边界 | 仅 Tailscale 内网可达（100.77.242.40 是 Pi 的 Tailscale 地址），**未暴露公网**。隧道模式客户端见 §3 代理说明 |
| 设备 | `desk-cube`（HoloCubic，240×240），经 MQTT `muse/display/desk-cube/#` 控制本机 Mosquitto |

**获取 token**：在 Pi 上执行 `sudo grep MUSE_DISPLAY_TOKEN /etc/muse-display/env`。该值禁止外传、禁止写入任何仓库或聊天记录；联调结束后如有泄露风险应重新生成。

## 2. 已注册工具（tools/list 实测 7 个）

| 工具 | 参数 | 当前设备支持 |
|---|---|---|
| `display.list_devices` | 无 | ✅ 返回在线状态 / 传输 / 能力 |
| `display.get_status` | `device_id` | ✅ presence 来源 mqtt_presence |
| `display.show_text` | `device_id`, `text`, `ttl?` | ✅ 全屏文本，自动换行溢出截断 |
| `display.clear` | `device_id` | ✅ 恢复默认界面 |
| `display.show_card` | `device_id`, `title`, `body`, `priority?`, `ttl?` | ❌ 设备无 card 能力，返回 CAPABILITY_NOT_SUPPORTED |
| `display.show_image` | `device_id`, `prompt?/image_url?/image_path?` | ❌ 设备端未实现，同样被能力门拦截 |
| `display.show_video` | `device_id`, `video_path`, `seconds` | ❌ 同上 |

`device_id` 固定为 `desk-cube`。设备离线时工具返回 `DEVICE_OFFLINE`；命令 5 秒无 ack 返回 `DEVICE_TIMEOUT`。

## 3. 联调步骤

> **隧道模式客户端必读（review R3）**：Tailscale 客户端若工作在隧道模式（如 Muse VM：经 3130 端口 HTTP CONNECT 代理单向出访），**直连 `100.77.242.40` 不通，`ping` 不通属正常**，不要卡在第一步。所有请求走代理即可，curl 示例：
>
> ```bash
> curl -x http://<代理地址>:3130 -i -X POST http://100.77.242.40:8080/mcp ...
> ```
>
> 标准 MCP 客户端需在其 HTTP 代理配置中填入同一代理；其余步骤完全相同。

1. 联调机加入 Tailscale（直连模式 `ping 100.77.242.40` 通；隧道模式跳过此检查，直接按代理方式请求）。
2. 初始化握手（会返回 `Mcp-Session-Id` 响应头，后续请求都要带上）。**v0.2 起必须带 `Authorization: Bearer`，否则 401**：

```bash
curl -i -X POST http://100.77.242.40:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer <token>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"muse","version":"0.2"}}}'
```

3. 发送 `notifications/initialized`，然后带 `Mcp-Session-Id` 与 `Authorization` 头调 `tools/list` 确认 7 个工具。
4. 点屏验证（同样需要 Bearer 头；隧道模式在最前面加 `-x http://<代理地址>:3130`）：

```bash
curl -X POST http://100.77.242.40:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer <token>" \
  -H "Mcp-Session-Id: <上一步返回的会话ID>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"display.show_text","arguments":{"device_id":"desk-cube","text":"hello from muse"}}}'
```

5. HoloCubic 屏幕应显示文本；再调 `display.clear` 恢复。

标准 MCP 客户端（Muse 直接配置 endpoint 即可）无需手写以上报文，按 Streamable HTTP 流程连接。

## 4. 服务端运维速查（Pi 上执行）

```bash
systemctl status muse-display                 # 服务状态
sudo journalctl -u muse-display -n 50 --no-pager   # 日志（不含敏感内容）
ss -ltn | grep 8080                           # 应只见 100.77.242.40:8080
sudo systemctl restart muse-display           # 重启
```

- 代码：`/opt/muse-display`（editable 安装），venv：`/opt/muse-display/.venv`
- 设备注册表：`/opt/muse-display/config/devices.yaml`
- 环境文件：`/etc/muse-display/env`（MUSE_DISPLAY_TOKEN / MQTT 凭据，勿外传、勿入库）
- MQTT：本机 Mosquitto，用户 `muse-display`，ACL 限定 `muse/display/desk-cube/#`

## 5. 约束与注意事项

- 服务只允许经 LAN/Tailscale 访问；禁止做公网端口映射。MQTT 禁止匿名公网访问（现仅监听本机与局域网口）。
- 日志与联调记录不得包含 token、MQTT 密码或带凭据查询参数的 URL。
- HoloCubic 固件当前仅实现 command / ack / status 三个 topic 与 text / clear 能力；image / video 属于能力门之后的后续轮次。
- BOOX 设备未注册（当前无 ADB 设备）；接入后需在 `devices.yaml` 补充真实地址，不要使用仓库示例占位 IP。
- 改注册表或环境文件后需 `sudo systemctl restart muse-display` 生效。
