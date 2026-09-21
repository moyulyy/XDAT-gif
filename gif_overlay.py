#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GIF 逐帧标注: 在当前帧上叠加

    * 力 / 能量 / 相对首帧 RMS 等数值
    * 多个分析曲线上「对应位置」的点与竖线 (由 matplotlib 预渲染底板 + PIL 打点)

设计要点
--------
matplotlib 逐帧重绘很慢, 所以这里:
    1. 一次性用 Agg 渲染所有曲线的「底板」(坐标轴 + 曲线) 并保存为 PIL 图像,
       同时记录每个数据点对应的像素坐标;
    2. 每一帧只需在透明图层上画几根竖线 / 圆点, 再和底板合成 —— 极快。

对外主要接口:
    CurveSpec                 一条曲线的数据与样式
    CurvePanel                曲线底板 + 逐帧打点
    FrameAnnotator            数值文本 + 曲线底板, 生成最终帧图像
    load_font / hex_to_rgb    小工具
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 让 matplotlib 使用 Agg (无界面) 渲染
import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure                       # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402

# 中文字体 (Windows 自带)
for _name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"):
    try:
        plt.rcParams["font.sans-serif"] = [_name]
        break
    except Exception:
        continue
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["font.size"] = 8

PANEL_COLORS = ["#007AFF", "#FF3B30", "#34C759", "#AF52DE",
                "#FF9500", "#5AC8FA", "#FF2D55", "#5856D6"]


# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------
_FONT_CACHE: Dict[Tuple[int, bool], ImageFont.FreeTypeFont] = {}
_FONT_CANDIDATES_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyhbd.ttf",
    "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/arialbd.ttf",
]
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/arial.ttf",
]


