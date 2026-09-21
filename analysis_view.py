#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
分析曲线控件: 用 matplotlib 在 Qt 中绘制

    * 指定原子对的键长
    * 相对首帧的位移 RMS
    * 力收敛 / 力收敛一阶差分
    * 能量 / 能量一阶差分

特点:
    * 每个子图**固定大小**, 曲线越多不是越小, 而是向下排列并在滚动区内滚动。
    * 与 3D 轨迹浏览器联动: 竖线指示当前帧。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # noqa: E402
from matplotlib.figure import Figure                             # noqa: E402

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from gif_overlay import PANEL_COLORS, CurveSpec
import vasp_parser as vp

for _n in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"):
    try:
        matplotlib.rcParams["font.sans-serif"] = [_n]
        break
    except Exception:
        continue
matplotlib.rcParams["axes.unicode_minus"] = False


# 曲线定义: key -> (中文名, 单位, ylabel, 颜色)
CURVE_DEFS = {
    "force":      ("力收敛 (最大力)", "eV/Å", "Max F", PANEL_COLORS[1]),
    "force_diff": ("力收敛 · 一阶差分", "eV/Å", "ΔF", PANEL_COLORS[6]),
    "energy":     ("能量", "eV", "E", PANEL_COLORS[0]),
    "energy_diff": ("能量 · 一阶差分", "eV", "ΔE", PANEL_COLORS[5]),
    "rms":        ("相对首帧 RMS 位移", "Å", "RMS", PANEL_COLORS[2]),
    "max_disp":   ("最大单原子位移", "Å", "max|Δr|", PANEL_COLORS[7]),
}

# 曲线 key -> AnalysisResult 字段名 (必须显式映射, 否则取不到数据)
CURVE_ATTR = {
    "force":       "max_force",
    "force_diff":  "force_diff",
    "energy":      "energies",
    "energy_diff": "energy_diff",
    "rms":         "rms",
    "max_disp":    "max_disp",
}

# 每个子图的固定像素尺寸
SUB_W = 420
SUB_H = 250
DPI = 100


