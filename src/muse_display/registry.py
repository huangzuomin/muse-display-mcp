"""设备注册表：YAML 加载 + fail-fast 校验（评审 T1）。

规则：重复 device_id / 未知 adapter 类型 / 缺必填字段 / 未知配置键 / 非法能力
→ 启动即抛 MuseDisplayError(INTERNAL_ERROR)，绝不带病运行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ErrorCode, MuseDisplayError, internal_error

CAPABILITIES = ("text", "card", "image", "video", "clear", "status")
ADAPTER_TYPES = ("boox", "holocubic")

_COMMON_REQUIRED = ("name", "type", "transport", "width", "height", "capabilities")
_TYPE_REQUIRED = {
    "holocubic": ("topic", "broker_host", "broker_port"),
    "boox": ("addresses",),
}
_TRANSPORT_BY_TYPE = {"holocubic": "mqtt", "boox": "adb"}


@dataclass(frozen=True)
class DeviceConfig:
    device_id: str
    name: str
    type: str
    transport: str
    width: int
    height: int
    capabilities: tuple[str, ...] = field(default=())
    # holocubic (mqtt)
    topic: str = ""
    broker_host: str = "127.0.0.1"
    broker_port: int = 1883
    stream_host: str = ""
    # boox (adb)，1-2 个 "host:port"；两个时互为备胎
    addresses: tuple[str, ...] = field(default=())
    # boox 可选：晨画脚本（show_image prompt= 模式依赖）
    art_script: str = ""


class _StrictLoader(yaml.SafeLoader):
    """拒绝重复键的 SafeLoader——YAML 里写重复 device_id 必须 fail-fast。"""


def _construct_mapping(loader: _StrictLoader, node, deep: bool = False):
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            ) from exc
        if key in mapping:
            raise MuseDisplayError(
                ErrorCode.INTERNAL_ERROR,
                f"registry YAML has duplicate key: {key!r}",
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise internal_error(message)


def _validate_device(device_id: str, raw: Any) -> DeviceConfig:
    _require(isinstance(device_id, str) and device_id.strip() != "",
             f"device_id must be a non-empty string, got {device_id!r}")
    _require(isinstance(raw, dict), f"device {device_id}: mapping expected")

    unknown = set(raw) - {
        "name", "type", "transport", "width", "height", "capabilities",
        "topic", "broker_host", "broker_port", "stream_host", "addresses", "art_script",
    }
    _require(not unknown,
             f"device {device_id}: unknown config keys {sorted(unknown)}")

    for key in _COMMON_REQUIRED:
        _require(key in raw, f"device {device_id}: missing required field {key!r}")

    dev_type = raw["type"]
    _require(dev_type in ADAPTER_TYPES,
             f"device {device_id}: unknown adapter type {dev_type!r} "
             f"(expected one of {ADAPTER_TYPES})")

    transport = raw["transport"]
    _require(transport == _TRANSPORT_BY_TYPE[dev_type],
             f"device {device_id}: type {dev_type!r} requires transport "
             f"{_TRANSPORT_BY_TYPE[dev_type]!r}, got {transport!r}")

    for key in _TYPE_REQUIRED[dev_type]:
        _require(key in raw, f"device {device_id}: missing required field {key!r}")

    width, height = raw["width"], raw["height"]
    _require(isinstance(width, int) and width > 0,
             f"device {device_id}: width must be a positive int")
    _require(isinstance(height, int) and height > 0,
             f"device {device_id}: height must be a positive int")

    caps = raw["capabilities"]
    _require(isinstance(caps, list) and len(caps) > 0,
             f"device {device_id}: capabilities must be a non-empty list")
    _require(all(c in CAPABILITIES for c in caps),
             f"device {device_id}: capabilities must be within {CAPABILITIES}, "
             f"got {caps}")
    _require(len(set(caps)) == len(caps),
             f"device {device_id}: duplicate capability in {caps}")

    addresses: tuple[str, ...] = ()
    if dev_type == "boox":
        raw_addrs = raw["addresses"]
        _require(isinstance(raw_addrs, list) and 1 <= len(raw_addrs) <= 2,
                 f"device {device_id}: addresses must be a list of 1-2 'host:port'")
        for a in raw_addrs:
            _require(isinstance(a, str) and a.count(":") == 1 and a.split(":")[1].isdigit(),
                     f"device {device_id}: bad ADB address {a!r} (want 'host:port')")
        addresses = tuple(raw_addrs)

    topic = str(raw.get("topic", ""))
    if dev_type == "holocubic":
        _require(topic.strip() != "", f"device {device_id}: topic must be non-empty")

    # broker_port 只被 holocubic 消费，但校验对全部类型生效（缺省 1883）：
    # 垃圾值（如 broker_port: abc）在 boox 设备上也会 fail-fast 报
    # MuseDisplayError，而不是漏到 int() 变成裸 ValueError
    raw_port = raw.get("broker_port", 1883)
    _require(isinstance(raw_port, int) and not isinstance(raw_port, bool)
             and 1 <= raw_port <= 65535,
             f"device {device_id}: broker_port must be an integer 1-65535, got {raw_port!r}")

    stream_host = raw.get("stream_host", "")
    _require(isinstance(stream_host, str),
             f"device {device_id}: stream_host must be a string")
    if dev_type == "holocubic" and "video" in caps:
        _require(stream_host.strip() not in ("", "localhost", "127.0.0.1", "0.0.0.0"),
                 f"device {device_id}: stream_host must be reachable for video capability")

    return DeviceConfig(
        device_id=device_id,
        name=str(raw["name"]),
        type=dev_type,
        transport=transport,
        width=width,
        height=height,
        capabilities=tuple(caps),
        topic=topic,
        broker_host=str(raw.get("broker_host", "127.0.0.1")),
        broker_port=raw_port,
        stream_host=stream_host,
        addresses=addresses,
        art_script=str(raw.get("art_script", "")),
    )


def load_registry(path: str | Path) -> dict[str, DeviceConfig]:
    """加载并校验设备注册表，返回 {device_id: DeviceConfig}。任何问题直接抛。"""
    p = Path(path)
    if not p.is_file():
        raise internal_error(f"registry file not found: {p}")
    try:
        data = yaml.load(p.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise internal_error(f"registry YAML parse error: {exc}") from exc

    _require(isinstance(data, dict) and isinstance(data.get("devices"), dict),
             "registry YAML must be a mapping with a 'devices' table")
    devices_raw = data["devices"]
    _require(len(devices_raw) > 0, "registry must define at least one device")

    devices: dict[str, DeviceConfig] = {}
    for device_id, raw in devices_raw.items():
        devices[device_id] = _validate_device(device_id, raw)
    return devices
