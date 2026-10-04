"""render.py smoke：尺寸 / 非空白 / 截断 / 卡片 / fit / JPEG 往返。

无字体文件环境（回退 PIL 内置字体）即可全跑——本地零外部依赖。
"""

import pytest
from PIL import Image

from muse_display.render import (
    fit_image,
    render_card_page,
    render_text_page,
    save_jpeg,
)

W, H = 1404, 1872


def test_text_page_size_and_content():
    img = render_text_page(W, H, "你好，树莓派")
    assert img.size == (W, H)
    extrema = img.getextrema()            # RGB 三通道各 (lo, hi)
    assert any(lo < hi for lo, hi in extrema)   # 有字 → 非纯白


def test_text_page_wraps_cjk_and_truncates():
    long_text = "屏" * 5000                # 远超一屏 → 必须换行 + 截断
    img = render_text_page(W, H, long_text)
    assert img.size == (W, H)
    # 截断标记省不了——直接验证纯函数不炸、尺寸正确即可
    small = render_text_page(400, 300, long_text, margin=20)
    assert small.size == (400, 300)


def test_text_page_explicit_newlines_kept():
    img = render_text_page(W, H, "第一行\n\n第三行")
    assert img.size == (W, H)


def test_card_priorities_render():
    for priority in ("low", "normal", "high"):
        img = render_card_page(W, H, "标题", "正文内容", priority)
        assert img.size == (W, H)


def test_card_invalid_priority():
    with pytest.raises(ValueError):
        render_card_page(W, H, "t", "b", "urgent")


def test_fit_contain_full_visible_on_white_canvas():
    src = Image.new("RGB", (100, 50), "red")
    out = fit_image(src, 200, 200, "contain")
    assert out.size == (200, 200)
    # 白画布四角为白，中心为红
    assert out.getpixel((0, 0)) == (255, 255, 255)
    assert out.getpixel((100, 100)) == (255, 0, 0)


def test_fit_cover_crops_exact_size():
    src = Image.new("RGB", (100, 50), "blue")
    out = fit_image(src, 50, 50, "cover")
    assert out.size == (50, 50)
    assert out.getpixel((0, 0)) == (0, 0, 255)


def test_fit_invalid_mode():
    with pytest.raises(ValueError):
        fit_image(Image.new("RGB", (10, 10)), 20, 20, "stretch")


def test_save_jpeg_roundtrip(tmp_path):
    img = render_text_page(300, 200, "存盘")
    path = tmp_path / "out.jpg"
    save_jpeg(img, path)
    with Image.open(path) as loaded:
        assert loaded.format == "JPEG"
        assert loaded.size == (300, 200)
