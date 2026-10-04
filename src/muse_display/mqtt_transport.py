"""Paho-MQTT 传输实现（paho-mqtt>=2，CallbackAPIVersion.VERSION2）。

只被 server 装配层 import；测试走 adapters/holocubic.MqttTransport 协议的
FakeTransport，无需 broker、无需安装 paho。

评审 D3/D4 语义：
- 连接成功回调里先订阅 {base}/ack(QoS1) 与 {base}/status(QoS0)，再置 ready；
  适配器在 wait_ready 通过前不发布命令（先订阅 ack 再发布）
- 设备心跳 / LWT offline 都是 retained 消息，订阅后由 broker 自然补发，
  适配器据此在重启后恢复在线态
"""

from __future__ import annotations

import threading

from paho.mqtt.client import CallbackAPIVersion, Client


class PahoTransport:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        client_id: str,
        username: str | None = None,
        password: str | None = None,
        ack_topic: str,
        status_topic: str,
    ):
        self._host = host
        self._port = port
        self._ack_topic = ack_topic
        self._status_topic = status_topic
        self._cb = None
        self._ready = threading.Event()
        self._client = Client(CallbackAPIVersion.VERSION2, client_id=client_id)
        if username is not None:
            self._client.username_pw_set(username, password or "")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # ---- MqttTransport 协议面 ----------------------------------------

    def on_message(self, callback) -> None:
        self._cb = callback

    def start(self) -> None:
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def wait_ready(self, timeout_s: float) -> bool:
        return self._ready.wait(timeout_s)

    def publish(self, topic: str, payload: bytes, qos: int = 1,
                retain: bool = False) -> None:
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != 0:
            raise OSError(f"mqtt publish failed rc={info.rc}")

    def close(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    # ---- paho 回调（网络线程） ---------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if getattr(reason_code, "is_failure", False):
            return
        client.subscribe(self._ack_topic, qos=1)     # D3：先订阅 ack
        client.subscribe(self._status_topic, qos=0)  # 心跳 QoS0；retained 会补发
        self._ready.set()

    def _on_message(self, client, userdata, message):
        if self._cb is not None:
            self._cb(message.topic, message.payload)
