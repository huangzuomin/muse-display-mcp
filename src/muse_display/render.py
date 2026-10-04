"""PIL 渲染层：BOOX 全页文本 / 卡片布局 + 图片适配（评审 T3/T5）。

- 无字体文件时回退 PIL 内置字体（本地开发/测试零外部依赖）；
  Pi 部署默认 /usr/share/fonts/truetype/wqy/wqy-zenhei.ttc
  （背景信息 §4.3：系统字体缺 ASCII 数字和「·」）。
- 渲染为纯函数（参数 → RGB Image），smoke 测试直接断言尺寸与内容。
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
WHITE = (255, 255, 255)
BLACK = (10, 10, 10)
GRAY = (110, 110, 110)


def load_font(size: int, font_path: str | None = DEFAULT_FONT_PATH):
    """优先 TrueType 字体；文件不存在回退 PIL 内置字体。"""
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            pass  # 非树莓派环境 / 字体未安装
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1 可缩放
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """逐字换行（CJK 安全）；保留显式换行。"""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw:
            lines.append("")
            continue
        current = ""
        for ch in raw:
            trial = current + ch
            if not current or draw.textlength(trial, font=font) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = ch
        lines.append(current)
    return lines


def _fit_lines(lines: list[str], max_lines: int, draw, font, max_width: int) -> list[str]:
    """截断到 max_lines 行，末行加 …。"""
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    last = kept[-1]
    while last and draw.textlength(last + "…", font=font) > max_width:
        last = last[:-1]
    kept[-1] = last + "…"
    return kept


def _line_height(draw, font) -> int:
    l, t, r, b = draw.textbbox((0, 0), "汉Ag1·", font=font)
    return max(1, int((b - t) * 1.35))


def render_text_page(
    width: int,
    height: int,
    text: str,
    *,
    font_path: str | None = DEFAULT_FONT_PATH,
    font_size: int | None = None,
    margin: int | None = None,
) -> Image.Image:
    """全页纯文本：白底黑字、自动换行、溢出截断加 …。"""
    margin = margin if margin is not None else max(40, width // 16)
    size = font_size or min(96, max(48, width // 14))
    img = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(img)
    font = load_font(size, font_path)
    max_w = width - margin * 2
    line_h = _line_height(draw, font)
    max_lines = max(1, (height - margin * 2) // line_h)
    lines = _fit_lines(_wrap(draw, text, font, max_w), max_lines, draw, font, max_w)
    y = margin
    for line in lines:
        draw.text((margin, y), line, font=font, fill=BLACK)
        y += line_h
    return img


def render_card_page(
    width: int,
    height: int,
    title: str,
    body: str,
    priority: str = "normal",
    *,
    font_path: str | None = DEFAULT_FONT_PATH,
) -> Image.Image:
    """结构化卡片：外框 + 优先级角标 + 标题 + 分隔线 + 正文（黑白，e-ink）。"""
    if priority not in ("low", "normal", "high"):
        raise ValueError(f"invalid priority: {priority!r}")
    img = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(img)
    border = max(8, width // 36)
    inset = border * 2
    draw.rectangle(
        [inset, inset, width - inset, height - inset], outline=BLACK, width=border
    )

    title_font = load_font(max(40, width // 10), font_path)
    body_font = load_font(max(24, width // 20), font_path)
    tag_font = load_font(max(18, width // 26), font_path)
    pad = inset + border * 3
    inner_w = width - pad * 2

    # 优先级角标：high 反白块，normal 黑字，low 灰字
    tag_text = priority.upper()
    _, t, r, b = draw.textbbox((0, 0), tag_text, font=tag_font)
    tag_w, tag_h = r, b - t
    tag_x = width - pad - tag_w
    if priority == "high":
        draw.rectangle([tag_x - 10, pad - 8, width - pad + 10, pad + tag_h + 16], fill=BLACK)
        draw.text((tag_x, pad), tag_text, font=tag_font, fill=WHITE)
    else:
        draw.text((tag_x, pad), tag_text, font=tag_font, fill=BLACK if priority == "normal" else GRAY)

    # 标题（最多 3 行）
    y = pad + tag_h + max(30, width // 24)
    title_line_h = _line_height(draw, title_font)
    title_lines = _fit_lines(_wrap(draw, title, title_font, inner_w), 3, draw, title_font, inner_w)
    for line in title_lines:
        draw.text((pad, y), line, font=title_font, fill=BLACK)
        y += title_line_h

    # 分隔线
    y += max(16, width // 64)
    draw.line([pad, y, width - pad, y], fill=GRAY, width=max(2, width // 350))
    y += max(24, width // 40)

    # 正文：撑满剩余空间
    body_line_h = _line_height(draw, body_font)
    max_lines = max(1, (height - pad - y) // body_line_h)
    for line in _fit_lines(_wrap(draw, body, body_font, inner_w), max_lines, draw, body_font, inner_w):
        draw.text((pad, y), line, font=body_font, fill=BLACK)
        y += body_line_h
    return img


def fit_image(img: Image.Image, width: int, height: int, mode: str = "contain") -> Image.Image:
    """需求 §4.4：contain 等比完整放入（白边），cover 等比放大后居中裁切。"""
    if mode not in ("contain", "cover"):
        raise ValueError(f"invalid fit mode: {mode!r}")
    img = img.convert("RGB")
    if mode == "contain":
        scaled = img.copy()
        scaled.thumbnail((width, height), Image.LANCZOS)
        canvas = Image.new("RGB", (width, height), WHITE)
        canvas.paste(scaled, ((width - scaled.width) // 2, (height - scaled.height) // 2))
        return canvas
    scale = max(width / img.width, height / img.height)
    resized = img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def save_jpeg(img: Image.Image, path, quality: int = 90) -> None:
    img.save(str(path), "JPEG", quality=quality)
