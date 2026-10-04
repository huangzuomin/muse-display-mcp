"""BooxAdapter：双地址回退 / 连接启发式 / clear / get_status / show_* 全链。

全部走 monkeypatch 替身 `_run`，无 adb、无设备；渲染用 PIL 真跑。
"""

import io
import json

import pytest
from PIL import Image

from muse_display.adapters.boox import BooxAdapter
from muse_display.errors import ErrorCode, MuseDisplayError
from muse_display.registry import DeviceConfig

LAN = "192.0.2.10:5555"
TAILNET = "192.0.2.10:5555"


class FakeProc:
    def __init__(self, out=b"", err=b"", rc=0):
        self.stdout = out if isinstance(out, bytes) else out.encode()
        self.stderr = err if isinstance(err, bytes) else err.encode()
        self.returncode = rc


def connected(addr):
    return FakeProc(out=f"connected to {addr}\n".encode())


def cannot(addr):
    return FakeProc(out=f"cannot connect to {addr}".encode())


class ScriptedAdb:
    """按调用序返回预设 FakeProc；记录全部 argv 与 timeout。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, args, timeout_s):
        self.calls.append((list(args), timeout_s))
        if not self.steps:
            raise AssertionError(f"unexpected adb call: {args}")
        step = self.steps.pop(0)
        return step(args) if callable(step) else step

    def connects(self):
        return [c[0] for c in self.calls if "connect" in c[0]]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_device(addresses=(LAN, TAILNET), art_script=""):
    return DeviceConfig(
        device_id="boox", name="BOOX", type="boox", transport="adb",
        width=1404, height=1872,
        capabilities=("text", "card", "image", "clear", "status"),
        addresses=addresses, art_script=art_script,
    )


def make_boox(steps, tmp_path, addresses=(LAN, TAILNET), art_script=""):
    device = make_device(addresses, art_script)
    adapter = BooxAdapter(
        device, adb_bin="adb-fake", work_dir=tmp_path,
        clock=FakeClock(), sleep=lambda s: None,
    )
    runner = ScriptedAdb(steps)
    adapter._run = runner
    return adapter, runner


def jpeg_bytes(w=320, h=240):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "JPEG")
    return buf.getvalue()


# ---- 连接层 ----------------------------------------------------------------

def test_dual_address_fallback(tmp_path):
    adapter, adb = make_boox(
        [cannot(LAN), connected(TAILNET),
         FakeProc()],  # clear 的 shell
        tmp_path,
    )
    result = adapter.clear()
    assert result["address"] == TAILNET
    assert len(adb.connects()) == 2                      # 各试 1 次（有备胎）
    assert f"connect {LAN}".split()[1] in adb.connects()[0]
    assert f"connect {TAILNET}".split()[1] in adb.connects()[1]
    # clear 走的是回退后地址
    shell_argv = adb.calls[-1][0]
    assert shell_argv[:4] == ["adb-fake", "-s", TAILNET, "input"]


def test_single_address_two_attempts(tmp_path):
    adapter, adb = make_boox(
        [cannot(LAN), cannot(LAN)], tmp_path, addresses=(LAN,),
    )
    with pytest.raises(MuseDisplayError) as ei:
        adapter.clear()
    assert ei.value.code == ErrorCode.DEVICE_OFFLINE
    assert len(adb.connects()) == 2                      # 无备胎 → 同地址试 2 次
    assert "adb tcpip 5555" in ei.value.message          # 母本补救提示


def test_multi_address_one_attempt_each(tmp_path):
    adapter, adb = make_boox(
        [cannot(LAN), cannot(TAILNET)], tmp_path,
    )
    with pytest.raises(MuseDisplayError) as ei:
        adapter.clear()
    assert ei.value.code == ErrorCode.DEVICE_OFFLINE
    assert len(adb.connects()) == 2                      # 不是 2×2


def test_connected_heuristic(tmp_path):
    # "already connected" 含 connected 且无 cannot → 视为成功
    adapter, adb = make_boox(
        [FakeProc(out=f"already connected to {LAN}\n"), FakeProc()],
        tmp_path, addresses=(LAN,),
    )
    result = adapter.clear()
    assert result["address"] == LAN


def test_shell_failure_invalidates_connection(tmp_path):
    adapter, adb = make_boox(
        [connected(LAN),
         FakeProc(err=b"device offline", rc=1),   # clear 的 shell 失败
         connected(LAN),                          # 下一条命令重新探测
         FakeProc()],                             # clear 重试成功
        tmp_path, addresses=(LAN,),
    )
    with pytest.raises(MuseDisplayError) as ei:
        adapter.clear()
    assert ei.value.code == ErrorCode.TRANSPORT_ERROR
    assert adapter._addr is None                          # 缓存已失效
    result = adapter.clear()
    assert result["address"] == LAN
    assert len(adb.connects()) == 2


# ---- clear / get_status ------------------------------------------------------

def test_clear_argv_is_home_key(tmp_path):
    adapter, adb = make_boox([connected(LAN), FakeProc()], tmp_path,
                             addresses=(LAN,))
    result = adapter.clear()
    assert result == {"command_id": result["command_id"],
                      "action": "home_key", "address": LAN}
    assert result["command_id"].startswith("cmd-")
    assert adb.calls[-1][0][-3:] == ["input", "keyevent", "3"]


def test_get_status_offline_is_legal_answer(tmp_path):
    adapter, _ = make_boox([cannot(LAN), cannot(LAN)], tmp_path,
                           addresses=(LAN,))
    st = adapter.get_status()                             # 不抛
    assert st["online"] is False
    assert "offline" in st["error"].lower()


def test_get_status_happy_parses_fields(tmp_path):
    battery = FakeProc(out=b"Current Battery Service state:\n  level: 80\n  status: 2\n")
    power = FakeProc(out=b"mWakefulness=Awake\n  mWakefulnessChanging=false\n")
    act = FakeProc(out=b"  mResumedActivity: ActivityRecord{abc u0 com.onyx.gallery/.activities.PhotoActivity t42}\n")
    adapter, adb = make_boox(
        [connected(LAN), FakeProc(out=b"NoteX\n"), battery, power, act],
        tmp_path, addresses=(LAN,),
    )
    st = adapter.get_status()
    assert st["online"] is True
    assert st["model"] == "NoteX"
    assert st["battery"] == "80"
    assert st["screen"] == "awake"
    assert "PhotoActivity" in st["foreground_app"]


def test_is_online_probe_cached(tmp_path):
    adapter, adb = make_boox([connected(LAN)], tmp_path, addresses=(LAN,))
    assert adapter.is_online() is True
    assert adapter.is_online() is True
    assert len(adb.connects()) == 1                       # 60s 探测缓存


# ---- show_text / show_image（渲染真跑，ADB 全替身） ---------------------------

def test_show_text_full_flow(tmp_path):
    adapter, adb = make_boox(
        [connected(LAN),
         FakeProc(out=b"1 file pushed"),   # push
         FakeProc(out=b"Starting: Intent")],  # am start
        tmp_path, addresses=(LAN,),
    )
    result = adapter.show_text({"text": "你好，树莓派"})
    assert result["command_id"].startswith("cmd-")
    # push 段：adb -s <addr> push <work_dir 源> /sdcard/Muse/...
    push_argv = adb.calls[1][0]
    assert push_argv[3] == "push"
    assert str(tmp_path) in push_argv[4]
    assert push_argv[5] == result["file"]
    assert "/sdcard/Muse/" in result["file"]
    # am start 段：整条 shell 命令是单个字符串元素
    shell_cmd = adb.calls[2][0][3]
    assert "-S" in shell_cmd
    assert "com.onyx.gallery/.activities.PhotoActivity" in shell_cmd
    assert f"file://{result['file']}" in shell_cmd


def test_show_text_rejects_empty(tmp_path):
    adapter, adb = make_boox([], tmp_path)
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_text({"text": "  "})
    assert ei.value.code == ErrorCode.INVALID_PAYLOAD
    assert adb.calls == []


def test_show_image_from_path(tmp_path):
    img_file = tmp_path / "in.jpg"
    img_file.write_bytes(jpeg_bytes())
    adapter, adb = make_boox(
        [connected(LAN), FakeProc(out=b"1 file pushed"), FakeProc(out=b"Starting")],
        tmp_path, addresses=(LAN,),
    )
    result = adapter.show_image({"image_path": str(img_file)})
    assert result["file"].startswith("/sdcard/Muse/")
    # 渲染产物与设备分辨率一致
    from PIL import Image as I
    staged = tmp_path / f"muse-{result['command_id'].removeprefix('cmd-')}.jpg"
    with I.open(staged) as im:
        assert im.size == (1404, 1872)


def test_show_image_bad_fit(tmp_path):
    adapter, adb = make_boox([], tmp_path)
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_image({"image_path": "x.jpg", "fit": "stretch"})
    assert ei.value.code == ErrorCode.INVALID_PAYLOAD
    assert adb.calls == []


def test_show_image_needs_exactly_one_source(tmp_path):
    adapter, adb = make_boox([], tmp_path)
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_image({})
    assert ei.value.code == ErrorCode.INVALID_PAYLOAD
    with pytest.raises(MuseDisplayError):
        adapter.show_image({"image_path": "a", "image_url": "b"})
    assert adb.calls == []


# ---- show_image prompt= 生成管线（daily-art.py 契约） -------------------------

def write_script(tmp_path):
    script = tmp_path / "daily-art.py"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return str(script)


def test_prompt_requires_art_script(tmp_path):
    adapter, adb = make_boox([], tmp_path)
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_image({"prompt": "sunset"})
    assert ei.value.code == ErrorCode.INVALID_PAYLOAD
    assert adb.calls == []


def test_prompt_missing_script_file(tmp_path):
    adapter, adb = make_boox([], tmp_path, art_script=str(tmp_path / "nope.py"))
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_image({"prompt": "sunset"})
    assert ei.value.code == ErrorCode.INVALID_PAYLOAD
    assert adb.calls == []


def test_prompt_happy_result_json(tmp_path):
    script = write_script(tmp_path)

    def art_step(args):
        assert args == ["python3", script, "--now", "--theme", "sunset", "--gray"]
        return FakeProc(out=(
            "painting...\n"
            'RESULT_JSON: {"ok": true, "file": "/home/user/boox-art/x.jpg", '
            '"displayed": true, "addr": "%s"}\n' % LAN
        ).encode())

    adapter, adb = make_boox([art_step], tmp_path, addresses=(LAN,),
                             art_script=script)
    result = adapter.show_image({"prompt": "sunset"})
    assert result["file"] == "/home/user/boox-art/x.jpg"
    assert result["prompt"] == "sunset"
    assert result["address"] == LAN
    assert len(adb.calls) == 1                            # 唯一外部调用就是 art 脚本


def test_prompt_generated_but_push_failed_is_offline(tmp_path):
    script = write_script(tmp_path)
    out = ('RESULT_JSON: {"ok": true, "file": "/home/user/boox-art/x.jpg", '
           '"displayed": false, "addr": null}\n').encode()
    adapter, _ = make_boox([FakeProc(out=out)], tmp_path, addresses=(LAN,),
                           art_script=script)
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_image({"prompt": "sunset"})
    assert ei.value.code == ErrorCode.DEVICE_OFFLINE
    assert "generated at" in ei.value.message


def test_prompt_script_failure_is_render_error(tmp_path):
    script = write_script(tmp_path)
    out = b"traceback...\nRESULT_JSON: {\"ok\": false, \"error\": \"SDXL timeout\"}\n"
    adapter, _ = make_boox([FakeProc(out=out, rc=1)], tmp_path,
                           addresses=(LAN,), art_script=script)
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_image({"prompt": "sunset"})
    assert ei.value.code == ErrorCode.RENDER_ERROR
    assert "SDXL timeout" in ei.value.message


def test_prompt_no_result_json_is_render_error(tmp_path):
    script = write_script(tmp_path)
    adapter, _ = make_boox([FakeProc(out=b"nothing here\n")], tmp_path,
                           addresses=(LAN,), art_script=script)
    with pytest.raises(MuseDisplayError) as ei:
        adapter.show_image({"prompt": "sunset"})
    assert ei.value.code == ErrorCode.RENDER_ERROR
    assert "RESULT_JSON" in ei.value.message
