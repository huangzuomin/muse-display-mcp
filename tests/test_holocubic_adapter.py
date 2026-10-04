"""HolocubicAdapter：D3 订阅先于发布 / D4 retained 状态 / 5s 超时 / failed-ack 映射。

FakeTransport 完全替身 paho+broker；假时钟驱动 90s 离线窗口。
"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from muse_display.adapters.holocubic import HolocubicAdapter
from muse_display.errors import ErrorCode, MuseDisplayError
from muse_display.registry import DeviceConfig

BASE = "muse/display/cube"


def make_device(stream_host="192.0.2.10"):
    return DeviceConfig(
        device_id="cube", name="Cube", type="holocubic", transport="mqtt",
        width=240, height=240,
        capabilities=("text", "image", "video", "clear", "status"),
        topic=BASE, stream_host=stream_host,
    )


class FakeTransport:
    """记录调用序列；publish 可选自动回 ack（模拟固件立即应答）。"""

    def __init__(self, auto_ack=None):
        self.ops = []          # ("start"|"publish"|"close", ...)
        self.publishes = []    # (topic, payload_bytes, qos)
        self.auto_ack = auto_ack
        self._cb = None

    def on_message(self, cb):
        self._cb = cb

    def start(self):
        self.ops.append("start")

    def wait_ready(self, timeout_s):
        return True

    def publish(self, topic, payload, qos=1, retain=False):
        self.ops.append("publish")
        self.publishes.append((topic, payload, qos))
        if self.auto_ack is not None:
            self.deliver(f"{BASE}/ack", self.auto_ack(payload))

    def close(self):
        self.ops.append("close")

    def deliver(self, topic, obj):
        payload = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
        self._cb(topic, payload)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class NeverEvent:
    """wait 永远超时——驱动 5s 命令超时路径，不真等 5 秒。"""

    def set(self):
        pass

    def wait(self, timeout=None):
        return False


def status_online():
    return {"online": True}


def make_adapter(auto_ack=None, event_factory=threading.Event, device=None, **kwargs):
    transport = FakeTransport(auto_ack=auto_ack)
    clock = FakeClock()
    adapter = HolocubicAdapter(device or make_device(), transport,
                               clock=clock, event_factory=event_factory, **kwargs)
    adapter.start()
    return adapter, transport, clock


def displayed_ack(payload: bytes) -> dict:
    cid = json.loads(payload)["command_id"]
    return {"command_id": cid, "status": "displayed"}


# ---- D3：无订阅不发布；离线绝不发布 ---------------------------------------

def test_no_publish_before_heartbeat():
    adapter, transport, clock = make_adapter()
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_text({"text": "hi"})
    assert ei.value.code == ErrorCode.DEVICE_OFFLINE
    assert transport.publishes == []          # 未收到任何 status → 不发命令


def test_start_then_publish_order():
    adapter, transport, clock = make_adapter(auto_ack=displayed_ack)
    transport.deliver(f"{BASE}/status", status_online())
    adapter.show_text({"text": "你好"})
    assert transport.ops == ["start", "publish"]   # start（含订阅）先于发布
    topic, payload, qos = transport.publishes[0]
    assert topic == f"{BASE}/command"
    assert qos == 1                                # D3：命令 QoS1
    body = json.loads(payload)
    assert body["command"] == "show_text"
    assert body["payload"] == {"text": "你好"}
    assert body["command_id"].startswith("cmd-")


def test_duplicate_ack_ignored():
    adapter, transport, clock = make_adapter(auto_ack=displayed_ack)
    transport.deliver(f"{BASE}/status", status_online())
    adapter.show_text({"text": "hi"})
    cid = json.loads(transport.publishes[0][1])["command_id"]
    stale = {"command_id": cid, "status": "displayed"}
    transport.deliver(f"{BASE}/ack", stale)   # 重复 ack：忽略不炸
    transport.deliver(f"{BASE}/ack", stale)
    assert adapter._seen.count(cid) == 1
    assert adapter._pending == {}


# ---- 命令收发：超时 / failed ack 映射 --------------------------------------

def test_command_timeout_maps_to_device_timeout():
    adapter, transport, clock = make_adapter(
        event_factory=NeverEvent)
    transport.deliver(f"{BASE}/status", status_online())
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_text({"text": "hi"})
    assert ei.value.code == ErrorCode.DEVICE_TIMEOUT
    assert "5s" in ei.value.message           # 需求 §12：普通命令 5s
    assert adapter._pending == {}             # 超时后 pending 必须清掉


def test_failed_ack_render_error():
    def fail_render(payload):
        cid = json.loads(payload)["command_id"]
        return {"command_id": cid, "status": "failed", "error": "decode failed"}
    adapter, transport, _ = make_adapter(auto_ack=fail_render)
    transport.deliver(f"{BASE}/status", status_online())
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_text({"text": "hi"})
    assert ei.value.code == ErrorCode.RENDER_ERROR


def test_failed_ack_transport_error():
    def fail_net(payload):
        cid = json.loads(payload)["command_id"]
        return {"command_id": cid, "status": "failed", "error": "wifi down"}
    adapter, transport, _ = make_adapter(auto_ack=fail_net)
    transport.deliver(f"{BASE}/status", status_online())
    with pytest.raises(MuseDisplayError) as ei:
        adapter.clear()
    assert ei.value.code == ErrorCode.TRANSPORT_ERROR


# ---- D4：retained 心跳 / LWT / 90s 窗口 ------------------------------------

def test_retained_heartbeat_then_90s_expiry():
    adapter, transport, clock = make_adapter()
    transport.deliver(f"{BASE}/status", status_online())  # 重启后 retained 补发
    assert adapter.is_online() is True
    clock.advance(89)
    assert adapter.is_online() is True
    clock.advance(2)                                       # 共 91s
    assert adapter.is_online() is False                    # 90s 无心跳判离线


def test_lwt_marks_offline_immediately():
    adapter, transport, clock = make_adapter()
    transport.deliver(f"{BASE}/status", status_online())
    clock.advance(5)
    transport.deliver(f"{BASE}/status", {"online": False})  # LWT retained
    assert adapter.is_online() is False                     # 无视 90s 窗口


# ---- 能力天花板 + payload 校验 + get_status --------------------------------

def test_show_card_not_supported():
    adapter, transport, _ = make_adapter()
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_card({"title": "t", "body": "b"})
    assert ei.value.code == ErrorCode.CAPABILITY_NOT_SUPPORTED
    assert transport.publishes == []


def test_show_image_requires_http_url():
    adapter, transport, _ = make_adapter()
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_image({"image_path": "x.jpg"})
    assert ei.value.code == ErrorCode.INVALID_PAYLOAD
    assert transport.publishes == []


def test_show_image_sends_http_url():
    adapter, transport, _ = make_adapter(auto_ack=displayed_ack)
    transport.deliver(f"{BASE}/status", status_online())
    adapter.show_image({"image_url": "http://192.0.2.10/image.jpg"})
    body = json.loads(transport.publishes[-1][1])
    assert body["command"] == "show_image"
    assert body["payload"]["url"] == "http://192.0.2.10/image.jpg"


def test_show_video_transcodes_and_sends_stream_url(tmp_path, monkeypatch):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        Path(args[-1]).write_bytes(b"mjpeg-data")
        return SimpleNamespace(returncode=0, stderr=b"")

    from muse_display.adapters import holocubic

    servers = []

    class FakeHTTPServer:
        def __init__(self, address, handler):
            self.server_address = address
            self.daemon_threads = False
            self.directory = address[0]
            servers.append(self)

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            pass

    adapter, transport, _ = make_adapter(
        auto_ack=displayed_ack, run=fake_run, ffmpeg="fake-ffmpeg")
    monkeypatch.setattr(holocubic, "ThreadingHTTPServer", FakeHTTPServer)
    transport.deliver(f"{BASE}/status", status_online())
    result = adapter.show_video({"video_path": str(source), "seconds": 8})
    body = json.loads(transport.publishes[-1][1])
    assert result["seconds"] == 8
    assert body["command"] == "show_video"
    assert body["payload"] == {
        "url": "http://192.0.2.10:8124/video.mjpg", "seconds": 8}
    assert calls[0][0][0] == "fake-ffmpeg"
    assert "fps=12" in calls[0][0][calls[0][0].index("-vf") + 1]
    assert Path(calls[0][0][-1]).read_bytes() == b"mjpeg-data"
    adapter.close()


def test_show_video_rejects_invalid_seconds_and_missing_file(tmp_path):
    adapter, transport, _ = make_adapter(auto_ack=displayed_ack)
    transport.deliver(f"{BASE}/status", status_online())
    for payload in ({"video_path": "missing.mp4"},
                    {"video_path": "missing.mp4", "seconds": True}):
        with pytest.raises(MuseDisplayError) as ei:
            adapter.show_video(payload)
        assert ei.value.code == ErrorCode.INVALID_PAYLOAD
    assert transport.publishes == []


def test_show_video_rejects_unreachable_stream_host(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    device = make_device(stream_host="127.0.0.1")
    adapter, transport, _ = make_adapter(
        device=device, run=lambda *a, **k: pytest.fail("ffmpeg should not run"))
    transport.deliver(f"{BASE}/status", status_online())
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_video({"video_path": str(source)})
    assert ei.value.code == ErrorCode.INVALID_PAYLOAD


def test_empty_text_invalid_payload():
    adapter, transport, _ = make_adapter(auto_ack=displayed_ack)
    transport.deliver(f"{BASE}/status", status_online())
    for bad in ("", "   ", None, 123):
        with pytest.raises(MuseDisplayError) as ei:
            adapter.show_text({"text": bad})
        assert ei.value.code == ErrorCode.INVALID_PAYLOAD
    assert transport.publishes == []


def test_bad_ttl_invalid_payload():
    adapter, transport, _ = make_adapter(auto_ack=displayed_ack)
    transport.deliver(f"{BASE}/status", status_online())
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_text({"text": "hi", "ttl": 0})
    assert ei.value.code == ErrorCode.INVALID_PAYLOAD
    # 合法 ttl 透传给固件
    adapter.show_text({"text": "hi", "ttl": 60})
    body = json.loads(transport.publishes[-1][1])
    assert body["ttl"] == 60


def test_clear_success():
    adapter, transport, _ = make_adapter(auto_ack=displayed_ack)
    transport.deliver(f"{BASE}/status", status_online())
    result = adapter.clear()
    body = json.loads(transport.publishes[-1][1])
    assert body["command"] == "clear"
    assert result["command_id"] == body["command_id"]


def test_get_status_presence_level():
    adapter, transport, clock = make_adapter()
    st = adapter.get_status()                      # 从未收到心跳
    assert st["online"] is False
    transport.deliver(f"{BASE}/status", status_online())
    clock.advance(10)
    st = adapter.get_status()
    assert st["online"] is True
    assert st["source"] == "mqtt_presence"
    assert st["last_seen_age_s"] == 10.0


# ---- D3 机制级：PahoTransport 连接成功即订阅（装了 paho 才跑） ---------------

def test_paho_subscribes_on_connect():
    pytest.importorskip("paho.mqtt.client")
    from muse_display.mqtt_transport import PahoTransport

    t = PahoTransport("h", 1883, client_id="t",
                      ack_topic=f"{BASE}/ack", status_topic=f"{BASE}/status")
    subs = []
    t._client.subscribe = lambda topic, qos=0: subs.append((topic, qos))
    rc = SimpleNamespace(is_failure=False)
    t._on_connect(t._client, None, None, rc, None)
    assert subs == [(f"{BASE}/ack", 1), (f"{BASE}/status", 0)]  # D3：ack 先订
    assert t.wait_ready(0) is True

    subs2 = []
    t2 = PahoTransport("h", 1883, client_id="t2",
                       ack_topic=f"{BASE}/ack", status_topic=f"{BASE}/status")
    t2._client.subscribe = lambda topic, qos=0: subs2.append(topic)
    t2._on_connect(t2._client, None, None, SimpleNamespace(is_failure=True), None)
    assert subs2 == []                              # 连接失败不置 ready
    assert t2.wait_ready(0) is False
