"""生成桌面应用图标（.icns）。

设计：浅色圆角底 + 4 根渐高圆角柱（价格走势看板）。

⚠️ 三个必须自己做的点（都不是"系统会自动处理"）：
  1. **圆角必须自绘**：macOS 系统 app 的 icns 本身就是"圆角矩形 + 四角透明"，
     圆角是图标自带的，不是渲染时裁的。铺满方形画布 → Finder 里是直角方块。
  2. **画布要留白**：圆角矩形只占画布 85.9%（实测 Notes/Reminders/App Store/
     Calculator/Maps 一致），填满 100% 会明显比别的 app 大一圈。
  3. **图形占比按 HIG**：浅色背景 app 图形约占画布 0.62~0.70（Notes/Reminders 的量级）。

用法：python make_icon.py <输出目录>
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

CANVAS = 1024
SS = 4                      # 超采样倍数：先放大画再缩小，圆角无锯齿
SHAPE_RATIO = 0.859         # 圆角矩形占画布比例（实测 macOS 系统 app 值）
RADIUS_RATIO = 0.2237       # 圆角半径按**形状宽**算（Apple 模板 185.4/824）
GLYPH_RATIO = 0.65          # 图形占画布比例（HIG 浅色 app 经验值）

# 柱状图配色：浅蓝 → 深蓝的纵向渐变，代表"价格走势"
BAR_TOP = (96, 165, 250)
BAR_BOTTOM = (37, 99, 235)
BAR_HEIGHTS = (0.42, 0.60, 0.78, 1.00)      # 递增，读作"上涨"
BAR_WIDTH_RATIO = 0.145                     # 相对图形区宽度
BAR_GAP_RATIO = 0.55                        # 间隙 = 柱宽 × 该比例


def squircle_mask(size: int) -> Image.Image:
    """圆角矩形 alpha 蒙版（超采样后缩小，边缘平滑）。

    ⚠️ 蒙版是单通道 `L` 模式，**不能**在这里画带 alpha 的描边色
       （`outline=(0,0,0,26)` 会报 "color must be int or single-element tuple"）。
       描边要画在合成后的白底上 —— 见 `render()`。
    """
    big = size * SS
    m = Image.new("L", (big, big), 0)
    shape = round(big * SHAPE_RATIO)
    off = (big - shape) // 2
    radius = round(shape * RADIUS_RATIO)
    ImageDraw.Draw(m).rounded_rectangle(
        (off, off, off + shape - 1, off + shape - 1), radius=radius, fill=255,
    )
    return m.resize((size, size), Image.LANCZOS)


def _outline_geometry(size: int) -> tuple[int, int, int]:
    """圆角矩形的 (左上角, 边长, 圆角半径)，用于画描边。"""
    shape = round(size * SHAPE_RATIO)
    off = (size - shape) // 2
    return off, shape, round(shape * RADIUS_RATIO)


def draw_bars(size: int) -> Image.Image:
    """图形层：4 根渐高圆角柱。透明背景，由调用方合成到白底上。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    glyph = size * GLYPH_RATIO
    bar_w = glyph * BAR_WIDTH_RATIO
    gap = bar_w * BAR_GAP_RATIO
    total_w = bar_w * len(BAR_HEIGHTS) + gap * (len(BAR_HEIGHTS) - 1)
    x0 = (size - total_w) / 2
    base_y = (size + glyph) / 2                  # 柱脚对齐到图形区底部
    radius = bar_w / 2

    for i, h in enumerate(BAR_HEIGHTS):
        bx = x0 + i * (bar_w + gap)
        top = base_y - glyph * h
        d.rounded_rectangle((bx, top, bx + bar_w, base_y), radius=radius, fill=BAR_TOP)

    # 纵向渐变：按 y 逐行把颜色从 BAR_TOP 混到 BAR_BOTTOM
    grad = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        c = tuple(round(BAR_TOP[k] + (BAR_BOTTOM[k] - BAR_TOP[k]) * t) for k in range(3))
        gd.line([(0, y), (size, y)], fill=(*c, 255))
    # 用柱形做蒙版把渐变裁出来
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), layer.split()[3])
    return out


def render(size: int) -> Image.Image:
    """合成一张完整图标：白底圆角矩形 + 极淡描边 + 图形。"""
    mask = squircle_mask(size)
    base = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    base.paste(Image.new("RGBA", (size, size), (255, 255, 255, 255)), (0, 0), mask)

    # 极淡描边：系统 app 都有。没有它，纯白圆角矩形在浅色背景上会"糊"掉，
    # 视觉上更显大。必须画在**白底之上**（蒙版是 L 模式，放不下带 alpha 的色）。
    off, shape, radius = _outline_geometry(size)
    ImageDraw.Draw(base).rounded_rectangle(
        (off, off, off + shape - 1, off + shape - 1),
        radius=radius, outline=(0, 0, 0, 26), width=max(1, round(size / 341)),
    )

    # ⚠️ 必须用 alpha_composite，不能用别的 RGB 合成 ——
    #    否则会盖掉底板四角的透明
    base.alpha_composite(draw_bars(size))
    return base


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    iconset = out_dir / "app.iconset"
    iconset.mkdir(exist_ok=True)

    # 标准尺寸：16/32/128/256/512 及其 @2x。**不要**放独立的 icon_64x64.png
    # （iconutil 会报 Invalid Iconset）
    specs = [
        ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
    ]
    for name, px in specs:
        render(px).save(iconset / name)
    render(CANVAS).save(out_dir / "icon-preview-1024.png")

    icns = out_dir / "app.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)

    # ---- 自查：不能只看"文件生成了"，必须验四角透明 + 占比 ----
    im = Image.open(icns).convert("RGBA")
    s = im.size[0]
    corners_ok = all(im.getpixel(p)[3] == 0 for p in
                     ((3, 3), (s - 4, 3), (3, s - 4), (s - 4, s - 4)))
    bbox = im.getbbox()
    ratio = (bbox[2] - bbox[0]) / s if bbox else 0

    print(f"  生成：{icns}（{im.size[0]}×{im.size[1]}）")
    print(f"  四角透明：{'✅' if corners_ok else '❌ 圆角没做对'}")
    print(f"  圆角矩形占画布：{ratio * 100:.1f}%（目标 ~85.9%）")
    print(f"  图标集：{len(specs)} 张")
    return 0 if corners_ok else 1


if __name__ == "__main__":
    sys.exit(main())
