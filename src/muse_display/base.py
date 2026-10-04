"""DisplayAdapter 统一接口（需求 §6）。

MCP Server 只面向本接口，不含具体设备逻辑；
Adapter 负责协议转换、图片转换、布局、设备差异与错误处理。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DisplayAdapter(ABC):
    """每个注册设备一个 Adapter 实例。

    capabilities: 该设备支持的语义能力子集，取值 ⊆
        {"text", "card", "image", "video", "clear", "status"}；
        registry 配置与 Adapter 类声明必须一致（registry 加载时校验）。
    """

    capabilities: tuple[str, ...] = ()

    @abstractmethod
    def show_text(self, payload: dict) -> dict:
        """payload: {text, ttl?} → 显示纯文本。"""

    @abstractmethod
    def show_card(self, payload: dict) -> dict:
        """payload: {title, body, priority?, ttl?} → 显示结构化卡片。"""

    @abstractmethod
    def show_image(self, payload: dict) -> dict:
        """payload: {image_url | image_path | prompt, fit?, ttl?} → 显示图片。"""

    @abstractmethod
    def clear(self) -> dict:
        """清除当前内容，设备回到默认状态。"""

    @abstractmethod
    def get_status(self) -> dict:
        """读取设备状态。离线是合法应答（online: false），不抛异常。"""

    def is_online(self) -> bool:
        """list_devices 用的在线快查；默认未知 → False。"""
        return False

    def close(self) -> None:
        """释放连接资源（MQTT 断开等）。"""
