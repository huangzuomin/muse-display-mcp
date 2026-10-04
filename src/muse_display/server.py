"""MCP Server 装配 + 入口（评审 T1/T4）。

- mcp FastMCP；async 工具处理器经 anyio.to_thread 调同步 DisplayService
  （评审 D2：async 包线程，适配器保持同步）
- MUSE_DISPLAY_TOKEN Day 1 强制校验：未设置 → 拒绝启动（评审 T1）
- 配置路径：MUSE_DISPLAY_CONFIG，默认 ./config/devices.yaml
- MQTT 凭据：MQTT_USERNAME / MQTT_PASSWORD 环境变量（需求 §13，不入仓库）
- 日志走 stderr：stdio 传输下 stdout 是 MCP 协议通道，日志污染会打断 Muse
"""

from __future__ import annotations

import atexit
import functools
import logging
import os
import sys

from .adapters.boox import BooxAdapter
from .adapters.holocubic import HolocubicAdapter
from .errors import MuseDisplayError, internal_error
from .mqtt_transport import PahoTransport
from .registry import load_registry
from .tools import DisplayService

log = logging.getLogger("muse_display.server")


def require_token() -> str:
    """Day 1 强制校验（评审 T1）：没有 token 就不开机。"""
    token = os.environ.get("MUSE_DISPLAY_TOKEN", "").strip()
    if not token:
        print(
            "muse-display: MUSE_DISPLAY_TOKEN is not set; refusing to start "
            "(评审 T1: token 校验 Day 1 强制)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token


def build_service(config_path: str, *, start_transports: bool = True) -> DisplayService:
    devices = load_registry(config_path)
    adapters = {}
    for device_id, device in devices.items():
        if device.type == "boox":
            adapters[device_id] = BooxAdapter(device)
        elif device.type == "holocubic":
            transport = PahoTransport(
                device.broker_host,
                device.broker_port,
                client_id=f"muse-display-{device_id}",
                username=os.environ.get("MQTT_USERNAME"),
                password=os.environ.get("MQTT_PASSWORD"),
                ack_topic=f"{device.topic}/ack",
                status_topic=f"{device.topic}/status",
            )
            adapter = HolocubicAdapter(device, transport)
            if start_transports:
                try:
                    adapter.start()  # D3：先订阅 ack/status 再谈发布
                except MuseDisplayError as exc:
                    # broker 暂不可达不拖死整个服务（否则 mosquitto 一重启
                    # MCP 就崩，BOOX 也被陪葬）；命令级 is_online/publish
                    # 门会兜住后续行为，连上后自动恢复。
                    log.warning("transport start deferred for %s: %s",
                                device.device_id, exc.message)
            adapters[device_id] = adapter
        else:
            raise internal_error(f"unhandled adapter type {device.type!r}")
    log.info("registry loaded: %d device(s): %s",
             len(devices), ", ".join(sorted(devices)))
    return DisplayService(devices, adapters)


def build_mcp(service: DisplayService):
    """注册 display.* 工具；工具名带点号，与需求 §3 命名一致。"""
    import anyio
    from mcp.server.mcpserver import MCPServer  # mcp 2.x（FastMCP 已更名）

    mcp = MCPServer("muse-display")

    def _sync(tool: str, **kwargs) -> dict:
        return service.dispatch(tool, **kwargs)

    @mcp.tool(name="display.list_devices")
    async def display_list_devices() -> dict:
        """列出已注册的显示设备，含在线状态、传输方式与能力列表。"""
        return await anyio.to_thread.run_sync(functools.partial(_sync, "display.list_devices"))

    @mcp.tool(name="display.show_text")
    async def display_show_text(device_id: str, text: str, ttl: int | None = None) -> dict:
        """在指定设备全屏显示纯文本（自动换行，溢出截断）。ttl 为可选展示秒数，设备不支持时忽略。"""
        return await anyio.to_thread.run_sync(
            functools.partial(_sync, "display.show_text", device_id=device_id,
                              text=text, ttl=ttl)
        )

    @mcp.tool(name="display.show_card")
    async def display_show_card(device_id: str, title: str, body: str,
                                priority: str = "normal",
                                ttl: int | None = None) -> dict:
        """显示结构化卡片：标题 + 正文 + 优先级角标（low/normal/high）。仅支持 card 能力的设备可用。"""
        return await anyio.to_thread.run_sync(
            functools.partial(_sync, "display.show_card", device_id=device_id,
                              title=title, body=body, priority=priority, ttl=ttl)
        )

    @mcp.tool(name="display.show_image")
    async def display_show_image(device_id: str, prompt: str | None = None,
                                 image_url: str | None = None,
                                 image_path: str | None = None,
                                 fit: str = "contain",
                                 ttl: int | None = None) -> dict:
        """显示图片。三选一：prompt（本地 AI 生成，约 4 分钟）、image_url（下载后推送，约 15 秒）、image_path（服务器本地路径）。fit 为 contain（完整显示留白边）或 cover（裁切填满）。"""
        return await anyio.to_thread.run_sync(
            functools.partial(_sync, "display.show_image", device_id=device_id,
                              prompt=prompt, image_url=image_url,
                              image_path=image_path, fit=fit, ttl=ttl)
        )

    @mcp.tool(name="display.show_video")
    async def display_show_video(device_id: str, video_path: str,
                                seconds: int = 10) -> dict:
        """播放服务器本地视频，在 HoloCubic 上静音播放；时长 1–30 秒。"""
        return await anyio.to_thread.run_sync(
            functools.partial(_sync, "display.show_video", device_id=device_id,
                              video_path=video_path, seconds=seconds)
        )

    @mcp.tool(name="display.clear")
    async def display_clear(device_id: str) -> dict:
        """清除设备上的 Muse 内容（BOOX 回桌面，HoloCubic 恢复默认表盘/界面）。"""
        return await anyio.to_thread.run_sync(
            functools.partial(_sync, "display.clear", device_id=device_id)
        )

    @mcp.tool(name="display.get_status")
    async def display_get_status(device_id: str) -> dict:
        """查询设备状态：在线、电量/屏幕（BOOX）或 presence（HoloCubic）、最近显示内容（MCP 派生值）。"""
        return await anyio.to_thread.run_sync(
            functools.partial(_sync, "display.get_status", device_id=device_id)
        )

    return mcp


def main() -> None:
    # stdout 是 MCP stdio 协议通道，日志只能走 stderr
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    require_token()
    config_path = os.environ.get(
        "MUSE_DISPLAY_CONFIG", os.path.join("config", "devices.yaml")
    )
    service = build_service(config_path)

    def _close_all():
        for adapter in service.adapters.values():
            try:
                adapter.close()
            except Exception:  # noqa: BLE001 — 退出清理不二次抛
                pass

    atexit.register(_close_all)

    mcp = build_mcp(service)
    # 传输：stdio（MCP 客户端直接拉起，默认）| http（systemd 常驻，需求 §3）。
    # http 模式网络边界（需求 §13）：无公网 IP，仅 LAN/Tailscale 可达；
    # bearer 中间件归补齐轮，V0.1 靠网络边界 + MUSE_DISPLAY_TOKEN 启动门。
    transport = os.environ.get("MUSE_DISPLAY_TRANSPORT", "stdio").strip().lower()
    if transport in ("http", "streamable-http", "sse"):
        host = os.environ.get("MUSE_DISPLAY_HTTP_HOST", "0.0.0.0")
        port = int(os.environ.get("MUSE_DISPLAY_HTTP_PORT", "8080"))
        log.info("serving streamable-http on %s:%d (path /mcp)", host, port)
        # mcp 2.x：host/port 直接作为 run 参数（1.x 才走 settings）
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
