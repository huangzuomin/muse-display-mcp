"""HoloCubic Adapter — MQTT 命令/ack + retained 状态恢复（评审 D3/D4/T4）。

Topic 契约（需求 §7；ack topic 文档未定名，V0.1 定为 {base}/ack，需写进固件契约）：
  {topic}/command  下行命令 QoS1：{command_id, command, payload, ttl?}
  {topic}/ack      上行回执 QoS1：{command_id, status: displayed|failed, error?}
  {topic}/status   心跳 30s QoS0 retain=true；LWT offline 同 retain=true（D4）

评审 D3：MCP 侧先订阅 ack 再发布命令；命令超时 5s；重复 ack 忽略。
评审 c8009bd4 天花板：V0.1 固件只实现 clear + show_text；
show_card / show_image → CAPABILITY_NOT_SUPPORTED（补齐轮）；
get_status 为 presence 级 online（心跳/LWT 派生，不含设备字段）。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Protocol

from ..base import DisplayAdapter
from ..errors import (
    MuseDisplayError,
    capability_not_supported,
    device_offline,
    device_timeout,
    invalid_payload,
    map_device_failure,
    transport_error,
)
from ..util import new_command_id


class MqttTransport(Protocol):
    """最小传输面：PahoTransport 为真实实现，测试用 FakeTransport。"""

    def start(self) -> None: ...
    def close(self) -> None: ...
    def wait_ready(self, timeout_s: float) -> bool: ...
    def publish(self, topic: str, payload: bytes, qos: int = 1,
                retain: bool = False) -> None: ...
    def on_message(self, callback: Callable[[str, bytes], None]) -> None: ...


class _VideoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, fmt, *args):
        pass


class HolocubicAdapter(DisplayAdapter):
    capabilities = ("text", "image", "video", "clear", "status")

    OFFLINE_AFTER_S = 90.0     # 90s 无心跳判 offline（评审 D3/T4）
    COMMAND_TIMEOUT_S = 5.0    # 需求 §12：普通命令 5s
    VIDEO_COMMAND_TIMEOUT_S = 45.0
    READY_TIMEOUT_S = 5.0

    def __init__(self, device, transport: MqttTransport, *,
                 clock=time.monotonic, event_factory=threading.Event,
                 run=subprocess.run, ffmpeg="ffmpeg", stream_port=8124):
        self._device = device
        self._run = run
        self._ffmpeg = ffmpeg
        self._stream_port = stream_port
        self._stream_server = None
        self._stream_thread = None
        self._stream_dir = None
        self._video_lock = threading.Lock()
        self._t = transport
        self._clock = clock
        self._event_factory = event_factory
        base = device.topic.rstrip("/")
        self._command_topic = f"{base}/command"
        self._ack_topic = f"{base}/ack"
        self._status_topic = f"{base}/status"
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}        # cid -> {event, ack}
        self._seen: deque[str] = deque(maxlen=64)  # 重复 ack 忽略
        self._last_seen: float | None = None
        self._flag: bool | None = None             # None=尚未收到任何 status/LWT
        self._t.on_message(self._handle_message)

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> None:
        """连接 broker 并完成 ack/status 订阅（D3：订阅先于任何发布）。"""
        self._t.start()
        if not self._t.wait_ready(self.READY_TIMEOUT_S):
            raise transport_error(
                f"MQTT broker not reachable for {self._device.device_id} "
                f"within {self.READY_TIMEOUT_S:g}s"
            )

    def close(self) -> None:
        self._stop_stream_server()
        self._t.close()

    def _ensure_stream_server(self) -> str:
        host = getattr(self._device, "stream_host", "")
        if not host or host in ("127.0.0.1", "localhost", "0.0.0.0"):
            raise invalid_payload("HoloCubic stream_host must be reachable from the device")
        if self._stream_server is None:
            self._stream_dir = tempfile.TemporaryDirectory(prefix="muse-video-")
            handler = lambda *args, **kwargs: _VideoHandler(
                *args, directory=self._stream_dir.name, **kwargs
            )
            self._stream_server = ThreadingHTTPServer(("0.0.0.0", self._stream_port), handler)
            self._stream_server.daemon_threads = True
            self._stream_thread = threading.Thread(
                target=self._stream_server.serve_forever, daemon=True
            )
            self._stream_thread.start()
        return f"http://{host}:{self._stream_port}"

    def _stop_stream_server(self) -> None:
        if self._stream_server is not None:
            self._stream_server.shutdown()
            self._stream_server.server_close()
            self._stream_server = None
        if self._stream_dir is not None:
            self._stream_dir.cleanup()
            self._stream_dir = None
        self._stream_thread = None

    # ---- 消息路由（网络线程回调） -------------------------------------

    def _handle_message(self, topic: str, payload: bytes) -> None:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return  # 坏消息静默丢弃，保持服务存活
        if not isinstance(data, dict):
            return
        if topic == self._status_topic:
            with self._lock:
                self._last_seen = self._clock()
                self._flag = bool(data.get("online", True))
            return
        if topic != self._ack_topic:
            return
        cid = data.get("command_id")
        if not cid or cid in self._seen:
            return  # 重复 ack：忽略（D3）
        with self._lock:
            slot = self._pending.get(cid)
        if slot is None:
            return  # 已超时清理 / 非本会话命令
        self._seen.append(cid)
        slot["ack"] = data
        slot["event"].set()

    # ---- 在线判定（D4：retained 心跳 + 90s 窗口 + LWT） ----------------

    def is_online(self) -> bool:
        with self._lock:
            flag, last = self._flag, self._last_seen
        if flag is None or last is None or flag is False:
            return False
        return (self._clock() - last) <= self.OFFLINE_AFTER_S

    # ---- 命令收发（D3：QoS1 + 关联超时 + failed ack 映射） --------------

    def _send_command(self, command: str, payload: dict,
                      ttl: int | None = None) -> dict:
        if not self.is_online():
            raise device_offline(self._device.device_id)
        cid = new_command_id()
        body: dict = {"command_id": cid, "command": command, "payload": payload}
        if ttl is not None:
            body["ttl"] = ttl
        event = self._event_factory()
        with self._lock:
            self._pending[cid] = {"event": event, "ack": None}
        try:
            wire = json.dumps(body, ensure_ascii=False).encode("utf-8")
            try:
                self._t.publish(self._command_topic, wire, qos=1)
            except OSError as exc:
                raise transport_error(f"MQTT publish failed: {exc}")
            if not event.wait(self.COMMAND_TIMEOUT_S):
                raise device_timeout(self._device.device_id, self.COMMAND_TIMEOUT_S)
        finally:
            with self._lock:
                slot = self._pending.pop(cid, None)
        ack = (slot or {}).get("ack") or {}
        if ack.get("status") == "failed":
            raise map_device_failure(str(ack.get("error", "")))
        return {"command_id": cid, "ack": ack}

    def _check_ttl(self, ttl) -> int | None:
        if ttl is None:
            return None
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise invalid_payload("ttl must be a positive integer (seconds)")
        return ttl

    # ---- DisplayAdapter 接口 ------------------------------------------

    def show_text(self, payload: dict) -> dict:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise invalid_payload("show_text requires non-empty 'text'")
        ttl = self._check_ttl(payload.get("ttl"))
        sent = self._send_command("show_text", {"text": text}, ttl)
        return {"command_id": sent["command_id"], "ack": sent["ack"]}

    def show_card(self, payload: dict) -> dict:
        # 评审 c8009bd4：V0.1 固件无 card 布局 → 补齐轮
        raise capability_not_supported(
            self._device.device_id, "show_card", self.capabilities
        )

    def show_image(self, payload: dict) -> dict:
        url = payload.get("image_url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise invalid_payload("show_image requires an HTTP image_url")
        sent = self._send_command("show_image", {"url": url})
        return {"command_id": sent["command_id"], "ack": sent["ack"]}

    def show_video(self, payload: dict) -> dict:
        path = payload.get("video_path")
        seconds = payload.get("seconds", 10)
        if not isinstance(path, str) or not path.strip():
            raise invalid_payload("show_video requires a non-empty video_path")
        if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= 30:
            raise invalid_payload("show_video seconds must be an integer from 1 to 30")
        source = Path(path)
        if not source.is_file():
            raise invalid_payload(f"video_path not found: {path}")
        if source.stat().st_size > 100 * 1024 * 1024:
            raise invalid_payload("video file exceeds 100 MiB limit")
        if not self._video_lock.acquire(blocking=False):
            raise invalid_payload("another HoloCubic video is already being prepared or played")
        try:
            if not self.is_online():
                raise device_offline(self._device.device_id)
            stream_base = self._ensure_stream_server()
            output = Path(self._stream_dir.name) / "video.mjpg"
            timeout_s = max(60, seconds + 15)
            try:
                proc = self._run(
                    [self._ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-i", str(source), "-t", str(seconds), "-an", "-vf",
                     "scale=240:240:force_original_aspect_ratio=increase,crop=240:240,fps=12",
                     "-c:v", "mjpeg", "-q:v", "6", "-f", "image2pipe",
                     "-vcodec", "mjpeg", str(output)],
                    timeout=timeout_s,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except subprocess.TimeoutExpired:
                raise device_timeout(self._device.device_id, timeout_s)
            except OSError as exc:
                raise transport_error(f"ffmpeg could not start: {exc}")
            if proc.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                detail = proc.stderr.decode(errors="replace")[-300:] if proc.stderr else "no video frames"
                raise invalid_payload(f"video conversion failed: {detail}")

            cid = new_command_id()
            url = f"{stream_base}/video.mjpg"
            body = {"command_id": cid, "command": "show_video",
                    "payload": {"url": url, "seconds": seconds}}
            event = self._event_factory()
            with self._lock:
                self._pending[cid] = {"event": event, "ack": None}
            try:
                self._t.publish(self._command_topic,
                                json.dumps(body).encode("utf-8"), qos=1)
                wait_s = max(self.VIDEO_COMMAND_TIMEOUT_S, seconds + 15)
                if not event.wait(wait_s):
                    raise device_timeout(self._device.device_id, wait_s)
            except OSError as exc:
                raise transport_error(f"MQTT publish failed: {exc}")
            finally:
                with self._lock:
                    slot = self._pending.pop(cid, None)
            ack = (slot or {}).get("ack") or {}
            if ack.get("status") == "failed":
                raise map_device_failure(str(ack.get("error", "")))
            return {"command_id": cid, "ack": ack, "seconds": seconds}
        finally:
            self._video_lock.release()

    def clear(self) -> dict:
        sent = self._send_command("clear", {})
        return {"command_id": sent["command_id"], "ack": sent["ack"]}

    def get_status(self) -> dict:
        """presence 级：由心跳/LWT 派生；完整版（电量/前台应用）归补齐轮。"""
        cid = new_command_id()
        online = self.is_online()
        with self._lock:
            last = self._last_seen
        out = {"command_id": cid, "online": online, "source": "mqtt_presence"}
        if last is not None:
            out["last_seen_age_s"] = round(self._clock() - last, 1)
        return out
