"""生成程序图标 assets/steamboot.ico（多尺寸）。

为什么单独用 Pillow 画：PyInstaller 需要一个 .ico 文件，
而界面里的图标是 Qt 在运行时现画的（ui/icons.py）。这里用同一套几何画一份落盘的 ico，
保证 exe 图标与窗口内图标一致。

运行： python tools/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
BLUE_TOP = (61, 155, 255, 255)     # #3d9bff
BLUE_BOTTOM = (0, 113, 227, 255)   # #0071e3
WHITE = (255, 255, 255, 255)
SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_icon(size: int) -> Image.Image:
    """圆角方块 + 向上箭头（与 ui/icons.py 的绘制保持一致）。"""
    scale = 4  # 先画大图再缩小，边缘更干净
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    # 竖向渐变底
    gradient = Image.new("RGBA", (big, big))
    pixels = gradient.load()
    for y in range(big):
        ratio = y / max(1, big - 1)
        color = tuple(
            int(BLUE_TOP[i] + (BLUE_BOTTOM[i] - BLUE_TOP[i]) * ratio) for i in range(4)
        )
        for x in range(big):
            pixels[x, y] = color

    radius = int(big * 0.24)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=255)
    image.paste(gradient, (0, 0), mask)

    # 箭头
    arrow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(arrow)
    points = [
        (32, 14), (48, 33), (38, 33), (38, 50), (26, 50), (26, 33), (16, 33),
    ]
    draw.polygon([(x * big / 64, y * big / 64) for x, y in points], fill=WHITE)
    image.alpha_composite(arrow)

    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    frames = [draw_icon(size) for size in SIZES]
    ico_path = ASSETS / "steamboot.ico"
    frames[-1].save(ico_path, format="ICO", sizes=[(s, s) for s in SIZES])
    # 顺带存一张 PNG，便于在文档里引用
    frames[-1].save(ASSETS / "steamboot.png", format="PNG")
    print(f"已生成：{ico_path}")
    print(f"已生成：{ASSETS / 'steamboot.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
