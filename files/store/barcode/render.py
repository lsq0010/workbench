#!/usr/bin/env python3
"""把编码出来的位串画成 PNG / SVG。

位串里 '1' 是黑条、'0' 是白空 —— 这是所有一维码的通用表示，
所以画图这步和具体码制无关。
"""
import io
import os

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def _hex(c):
    c = (c or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def bits_to_png(bits, module=2, height=90, quiet=10, bar="#000000", bg="#ffffff",
                text="", text_size=None, show_text=True):
    """位串 → PNG 字节。

    module   一个窄条的像素宽度（2 就够清楚；要打印用 3~4）
    height   条高（不含文字）
    quiet    左右静区宽度，单位是窄条数 —— **不能省**，省了扫不出来
    """
    if not HAS_PIL:
        raise RuntimeError("需要 PIL（pip install pillow）")
    q = quiet * module
    body_w = len(bits) * module
    text_h = 0
    if show_text and text:
        text_h = text_size or max(14, int(height * 0.22))
    W = q * 2 + body_w
    H = height + text_h + (6 if text_h else 0)

    im = Image.new("RGB", (W, H), _hex(bg))
    d = ImageDraw.Draw(im)
    fg = _hex(bar)
    # 用**下标直接算 x**（q + i*module）。
    # 踩过的坑：早先自己维护一个 x 变量，遇到 '0' 时只 i+=1 没推 x，
    # 结果所有黑条都画在同一个位置、糊成一整块黑 —— 扫不出来。
    i, n = 0, len(bits)
    while i < n:
        if bits[i] == "1":
            j = i
            while j < n and bits[j] == "1":
                j += 1
            d.rectangle([q + i * module, 0, q + j * module - 1, height - 1], fill=fg)
            i = j
        else:
            i += 1

    if text_h and text:
        try:
            from PIL import ImageFont
            fp = "/System/Library/Fonts/Supplemental/Arial.ttf"
            if not os.path.isfile(fp):
                fp = "/System/Library/Fonts/Helvetica.ttc"
            font = ImageFont.truetype(fp, int(text_h * 0.82))
        except Exception:
            font = None
        try:
            bbox = d.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(text) * text_h * 0.55
        d.text(((W - tw) / 2, height + 4), text, fill=fg, font=font)

    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def bits_to_svg(bits, module=2, height=90, quiet=10, bar="#000000", bg="#ffffff",
                text="", show_text=True):
    """位串 → SVG 字符串（矢量，打印不糊）。"""
    q = quiet * module
    body_w = len(bits) * module
    text_h = 20 if (show_text and text) else 0
    W = q * 2 + body_w
    H = height + text_h + (6 if text_h else 0)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d">' % (W, H, W, H),
             '<rect width="%d" height="%d" fill="%s"/>' % (W, H, bg)]
    i, n = 0, len(bits)
    while i < n:
        if bits[i] == "1":
            j = i
            while j < n and bits[j] == "1":
                j += 1
            parts.append('<rect x="%d" y="0" width="%d" height="%d" fill="%s"/>'
                         % (q + i * module, (j - i) * module, height, bar))
            i = j
        else:
            i += 1
    if text_h and text:
        parts.append('<text x="%d" y="%d" font-family="monospace" font-size="%d" '
                     'text-anchor="middle" fill="%s">%s</text>'
                     % (W // 2, height + 15, int(text_h * 0.8), bar,
                        _svg_esc(text)))
    parts.append('</svg>')
    return "".join(parts)


def _svg_esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def bits_to_datauri(bits, **kw):
    """给网页 <img src> 用的 data URI"""
    import base64
    png = bits_to_png(bits, **kw)
    return "data:image/png;base64," + base64.b64encode(png).decode()


# 名字 → pyzbar/Vision 的码制名（验证和解码时用）
SYMBOLOGY = {
    "code128": "code128", "code128b": "code128", "code128c": "code128",
    "ean13": "ean13", "ean8": "ean8", "upca": "upce",
    "code39": "code39", "itf": "i2of5", "itf14": "itf14",
    "codabar": "codabar",
}
