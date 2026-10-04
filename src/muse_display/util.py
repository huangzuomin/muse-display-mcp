"""小工具：command_id 生成、日志 URL 脱敏、时间戳（需求 §10/§14）。"""

from __future__ import annotations

import re
import secrets
from datetime import datetime


def new_command_id() -> str:
    """每条命令生成唯一 command_id：cmd-<8hex>（评审 T4）。"""
    return "cmd-" + secrets.token_hex(4)


# 需求 §14：图片 URL 若含 token 应脱敏。命中 ?token= / &key= 等（大小写不敏感）。
_TOKEN_PARAM_RE = re.compile(r"(?P<prefix>[?&](?:token|key)=)[^&\s]+", re.IGNORECASE)


def redact_url(url: str) -> str:
    """把 URL 里的 token/key 查询参数值替换为 ***，其余保持原样。"""
    if not url:
        return url
    return _TOKEN_PARAM_RE.sub(lambda m: m.group("prefix") + "***", url)


def now_iso() -> str:
    """本地时区 ISO 时间戳（秒级），如 2026-10-04T09:30:00+08:00。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
