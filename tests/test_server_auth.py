"""BearerAuthMiddleware：review R1 验收——缺/错 401 不进 JSON-RPC，对才放行。

用假 inner ASGI app 记录是否被穿透；日志断言不含 token 片段。
"""

import asyncio
import json
import logging

from muse_display.server import BearerAuthMiddleware, _package_version

TOKEN = "test-token-123"


class InnerApp:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def call_middleware(app, headers=None, scope_type="http"):
    scope = {"type": scope_type, "headers": headers or [],
             "client": ("10.0.0.9", 55555)}
    received = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        received.append(message)

    asyncio.run(BearerAuthMiddleware(app, TOKEN)(scope, receive, send))
    return received


def start_message(received):
    return next(m for m in received if m["type"] == "http.response.start")


def test_missing_header_rejected():
    inner = InnerApp()
    received = call_middleware(inner)
    assert start_message(received)["status"] == 401
    assert inner.called is False


def test_wrong_token_rejected():
    inner = InnerApp()
    received = call_middleware(inner, [(b"authorization", b"Bearer wrong")])
    assert start_message(received)["status"] == 401
    assert inner.called is False


def test_wrong_scheme_rejected():
    inner = InnerApp()
    received = call_middleware(
        inner, [(b"authorization", b"Basic " + TOKEN.encode())])
    assert start_message(received)["status"] == 401
    assert inner.called is False


def test_correct_token_passes_through():
    inner = InnerApp()
    received = call_middleware(
        inner, [(b"authorization", b"Bearer " + TOKEN.encode())])
    assert start_message(received)["status"] == 200
    assert inner.called is True


def test_rejection_body_shape():
    inner = InnerApp()
    received = call_middleware(inner)
    start = start_message(received)
    www = dict(start["headers"]).get(b"www-authenticate")
    assert www == b'Bearer realm="muse-display"'
    body = next(m for m in received if m["type"] == "http.response.body")
    parsed = json.loads(body["body"])
    assert parsed["error"]["message"] == "unauthorized"


def test_rejection_log_has_ip_but_no_token(caplog):
    inner = InnerApp()
    with caplog.at_level(logging.WARNING, logger="muse_display.server"):
        call_middleware(inner, [(b"authorization", b"Bearer wrong")])
    text = caplog.text
    assert "10.0.0.9" in text
    assert TOKEN not in text
    assert "wrong" not in text


def test_non_http_scope_bypasses_auth():
    inner = InnerApp()
    call_middleware(inner, scope_type="lifespan")
    assert inner.called is True  # 原样放行，未做鉴权检查


def test_package_version_non_empty():
    assert _package_version()
