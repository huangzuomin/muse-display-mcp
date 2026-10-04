"""util.py：command_id 形状/唯一性、redact_url、now_iso（评审 T5）。"""

import re

from muse_display.util import new_command_id, now_iso, redact_url


def test_command_id_shape():
    for _ in range(50):
        assert re.fullmatch(r"cmd-[0-9a-f]{8}", new_command_id())


def test_command_id_unique():
    seen = {new_command_id() for _ in range(200)}
    assert len(seen) == 200


def test_redact_url_token_and_key():
    url = "http://pi.local:8000/assets/a.png?token=mgst_abc123&size=big"
    out = redact_url(url)
    assert "mgst_abc123" not in out
    assert "token=***" in out
    assert "size=big" in out

    out2 = redact_url("http://x/y?key=SECRET&q=1")
    assert "SECRET" not in out2
    assert "key=***" in out2
    assert "q=1" in out2


def test_redact_url_no_params_untouched():
    plain = "http://pi.local:8000/assets/a.png"
    assert redact_url(plain) == plain
    assert redact_url("") == ""


def test_now_iso_has_offset():
    ts = now_iso()
    assert "T" in ts
    # 本地时区偏移（+08:00 等）
    assert re.search(r"[+-]\d{2}:\d{2}$", ts)
