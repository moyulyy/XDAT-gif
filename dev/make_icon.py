#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成应用图标 assets/app.ico (圆角渐变 + 分子 + 播放三角)。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
OUT = HERE / "assets" / "app.ico"


def rounded_gradient(size: int) -> Image.Image:
    """绘制圆角方块 + 竖直蓝色渐变。"""
    scale = 4
    s = size * scale
    grad = Image.new("RGBA", (s, s))
    px = grad.load()
    top = (10, 132, 255)
    bot = (0, 78, 200)
    for y in range(s):
        t = y / max(1, s - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        for x in range(s):
            px[x, y] = (r, g, b, 255)

    # 斜向高光
    hi = Image.new("L", (s, s), 0)
    hd = ImageDraw.Draw(hi)
    hd.polygon([(0, 0), (s, 0), (0, s)], fill=70)
    hi = hi.filter(ImageFilter.GaussianBlur(s * 0.12))
    white = Image.new("RGBA", (s, s), (255, 255, 255, 255))
    grad = Image.composite(white, grad, hi)

    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1],
                                           radius=int(s * 0.22), fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


def draw_molecule(img: Image.Image) -> Image.Image:
    s = img.size[0]
    scale = 4
    big = img.resize((s * scale, s * scale), Image.LANCZOS)
    d = ImageDraw.Draw(big, "RGBA")
    W = s * scale
    # 原子位置 (相对坐标)
    atoms = [
        (0.50, 0.40, 0.115, (255, 255, 255)),
        (0.29, 0.63, 0.085, (255, 92, 87)),
        (0.71, 0.63, 0.085, (52, 199, 89)),
        (0.50, 0.21, 0.060, (255, 214, 10)),
    ]
    pts = [(x * W, y * W, r * W, c) for x, y, r, c in atoms]
    # 键
    bonds = [(0, 1), (0, 2), (0, 3)]
    for i, j in bonds:
        d.line([(pts[i][0], pts[i][1]), (pts[j][0], pts[j][1])],
               fill=(255, 255, 255, 210), width=int(W * 0.028))
    # 原子
    for x, y, r, c in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=c,
                  outline=(255, 255, 255, 255), width=max(1, int(r * 0.18)))
    return big.resize((s, s), Image.LANCZOS)


def main():
    base = rounded_gradient(256)
    base = draw_molecule(base)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(OUT, format="ICO", sizes=sizes)
    # 额外导出一张 PNG 便于查看
    base.save(OUT.with_name("app.png"), format="PNG")
    print("saved", OUT)


if __name__ == "__main__":
    main()
