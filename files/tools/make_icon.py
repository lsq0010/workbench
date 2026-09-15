#!/usr/bin/env python3
"""生成工作平台的 App 图标（.icns）。

为什么不用现成的图
  macOS 的 App 图标有固定规范：圆角矩形、内容留边、多尺寸打包。
  随便放张 PNG 会显得很山寨（在 Dock 里大小不对、圆角不对）。
  这里按 macOS 的规范画，然后 iconutil 打包成 .icns。

画的是什么
  深色圆角方块 + 一个"工具台"意象：
  上面一排小方块（代表功能），下面一条横线（代表桌面）。
  简洁、在小尺寸下也认得出。
"""
import os
import subprocess
import sys

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    print("  ❌ 需要 pillow：pip3 install --user pillow")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))

# macOS 图标规范：1024 画布里，内容占 824×824 居中（四周约 100px 留白）
CANVAS = 1024
INSET = 100
SIZE = CANVAS - INSET * 2
RADIUS = int(SIZE * 0.2237)      # macOS Big Sur 之后的圆角比例


def make_base():
    """画底：圆角方块 + 渐变"""
    im = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # 渐变（从左上到右下）—— 手动逐行画，避免依赖 numpy
    grad = Image.new("RGBA", (SIZE, SIZE))
    gd = ImageDraw.Draw(grad)
    c1 = (47, 109, 246)      # 蓝
    c2 = (124, 58, 237)      # 紫
    for y in range(SIZE):
        for_x = y / SIZE
        r = int(c1[0] + (c2[0] - c1[0]) * for_x)
        g = int(c1[1] + (c2[1] - c1[1]) * for_x)
        b = int(c1[2] + (c2[2] - c1[2]) * for_x)
        gd.line([(0, y), (SIZE, y)], fill=(r, g, b, 255))

    # 圆角遮罩
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1],
                                           radius=RADIUS, fill=255)
    im.paste(grad, (INSET, INSET), mask)
    return im


def draw_mark(im):
    """画内容：一排功能格子 + 底部两条桌面线。

    **要画在独立的叠加层上再合成** —— ImageDraw 在 RGBA 图上直接画
    是"替换像素"不是"混合"，半透明白色会变成透明窟窿，
    而不是"白色叠在渐变上"。这是个容易忽略的坑。
    """
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    W = (255, 255, 255, 255)
    W70 = (255, 255, 255, 180)
    W35 = (255, 255, 255, 90)

    # 一排 3 个等大格子（代表工具）
    # 尺寸要先算好 —— 第一版我让第一个"大一点"，结果第三个超出圆角边界被切了。
    # 宁可用等大的，保证一定放得下。
    pad = int(SIZE * 0.16)
    x0 = INSET + pad
    avail = SIZE - pad * 2                    # 可用宽度
    gap = int(avail * 0.07)
    sq = (avail - gap * 2) // 3               # 每个格子的边长
    y0 = INSET + int(SIZE * 0.185)
    r = int(sq * 0.27)

    for i, alpha in enumerate((W, W70, W35)):
        x = x0 + i * (sq + gap)
        d.rounded_rectangle([x, y0, x + sq, y0 + sq], radius=r, fill=alpha)

    # 底部两条"桌面"横线
    ly = y0 + sq + int(SIZE * 0.09)
    lh = int(SIZE * 0.04)
    d.rounded_rectangle([x0, ly, x0 + int(avail * 0.72), ly + lh],
                        radius=lh // 2, fill=W70)
    ly2 = ly + int(SIZE * 0.078)
    d.rounded_rectangle([x0, ly2, x0 + int(avail * 0.47), ly2 + lh],
                        radius=lh // 2, fill=W35)

    # 合成回主图（这一步才真正"混合"）
    return Image.alpha_composite(im, layer)


def make_icon_png(size=1024):
    im = draw_mark(make_base())
    if size != CANVAS:
        im = im.resize((size, size), Image.LANCZOS)
    return im


def build_icns(out_path):
    """生成 .icns —— macOS 要一套尺寸打进一个文件里"""
    iconset = os.path.join(HERE, "工作平台.iconset")
    os.makedirs(iconset, exist_ok=True)
    # macOS 要求的尺寸清单
    specs = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
             (256, 1), (256, 2), (512, 1), (512, 2)]
    for base, scale in specs:
        px = base * scale
        name = "icon_%dx%d%s.png" % (base, base, "@2x" if scale == 2 else "")
        make_icon_png(px).save(os.path.join(iconset, name))
    r = subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("  ❌ iconutil 失败：%s" % (r.stderr or r.stdout)[:200])
        return False
    # 清掉中间产物
    for f in os.listdir(iconset):
        os.remove(os.path.join(iconset, f))
    os.rmdir(iconset)
    return True


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "工作平台.icns")
    # 顺便存一张 PNG 便于预览
    make_icon_png(512).save(os.path.join(HERE, "工作平台-预览.png"))
    if build_icns(out):
        print("  ✅ %s（%d 字节）" % (out, os.path.getsize(out)))
        print("  ✅ 预览图：%s" % os.path.join(HERE, "工作平台-预览.png"))
