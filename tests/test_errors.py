"""errors.py：§11 错误体形状 + failed-ack 映射（评审 T5/T4）。"""

from muse_display.errors import (
    ErrorCode,
    MuseDisplayError,
    capability_not_supported,
    device_not_found,
    device_offline,
    device_timeout,
    internal_error,
    invalid_payload,
    map_device_failure,
    render_error,
    transport_error,
)


def test_to_dict_shape():
    err = MuseDisplayError(ErrorCode.DEVICE_OFFLINE, "Device x is offline.")
    assert err.to_dict() == {
        "success": False,
        "error": {"code": "DEVICE_OFFLINE", "message": "Device x is offline."},
    }


def test_constructors_carry_code():
    assert device_not_found("a").code == "DEVICE_NOT_FOUND"
    assert device_offline("a").code == "DEVICE_OFFLINE"
    assert "offline" in device_offline("a", "deep sleep").message
    assert capability_not_supported("a", "show_card", ("text",)).code == \
        "CAPABILITY_NOT_SUPPORTED"
    assert invalid_payload("bad").code == "INVALID_PAYLOAD"
    assert transport_error("x").code == "TRANSPORT_ERROR"
    assert internal_error("x").code == "INTERNAL_ERROR"
    assert render_error("x").code == "RENDER_ERROR"


def test_timeout_message_includes_seconds():
    err = device_timeout("cube", 5.0)
    assert "5s" in err.message
    err2 = device_timeout("cube", 240.0)
    assert "240s" in err2.message


def test_map_render_hints():
    for text in ("decode failed", "bitmap too large", "render oom",
                 "内容不合法", "尺寸超限", "invalid_image data"):
        mapped = map_device_failure(text)
        assert mapped.code == ErrorCode.RENDER_ERROR, text
        assert text in mapped.message


def test_map_other_is_transport():
    for text in ("wifi down", "mqtt disconnect", "stack overflow", ""):
        assert map_device_failure(text).code == ErrorCode.TRANSPORT_ERROR


def test_map_case_insensitive():
    assert map_device_failure("DECODE FAILED").code == ErrorCode.RENDER_ERROR
