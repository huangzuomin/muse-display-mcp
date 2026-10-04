"""BOOX NoteX Adapter — copy-adapt 已验证的 ADB 控制链。

母本：D:\\电脑医生\\muse-boox\\executor.py（musegadget boox.* 命令，
2026-10-04 实机验证）；硬化经验见 docs/背景信息.md §4。

评审 T2/T3 范围：
- 双地址回退（母本语义：两个默认地址互为备胎各试 1 次，单地址试 2 次）
- connect timeout=15s 是 subprocess 上限，LAN 实测不可达 ≈3s/次即失败
  （背景信息 §4.1 记 echo 探测 2×1.2s，为 Pi 部署版实现，语义一致）
- 成功启发式 "connected" in out and "cannot" not in out；shell 空输出重试 1 次
- show_image 双超时（评审 D6）：prompt= 生成 240s / url|path= 推送 15s
- get_status 超时 = max(5s, 探测预算 + 秒级执行)
- clear = input keyevent 3（回桌面，母本已验证）
- 图片显示 = push /sdcard/Muse + am start -S 显式 PhotoActivity 组件
  （背景信息 §4.2：-S 防旧图缓存，显式组件防 Neo 浏览器 ERR_ACCESS_DENIED）
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image

from .. import render
from ..base import DisplayAdapter
from ..errors import (
    MuseDisplayError,
    device_offline,
    device_timeout,
    invalid_payload,
    render_error,
    transport_error,
)
from ..util import new_command_id
from ..render import fit_image, render_card_page, render_text_page, save_jpeg

ADB_BIN = shutil.which("adb") or "/usr/lib/android-sdk/platform-tools/adb"

_KEY_HOME = 3  # executor.py BOOX_KEY_NAMES["home"]
_STAGING_DIR = "/sdcard/Muse"
# -S 先杀相册进程：已展示时重发同路径 intent，PhotoActivity 不会重载位图
_SHOW_IMAGE_SHELL = (
    "am start -S -n com.onyx.gallery/.activities.PhotoActivity "
    "-a android.intent.action.VIEW -d file://{path} -t image/jpeg"
)
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 需求 §4.4：限制图片尺寸


class BooxAdapter(DisplayAdapter):
    capabilities = ("text", "card", "image", "clear", "status")

    CONNECT_TIMEOUT_S = 15.0   # subprocess 上限；实测不可达 ≈3s/次
    RETRY_SLEEP_S = 1.5
    SHELL_TIMEOUT_S = 30.0
    STATUS_SHELL_TIMEOUT_S = 5.0  # 需求 §12：普通命令 5s
    PUSH_TIMEOUT_S = 90.0
    SHOW_TIMEOUT_S = 25.0
    IMAGE_BUDGET_S = 15.0      # D6: url 下载超时预算（推送另受 PUSH_TIMEOUT_S 约束）
    ART_BUDGET_S = 240.0       # D6: prompt 生成总预算（daily-art.py 内部 ≈230s）
    PROBE_TTL_S = 60.0         # is_online 探测缓存，防 list_devices 高频打 ADB

    def __init__(
        self,
        device,
        *,
        adb_bin: str | None = None,
        art_script: str = "",
        font_path: str | None = render.DEFAULT_FONT_PATH,
        work_dir: str | Path | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
        fetch=None,
    ):
        self._device = device
        self._addresses = list(device.addresses)
        self._adb_bin = adb_bin or ADB_BIN
        self._art_script = art_script or device.art_script
        self._font_path = font_path
        self._work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="muse-display-boox-"))
        self._clock = clock
        self._sleep = sleep
        self._fetch = fetch or self._fetch_url
        self._addr: str | None = None
        self._probe: tuple[bool, float] | None = None  # (online, at)

    # ------------------------------------------------------------------
    # 连接层（executor.py _boox_connect / _boox_connect_one 语义）
    # ------------------------------------------------------------------

    def _run(self, args: list[str], timeout_s: float) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, timeout=timeout_s)

    def _connect_one(self, addr: str, attempts: int) -> str | None:
        """连一个地址；成功返回 None，失败返回可读错误（含 tcpip 5555 补救提示）。"""
        fail = ""
        for attempt in range(attempts):
            out = ""
            try:
                proc = self._run([self._adb_bin, "connect", addr], self.CONNECT_TIMEOUT_S)
                out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
            except subprocess.TimeoutExpired:
                out = ""
            except OSError as exc:
                out = f"adb not runnable: {exc}"
            if "connected" in out and "cannot" not in out:
                return None
            fail = out or f"no output from adb connect {addr}"
            if attempt < attempts - 1:
                self._sleep(self.RETRY_SLEEP_S)
        return (
            f"cannot reach the BOOX at {addr} ({fail}). If the e-reader was "
            "rebooted, plug it into a computer over USB and run `adb tcpip 5555` "
            "once to restore."
        )

    def _connect(self) -> str:
        """双地址回退；返回可用地址或抛 DEVICE_OFFLINE。"""
        if self._addr is not None:
            return self._addr
        candidates = list(self._addresses)
        multi = len(candidates) > 1  # 母本规则：有备胎各 1 次，单地址 2 次
        fail = ""
        for cand in candidates:
            fail = self._connect_one(cand, 1 if multi else 2)
            if fail is None:
                self._addr = cand
                return cand
        raise device_offline(self._device.device_id, fail or "no ADB address configured")

    def _adb(self, addr: str, args: list[str], timeout_s: float):
        return self._run([self._adb_bin, "-s", addr, *args], timeout_s)

    def _shell(self, addr: str, shell_args: list[str], timeout_s: float,
               retry_empty: bool = True) -> str:
        """adb shell；首条命令冷启动可能空输出 → 重试 1 次（母本一致）。"""
        out = ""
        attempts = 2 if retry_empty else 1
        for attempt in range(attempts):
            try:
                proc = self._adb(addr, shell_args, timeout_s)
            except subprocess.TimeoutExpired:
                raise device_timeout(self._device.device_id, timeout_s)
            if proc.returncode != 0:
                self._invalidate()
                err = proc.stderr.decode(errors="replace").strip()
                raise transport_error(
                    f"adb shell {' '.join(shell_args[:2])} failed: {err or proc.returncode}"
                )
            out = proc.stdout.decode(errors="replace").strip()
            if out or attempt == attempts - 1:
                break
            self._sleep(self.RETRY_SLEEP_S)
        return out

    def _sh_quiet(self, addr: str, shell_args: list[str]) -> str:
        """get_status 专用：单字段读取失败不致命，吞错返回空。"""
        try:
            return self._shell(addr, shell_args, self.STATUS_SHELL_TIMEOUT_S,
                               retry_empty=False)
        except MuseDisplayError:
            return ""

    def _invalidate(self) -> None:
        """连接疑似失效（设备深睡等）：清缓存，下次命令重新探测。"""
        self._addr = None
        self._probe = None

    # ------------------------------------------------------------------
    # 图片管线
    # ------------------------------------------------------------------

    def _push_and_show(self, addr: str, path: Path) -> str:
        target = f"{_STAGING_DIR}/{path.name}"
        try:
            proc = self._adb(addr, ["push", str(path), target], self.PUSH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._invalidate()
            raise device_timeout(self._device.device_id, self.PUSH_TIMEOUT_S)
        if proc.returncode != 0:
            self._invalidate()
            raise transport_error(
                "adb push failed: " + proc.stderr.decode(errors="replace").strip()
            )
        try:
            self._shell(addr, [_SHOW_IMAGE_SHELL.format(path=target)],
                        self.SHOW_TIMEOUT_S, retry_empty=False)
        except MuseDisplayError:
            self._invalidate()
            raise
        return target

    def _save(self, img: Image.Image, cid: str) -> Path:
        path = self._work / f"muse-{cid.removeprefix('cmd-')}.jpg"
        save_jpeg(img, path)
        return path

    @staticmethod
    def _fetch_url(url: str, timeout_s: float) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "muse-display-mcp/0.1"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            return resp.read()

    def _show_rendered(self, img: Image.Image) -> dict:
        cid = new_command_id()
        addr = self._connect()
        target = self._push_and_show(addr, self._save(img, cid))
        return {"command_id": cid, "file": target, "address": addr}

    # ------------------------------------------------------------------
    # DisplayAdapter 接口
    # ------------------------------------------------------------------

    def show_text(self, payload: dict) -> dict:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise invalid_payload("show_text requires non-empty 'text'")
        # ttl：BOOX 无设备端 TTL，V0.1 不模拟（评审 c8009bd4：TTL 持久化归补齐轮）
        img = render_text_page(self._device.width, self._device.height, text,
                               font_path=self._font_path)
        return self._show_rendered(img)

    def show_card(self, payload: dict) -> dict:
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise invalid_payload("show_card requires non-empty 'title'")
        body = payload.get("body") or ""
        if not isinstance(body, str):
            raise invalid_payload("show_card 'body' must be a string")
        priority = payload.get("priority", "normal")
        if priority not in ("low", "normal", "high"):
            raise invalid_payload("show_card priority must be low/normal/high")
        img = render_card_page(self._device.width, self._device.height,
                               title, body, priority, font_path=self._font_path)
        return self._show_rendered(img)

    def show_image(self, payload: dict) -> dict:
        prompt = payload.get("prompt")
        url = payload.get("image_url")
        path_arg = payload.get("image_path")
        given = [s for s in (prompt, url, path_arg) if s]
        if len(given) != 1:
            raise invalid_payload(
                "show_image needs exactly one of prompt / image_url / image_path"
            )
        if prompt:
            return self._show_generated(prompt)

        started = self._clock()  # D6: url|path 总预算 15s
        if path_arg:
            src = Path(str(path_arg))
            if not src.is_file():
                raise invalid_payload(f"image_path not found: {path_arg}")
            data = src.read_bytes()
        else:
            remaining = max(3.0, self.IMAGE_BUDGET_S - (self._clock() - started) - 6.0)
            try:
                data = self._fetch(str(url), min(10.0, remaining))
            except MuseDisplayError:
                raise
            except Exception as exc:
                raise transport_error(f"image download failed: {exc}")
        if len(data) > _MAX_IMAGE_BYTES:
            raise invalid_payload(
                f"image too large: {len(data)} bytes (max {_MAX_IMAGE_BYTES})"
            )
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as exc:
            raise render_error(f"image decode failed: {exc}")
        fit = payload.get("fit", "contain")
        if fit not in ("contain", "cover"):
            raise invalid_payload("fit must be contain or cover")
        img = fit_image(img, self._device.width, self._device.height, fit)
        return self._show_rendered(img)

    def _show_generated(self, prompt: str) -> dict:
        """prompt= → daily-art.py 生成管线（--now 自带生成+推送，RESULT_JSON 回执）。"""
        if not self._art_script:
            raise invalid_payload(
                "show_image prompt= requires art_script in the device config"
            )
        if not Path(self._art_script).is_file():
            raise invalid_payload(f"art_script not found: {self._art_script}")
        cid = new_command_id()
        try:
            proc = self._run(
                ["python3", self._art_script, "--now", "--theme", prompt, "--gray"],
                max(10.0, self.ART_BUDGET_S - 10.0),  # 留 10s 给解析
            )
        except subprocess.TimeoutExpired:
            raise device_timeout(self._device.device_id, self.ART_BUDGET_S)
        except OSError as exc:
            raise transport_error(f"art script not runnable: {exc}")
        result = self._parse_result_json(proc.stdout.decode(errors="replace"))
        if proc.returncode != 0 or not result.get("ok"):
            raise render_error(
                f"art generation failed: {result.get('error') or f'exit {proc.returncode}'}"
            )
        if not result.get("displayed"):
            # 脚本已生成但推送失败（多半设备深睡）→ 按离线处理，文件已留档
            raise device_offline(
                self._device.device_id,
                f"generated at {result.get('file')} but push failed",
            )
        return {
            "command_id": cid,
            "file": result.get("file"),
            "prompt": prompt,  # 回显请求值；RESULT_JSON 不带 prompt
            "address": result.get("addr"),
        }

    @staticmethod
    def _parse_result_json(stdout: str) -> dict:
        for line in reversed(stdout.splitlines()):
            if line.startswith("RESULT_JSON:"):
                try:
                    return json.loads(line[len("RESULT_JSON:"):])
                except json.JSONDecodeError:
                    break
        raise render_error("art script produced no RESULT_JSON line")

    def clear(self) -> dict:
        cid = new_command_id()
        addr = self._connect()
        # clear = 回桌面（母本：input keyevent 3）
        self._shell(addr, ["input", "keyevent", str(_KEY_HOME)],
                    self.SHELL_TIMEOUT_S, retry_empty=False)
        return {"command_id": cid, "action": "home_key", "address": addr}

    def get_status(self) -> dict:
        """离线是合法应答（online: false），不抛异常。"""
        cid = new_command_id()
        try:
            addr = self._connect()
        except MuseDisplayError as exc:
            return {"command_id": cid, "online": False, "error": exc.message}
        model = self._sh_quiet(addr, ["getprop", "ro.product.model"])
        battery = self._sh_quiet(addr, ["dumpsys", "battery"])
        power = self._sh_quiet(addr, ["dumpsys", "power"])
        act = self._sh_quiet(addr, ["dumpsys", "activity", "activities"])
        return {
            "command_id": cid,
            "online": True,
            "address": addr,
            "model": model or "unknown",
            "battery": _extract_field(battery, predicate=lambda ln: ln.strip().startswith("level"), sep=":"),
            "screen": "awake" if _extract_field(power, predicate=lambda ln: "mWakefulness=" in ln, sep="=") == "Awake" else "asleep",
            "foreground_app": _extract_component(act),
        }

    def is_online(self) -> bool:
        now = self._clock()
        if self._probe and now - self._probe[1] < self.PROBE_TTL_S:
            return self._probe[0]
        try:
            self._connect()
            ok = True
        except MuseDisplayError:
            ok = False
        self._probe = (ok, now)
        return ok


def _extract_field(text: str, *, predicate, sep) -> str:
    for line in text.splitlines():
        if predicate(line):
            value = line.strip()
            if sep:
                return value.split(sep, 1)[1].strip()
            return value.split()[-1] if value.split() else ""
    return ""


def _extract_component(text: str) -> str:
    """ResumedActivity 行里抓组件名：含 / 的那个 token
    （ActivityRecord{abc u0 com.pkg/.Act t42} 的末 token 是 t42}，不能用）。"""
    for line in text.splitlines():
        if "ResumedActivity" in line:
            for token in line.split():
                if "/" in token:
                    return token
    return ""