class AnalysisView(QWidget):
    """曲线选择 + 固定尺寸多子图 (可滚动)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result: Optional[vp.AnalysisResult] = None
        self._vlines: List = []
        self._current = 0
        self._cols = 2

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ---- 选择器 (网格排布, 不会互相挤压 / 遮挡) ----
        self.checks: Dict[str, QCheckBox] = {}
        short = {"force": "力收敛 (最大力)", "force_diff": "力 · 一阶差分",
                 "energy": "能量", "energy_diff": "能量 · 一阶差分",
                 "rms": "相对首帧 RMS 位移", "max_disp": "最大单原子位移"}
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(5)
        for idx, (key, (name, _u, _yl, color)) in enumerate(CURVE_DEFS.items()):
            cb = QCheckBox(short.get(key, name))
            cb.setCursor(Qt.PointingHandCursor)
            cb.setStyleSheet(f"QCheckBox{{color: {color}; font-weight:600;}}")
            cb.toggled.connect(self.replot)
            self.checks[key] = cb
            grid.addWidget(cb, idx // 3, idx % 3)
        self.btn_save = QPushButton("导出 PNG")
        self.btn_save.setObjectName("Ghost")
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self.export_png)
        grid.addWidget(self.btn_save, 0, 3, 2, 1)
        grid.setColumnStretch(4, 1)
        root.addLayout(grid)

        # ---- 滚动区 + 画布 ----
        self.area = QScrollArea()
        self.area.setWidgetResizable(False)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.area.viewport().setAutoFillBackground(False)
        self.area.setMinimumHeight(SUB_H + 30)
        self.area.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        self.figure = Figure(figsize=(SUB_W * 2 / DPI, SUB_H / DPI),
                             dpi=DPI, facecolor="#FFFFFF",
                             layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.area.setWidget(self.canvas)
        root.addWidget(self.area, 1)

        # 根据可用宽度自适应列数, 保证子图尺寸固定且不出现横向滚动
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(180)
        self._resize_timer.timeout.connect(self._apply_resize)

        # 默认勾选
        for k in ("force", "energy", "rms"):
            self.checks[k].setChecked(True)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_resize_timer"):
            self._resize_timer.start()

    def _apply_resize(self):
        avail = max(SUB_W, self.width() - 24)
        cols = max(1, min(3, int(avail // SUB_W)))
        if cols != self._cols:
            self._cols = cols
            self.replot()

    # ------------------------------------------------------------------
    def set_result(self, result: vp.AnalysisResult):
        self.result = result
        for key in ("force", "force_diff"):
            self.checks[key].setEnabled(result.has_force)
        for key in ("energy", "energy_diff"):
            self.checks[key].setEnabled(result.has_energy)
        self.replot()

    def set_current(self, i: int):
        self._current = int(i)
        for vl in self._vlines:
            try:
                vl.set_xdata([self._current, self._current])
            except Exception:
                pass
        if self._vlines:
            self.canvas.draw_idle()

    # ------------------------------------------------------------------
    def _specs(self) -> List[CurveSpec]:
        specs: List[CurveSpec] = []
        res = self.result
        if res is None:
            return specs
        for key, (name, unit, ylabel, color) in CURVE_DEFS.items():
            if not self.checks[key].isChecked():
                continue
            if key in ("force", "force_diff") and not res.has_force:
                continue
            if key in ("energy", "energy_diff") and not res.has_energy:
                continue
            arr = getattr(res, CURVE_ATTR.get(key, key), None)
            if arr is None or len(arr) == 0:
                continue
            x = np.arange(len(arr))
            specs.append(CurveSpec(key, name, x, np.asarray(arr, dtype=float),
                                   color, ylabel))
        for key, arr in res.bonds.items():
            label = f"键长 {key}"
            specs.append(CurveSpec(f"bond:{key}", label, np.arange(len(arr)),
                                   np.asarray(arr, dtype=float),
                                   PANEL_COLORS[len(specs) % len(PANEL_COLORS)],
                                   "d / Å"))
        return specs

    def selected_specs(self) -> List[CurveSpec]:
        return self._specs()

    # ------------------------------------------------------------------
    def replot(self):
        if not hasattr(self, "figure"):
            return
        self.figure.clear()
        try:
            self.figure.set_layout_engine("constrained")
        except Exception:
            pass
        self._vlines = []
        specs = self._specs()
        cols = self._cols
        if not specs:
            self.figure.set_size_inches(SUB_W * cols / DPI, SUB_H / DPI)
            self.canvas.setFixedSize(SUB_W * cols, SUB_H)
            ax = self.figure.add_subplot(111)
            ax.text(0.5, 0.5, "勾选上方曲线以显示 (需先加载轨迹 / OUTCAR)",
                    ha="center", va="center", color="#8A8A8E",
                    transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
            self.canvas.draw_idle()
            return

        rows = int(math.ceil(len(specs) / cols))
        fig_w = SUB_W * cols
        fig_h = SUB_H * rows
        self.figure.set_size_inches(fig_w / DPI, fig_h / DPI)
        self.canvas.setFixedSize(fig_w, fig_h)

        axes = self.figure.subplots(rows, cols, squeeze=False)
        for idx, spec in enumerate(specs):
            r, c = divmod(idx, cols)
            ax = axes[r][c]
            y = np.asarray(spec.y, dtype=float)
            x = np.asarray(spec.x, dtype=float)
            ax.plot(x, y, color=spec.color, linewidth=1.4)
            ax.set_title(spec.label, fontsize=9.5, pad=6)
            ax.set_ylabel(spec.ylabel or spec.label, fontsize=8.5)
            if r == rows - 1:                       # 只在最底行标 x 轴
                ax.set_xlabel("帧", fontsize=8.5)
            ax.grid(True, alpha=0.22, linewidth=0.7)
            ax.tick_params(labelsize=8, length=3)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            finite = y[np.isfinite(y)]
            if len(finite):
                lo, hi = float(finite.min()), float(finite.max())
                if hi - lo < 1e-12:
                    hi = lo + 1.0
                pad = (hi - lo) * 0.10
                ax.set_ylim(lo - pad, hi + pad)
            ax.set_xlim(-0.5, max(1.0, float(len(x)) - 0.5))
            vl = ax.axvline(self._current, color="#FF3B30", linewidth=1.2,
                            linestyle="--", alpha=0.85)
            self._vlines.append(vl)
        # 多余的格子关掉(不占位)
        for idx in range(len(specs), rows * cols):
            r, c = divmod(idx, cols)
            axes[r][c].axis("off")

        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    def export_png(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "导出曲线图", "curves.png",
                                              "PNG 图片 (*.png)")
        if path:
            self.figure.savefig(path, dpi=150, facecolor=self.figure.get_facecolor())