def _matplotlib_font() -> Optional[str]:
    try:
        from matplotlib import font_manager
        return font_manager.findfont(font_manager.FontProperties(
            family="sans-serif"), fallback_to_default=True)
    except Exception:
        return None


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载一个支持中文的 TrueType 字体, 带缓存与多级回退。"""
    key = (int(size), bool(bold))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = (_FONT_CANDIDATES_BOLD if bold else []) + _FONT_CANDIDATES
    for path in candidates:
        try:
            if Path(path).is_file():
                font = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = font
                return font
        except Exception:
            continue
    mp = _matplotlib_font()
    if mp:
        try:
            font = ImageFont.truetype(mp, size)
            _FONT_CACHE[key] = font
            return font
        except Exception:
            pass
    font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def hex_to_rgb(color: str) -> Tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# 曲线数据
# --------------------------------------------------------------------------
@dataclass
class CurveSpec:
    key: str
    label: str
    x: np.ndarray
    y: np.ndarray
    color: str = "#007AFF"
    ylabel: str = ""
    fmt: str = "{:.4g}"


# --------------------------------------------------------------------------
# 曲线底板
# --------------------------------------------------------------------------
class CurvePanel:
    """把若干曲线渲染成一张底板图像, 并支持逐帧打点。"""

    def __init__(self, series: Sequence[CurveSpec], width: int, height: int,
                 dpi: int = 100, max_cols: int = 3, theme: str = "light",
                 title: str = ""):
        self.series = [s for s in series if s is not None and len(s.x)]
        self.width = int(width)
        self.height = int(height)
        self.dpi = int(dpi)
        self.theme = theme
        self._pts: Dict[str, np.ndarray] = {}
        self._base: Optional[Image.Image] = None
        self._title = title
        self._max_cols = max(1, max_cols)

        if not self.series:
            self._base = Image.new("RGB", (self.width, max(1, self.height)),
                                   "#FFFFFF")
            return
        self._render()

    # ---------------------------------------------------------------
    @property
    def base(self) -> Image.Image:
        return self._base

    def marker_positions(self, key: str) -> Optional[np.ndarray]:
        return self._pts.get(key)

    # ---------------------------------------------------------------
    def _render(self):
        n = len(self.series)
        cols = min(self._max_cols, n)
        rows = int(math.ceil(n / cols))
        bg = "#FFFFFF" if self.theme == "light" else "#1E1E20"
        fg = "#1C1C1E" if self.theme == "light" else "#F2F2F7"
        grid = "#E3E3E8" if self.theme == "light" else "#3A3A3C"

        fig = Figure(figsize=(self.width / self.dpi, self.height / self.dpi),
                     dpi=self.dpi, facecolor=bg, layout="constrained")
        FigureCanvasAgg(fig)
        n = len(self.series)
        axes = []
        grid_axes = fig.subplots(rows, cols, squeeze=False)

        for idx, spec in enumerate(self.series):
            r, c = divmod(idx, cols)
            ax = grid_axes[r][c]
            axes.append(ax)
            y = np.asarray(spec.y, dtype=float)
            x = np.asarray(spec.x, dtype=float)
            if len(x) != len(y):
                m = min(len(x), len(y))
                x, y = x[:m], y[:m]
            ax.plot(x, y, color=spec.color, linewidth=1.4,
                    marker="o", markersize=1.6, markeredgewidth=0)
            ax.set_facecolor(bg)
            ax.tick_params(colors=fg, labelsize=7, length=2)
            for spine in ax.spines.values():
                spine.set_color(grid)
                spine.set_linewidth(0.8)
            ax.grid(True, color=grid, linewidth=0.6, alpha=0.8)
            ax.set_xlim(min(x.min(), 0) - 0.5, max(x.max(), 1) + 0.5)
            finite = y[np.isfinite(y)]
            if len(finite):
                lo, hi = float(finite.min()), float(finite.max())
                if hi - lo < 1e-12:
                    hi = lo + 1.0
                pad = (hi - lo) * 0.12
                ax.set_ylim(lo - pad, hi + pad)
            ax.set_title(spec.label, color=fg, fontsize=8, pad=3)
            if spec.ylabel:
                ax.set_ylabel(spec.ylabel, color=fg, fontsize=7)
            ax.set_xlabel("Frame", color=fg, fontsize=7)

        for idx in range(n, rows * cols):
            r, c = divmod(idx, cols)
            grid_axes[r][c].axis("off")

        if self._title:
            fig.suptitle(self._title, color=fg, fontsize=9)

        canvas = fig.canvas
        canvas.draw()
        buf = np.asarray(canvas.buffer_rgba())
        # matplotlib 的 figure 像素尺寸会因 figsize/dpi 取整而与请求值差 1px,
        # 这里以实际画布为准回写尺寸, 保证底板与标记层大小严格一致。
        self._base = Image.fromarray(buf[:, :, :3].copy(), "RGB")
        self.height, self.width = self._base.height, self._base.width

        h_px = buf.shape[0]
        for ax, spec in zip(axes, self.series):
            x = np.asarray(spec.x, dtype=float)
            y = np.asarray(spec.y, dtype=float)
            pts = []
            for xi, yi in zip(x, y):
                if not np.isfinite(xi) or not np.isfinite(yi):
                    pts.append((np.nan, np.nan))
                    continue
                px, py = ax.transData.transform((xi, yi))
                pts.append((float(px), float(h_px - py)))
            self._pts[spec.key] = np.asarray(pts, dtype=float)
        plt.close(fig)

    # ---------------------------------------------------------------
    def marker_overlay(self, index: int, radius: int = 4,
                       vline: bool = True) -> Image.Image:
        """当前帧对应位置的透明标记层。"""
        rgba = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        if not self._pts:
            return rgba
        draw = ImageDraw.Draw(rgba)
        for spec in self.series:
            pts = self._pts.get(spec.key)
            if pts is None or index < 0 or index >= len(pts):
                continue
            x, y = pts[index]
            if not (np.isfinite(x) and np.isfinite(y)):
                continue
            rgb = hex_to_rgb(spec.color)
            if vline:
                draw.line([(x, 0), (x, self.height)], fill=rgb + (110,), width=1)
            r = radius
            draw.ellipse([x - r, y - r, x + r, y + r],
                         fill=rgb + (255,), outline=(255, 255, 255, 255), width=2)
        return rgba


# --------------------------------------------------------------------------
# 帧标注
# --------------------------------------------------------------------------
def _hex_rgb(color) -> Tuple[int, int, int]:
    """'#RRGGBB' / (r,g,b) -> (r, g, b)。"""
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        return int(color[0]), int(color[1]), int(color[2])
    s = str(color).lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return 28, 28, 30


def _line_parts(ln):
    """标注行 -> (文本, 颜色)。允许 str 或 (text, color) 元组。"""
    if isinstance(ln, (tuple, list)) and len(ln) >= 2:
        return str(ln[0]), (ln[1] or None)
    return str(ln), None


class FrameAnnotator:
    """给单帧图像叠加文本 + (可选) 曲线底板。"""

    def __init__(self,
                 panel: Optional[CurvePanel] = None,
                 show_numbers: bool = True,
                 position: str = "bottom",
                 theme: str = "light",
                 font_scale: float = 1.0,
                 panel_gap: int = 8,
                 numbers_corner: str = "top-left"):
        self.panel = panel
        self.show_numbers = show_numbers
        self.position = position               # bottom / top / overlay
        self.theme = theme
        self.font_scale = font_scale
        self.panel_gap = panel_gap
        self.numbers_corner = numbers_corner

    # ---------------------------------------------------------------
    def _draw_numbers(self, img: Image.Image, lines: List[str]) -> Image.Image:
        if not lines:
            return img
        base_size = max(13, int(img.width * 0.033 * self.font_scale))
        font = load_font(base_size, bold=False)
        bold = load_font(base_size, bold=True)

        pad = max(8, int(base_size * 0.62))
        asc, desc = font.getmetrics()
        line_h = asc + desc + max(2, int(base_size * 0.28))

        # 计算文本框尺寸
        texts = [_line_parts(ln) for ln in lines]
        widths = []
        for t, _ in texts:
            bbox = font.getbbox(t)
            widths.append(bbox[2] - bbox[0])
        box_w = max(widths) + pad * 2
        box_h = line_h * len(texts) + pad * 2

        m = max(10, int(img.width * 0.02))
        if "right" in self.numbers_corner:
            bx = img.width - box_w - m
        else:
            bx = m
        if "bottom" in self.numbers_corner:
            by = img.height - box_h - m
        else:
            by = m

        rgba = img.convert("RGBA")
        layer = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        bg = (255, 255, 255, 216) if self.theme == "light" else (28, 28, 30, 220)
        d.rounded_rectangle([bx, by, bx + box_w, by + box_h], radius=10, fill=bg)

        text_col = (28, 28, 30, 255) if self.theme == "light" else (242, 242, 247, 255)
        y = by + pad
        for i, (txt, color) in enumerate(texts):
            f = bold if i == 0 else font
            if color:
                rgb = _hex_rgb(color)
                fill = (rgb[0], rgb[1], rgb[2], 255)
            else:
                fill = text_col
            d.text((bx + pad, y), txt, font=f, fill=fill)
            y += line_h
        return Image.alpha_composite(rgba, layer).convert("RGB")

    # ---------------------------------------------------------------
    def _numbers_block(self, lines: List[str], width: int) -> Image.Image:
        """把数值文本渲染成一块宽度固定、高度自适应的面板 (右侧布局用)。

        每行可以是 str, 也可以是 (文本, 颜色); 颜色为空则用默认文字色。
        """
        bg = (255, 255, 255) if self.theme == "light" else (30, 30, 32)
        col = (28, 28, 30) if self.theme == "light" else (242, 242, 247)
        texts = [_line_parts(ln) for ln in lines]
        width = max(80, int(width))
        pad = max(8, int(width * 0.035))
        size = max(11, int(width * 0.052 * self.font_scale))
        font = load_font(size)
        while size > 10 and max(font.getbbox(t)[2] for t, _ in texts) > width - 2 * pad:
            size -= 1
            font = load_font(size)
        bold = load_font(size, bold=True)
        asc, desc = font.getmetrics()
        line_h = asc + desc + max(2, int(size * 0.30))
        h = line_h * len(texts) + pad * 2
        block = Image.new("RGB", (width, max(1, h)), bg)
        d = ImageDraw.Draw(block)
        y = pad
        for i, (txt, color) in enumerate(texts):
            d.text((pad, y), txt, font=(bold if i == 0 else font),
                   fill=(_hex_rgb(color) if color else col))
            y += line_h
        return block

    # ---------------------------------------------------------------
    def _compose_right(self, img: Image.Image, index: int, lines: List[str],
                       mark_index: Optional[int] = None) -> Image.Image:
        """左右布局: 左边轨迹, 右边 (上) 数值 + (下) 曲线。"""
        bg = (255, 255, 255) if self.theme == "light" else (30, 30, 32)
        panel = self.panel
        if panel is None or panel.base is None or not panel.series:
            return self._draw_numbers(img, lines) if self.show_numbers else img

        pw, ph = panel.width, img.height
        mi = index if mark_index is None else mark_index
        overlay = panel.marker_overlay(mi)
        base = Image.alpha_composite(panel.base.convert("RGBA"),
                                     overlay).convert("RGB")
        col = Image.new("RGB", (pw, ph), bg)
        y = 0
        if self.show_numbers and lines:
            block = self._numbers_block(lines, pw)
            col.paste(block, (0, 0))
            y = min(block.height, ph)
        rest = ph - y
        if rest > 12:
            if (base.width, base.height) != (pw, rest):
                base = base.resize((pw, rest), Image.LANCZOS)
            col.paste(base, (0, y))
        # 与轨迹之间的 1px 分隔线
        sep = (214, 214, 219) if self.theme == "light" else (72, 72, 76)
        ImageDraw.Draw(col).line([(0, 0), (0, ph)], fill=sep, width=1)

        canvas = Image.new("RGB", (img.width + pw, ph), bg)
        canvas.paste(img, (0, 0))
        canvas.paste(col, (img.width, 0))
        return canvas

    # ---------------------------------------------------------------
    def annotate(self, frame: Image.Image, index: int, lines: List[str],
                 mark_index: Optional[int] = None) -> Image.Image:
        """合成最终帧。

        frame      原始 3D 截图 (PIL)
        index      当前帧序号 (用于曲线上打点)
        lines      文本行 (第一行通常为标题)
        """
        img = frame.convert("RGB")
        if self.position == "right":
            return self._compose_right(img, index, lines, mark_index)
        if self.show_numbers:
            img = self._draw_numbers(img, lines)

        if self.panel is None or self.panel.base is None or not self.panel.series:
            return img

        mi = index if mark_index is None else mark_index
        overlay = self.panel.marker_overlay(mi)
        panel_img = self.panel.base.copy()
        panel_img = Image.alpha_composite(panel_img.convert("RGBA"), overlay).convert("RGB")

        if self.position == "overlay":
            # 缩放底板到合适宽度后贴在右下角
            pw = int(img.width * 0.5)
            ph = int(panel_img.height * pw / panel_img.width)
            small = panel_img.resize((pw, ph), Image.LANCZOS)
            img = img.convert("RGBA")
            img.alpha_composite(small.convert("RGBA"),
                                (img.width - pw, img.height - ph))
            return img.convert("RGB")

        # bottom / top: 拼接
        gap = self.panel_gap
        total_h = img.height + gap + panel_img.height
        canvas = Image.new("RGB", (img.width, total_h),
                           "#FFFFFF" if self.theme == "light" else "#1E1E20")
        if self.position == "top":
            canvas.paste(panel_img, (0, 0))
            canvas.paste(img, (0, panel_img.height + gap))
        else:
            canvas.paste(img, (0, 0))
            canvas.paste(panel_img, (0, img.height + gap))
        return canvas


# --------------------------------------------------------------------------
# 辅助: 生成文本行
# --------------------------------------------------------------------------
def format_frame_lines(index: int, n_frames: int,
                       force: Optional[float],
                       energy: Optional[float],
                       rms: Optional[float],
                       energy_diff: Optional[float] = None,
                       force_diff: Optional[float] = None,
                       show_force: bool = True,
                       show_energy: bool = True,
                       show_rms: bool = True,
                       show_diff: bool = False,
                       colors: Optional[dict] = None,
                       lang: str = "zh") -> List[str]:
    """返回标注文本行。

    colors: 可选 {key: 颜色}, key ∈ force/energy/rms/energy_diff/force_diff。
             给了就把对应行变成 (文本, 颜色) 元组, 画图时用该颜色渲染
             (最大力 = 红线, 能量 = 蓝线, RMS = 绿线, 与曲线颜色一致)。
    """
    colors = colors or {}

    def _ln(key: str, text: str):
        c = colors.get(key)
        return (text, c) if c else text

    if lang == "zh":
        head = f"第 {index + 1} / {n_frames} 帧"
        l_force, l_energy, l_rms = "最大力", "能量", "相对首帧 RMS"
        l_de, l_df = "Δ能量", "Δ最大力"
    else:
        head = f"Frame {index + 1} / {n_frames}"
        l_force, l_energy, l_rms = "Max force", "Energy", "RMS vs frame 1"
        l_de, l_df = "dE", "dF"
    lines = [head]
    if show_force and force is not None:
        lines.append(_ln("force", f"{l_force}:  {force:.4f} eV/Å"))
    if show_energy and energy is not None:
        lines.append(_ln("energy", f"{l_energy}:  {energy:.5f} eV"))
    if show_rms and rms is not None:
        lines.append(_ln("rms", f"{l_rms}:  {rms:.4f} Å"))
    if show_diff:
        if energy_diff is not None:
            lines.append(_ln("energy_diff", f"{l_de}:  {energy_diff:+.5f} eV"))
        if force_diff is not None:
            lines.append(_ln("force_diff", f"{l_df}:  {force_diff:+.4f} eV/Å"))
    return lines
