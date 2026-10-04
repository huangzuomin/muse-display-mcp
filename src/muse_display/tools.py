"""display.* 六个 MCP 工具的同步服务核心（评审 T3/T1）。

MCP 异步处理器经 anyio.to_thread 调这里（评审 D2：async 包线程）。
职责：能力门（注册表 caps）→ 适配器调用 → 错误映射 → 内存状态派生 → 日志。

错误约定（需求 §11）：永不向 Muse 抛裸异常，返回
{"success": false, "error": {"code", "message"}}。
日志（需求 §14）：URL 里的 token/key 一律 redact_url 后再落日志。
"""

from __future__ import annotations

import logging
import time

from .errors import MuseDisplayError, device_not_found, internal_error
from .util import now_iso, redact_url

log = logging.getLogger("muse_display.tools")

_TOOL_METHOD = {
    "display.list_devices": "list_devices",
    "display.show_text": "show_text",
    "display.show_card": "show_card",
    "display.show_image": "show_image",
    "display.show_video": "show_video",
    "display.clear": "clear",
    "display.get_status": "get_status",
}

_TOOL_CAPABILITY = {
    "display.show_text": "text",
    "display.show_card": "card",
    "display.show_image": "image",
    "display.show_video": "video",
    "display.clear": "clear",
    "display.get_status": "status",
}


class DisplayService:
    """注册表 + 适配器 + 最近命令内存状态（state/current_content_id 的派生源）。"""

    def __init__(self, devices: dict, adapters: dict):
        self.devices = devices
        self.adapters = adapters
        self.state: dict[str, dict] = {}

    # ---- 内部 ----------------------------------------------------------

    def _resolve(self, device_id: str):
        device = self.devices.get(device_id)
        if device is None:
            raise device_not_found(device_id)
        adapter = self.adapters.get(device_id)
        if adapter is None:
            raise internal_error(f"device {device_id} has no adapter")
        return device, adapter

    def _gate(self, device, tool: str) -> None:
        cap = _TOOL_CAPABILITY[tool]
        if cap not in device.capabilities:
            from .errors import capability_not_supported

            raise capability_not_supported(device.device_id, tool, device.capabilities)

    def _record(self, device_id: str, tool: str, result: dict) -> None:
        # 派生状态（评审 T1）：MCP 内存值，非设备端持久化；get_status 里标注
        state = "idle" if tool == "display.clear" else "displaying"
        self.state[device_id] = {
            "state": state,
            "last_command": tool,
            "command_id": result.get("command_id"),
            "current_content_id": result.get("file") if tool != "display.clear" else None,
            "updated_at": now_iso(),
        }

    def _log(self, tool: str, device_id: str | None, message: str) -> str:
        safe = redact_url(message)  # §14：token/key 永不入日志
        log.info("%s device=%s detail=%s", tool, device_id, safe)
        return safe

    # ---- 六个工具（同步实现） -------------------------------------------

    def list_devices(self) -> dict:
        devices = []
        for device_id, device in self.devices.items():
            adapter = self.adapters[device_id]
            try:
                online = adapter.is_online()
            except MuseDisplayError:
                online = False
            devices.append({
                "id": device_id,
                "name": device.name,
                "type": device.type,
                "transport": device.transport,
                "online": online,
                "capabilities": list(device.capabilities),
            })
        return {"success": True, "devices": devices}

    def show_text(self, device_id: str, text: str, ttl: int | None = None) -> dict:
        device, adapter = self._resolve(device_id)
        self._gate(device, "display.show_text")
        payload = {"text": text}
        if ttl is not None:
            payload["ttl"] = ttl
        result = adapter.show_text(payload)
        self._record(device_id, "display.show_text", result)
        self._log("display.show_text", device_id,
                  f"command_id={result.get('command_id')}")
        return {"success": True, "device_id": device_id, **result}

    def show_card(self, device_id: str, title: str, body: str,
                  priority: str = "normal", ttl: int | None = None) -> dict:
        device, adapter = self._resolve(device_id)
        self._gate(device, "display.show_card")
        payload = {"title": title, "body": body, "priority": priority}
        if ttl is not None:
            payload["ttl"] = ttl
        result = adapter.show_card(payload)
        self._record(device_id, "display.show_card", result)
        self._log("display.show_card", device_id,
                  f"command_id={result.get('command_id')} priority={priority}")
        return {"success": True, "device_id": device_id, **result}

    def show_image(self, device_id: str, prompt: str | None = None,
                   image_url: str | None = None, image_path: str | None = None,
                   fit: str = "contain", ttl: int | None = None) -> dict:
        device, adapter = self._resolve(device_id)
        self._gate(device, "display.show_image")
        payload = {"fit": fit}
        if prompt:
            payload["prompt"] = prompt
        if image_url:
            payload["image_url"] = image_url
        if image_path:
            payload["image_path"] = image_path
        result = adapter.show_image(payload)
        self._record(device_id, "display.show_image", result)
        # URL 只记 redact 后的形态（§14）
        self._log("display.show_image", device_id,
                  f"command_id={result.get('command_id')} url={redact_url(image_url or '')}")
        return {"success": True, "device_id": device_id, **result}

    def show_video(self, device_id: str, video_path: str, seconds: int = 10) -> dict:
        device, adapter = self._resolve(device_id)
        self._gate(device, "display.show_video")
        result = adapter.show_video({"video_path": video_path, "seconds": seconds})
        self._record(device_id, "display.show_video", result)
        self._log("display.show_video", device_id,
                  f"command_id={result.get('command_id')} video={redact_url(video_path)}")
        return {"success": True, "device_id": device_id, **result}

    def clear(self, device_id: str) -> dict:
        device, adapter = self._resolve(device_id)
        self._gate(device, "display.clear")
        result = adapter.clear()
        self._record(device_id, "display.clear", result)
        self._log("display.clear", device_id, f"command_id={result.get('command_id')}")
        return {"success": True, "device_id": device_id, **result}

    def get_status(self, device_id: str) -> dict:
        device, adapter = self._resolve(device_id)
        self._gate(device, "display.get_status")
        report = adapter.get_status()  # 离线是合法应答 online:false
        derived = self.state.get(device_id)
        out = {
            "success": True,
            "device_id": device_id,
            "online": bool(report.get("online")),
            # 以下三项为 MCP 内存派生值，不是设备端持久化状态（评审 T1）
            "state": (derived or {}).get("state", "idle"),
            "current_content_id": (derived or {}).get("current_content_id"),
            "updated_at": (derived or {}).get("updated_at"),
            "derived": True,
            "device_report": {k: v for k, v in report.items() if k != "command_id"},
        }
        self._log("display.get_status", device_id, f"online={out['online']}")
        return out

    # ---- 统一入口：异常 → §11 错误体 --------------------------------------

    def dispatch(self, tool: str, **kwargs) -> dict:
        method = getattr(self, _TOOL_METHOD[tool])
        try:
            return method(**kwargs)
        except MuseDisplayError as exc:
            self._log(tool, kwargs.get("device_id"), f"{exc.code}: {exc.message}")
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001 — 最后防线，绝不裸抛给 Muse
            err = internal_error(f"unexpected error in {tool}: {exc}")
            log.exception("internal error in %s", tool)
            return err.to_dict()
