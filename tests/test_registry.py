"""registry.py fail-fast 行为（评审 T5/T1）。"""

import pytest

from muse_display.errors import ErrorCode, MuseDisplayError
from muse_display.registry import load_registry

BOOX_VALID = """
devices:
  boox-desk:
    name: BOOX NoteX
    type: boox
    transport: adb
    width: 1404
    height: 1872
    capabilities: [text, card, image, clear, status]
    addresses:
      - 192.0.2.10:5555
      - 192.0.2.10:5555
    art_script: /home/user/muse-art/daily-art.py
"""

HOLO_VALID = """
devices:
  desk-cube:
    name: HoloCubic
    type: holocubic
    transport: mqtt
    width: 240
    height: 240
    capabilities: [text, image, video, clear, status]
    topic: muse/display/desk-cube
    broker_host: 127.0.0.1
    broker_port: 1883
    stream_host: 192.0.2.10
"""


def write(tmp_path, text):
    p = tmp_path / "devices.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def expect_internal(fn, frag: str):
    with pytest.raises(MuseDisplayError) as ei:
        fn()
    assert ei.value.code == ErrorCode.INTERNAL_ERROR
    assert frag in ei.value.message


def test_boox_valid(tmp_path):
    devices = load_registry(write(tmp_path, BOOX_VALID))
    d = devices["boox-desk"]
    assert d.type == "boox"
    assert d.transport == "adb"
    assert d.width == 1404 and d.height == 1872
    assert d.addresses == ("192.0.2.10:5555", "192.0.2.10:5555")
    assert "daily-art.py" in d.art_script


def test_holocubic_video_requires_reachable_stream_host(tmp_path):
    for host in ("", "127.0.0.1"):
        text = HOLO_VALID.replace("stream_host: 192.0.2.10", f"stream_host: {host!r}")
        expect_internal(lambda: load_registry(write(tmp_path, text)), "stream_host must be reachable")


def test_holocubic_valid(tmp_path):
    devices = load_registry(write(tmp_path, HOLO_VALID))
    d = devices["desk-cube"]
    assert d.topic == "muse/display/desk-cube"
    assert d.broker_port == 1883
    assert d.capabilities == ("text", "image", "video", "clear", "status")
    assert d.stream_host == "192.0.2.10"


def test_two_devices(tmp_path):
    # 两台设备必须在同一个 devices 表下（两个顶层 devices 键会被拒）
    both = HOLO_VALID.rstrip() + BOOX_VALID.split("devices:", 1)[1]
    devices = load_registry(write(tmp_path, both))
    assert set(devices) == {"boox-desk", "desk-cube"}


def test_duplicate_device_id(tmp_path):
    dup = HOLO_VALID + "\n" + HOLO_VALID.split("devices:", 1)[0] + HOLO_VALID
    # 直接把同一 devices 表写两遍 → 顶层 devices 键重复
    text = "devices:\n  desk-cube:\n    name: A\n    type: holocubic\n    transport: mqtt\n    width: 1\n    height: 1\n    capabilities: [text]\n    topic: t\n" \
           "  desk-cube:\n    name: B\n"
    expect_internal(lambda: load_registry(write(tmp_path, text)), "duplicate key")


def test_missing_file(tmp_path):
    expect_internal(lambda: load_registry(tmp_path / "nope.yaml"), "not found")


def test_empty_devices(tmp_path):
    expect_internal(lambda: load_registry(write(tmp_path, "devices: {}")),
                    "at least one device")


def test_unknown_type(tmp_path):
    text = BOOX_VALID.replace("type: boox", "type: kindle")
    expect_internal(lambda: load_registry(write(tmp_path, text)), "unknown adapter type")


def test_transport_mismatch(tmp_path):
    text = BOOX_VALID.replace("transport: adb", "transport: mqtt")
    expect_internal(lambda: load_registry(write(tmp_path, text)), "requires transport")


def test_missing_required(tmp_path):
    text = BOOX_VALID.replace("    width: 1404\n", "")
    expect_internal(lambda: load_registry(write(tmp_path, text)), "missing required field 'width'")


def test_unknown_key(tmp_path):
    text = BOOX_VALID.replace("    art_script:", "    typo_key: x\n    art_script:")
    expect_internal(lambda: load_registry(write(tmp_path, text)), "unknown config keys")


def test_bad_capability(tmp_path):
    text = BOOX_VALID.replace("[text, card, image, clear, status]",
                              "[text, imaginary]")
    expect_internal(lambda: load_registry(write(tmp_path, text)), "must be within")


def test_duplicate_capability(tmp_path):
    text = BOOX_VALID.replace("[text, card, image, clear, status]",
                              "[text, text]")
    expect_internal(lambda: load_registry(write(tmp_path, text)), "duplicate capability")


def test_bad_address_no_port(tmp_path):
    text = BOOX_VALID.replace("192.0.2.10:5555", "192.0.2.10")
    expect_internal(lambda: load_registry(write(tmp_path, text)), "bad ADB address")


def test_bad_address_three_addresses(tmp_path):
    text = BOOX_VALID.replace(
        "      - 192.0.2.10:5555\n",
        "      - 192.0.2.10:5555\n      - 192.0.2.10:5555\n",
    )
    expect_internal(lambda: load_registry(write(tmp_path, text)), "1-2")


def test_boox_missing_addresses(tmp_path):
    text = BOOX_VALID.replace(
        "    addresses:\n      - 192.0.2.10:5555\n      - 192.0.2.10:5555\n", "")
    expect_internal(lambda: load_registry(write(tmp_path, text)),
                    "missing required field 'addresses'")


def test_holocubic_empty_topic(tmp_path):
    text = HOLO_VALID.replace("topic: muse/display/desk-cube", "topic: \"  \"")
    expect_internal(lambda: load_registry(write(tmp_path, text)), "topic must be non-empty")


def test_holocubic_bad_port(tmp_path):
    text = HOLO_VALID.replace("broker_port: 1883", "broker_port: 99999")
    expect_internal(lambda: load_registry(write(tmp_path, text)), "1-65535")
