"""V0.1 错误面（需求 §11）。

- 所有面向 Muse 的错误统一为 {"success": false, "error": {code, message}}。
- 设备端 failed ack → 错误码映射：内容/渲染类 → RENDER_ERROR，其余 → TRANSPORT_ERROR
  （评审 T4 结论）。
"""

from __future__ import annotations


class ErrorCode:
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    CAPABILITY_NOT_SUPPORTED = "CAPABILITY_NOT_SUPPORTED"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    DEVICE_TIMEOUT = "DEVICE_TIMEOUT"
    RENDER_ERROR = "RENDER_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class MuseDisplayError(Exception):
    """携带 V0.1 错误码 + 可读 message 的统一异常。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"success": False, "error": {"code": self.code, "message": self.message}}


# ---- 便捷构造 ------------------------------------------------------------


def device_not_found(device_id: str) -> MuseDisplayError:
    return MuseDisplayError(
        ErrorCode.DEVICE_NOT_FOUND, f"Device {device_id} is not registered."
    )


def device_offline(device_id: str, detail: str = "") -> MuseDisplayError:
    msg = f"Device {device_id} is offline."
    if detail:
        msg += f" {detail}"
    return MuseDisplayError(ErrorCode.DEVICE_OFFLINE, msg)


def capability_not_supported(device_id: str, tool: str, caps: tuple) -> MuseDisplayError:
    return MuseDisplayError(
        ErrorCode.CAPABILITY_NOT_SUPPORTED,
        f"Device {device_id} does not support {tool} "
        f"(capabilities: {', '.join(caps) or 'none'}).",
    )


def invalid_payload(message: str) -> MuseDisplayError:
    return MuseDisplayError(ErrorCode.INVALID_PAYLOAD, message)


def transport_error(message: str) -> MuseDisplayError:
    return MuseDisplayError(ErrorCode.TRANSPORT_ERROR, message)


def device_timeout(device_id: str, timeout_s: float) -> MuseDisplayError:
    return MuseDisplayError(
        ErrorCode.DEVICE_TIMEOUT,
        f"Device {device_id} did not respond within {timeout_s:g}s.",
    )


def render_error(message: str) -> MuseDisplayError:
    return MuseDisplayError(ErrorCode.RENDER_ERROR, message)


def internal_error(message: str) -> MuseDisplayError:
    return MuseDisplayError(ErrorCode.INTERNAL_ERROR, message)


# ---- 设备 failed-ack 映射 -------------------------------------------------

# 内容/渲染类关键词（命中 → RENDER_ERROR）；其余一律 TRANSPORT_ERROR。
_RENDER_HINTS = (
    "decode",
    "render",
    "format",
    "bitmap",
    "font",
    "layout",
    "content",
    "too_large",
    "invalid_image",
    "渲染",
    "内容",
    "尺寸",
)


def map_device_failure(device_error: str) -> MuseDisplayError:
    """把设备 ack 里 status=failed 的 error 文本映射成 V0.1 错误码。"""
    text = (device_error or "").lower()
    if any(hint in text for hint in _RENDER_HINTS):
        return render_error(f"device reported failure: {device_error or 'unknown'}")
    return transport_error(f"device reported failure: {device_error or 'unknown'}")
