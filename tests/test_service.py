"""DisplayService：能力门 / §11 dispatch 错误映射 / 派生状态 / §14 日志 redact。

适配器全部用替身——本文件只测服务层编排，不碰任何传输。
"""

import logging

import pytest

from muse_display.errors import ErrorCode, MuseDisplayError
from muse_display.registry import DeviceConfig
from muse_display.tools import DisplayService

CUBE = DeviceConfig(
    device_id="cube", name="Cube", type="holocubic", transport="mqtt",
    width=240, height=240, capabilities=("text", "video", "clear", "status"),
    topic="muse/display/cube",
)
BOOX = DeviceConfig(
    device_id="boox", name="BOOX", type="boox", transport="adb",
    width=1404, height=1872,
    capabilities=("text", "card", "image", "clear", "status"),
    addresses=("192.0.2.10:5555",),
)


class FakeAdapter:
    def __init__(self, result=None, online=True, raise_exc=None):
        self.result = result if result is not None else {"command_id": "cmd-1"}
        self.online = online
        self.raise_exc = raise_exc
        self.calls = []

    def _call(self, method, payload=None):
        self.calls.append((method, payload))
        if self.raise_exc is not None:
            raise self.raise_exc
        return dict(self.result)

    def is_online(self):
        if isinstance(self.online, Exception):
            raise self.online
        return self.online

    def show_text(self, payload):
        return self._call("show_text", payload)

    def show_card(self, payload):
        return self._call("show_card", payload)

    def show_image(self, payload):
        return self._call("show_image", payload)

    def show_video(self, payload):
        return self._call("show_video", payload)

    def clear(self):
        return self._call("clear")

    def get_status(self):
        return {"command_id": "cmd-1", "online": self.online is True}


def make_service(cube_adapter=None, boox_adapter=None):
    return DisplayService(
        {"cube": CUBE, "boox": BOOX},
        {"cube": cube_adapter or FakeAdapter(),
         "boox": boox_adapter or FakeAdapter()},
    )


# ---- list_devices ----------------------------------------------------------

def test_list_devices_reports_registry_and_presence():
    cube = FakeAdapter(online=False)          # MQTT 存在性：离线是合法答案
    service = make_service(cube_adapter=cube)
    out = service.list_devices()
    assert out["success"] is True
    by_id = {d["id"]: d for d in out["devices"]}
    assert by_id["cube"]["online"] is False
    assert by_id["cube"]["capabilities"] == ["text", "video", "clear", "status"]
    assert by_id["boox"]["online"] is True
    assert by_id["boox"]["type"] == "boox"


def test_list_devices_survives_probe_exception():
    boom = FakeAdapter(online=MuseDisplayError(ErrorCode.TRANSPORT_ERROR, "x"))
    service = make_service(cube_adapter=boom)
    out = service.list_devices()
    by_id = {d["id"]: d for d in out["devices"]}
    assert by_id["cube"]["online"] is False   # 探测炸了也只算离线，不冒泡


# ---- 能力门 + 错误映射（§11：永不裸抛） --------------------------------------

def test_dispatch_unknown_device_is_error_dict():
    service = make_service()
    out = service.dispatch("display.show_text", device_id="ghost", text="hi")
    assert out["success"] is False
    assert out["error"]["code"] == "DEVICE_NOT_FOUND"


def test_dispatch_capability_gate():
    service = make_service()
    out = service.dispatch("display.show_card",
                           device_id="cube", title="t", body="b")
    assert out["error"]["code"] == "CAPABILITY_NOT_SUPPORTED"


def test_dispatch_maps_adapter_error():
    service = make_service(
        boox_adapter=FakeAdapter(
            raise_exc=MuseDisplayError(ErrorCode.DEVICE_TIMEOUT, "timed out after 5s")),
    )
    out = service.dispatch("display.clear", device_id="boox")
    assert out["success"] is False
    assert out["error"]["code"] == "DEVICE_TIMEOUT"
    assert "5s" in out["error"]["message"]


def test_dispatch_never_leaks_bare_exception():
    service = make_service(
        boox_adapter=FakeAdapter(raise_exc=ZeroDivisionError("boom")),
    )
    out = service.dispatch("display.clear", device_id="boox")
    assert out["success"] is False
    assert out["error"]["code"] == "INTERNAL_ERROR"


# ---- 成功路径 + 派生状态 ------------------------------------------------------

def test_show_text_success_merges_result_and_records_state():
    boox = FakeAdapter(result={"command_id": "cmd-ab12cd34",
                               "file": "/sdcard/Muse/muse-ab12cd34.jpg"})
    service = make_service(boox_adapter=boox)
    out = service.dispatch("display.show_text", device_id="boox", text="你好")
    assert out["success"] is True
    assert out["device_id"] == "boox"
    assert out["command_id"] == "cmd-ab12cd34"

    status = service.get_status("boox")
    assert status["state"] == "displaying"
    assert status["current_content_id"] == "/sdcard/Muse/muse-ab12cd34.jpg"
    assert status["updated_at"] is not None
    assert status["derived"] is True                      # 明示是 MCP 内存派生值
    assert "command_id" not in status["device_report"]    # 剥掉内部字段


def test_show_video_success_records_command_and_duration():
    cube = FakeAdapter()
    service = make_service(cube_adapter=cube)
    out = service.dispatch("display.show_video", device_id="cube",
                           video_path="/srv/video.mp4", seconds=7)
    assert out["success"] is True
    assert cube.calls[-1] == ("show_video", {
        "video_path": "/srv/video.mp4", "seconds": 7})
    assert service.state["cube"]["last_command"] == "display.show_video"


def test_clear_resets_to_idle():
    service = make_service()
    service.dispatch("display.clear", device_id="boox")
    status = service.get_status("boox")
    assert status["state"] == "idle"
    assert status["current_content_id"] is None


def test_get_status_before_any_command_is_idle():
    service = make_service()
    status = service.get_status("cube")
    assert status["state"] == "idle"
    assert status["online"] is True
    assert status["device_report"] == {"online": True}    # command_id 已剥掉


# ---- §14：URL 永不入日志 ------------------------------------------------------

def test_show_image_log_redacts_url(caplog):
    service = make_service()
    with caplog.at_level(logging.INFO, logger="muse_display.tools"):
        service.show_image(
            "boox", image_url="http://pi.local:8000/a.png?token=SECRET123&q=1")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET123" not in joined
    assert "token=***" in joined
    assert "q=1" in joined                                # 其余参数保留
