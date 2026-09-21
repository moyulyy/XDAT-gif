#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
VASP 轨迹可视化 & 收敛分析工作台  (PySide6, iOS / macOS 风格)

功能总览
--------
1. 选择目录, 读取 XDATCAR (轨迹) 与 OUTCAR (能量/力收敛)。
2. 内嵌 3Dmol.js 交互窗口浏览轨迹 (拖动旋转 / 滚轮缩放 / 播放)。
3. 捕获相机视角作为 GIF 关键帧, 支持「固定视角」或「A→B 视角渐变」。
4. 分析曲线 (matplotlib, 与当前帧联动):
      键长 (自选原子对) / RMS 位移 / 力收敛 / 力收敛一阶差分 /
      能量 / 能量一阶差分
5. 生成 GIF, 可设置尺寸 / 帧率 / 颜色数 / 乒乓循环等;
   并可在每帧叠加:
      * 当前帧的力、能量、相对首帧 RMS 数值
      * 上述多个曲线上「对应位置」的点与竖线
6. 一键打包为独立 exe (见 build_exe.bat)。

运行:
    D:\\miniconda3\\envs\\chem_env\\python.exe xdatcar_gif_gui.py
    或双击 run_gui.bat
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import tempfile
import threading
import traceback
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return HERE


BASE_DIR = _base_dir()


# ==========================================================================
# 致命错误处理 (pythonw 下无控制台)
# ==========================================================================
def _log_dirs():
    """错误日志的落盘位置 (按优先级)。

    onefile 打包后 `HERE` 指向临时解包目录 (`_MEIPASS`), 程序退出就被删掉,
    所以冻结后优先写在 exe 旁边; 若目录不可写 (如装在 Program Files) 再退到
    当前目录与 %TEMP%。
    """
    cands = []
    if getattr(sys, "frozen", False):
        try:
            cands.append(Path(sys.executable).resolve().parent)
        except Exception:
            pass
    cands += [HERE, Path.cwd(), Path(tempfile.gettempdir())]
    out, seen = [], set()
    for d in cands:
        try:
            key = str(d)
            if key not in seen:
                seen.add(key)
                out.append(d)
        except Exception:
            continue
    return out


def _log_error(title, message):
    for d in _log_dirs():
        try:
            with open(d / "gui_error.log", "a", encoding="utf-8") as fh:
                fh.write(f"{title}\n\n{message}\n" + "-" * 60 + "\n")
            break
        except Exception:
            continue
    try:
        if sys.stderr is not None:
            print(f"{title}\n{message}", file=sys.stderr)
    except Exception:
        pass


def fatal_error(title, message):
    _log_error(title, message)
    # --selftest (或显式设了 VASP_NO_DIALOG) 时不要弹模态框, 否则自动化测试
    # 会卡在无人点击的对话框上直到超时。
    if "--selftest" in sys.argv or os.environ.get("VASP_NO_DIALOG") == "1":
        try:
            print(f"{title}\n{message}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(1)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(message)
        box.exec()
    except Exception:
        pass
    sys.exit(1)


try:
    import numpy as np
    from PIL import Image
    from PySide6.QtCore import (QEasingCurve, Property, QPropertyAnimation, QEvent,
                                QPointF, QRectF, QSize, Qt, QTimer, Signal)
    from PySide6.QtGui import (QColor, QCursor, QFont, QIcon, QImage, QMovie,
                               QPainter, QPainterPath, QPen, QPixmap)
    from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                                   QColorDialog, QComboBox, QDialog,
                                   QDoubleSpinBox, QFileDialog, QFrame,
                                   QGraphicsDropShadowEffect, QGridLayout,
                                   QHBoxLayout, QLabel, QLineEdit, QListWidget,
                                   QListWidgetItem, QMessageBox, QPlainTextEdit,
                                   QProgressBar, QPushButton, QScrollArea,
                                   QSizePolicy, QSlider, QSpinBox, QStackedWidget,
                                   QStyle, QStyleOption, QVBoxLayout, QWidget)

    import xdatcar_to_gif_3dmol as core
    import vasp_parser as vp
    import gif_overlay as go
    from ui_kit import (DARK, LIGHT, THEME, Card, ComboBox, SegmentedControl,
                        SliderField, Switch, apply_theme, field_label, hint_label,
                        hline)
    from trajectory_viewer import TrajectoryViewer
    from analysis_view import AnalysisView
except Exception as _exc:  # noqa: BLE001
    fatal_error(
        "启动失败",
        f"无法导入依赖模块: {_exc}\n\n"
        f"当前解释器: {sys.executable}\n"
        "请确认已安装 ase / pillow / playwright / PySide6, 且所有 .py 文件在同一目录。",
    )


# 六个标准视角 (根据晶胞矢量计算, 见 xdatcar_to_gif_3dmol.VIEW_NAMES)
VIEW_ITEMS = [("front", "正视"), ("back", "后视"), ("top", "俯视"),
              ("bottom", "仰视"), ("right", "右视"), ("left", "左视")]
VIEW_HINTS = {
    "front":  "a-c 面平行屏幕, a → 屏幕右",
    "back":   "a-c 面平行屏幕, a → 屏幕左",
    "top":    "a-b 面平行屏幕, a → 屏幕右",
    "bottom": "a-b 面平行屏幕, a → 屏幕左",
    "right":  "b-c 面平行屏幕, b → 屏幕右",
    "left":   "b-c 面平行屏幕, b → 屏幕左",
}
STYLE_ITEMS = [("ballstick", "球棍"), ("sphere", "球体"),
               ("stick", "棍状"), ("line", "线框")]
VIEW_MODE_ITEMS = [("preset", "预设视角"), ("capture_a", "捕获视角 A")]
# 绕轴旋转可选轴 (键与 xdatcar_to_gif_3dmol.ROT_AXIS_NAMES 一致)
ROT_AXIS_ITEMS = [("", "不旋转"), ("a", "a 轴"), ("b", "b 轴"),
                  ("c", "c 轴"), ("screen-v", "屏幕竖直"),
                  ("screen-h", "屏幕水平")]
PANEL_POS_ITEMS = [("right", "右侧"), ("bottom", "下方"),
                   ("top", "上方"), ("overlay", "叠加")]
ANGLE_CORNER_ITEMS = [("top-left", "左上"), ("top-right", "右上"),
                      ("bottom-left", "左下"), ("bottom-right", "右下")]


def _build_periodic_layout():
    """symbol -> (row, col), 基于 18 列的完整周期表。"""
    rows = {
        1: [("H", 1), ("He", 18)],
        2: [("Li", 1), ("Be", 2), ("B", 13), ("C", 14), ("N", 15), ("O", 16),
            ("F", 17), ("Ne", 18)],
        3: [("Na", 1), ("Mg", 2), ("Al", 13), ("Si", 14), ("P", 15), ("S", 16),
            ("Cl", 17), ("Ar", 18)],
        4: [("K", 1), ("Ca", 2), ("Sc", 3), ("Ti", 4), ("V", 5), ("Cr", 6),
            ("Mn", 7), ("Fe", 8), ("Co", 9), ("Ni", 10), ("Cu", 11), ("Zn", 12),
            ("Ga", 13), ("Ge", 14), ("As", 15), ("Se", 16), ("Br", 17), ("Kr", 18)],
        5: [("Rb", 1), ("Sr", 2), ("Y", 3), ("Zr", 4), ("Nb", 5), ("Mo", 6),
            ("Tc", 7), ("Ru", 8), ("Rh", 9), ("Pd", 10), ("Ag", 11),
            ("Cd", 12), ("In", 13), ("Sn", 14), ("Sb", 15), ("Te", 16),
            ("I", 17), ("Xe", 18)],
        6: [("Cs", 1), ("Ba", 2), ("Hf", 4), ("Ta", 5), ("W", 6), ("Re", 7),
            ("Os", 8), ("Ir", 9), ("Pt", 10), ("Au", 11), ("Hg", 12),
            ("Tl", 13), ("Pb", 14), ("Bi", 15), ("Po", 16), ("At", 17),
            ("Rn", 18)],
        7: [("Fr", 1), ("Ra", 2), ("Rf", 4), ("Db", 5), ("Sg", 6), ("Bh", 7),
            ("Hs", 8), ("Mt", 9), ("Ds", 10), ("Rg", 11), ("Cn", 12),
            ("Nh", 13), ("Fl", 14), ("Mc", 15), ("Lv", 16), ("Ts", 17),
            ("Og", 18)],
    }
    pos = {}
    for period, items in rows.items():
        for sym, col in items:
            pos[sym] = (period - 1, col - 1)
    lanth = ["La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy",
             "Ho", "Er", "Tm", "Yb", "Lu"]
    actin = ["Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf",
             "Es", "Fm", "Md", "No", "Lr"]
    for i, sym in enumerate(lanth):
        pos[sym] = (8, 2 + i)
    for i, sym in enumerate(actin):
        pos[sym] = (9, 2 + i)
    return pos


PERIODIC_POS = _build_periodic_layout()
PERIODIC_ELEMENTS = list(PERIODIC_POS.keys())


def _text_color_for(hex_color):
    hex_color = (hex_color or "#B8B8B8").lstrip("#")
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return "#1C1C1E"
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "#1C1C1E" if lum > 150 else "#FFFFFF"


# ==========================================================================
# 元素配色对话框
# ==========================================================================
class ElementColorDialog(QDialog):
    """完整周期表: 点击元素可修改对应元素的颜色与 ball 直径。"""

    def __init__(self, colors, radii, base_colors, base_radii,
                 structure_elements=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("元素配色 / 原子半径")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._structure = set(structure_elements or [])
        self._buttons = {}
        self._selected = None
        self._loading = False

        # 内置 VESTA 经典值打底
        self._base_colors = {sym: "#%02x%02x%02x" % tuple(rgb)
                             for sym, rgb in core.VESTA_COLORS.items()}
        self._base_radii = {sym: float(r) for sym, r in core.VESTA_RADII.items()}
        self._base_colors.update(base_colors or {})
        self._base_radii.update(base_radii or {})

        self._colors = {sym: "#B8B8B8" for sym in PERIODIC_ELEMENTS}
        self._colors.update(self._base_colors)
        self._colors.update({k: v for k, v in (colors or {}).items() if v})
        self._radii = {sym: 0.6 for sym in PERIODIC_ELEMENTS}
        self._radii.update(self._base_radii)
        self._radii.update({k: float(v) for k, v in (radii or {}).items() if v})

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        frame = QFrame()
        frame.setObjectName("Window")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        frame.setGraphicsEffect(shadow)
        outer.addWidget(frame)

        root = QVBoxLayout(frame)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)
        title = QLabel("元素配色 / 原子半径")
        title.setObjectName("H1")
        root.addWidget(title)
        sub = QLabel("点击周期表中任一元素, 修改其颜色与 ball 直径; "
                     "蓝色描边 = 当前结构中的元素")
        sub.setObjectName("Hint")
        root.addWidget(sub)

        # ---- 周期表 ----
        grid = QGridLayout()
        grid.setHorizontalSpacing(3)
        grid.setVerticalSpacing(3)
        for sym, (r, c) in PERIODIC_POS.items():
            btn = QPushButton(sym)
            btn.setFixedSize(36, 30)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, s=sym: self._select(s))
            self._buttons[sym] = btn
            grid.addWidget(btn, r, c)
        root.addLayout(grid)

        root.addWidget(hline())

        # ---- 选中元素详情 ----
        detail = QHBoxLayout()
        detail.setSpacing(10)
        self.lbl_sel = QLabel("点击上方元素")
        self.lbl_sel.setObjectName("FieldLabel")
        self.lbl_sel.setMinimumWidth(96)
        detail.addWidget(self.lbl_sel)
        self.btn_swatch = QPushButton()
        self.btn_swatch.setFixedSize(56, 26)
        self.btn_swatch.setCursor(Qt.PointingHandCursor)
        self.btn_swatch.setToolTip("点击修改颜色")
        self.btn_swatch.clicked.connect(self._pick_color)
        detail.addWidget(QLabel("颜色"))
        detail.addWidget(self.btn_swatch)
        detail.addSpacing(12)
        detail.addWidget(QLabel("球直径"))
        self.sp_diam = QDoubleSpinBox()
        self.sp_diam.setRange(0.10, 8.00)
        self.sp_diam.setSingleStep(0.1)
        self.sp_diam.setDecimals(2)
        self.sp_diam.setSuffix(" Å")
        self.sp_diam.setFixedWidth(96)
        self.sp_diam.valueChanged.connect(self._on_diam)
        detail.addWidget(self.sp_diam)
        self.btn_reset_el = QPushButton("该元素默认")
        self.btn_reset_el.setObjectName("Ghost")
        self.btn_reset_el.setCursor(Qt.PointingHandCursor)
        self.btn_reset_el.clicked.connect(self._reset_el)
        detail.addWidget(self.btn_reset_el)
        detail.addStretch(1)
        root.addLayout(detail)

        # ---- 底部按钮 ----
        row = QHBoxLayout()
        row.addStretch(1)
        btn_reset = QPushButton("恢复全部默认")
        btn_reset.setObjectName("Ghost")
        btn_reset.setCursor(Qt.PointingHandCursor)
        btn_reset.clicked.connect(self._reset_all)
        row.addWidget(btn_reset)
        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("Secondary")
        btn_cancel.setCursor(Qt.PointingHandCursor)
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        btn_ok = QPushButton("应用")
        btn_ok.setObjectName("Primary")
        btn_ok.setCursor(Qt.PointingHandCursor)
        btn_ok.clicked.connect(self.accept)
        row.addWidget(btn_ok)
        root.addLayout(row)

        self._refresh_cells()
        self._select(None)

        frame.mousePressEvent = self._press

    # ------------------------------------------------------------------
    def _cell_style(self, sym):
        col = self._colors.get(sym, "#B8B8B8")
        if sym == self._selected:
            border = "2px solid #FF9500"
        elif sym in self._structure:
            border = "2px solid #007AFF"
        else:
            border = "1px solid rgba(0,0,0,0.25)"
        fg = _text_color_for(col)
        return (f"QPushButton{{background:{col}; border:{border};"
                f"border-radius:6px; font-size:11px; font-weight:600;"
                f"color:{fg};}}")

    def _refresh_cells(self):
        for sym, btn in self._buttons.items():
            btn.setStyleSheet(self._cell_style(sym))

    def _select(self, sym):
        self._selected = sym
        self._loading = True
        enabled = sym is not None
        self.btn_swatch.setEnabled(enabled)
        self.sp_diam.setEnabled(enabled)
        self.btn_reset_el.setEnabled(enabled)
        if not enabled:
            self.lbl_sel.setText("点击上方元素")
            self.btn_swatch.setStyleSheet("background:#DDDDDD;"
                                          "border:1px solid rgba(0,0,0,0.25);"
                                          "border-radius:6px;")
        else:
            self.lbl_sel.setText(f"元素  {sym}")
            self.sp_diam.setValue(self._radii.get(sym, 0.6) * 2.0)
            self._paint_swatch()
        self._loading = False
        self._refresh_cells()

    def _paint_swatch(self):
        col = self._colors.get(self._selected, "#B8B8B8")
        self.btn_swatch.setStyleSheet(
            f"background:{col}; border:1px solid rgba(0,0,0,0.35);"
            f"border-radius:6px;")

    def _on_diam(self, val):
        if self._loading or self._selected is None:
            return
        self._radii[self._selected] = float(val) / 2.0

    def _pick_color(self):
        if self._selected is None:
            return
        cur = QColor(self._colors.get(self._selected, "#B8B8B8"))
        col = QColorDialog.getColor(cur, self, f"选择 {self._selected} 的颜色")
        if col.isValid():
            self._colors[self._selected] = col.name()
            self._paint_swatch()
            self._refresh_cells()

    def _reset_el(self):
        if self._selected is None:
            return
        sym = self._selected
        self._colors[sym] = self._base_colors.get(sym, "#B8B8B8")
        self._radii[sym] = self._base_radii.get(sym, 0.6)
        self._loading = True
        self.sp_diam.setValue(self._radii[sym] * 2.0)
        self._loading = False
        self._paint_swatch()
        self._refresh_cells()

    def _reset_all(self):
        for sym in PERIODIC_ELEMENTS:
            self._colors[sym] = self._base_colors.get(sym, "#B8B8B8")
            self._radii[sym] = self._base_radii.get(sym, 0.6)
        if self._selected is not None:
            self._loading = True
            self.sp_diam.setValue(self._radii[self._selected] * 2.0)
            self._loading = False
            self._paint_swatch()
        self._refresh_cells()

    def result_colors(self):
        return dict(self._colors)

    def result_radii(self):
        return dict(self._radii)

    def _press(self, event):
        if event.button() == Qt.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()


# ==========================================================================
# 进度弹窗 (生成 GIF 时显示)
# ==========================================================================
class ProgressDialog(QDialog):
    """无边框进度弹窗: 进度条 + 状态 + 取消。非模态, 不阻塞后台渲染。"""

    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("生成 GIF")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(False)
        self.setFixedWidth(380)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        frame = QFrame()
        frame.setObjectName("Window")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 100))
        frame.setGraphicsEffect(shadow)
        outer.addWidget(frame)

        root = QVBoxLayout(frame)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        self.lbl_title = QLabel("正在生成 GIF")
        self.lbl_title.setObjectName("H1")
        root.addWidget(self.lbl_title)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(True)
        self.bar.setFixedHeight(16)
        root.addWidget(self.bar)
        self.lbl_status = QLabel("准备中…")
        self.lbl_status.setObjectName("Hint")
        root.addWidget(self.lbl_status)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("Secondary")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.clicked.connect(self._on_cancel)
        row.addWidget(self.btn_cancel)
        root.addLayout(row)

    def begin(self, title="正在生成 GIF"):
        self.lbl_title.setText(title)
        self.bar.setValue(0)
        self.lbl_status.setText("准备中…")
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("取消")
        self.show()
        self.raise_()

    def set_progress(self, i, n):
        self.bar.setValue(int(100 * i / max(n, 1)))
        self.lbl_status.setText(f"正在渲染截图 {i}/{n}")

    def set_status(self, text):
        self.lbl_status.setText(text)

    def finish(self):
        self.bar.setValue(100)
        self.hide()

    def _on_cancel(self):
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText("正在停止…")
        self.lbl_status.setText("正在停止, 请稍候…")
        self.cancelled.emit()


# ==========================================================================
# 主窗口
# ==========================================================================
class GifCanvas(QWidget):
    """GIF 预览画布: 像看图工具一样看 GIF。

    · **自动适配**: 窗口/页面多大就显示多大, 能 1:1 就 1:1 (绝不放大),
      放不下才做**一次**平滑缩小 —— 保证任何情况下都不会二次重采样。
    · **滚轮缩放**(以光标为锚点) / **拖动平移** / **双击复位**, 所以不需要
      “适配窗口”这类按钮。
    · 绘制按**设备像素**对齐 (偏移取整), 否则 Qt 会因为小数偏移再插值一次。

    为什么不用 QLabel+setPixmap: QLabel 的 sizeHint 会跟着 pixmap 变, 而
    pixmap 又按 label 尺寸算 -> **布局反馈循环**, 大图会把 label 撑得比视口更宽,
    于是出现横向滚动条、图像被裁切(“遮盖”)。这里的尺寸提示是固定值,
    控件尺寸只由布局决定, 图像不会反过来撑开控件。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Preview")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.OpenHandCursor)
        self.movie = None
        self.placeholder = ("还没有生成 GIF\n\n先在「GIF 设置」页点「生成 GIF」,"
                            "完成后会自动跳到这里")
        self.user_zoom = None       # None = 自动适配; 数值 = 用户滚轮缩放的倍率
        self.pan = QPointF(0.0, 0.0)
        self._drag = None

    viewChanged = Signal()          # 缩放/平移/复位后发出, 让外部刷新倍率提示

    # 固定尺寸提示, 杜绝布局反馈
    def sizeHint(self):  # noqa: N802
        return QSize(520, 360)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(200, 160)

    def set_movie(self, movie):
        self.movie = movie
        self.user_zoom = None
        self.pan = QPointF(0.0, 0.0)
        self.update()
        self.viewChanged.emit()

    def _frame(self):
        try:
            movie = self.movie
            return movie.currentPixmap() if movie is not None else None
        except RuntimeError:            # C++ 对象已释放 (换图时的竞态)
            return None

    def fit_scale(self):
        """自动适配倍率 (1.0 = 1:1, 绝不放大)。"""
        pm = self._frame()
        if pm is None or pm.isNull() or pm.width() <= 0:
            return 1.0
        dpr = float(self.devicePixelRatioF() or 1.0)
        return min(self.width() * dpr / pm.width(),
                   self.height() * dpr / pm.height(), 1.0)

    def effective_scale(self):
        """当前实际显示倍率。"""
        return self.fit_scale() if self.user_zoom is None else float(self.user_zoom)

    def is_native(self):
        return self.effective_scale() >= 0.999

    def reset_view(self):
        self.user_zoom = None
        self.pan = QPointF(0.0, 0.0)
        self.update()
        self.viewChanged.emit()

    def refresh(self):
        self.update()

    # ------------------------- 交互: 滚轮缩放 / 拖动 / 双击复位 -------------------------
    def wheelEvent(self, event):  # noqa: N802
        pm = self._frame()
        if pm is None or pm.isNull():
            return
        delta = event.angleDelta().y()
        if not delta:
            return
        dpr = float(self.devicePixelRatioF() or 1.0)
        k0 = self.effective_scale()
        k1 = max(0.05, min(8.0, k0 * (1.1 ** (delta / 120.0))))
        # 以光标为锚点缩放: 让光标下的那一点保持不动
        ux = event.position().x() * dpr - self.width() * dpr / 2.0
        uy = event.position().y() * dpr - self.height() * dpr / 2.0
        px = self.pan.x() * dpr
        py = self.pan.y() * dpr
        ratio = k1 / k0 if k0 else 1.0
        self.pan = QPointF((ux - (ux - px) * ratio) / dpr,
                           (uy - (uy - py) * ratio) / dpr)
        self.user_zoom = k1
        self.update()
        self.viewChanged.emit()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self._frame() is not None:
            self._drag = (event.position(), QPointF(self.pan))
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag is None:
            return
        start, base = self._drag
        self.pan = base + (event.position() - start)
        self.update()
        self.viewChanged.emit()

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag = None
        self.setCursor(Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.reset_view()

    def paintEvent(self, event):  # noqa: N802
        # 先让 QSS 的背景/边框画出来
        opt = QStyleOption()
        opt.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, painter, self)
        pm = self._frame()
        if pm is None or pm.isNull() or pm.width() <= 0:
            painter.setPen(QColor(THEME["text_dim"]))
            painter.drawText(self.rect(), Qt.AlignCenter, self.placeholder)
            return
        dpr = float(self.devicePixelRatioF() or 1.0)
        k = self.effective_scale()
        tw = max(1, int(round(pm.width() * k)))
        th = max(1, int(round(pm.height() * k)))
        if (tw, th) != (pm.width(), pm.height()):
            # 缩小用平滑; 放大用最近邻(看像素细节时更清楚)
            mode = Qt.SmoothTransformation if k < 1.0 else Qt.FastTransformation
            pm = pm.scaled(tw, th, Qt.KeepAspectRatio, mode)
        # 偏移取整到设备像素, 保证 1:1 贴图不被再次重采样
        x_dev = round(self.width() * dpr / 2.0 + self.pan.x() * dpr - pm.width() / 2.0)
        y_dev = round(self.height() * dpr / 2.0 + self.pan.y() * dpr - pm.height() / 2.0)
        # **关键**: QPainter::drawPixmap 会自动按 pixmap 的 devicePixelRatio 换算。
        # pm 的 dpr 是 1.0, 而本控件设备 dpr 是 1.25, 于是 Qt 又把它放大 1.25 倍
        # (实测 600×400 的图被画成 750×500 设备像素) -> 插值 -> 发模。
        # 把 dpr 标上去, Qt 的自动换算正好抵消, 才是真正的 1:1。
        pm.setDevicePixelRatio(dpr)
        painter.drawPixmap(QPointF(x_dev / dpr, y_dev / dpr), pm)


class MainWindow(QWidget):
    POLL_MS = 100
    RESIZE_MARGIN = 6

    def __init__(self):
        super().__init__()
        self.setWindowTitle("VASP 轨迹可视化与收敛分析")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setMinimumSize(1120, 680)
        self._filled = False         # 默认以小窗口启动 (可手动最大化 / F11 全屏)
        self._first_show = False
        # 默认启动窗口尺寸 (小窗口, 不铺满桌面)
        self._default_size = QSize(1280, 800)
        self._set_icon()

        self.q = queue.Queue()
        self.worker = None
        self.cancel_evt = threading.Event()
        self.last_output = None
        self._dark = False
        self._switches = []

        # 数据
        self.trajectory = None            # ASE Atoms 列表 (完整)
        self.frame_indices = []           # 当前选择用于显示的帧索引
        self.analysis = None              # AnalysisResult
        self.elem_map = {}
        self.captured = {}                # 'A'/'B' -> (view, css_w)
        self.xdatcar_path = ""
        self.outcar_path = ""
        self._vesta_colors = {}
        self._vesta_radii = {}
        self.color_overrides = {}         # 元素 -> '#rrggbb' (用户自定义)
        self.radius_overrides = {}        # 元素 -> 半径 Å (用户自定义)
        self._preview_win = None
        self._loading = False

        self._build()
        self._restore_defaults()

        self.progress_dlg = ProgressDialog(self)
        self.progress_dlg.cancelled.connect(self._stop)

        self.timer = QTimer(self)
        self.timer.setInterval(self.POLL_MS)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    # ------------------------------------------------------------------
    def _set_icon(self):
        ico = BASE_DIR / "assets" / "app.ico"
        if ico.is_file():
            self.setWindowIcon(QIcon(str(ico)))

    # ==================================================================
    # 界面搭建
    # ==================================================================
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        self.outer = outer

        self.container = QFrame()
        self.container.setObjectName("Window")
        # 注意: 不能用 QGraphicsDropShadowEffect, 否则内嵌 WebEngine 无法渲染;
        #       阴影由本窗口 paintEvent 手动画出。
        outer.addWidget(self.container)

        root = QVBoxLayout(self.container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_titlebar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self._build_actionbar())

        self.pages = QStackedWidget()
        self.pages.addWidget(self._page_trajectory())
        self.pages.addWidget(self._page_analysis())
        self.pages.addWidget(self._page_output())
        self.pages.addWidget(self._page_preview())
        self.pages.addWidget(self._page_log())
        right.addWidget(self.pages, 1)
        right.addWidget(self._build_footer())

        wrap = QWidget()
        wrap.setLayout(right)
        body.addWidget(wrap, 1)
        root.addLayout(body, 1)

    # ---- 手绘柔和阴影 (替代 QGraphicsDropShadowEffect) ----
    def paintEvent(self, event):  # noqa: N802
        if self._is_framed_fullscreen():
            # 铺满桌面 / 最大化 / 全屏时不需要阴影与圆角留白
            super().paintEvent(event)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = 18
        rect = QRectF(m - 4, m - 2, self.width() - 2 * m + 8, self.height() - 2 * m + 8)
        base = QColor(0, 0, 0)
        layers = 16
        for i in range(layers, 0, -1):
            alpha = int(2.6 * (layers - i) / layers * (2.2 if self._dark else 1.6))
            grow = i * 0.9
            p.setBrush(QColor(base.red(), base.green(), base.blue(), max(0, alpha)))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(rect.adjusted(-grow, -grow + 2, grow, grow + 2),
                              16 + grow / 2, 16 + grow / 2)
        super().paintEvent(event)

    # ---------------------------- 标题栏 ----------------------------
    def _build_titlebar(self):
        bar = QWidget()
        bar.setObjectName("TitleBar")
        bar.setFixedHeight(48)
        bar.mousePressEvent = self._titlebar_press
        bar.mouseDoubleClickEvent = self._titlebar_double
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(8)

        for name, slot in (("TrafficClose", self.close),
                           ("TrafficMin", self.showMinimized),
                           ("TrafficMax", self._toggle_max)):
            btn = QPushButton()
            btn.setObjectName(name)
            btn.setFixedSize(12, 12)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(slot)
            lay.addWidget(btn)

        lay.addStretch(1)
        title = QLabel("VASP  轨迹可视化  ·  收敛分析")
        title.setObjectName("AppName")
        lay.addWidget(title)
        lay.addStretch(1)

        self.theme_btn = QPushButton("🌙")
        self.theme_btn.setObjectName("Ghost")
        self.theme_btn.setFixedWidth(38)
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.setToolTip("切换深色 / 浅色主题")
        self.theme_btn.clicked.connect(self._toggle_theme)
        lay.addWidget(self.theme_btn)
        return bar

    def _titlebar_press(self, event):
        if event.button() == Qt.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()
            event.accept()

    def _titlebar_double(self, event):
        if event.button() == Qt.LeftButton:
            self._toggle_max()

    def _toggle_max(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    # ---- 窗口尺寸自适应 + 最大化/全屏处理 ----
    def _is_framed_fullscreen(self) -> bool:
        return bool(self.isMaximized() or self.isFullScreen()
                    or getattr(self, "_filled", False))

    def _apply_window_frame(self):
        """最大化 / 铺满桌面时去掉阴影留白与圆角, 真正适应整屏。"""
        maxed = self._is_framed_fullscreen()
        m = 0 if maxed else 18
        self.outer.setContentsMargins(m, m, m, m)
        val = "true" if maxed else "false"
        if self.container.property("Maxed") != val:
            self.container.setProperty("Maxed", val)
            self.container.style().unpolish(self.container)
            self.container.style().polish(self.container)
        self.update()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self._first_show:
            self._first_show = True
            self._filled = False
            scr = QApplication.primaryScreen()
            if scr is not None:
                avail = scr.availableGeometry()
                # 小窗口启动: 取默认尺寸, 但不超过桌面可用区域(留边距),
                # 同时不小于窗口最小尺寸。
                w = max(self.minimumWidth(),
                        min(self._default_size.width(), avail.width() - 120))
                h = max(self.minimumHeight(),
                        min(self._default_size.height(), avail.height() - 120))
                self.resize(w, h)
                self.move(avail.x() + (avail.width() - w) // 2,
                          avail.y() + (avail.height() - h) // 2)
        self._apply_window_frame()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "preview_movie", None) is not None:
            self._fit_preview()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            if self.windowState() & Qt.WindowMaximized:
                self._filled = False
            self._apply_window_frame()

    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        if key == Qt.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            event.accept()
            return
        if key == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            event.accept()
            return
        super().keyPressEvent(event)

    def _toggle_theme(self):
        self._dark = not self._dark
        apply_theme(QApplication.instance(), DARK if self._dark else LIGHT)
        self.theme_btn.setText("☀️" if self._dark else "🌙")
        for sw in self._switches:
            sw.apply_theme(THEME)
        self._apply_window_frame()
        try:
            self.analysis_view.replot()
        except Exception:
            pass

    # ---------------------------- 侧边栏 ----------------------------
    def _build_sidebar(self):
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(216)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(16, 18, 16, 16)
        lay.setSpacing(4)

        brand = QLabel("轨迹可视化")
        brand.setObjectName("SidebarBrand")
        lay.addWidget(brand)
        sub = QLabel("XDATCAR  ·  OUTCAR")
        sub.setObjectName("SidebarSub")
        lay.addWidget(sub)
        lay.addSpacing(16)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        items = [("轨迹浏览", 0), ("分析曲线", 1), ("GIF 设置", 2),
                 ("GIF 预览", 3), ("日志", 4)]
        for text, idx in items:
            btn = QPushButton(text)
            btn.setObjectName("NavItem")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, i=idx: self.pages.setCurrentIndex(i))
            self.nav_group.addButton(btn)
            lay.addWidget(btn)
            if idx == 0:
                btn.setChecked(True)

        lay.addStretch(1)
        self.lbl_dataload = QLabel("尚未加载轨迹")
        self.lbl_dataload.setObjectName("SidebarSub")
        self.lbl_dataload.setWordWrap(True)
        lay.addWidget(self.lbl_dataload)
        return side

    # ---------------------------- 动作栏 ----------------------------
    def _build_actionbar(self):
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(24, 16, 24, 8)
        lay.setSpacing(10)

        self.page_title = QLabel("轨迹浏览")
        self.page_title.setObjectName("H1")
        lay.addWidget(self.page_title)
        lay.addStretch(1)

        self.btn_run = QPushButton("生成 GIF")
        self.btn_run.setObjectName("Primary")
        self.btn_run.setCursor(Qt.PointingHandCursor)
        self.btn_run.clicked.connect(lambda: self._start(preview=False))
        lay.addWidget(self.btn_run)
        return bar

    # ---------------------------- 页脚 ----------------------------
    def _build_footer(self):
        foot = QWidget()
        lay = QVBoxLayout(foot)
        lay.setContentsMargins(24, 6, 24, 16)
        lay.setSpacing(6)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        lay.addWidget(self.progress)
        self.status = QLabel("就绪 · 请选择 XDATCAR 与 OUTCAR")
        self.status.setObjectName("Footer")
        lay.addWidget(self.status)
        return foot

    # ---------------------------- 通用工具 ----------------------------
    def _make_scroll(self, margins=(24, 4, 24, 16), spacing=16):
        """返回 (滚动区, 内部纵向布局), 供各页拼装左/中/右栏。"""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        area.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setObjectName("PageInner")
        inner.setAttribute(Qt.WA_StyledBackground, True)
        area.setWidget(inner)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(*margins)
        lay.setSpacing(spacing)
        return area, lay

    def _side_panel(self, width=344):
        """右侧「参数/检查器」栏: 定宽 + 独立滚动。"""
        area, lay = self._make_scroll(margins=(0, 0, 8, 0), spacing=12)
        area.setFixedWidth(width)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setProperty("SidePanel", True)
        return area, lay

    def _two_col_page(self, build_main, build_side, margins=(20, 2, 20, 12),
                      spacing=14, side_width=344, main_tail_stretch=True):
        """通用两栏页: 左栏=主视图, 右栏=参数。加上最左侧导航即左/中/右。"""
        page = QWidget()
        h = QHBoxLayout(page)
        h.setContentsMargins(*margins)
        h.setSpacing(spacing)
        main, mlay = self._make_scroll(margins=(0, 0, 10, 0), spacing=12)
        build_main(mlay)
        if main_tail_stretch:
            mlay.addStretch(1)
        h.addWidget(main, 1)
        side, slay = self._side_panel(side_width)
        build_side(slay)
        slay.addStretch(1)
        h.addWidget(side, 0)
        return page

    def _scroll_page(self, builder, stretch=True):
        """旧版单栏页 (保留兼容)。"""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        area.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setObjectName("PageInner")
        inner.setAttribute(Qt.WA_StyledBackground, True)
        area.setWidget(inner)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(24, 4, 24, 16)
        lay.setSpacing(16)
        builder(lay)
        if stretch:
            lay.addStretch(1)
        return area

    def _file_row(self, label, var, browse_text="浏览", save=False, types=None,
                  folder=False):
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(field_label(label))
        edit = QLineEdit(var)
        row.addWidget(edit, 1)
        btn = QPushButton(browse_text)
        btn.setObjectName("Browse")
        btn.setCursor(Qt.PointingHandCursor)

        def do_pick():
            start = edit.text() or str(HERE)
            if folder:
                path = QFileDialog.getExistingDirectory(self, "选择目录", start)
            elif save:
                path, _ = QFileDialog.getSaveFileName(
                    self, "选择输出文件", start,
                    "GIF 动画 (*.gif);;PNG 图片 (*.png)")
            else:
                path, _ = QFileDialog.getOpenFileName(
                    self, "选择文件", start, types or "所有文件 (*)")
            if path:
                edit.setText(path)

        btn.clicked.connect(do_pick)
        row.addWidget(btn)
        return edit, row

    def _switch_row(self, text, value, hint=None):
        row = QHBoxLayout()
        row.setSpacing(10)
        lab = QLabel(text)
        lab.setObjectName("FieldLabel")
        row.addWidget(lab)
        row.addStretch(1)
        sw = Switch()
        sw.setChecked(value, animate=False)
        sw.apply_theme(THEME)
        self._switches.append(sw)
        row.addWidget(sw)
        box = QVBoxLayout()
        box.setSpacing(2)
        box.addLayout(row)
        if hint:
            box.addWidget(hint_label(hint))
        return sw, box

    def _entry_row(self, parent_card, label, value, width=120, hint=None,
                   label_width=96):
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(field_label(label, label_width))
        edit = QLineEdit(str(value))
        edit.setFixedWidth(width)
        row.addWidget(edit)
        row.addStretch(1)
        parent_card.add_layout(row)
        if hint:
            parent_card.add(hint_label(hint))
        return edit

    # ==================================================================
    # 页面 1: 轨迹浏览
    # ==================================================================
    def _page_trajectory(self):
        # 注意: 本页不能用 QScrollArea 包裹——否则鼠标滚轮会被滚动区域抢走,
        #       3D 窗口无法缩放, 而且 viewer 也撑不满高度。
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 2, 20, 12)
        outer.setSpacing(10)

        # ---- 顶部按钮行 (只有按钮, 无输入框) ----
        card = Card()
        card.vbox.setContentsMargins(16, 10, 16, 10)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.btn_pick_xdat = QPushButton("选择 XDATCAR")
        self.btn_pick_xdat.setObjectName("Secondary")
        self.btn_pick_xdat.setCursor(Qt.PointingHandCursor)
        self.btn_pick_xdat.clicked.connect(self._pick_xdatcar)
        row.addWidget(self.btn_pick_xdat)

        self.btn_pick_out = QPushButton("选择 OUTCAR")
        self.btn_pick_out.setObjectName("Secondary")
        self.btn_pick_out.setCursor(Qt.PointingHandCursor)
        self.btn_pick_out.clicked.connect(self._pick_outcar)
        row.addWidget(self.btn_pick_out)

        self.btn_colors = QPushButton("元素配色…")
        self.btn_colors.setObjectName("Secondary")
        self.btn_colors.setCursor(Qt.PointingHandCursor)
        self.btn_colors.setToolTip("完整周期表: 逐元素修改颜色与 ball 直径")
        self.btn_colors.clicked.connect(self._open_color_dialog)
        row.addWidget(self.btn_colors)

        self.lbl_files = QLabel("尚未选择文件")
        self.lbl_files.setObjectName("Hint")
        self.lbl_files.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_files.setMinimumWidth(0)
        row.addWidget(self.lbl_files, 1)

        self.btn_load2 = QPushButton("开始加载 / 分析")
        self.btn_load2.setObjectName("Primary")
        self.btn_load2.setCursor(Qt.PointingHandCursor)
        self.btn_load2.clicked.connect(self._load_clicked)
        row.addWidget(self.btn_load2)
        card.add_layout(row)
        outer.addWidget(card)

        body = QHBoxLayout()
        body.setSpacing(14)

        # ---- 中栏: 3D 浏览 (占满剩余高度, 大尺寸) ----
        holder = QFrame()
        holder.setObjectName("Card")
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(10, 10, 10, 10)
        hv.setSpacing(8)
        self.viewer = TrajectoryViewer()
        self.viewer.frameChanged.connect(self._on_viewer_frame)
        self.viewer.cameraCaptured.connect(self._on_camera_captured)
        hv.addWidget(self.viewer, 1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(QLabel("捕获关键帧:"))
        self.btn_capA = QPushButton("设为视角 A")
        self.btn_capA.setObjectName("Secondary")
        self.btn_capA.setCursor(Qt.PointingHandCursor)
        self.btn_capA.setToolTip("在 3D 窗口中旋转到满意角度后, 把当前方向捕获为视角 A")
        self.btn_capA.clicked.connect(lambda: self.viewer.capture_camera("A"))
        row2.addWidget(self.btn_capA)
        self.lbl_cap = QLabel("未捕获视角")
        self.lbl_cap.setObjectName("Chip")
        row2.addWidget(self.lbl_cap)
        row2.addStretch(1)
        hv.addLayout(row2)
        body.addWidget(holder, 1)

        # ---- 右栏: 视角与显示 (常驻, 不再折叠) ----
        side, sv = self._side_panel()
        sv.addWidget(self._view_settings_card())
        sv.addStretch(1)
        body.addWidget(side, 0)

        outer.addLayout(body, 1)
        return page

    def _on_viewmode_changed(self, mode):
        """预设视角 / 捕获视角 A: 切换时亮/灰预设选择器。"""
        preset_mode = (mode == "preset")
        self.lbl_view_base.setEnabled(preset_mode)
        self.seg_view.setEnabled(preset_mode)
        if preset_mode:
            self.lbl_view_hint.setText(VIEW_HINTS.get(self.seg_view.value(), ""))
        else:
            self.lbl_view_hint.setText("基准方向 = 「轨迹浏览」页捕获的视角 A")
        self._on_rotaxis_changed()

    def _on_rotaxis_changed(self, *_):
        """选了轴才允许填角度。"""
        on = bool(self.cb_rotaxis.currentData())
        self.e_rotang.setEnabled(on)
        self.e_rotang.setReadOnly(not on)

    def _apply_pan(self, *_):
        """平移滑杆 -> 立即应用到 3D 窗口。"""
        if not getattr(self, "_viewer_ready", False):
            return
        try:
            self.viewer.apply_pan(self.sl_panx.value(), self.sl_pany.value())
        except Exception as exc:  # noqa: BLE001
            self._log(f"[错误] 平移失败: {exc}")

    def _apply_preset_view(self, key):
        """预设视角: 根据晶胞矢量算出四元数, 立即应用到 3D 窗口。"""
        if hasattr(self, "lbl_view_hint"):
            self.lbl_view_hint.setText(VIEW_HINTS.get(key, ""))
        if not self.trajectory:
            return
        try:
            quat = core.view_quaternion(self.trajectory[0].get_cell(), key)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[错误] 计算预设视角失败: {exc}")
            return
        self.viewer.apply_orientation(quat)

    # ==================================================================
    # 页面 2: 分析曲线
    # ==================================================================
    def _page_analysis(self):
        # ---- 中栏: 曲线图 (占满) ----
        def build_main(mlay):
            card2 = Card("分析曲线")
            self.analysis_view = AnalysisView()
            card2.add(self.analysis_view)
            mlay.addWidget(card2, 1)

        # ---- 右栏: 原子对选择 ----
        def build_side(slay):
            card = Card("原子对 (键长曲线)")
            card.vbox.setContentsMargins(14, 8, 14, 8)
            grid = QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(8)
            grid.addWidget(field_label("原子 A", 50), 0, 0)
            self.cb_atom_a = ComboBox()
            self.cb_atom_a.setMinimumWidth(110)
            grid.addWidget(self.cb_atom_a, 0, 1)
            grid.addWidget(field_label("原子 B", 50), 1, 0)
            self.cb_atom_b = ComboBox()
            self.cb_atom_b.setMinimumWidth(110)
            grid.addWidget(self.cb_atom_b, 1, 1)
            btn_add = QPushButton("添加原子对")
            btn_add.setObjectName("Secondary")
            btn_add.setCursor(Qt.PointingHandCursor)
            btn_add.clicked.connect(self._add_pair)
            grid.addWidget(btn_add, 2, 0, 1, 2)
            btn_auto = QPushButton("自动检测键")
            btn_auto.setObjectName("Ghost")
            btn_auto.setCursor(Qt.PointingHandCursor)
            btn_auto.clicked.connect(self._auto_pairs)
            grid.addWidget(btn_auto, 3, 0)
            btn_del = QPushButton("移除选中")
            btn_del.setObjectName("Ghost")
            btn_del.setCursor(Qt.PointingHandCursor)
            btn_del.clicked.connect(self._remove_pair)
            grid.addWidget(btn_del, 3, 1)
            grid.setColumnStretch(1, 1)
            card.add_layout(grid)

            self.pair_list = QListWidget()
            self.pair_list.setMinimumHeight(120)
            card.add(self.pair_list)
            slay.addWidget(card)
            slay.addWidget(hint_label(
                "键长曲线会出现在左侧图表中；\n"
                "在左侧勾选需要导出到 GIF 的曲线。"))

        return self._two_col_page(build_main, build_side,
                                  main_tail_stretch=False)

    # ==================================================================
    # 页面 3: 视角与显示
    # ==================================================================
    def _stack(self, card, label, widget, hint=None):
        """窄栏表单: 标签在上、控件在下 (占满整栏宽度)。"""
        card.add(field_label(label, 0))
        card.add(widget)
        if hint:
            card.add(hint_label(hint))
        return widget

    def _view_settings_card(self):
        """右栏「视角与显示」参数卡 (窄栏形式: 标签在上、控件在下)。"""
        card = Card("视角与显示")

        self.seg_viewmode = SegmentedControl(VIEW_MODE_ITEMS, "preset")
        self.seg_viewmode.changed.connect(self._on_viewmode_changed)
        self._stack(card, "视角模式", self.seg_viewmode)

        self.lbl_view_base = field_label("预设视角", 0)
        card.add(self.lbl_view_base)
        self.seg_view = SegmentedControl(VIEW_ITEMS, "front")
        self.seg_view.changed.connect(self._apply_preset_view)
        card.add(self.seg_view)
        self.lbl_view_hint = hint_label(VIEW_HINTS["front"])
        card.add(self.lbl_view_hint)

        self.sl_zoom = SliderField("缩放", 0.3, 3.0, 1.0, 270, "{:.2f}", 52)
        card.add(self.sl_zoom)

        self.sl_panx = SliderField("水平平移", -0.5, 0.5, 0.0, 200, "{:+.2f}", 52)
        self.sl_panx.slider.valueChanged.connect(self._apply_pan)
        card.add(self.sl_panx)
        self.sl_pany = SliderField("垂直平移", -0.5, 0.5, 0.0, 200, "{:+.2f}", 52)
        self.sl_pany.slider.valueChanged.connect(self._apply_pan)
        card.add(self.sl_pany)
        card.add(hint_label("平移按画布比例: 正值 = 结构向右 / 向上; 0.5 = 半个画布"))
        self.cb_rotaxis = ComboBox()
        for key, label in ROT_AXIS_ITEMS:
            self.cb_rotaxis.addItem(label, key)
        self.cb_rotaxis.currentIndexChanged.connect(self._on_rotaxis_changed)
        self._stack(card, "旋转轴", self.cb_rotaxis)

        self.e_rotang = self._entry_row(card, "旋转角度", "360", 90,
                                        "整段轨迹绕上面轴转过的总角度 (度): "
                                        "360 = 转一圈, 180 = 半个", label_width=52)
        self._on_rotaxis_changed()

        card.add(hline())

        self.seg_style = SegmentedControl(STYLE_ITEMS, "ballstick")
        self._stack(card, "原子风格", self.seg_style)

        self.sl_radius = SliderField("半径缩放", 0.15, 1.2, 0.45, 105, "{:.2f}", 52)
        card.add(self.sl_radius)
        self.e_radius = self._entry_row(
            card, "统一半径", "", 110,
            "填数字则所有元素同一半径, 留空用 VESTA 半径", label_width=52)
        self.sw_cell, sw_box = self._switch_row("显示晶胞框", True)
        card.add_layout(sw_box)

        self.cb_bg = ComboBox()
        self.cb_bg.addItems(["white", "black", "#F2F2F7", "#1B1B1D"])
        self.cb_bg.setCurrentText("white")
        self._stack(card, "背景色", self.cb_bg)

        row4 = QHBoxLayout()
        row4.setSpacing(8)
        btn_colors = QPushButton("元素配色…")
        btn_colors.setObjectName("Secondary")
        btn_colors.setCursor(Qt.PointingHandCursor)
        btn_colors.clicked.connect(self._open_color_dialog)
        row4.addWidget(btn_colors, 1)
        btn_apply = QPushButton("应用到 3D")
        btn_apply.setObjectName("Secondary")
        btn_apply.setCursor(Qt.PointingHandCursor)
        btn_apply.clicked.connect(self._reload_viewer)
        row4.addWidget(btn_apply, 1)
        card.add_layout(row4)
        return card

    # ==================================================================
    # 页面 4: GIF 设置
    # ==================================================================
    def _page_output(self):
        def build_main(lay):
            card = Card("输出")
            self.e_out, r1 = self._file_row(
                "输出文件", str(HERE / "trajectory.gif"), "另存为", save=True)
            card.add_layout(r1)
            row = QHBoxLayout()
            row.addStretch(1)
            btn_prev = QPushButton("预览单帧")
            btn_prev.setObjectName("Ghost")
            btn_prev.setCursor(Qt.PointingHandCursor)
            btn_prev.setToolTip("按当前设置渲染一帧用于检查")
            btn_prev.clicked.connect(lambda: self._start(preview=True))
            row.addWidget(btn_prev)
            card.add_layout(row)
            lay.addWidget(card)

            card2 = Card("尺寸与帧率")
            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(10)
            self.e_width = self._grid_entry(grid, 0, 0, "画布宽", "1180")
            self.e_height = self._grid_entry(grid, 0, 2, "画布高", "640")
            self.e_fps = self._grid_entry(grid, 1, 0, "帧率 fps", "5")
            self.e_colors = self._grid_entry(grid, 1, 2, "颜色数", "256")
            self.e_scale = self._grid_entry(grid, 2, 0, "高清倍率", "1.0")
            grid.addWidget(field_label("浏览器", 88), 2, 2)
            self.cb_browser = ComboBox()
            self.cb_browser.addItems(["auto", "msedge", "chrome", "chromium"])
            grid.addWidget(self.cb_browser, 2, 3)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(3, 1)
            card2.add_layout(grid)
            card2.add(hint_label(
                "「高清倍率」为超采样倍率 (渲染分辨率 = 画布尺寸 × 倍率), 再缩放回画布尺寸。"))
            lay.addWidget(card2)

            card3 = Card("帧范围与循环")
            self.e_index = self._entry_row(card3, "帧索引", ":", 150)
            card3.add(hint_label(": 全部  ·  -1 最后一帧  ·  0:100:2 切片"))
            self.e_stride = self._entry_row(
                card3, "抽帧", "", 110, "留空 = 不抽帧 (使用全部帧); 填 N = 隔 N 帧取 1 帧")
            self.e_max = self._entry_row(
                card3, "最多帧数", "", 110, "留空 = 不限制")
            # 实时显示将要渲染的帧数 (默认不抽帧 → 等于 XDATCAR 总帧数)
            row_fc = QHBoxLayout()
            row_fc.addStretch(1)
            self.lbl_framecount = QLabel("尚未加载轨迹")
            self.lbl_framecount.setObjectName("Chip")
            row_fc.addWidget(self.lbl_framecount)
            card3.add_layout(row_fc)
            for w in (self.e_index, self.e_stride, self.e_max):
                w.textChanged.connect(self._update_framecount)
            self.sw_pingpong, b1 = self._switch_row("乒乓循环", False, "正放+倒放")
            card3.add_layout(b1)
            self.sw_loop, b1b = self._switch_row(
                "循环播放 GIF", True, "关闭则 GIF 只播放一遍")
            card3.add_layout(b1b)
            self.sw_keep, b2 = self._switch_row("保留逐帧 PNG", False)
            card3.add_layout(b2)
            lay.addWidget(card3)

        def build_side(lay):
            card4 = Card("GIF 帧标注")
            self.sw_numbers, b3 = self._switch_row(
                "显示当前帧数值 (力 / 能量 / RMS)", True,
                "在每帧上叠加该帧的力、能量、相对首帧 RMS")
            card4.add_layout(b3)
            self.sw_diff, b4 = self._switch_row(
                "同时显示一阶差分 (Δ能量 / Δ力)", False)
            card4.add_layout(b4)
            self.sw_curves, b5 = self._switch_row(
                "叠加曲线面板并在对应帧打点", True,
                "使用「分析曲线」页勾选的曲线 (可多选), 在当前帧位置画竖线与圆点")
            card4.add_layout(b5)

            row = QHBoxLayout()
            row.setSpacing(10)
            self.seg_panelpos = SegmentedControl(PANEL_POS_ITEMS, "right")
            self.seg_panelpos.changed.connect(self._on_panelpos_changed)
            self._stack(card4, "面板位置", self.seg_panelpos,
                        "「右侧」= 横屏左右布局 (左轨迹 + 右曲线/数值)")

            self.cb_corner = ComboBox()
            self.cb_corner.addItems(["左上", "右上", "左下", "右下"])
            self._stack(card4, "数值位置", self.cb_corner,
                        "仅下方/上方/叠加布局有效")
            self.sl_panelh = SliderField("面板高度", 100, 360, 190, 260, "{:.0f}px", 52)
            card4.add(self.sl_panelh)
            self.e_panelw = self._entry_row(
                card4, "面板宽度", "380", 90,
                "左右布局时右侧曲线区宽度", label_width=52)
            lay.addWidget(card4)

        return self._two_col_page(build_main, build_side)

    def _grid_entry(self, grid, row, col, label, value):
        grid.addWidget(field_label(label, 88), row, col)
        edit = QLineEdit(value)
        edit.setFixedWidth(96)
        grid.addWidget(edit, row, col + 1)
        return edit

    def _on_panelpos_changed(self, key):
        """选“右侧”时自动把画布调成横屏 (宽 > 高), 并禁用无关控件。"""
        is_right = (key == "right")
        for w in (getattr(self, "e_panelw", None),):
            if w is not None:
                w.setEnabled(is_right)
        if hasattr(self, "cb_corner"):
            self.cb_corner.setEnabled(not is_right)
        if hasattr(self, "sl_panelh"):
            self.sl_panelh.setEnabled(not is_right)
        if not is_right:
            return
        try:
            w = int(self.e_width.text() or 0)
            h = int(self.e_height.text() or 0)
        except ValueError:
            return
        if w and h and h >= w:
            self.e_width.setText(str(max(h, 1180)))
            self.e_height.setText(str(max(min(w, h), 640)))

    # ==================================================================
    # 页面 5: 日志
    # ==================================================================
    def _page_log(self):
        def build_main(lay):
            card = Card("运行日志")
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMinimumHeight(320)
            self.log.setMaximumBlockCount(6000)
            card.add(self.log)
            row = QHBoxLayout()
            row.addStretch(1)
            btn = QPushButton("清空")
            btn.setObjectName("Ghost")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(self.log.clear)
            row.addWidget(btn)
            card.add_layout(row)
            lay.addWidget(card, 1)

        def build_side(lay):
            card = Card("快捷操作")
            b1 = QPushButton("打开输出文件")
            b1.setObjectName("Secondary")
            b1.setCursor(Qt.PointingHandCursor)
            b1.clicked.connect(self._open_output)
            card.add(b1)
            b2 = QPushButton("打开输出所在文件夹")
            b2.setObjectName("Ghost")
            b2.setCursor(Qt.PointingHandCursor)
            b2.clicked.connect(self._open_output_dir)
            card.add(b2)
            lay.addWidget(card)

            card2 = Card("当前设置")
            self.lbl_summary = QLabel("—")
            self.lbl_summary.setObjectName("Hint")
            self.lbl_summary.setWordWrap(True)
            self.lbl_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
            card2.add(self.lbl_summary)
            b3 = QPushButton("刷新设置摘要")
            b3.setObjectName("Ghost")
            b3.setCursor(Qt.PointingHandCursor)
            b3.clicked.connect(self._refresh_summary)
            card2.add(b3)
            lay.addWidget(card2)

        return self._two_col_page(build_main, build_side,
                                  main_tail_stretch=False)

    def _page_preview(self):
        """预览页: 图像独占整块区域 (**不放右侧参数栏**)。

        旧版把预览塞进三栏的中间栏: 1180×640 的 GIF 在 1536 宽窗口下只能显示
        0.92×, 窗口一小说 0.48× —— 那才是“预览比原图模糊”的真正原因
        (任何缩小都是一次重采样)。预览不需要参数, 所以整页都给图像。
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 6, 16, 10)
        lay.setSpacing(8)

        card = Card("")
        card.vbox.setContentsMargins(10, 10, 10, 10)
        self.preview_canvas = GifCanvas()
        self.preview_canvas.viewChanged.connect(self._update_preview_info)
        card.add(self.preview_canvas)
        card.body.setStretchFactor(self.preview_canvas, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_prev_play = QPushButton("暂停")
        self.btn_prev_play.setObjectName("Secondary")
        self.btn_prev_play.setCursor(Qt.PointingHandCursor)
        self.btn_prev_play.clicked.connect(self._toggle_preview_play)
        row.addWidget(self.btn_prev_play)
        btn_re = QPushButton("重新载入")
        btn_re.setObjectName("Ghost")
        btn_re.setCursor(Qt.PointingHandCursor)
        btn_re.clicked.connect(lambda: self._load_gif_preview(self.preview_path))
        row.addWidget(btn_re)
        b1 = QPushButton("打开 GIF 文件")
        b1.setObjectName("Ghost")
        b1.setCursor(Qt.PointingHandCursor)
        b1.clicked.connect(self._open_output)
        row.addWidget(b1)
        b2 = QPushButton("打开所在文件夹")
        b2.setObjectName("Ghost")
        b2.setCursor(Qt.PointingHandCursor)
        b2.clicked.connect(self._open_output_dir)
        row.addWidget(b2)
        hint = QLabel("滚轮缩放 · 拖动平移 · 双击复位")
        hint.setObjectName("Hint")
        row.addWidget(hint)
        row.addStretch(1)
        self.lbl_prev_path_short = QLabel("—")
        self.lbl_prev_path_short.setObjectName("Hint")
        row.addWidget(self.lbl_prev_path_short)
        self.lbl_prev_info = QLabel("—")
        self.lbl_prev_info.setObjectName("Chip")
        row.addWidget(self.lbl_prev_info)
        card.add_layout(row)
        lay.addWidget(card, 1)
        return page

    def _load_gif_preview(self, path):
        """把生成的 GIF 载入预览页: QMovie 只做逐帧解码, 绘制交给 GifCanvas。"""
        if not path or not os.path.exists(str(path)):
            return
        path = str(path)
        if path.lower().endswith(".png"):
            return
        self._stop_preview()
        self.preview_path = path
        try:
            with Image.open(path) as im:
                size = im.size
                n = getattr(im, "n_frames", 1)
        except Exception:  # noqa: BLE001
            size, n = None, 0
        movie = QMovie(path)
        self.preview_movie = movie
        self._gif_size = size
        self.preview_canvas.set_movie(movie)
        movie.frameChanged.connect(self._paint_preview_frame)
        movie.start()
        self.btn_prev_play.setText("暂停")
        mb = os.path.getsize(path) / 1e6
        self.lbl_prev_path_short.setText(
            (path if len(path) <= 46 else "…" + path[-45:]).replace("\\", "/"))
        self.lbl_prev_path_short.setToolTip(path)
        self.lbl_prev_path_short.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._log(f"[信息] 预览已载入: {path}")
        self._log(f"[信息] GIF {size[0] if size else '?'}×{size[1] if size else '?'}, "
                  f"{n} 帧, {mb:.2f} MB")
        QTimer.singleShot(0, self._paint_preview_frame)

    def _stop_preview(self):
        """停掉并释放预览 (Windows 下否则会锁住文件, 重新生成到同一路径会失败)。

        注意: 必须先断开 frameChanged 并清掉画布对 QMovie 的引用, 再 deleteLater,
        否则待删除对象还会被 paintEvent 访问 -> libshiboken 报“C++ 对象已释放”。
        """
        mv = getattr(self, "preview_movie", None)
        if mv is not None:
            try:
                mv.stop()
                mv.frameChanged.disconnect(self._paint_preview_frame)
            except Exception:  # noqa: BLE001
                pass
        self.preview_movie = None
        cv = getattr(self, "preview_canvas", None)
        if cv is not None:
            cv.set_movie(None)
        if mv is not None:
            mv.deleteLater()

    def _fit_preview(self):
        """重新适配当前区域 (按钮 / 窗口尺寸变化后调)。"""
        self._paint_preview_frame()

    def _update_preview_info(self):
        """刷新右下角的尺寸/倍率提示 (自动适配 1:1 / 适应 x× / 手动 x%)。"""
        cv = getattr(self, "preview_canvas", None)
        size = getattr(self, "_gif_size", None)
        if cv is None or not size:
            return
        k = cv.effective_scale()
        if cv.user_zoom is not None:
            tag = f"{k * 100:.0f}%"
        elif k >= 0.999:
            tag = "1:1"
        else:
            tag = f"适应 {k:.2f}×"
        self.lbl_prev_info.setText(f"{size[0]} × {size[1]}  ·  {tag}")

    def _paint_preview_frame(self, *_):
        """让画布重绘当前帧, 并刷新尺寸/倍率提示。"""
        cv = getattr(self, "preview_canvas", None)
        if cv is None:
            return
        cv.refresh()
        self._update_preview_info()

    def _toggle_preview_play(self):
        mv = getattr(self, "preview_movie", None)
        if mv is None:
            return
        if mv.state() == QMovie.Running:
            mv.stop()
            self.btn_prev_play.setText("播放")
        else:
            mv.start()
            self.btn_prev_play.setText("暂停")

    def _open_output_dir(self):
        target = self.last_output or self._text(self.e_out)
        if target and os.path.exists(target):
            target = os.path.dirname(os.path.abspath(target))
        else:
            target = str(HERE)
        try:
            os.startfile(target)
        except AttributeError:
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, target])

    def _refresh_summary(self):
        lbl = getattr(self, "lbl_summary", None)
        if lbl is None:
            return
        try:
            args = self.collect_args()
        except ValueError as exc:
            lbl.setText(f"参数不完整: {exc}")
            return
        n = len(self.frame_indices) if self.frame_indices else 0
        specs = self.analysis_view.selected_specs()
        pw = getattr(args, "panel_width", None)
        pos = {"right": "右侧(横屏)", "bottom": "下方", "top": "上方",
               "overlay": "叠加"}.get(args.panel_pos, args.panel_pos)
        lines = [
            f"轨迹: {os.path.basename(args.file)}",
            f"OUTCAR: {os.path.basename(args.outcar) if args.outcar else '(无)'}",
            f"画布: {args.width} × {args.height}",
            f"帧: {n} 帧 · {self._text(self.e_fps)} fps",
            f"标注: {pos}" + (f" · 面板 {pw}px" if pos.startswith("右侧") else ""),
            f"GIF 曲线: {len(specs)} 条",
            f"输出: {args.out}",
        ]
        lbl.setText("\n".join(lines))

    def _restore_defaults(self):
        self.pages.currentChanged.connect(self._on_page_changed)
        try:
            self._on_panelpos_changed(self.seg_panelpos.value())
        except Exception:
            pass
        if (HERE / "test" / "XDATCAR").exists():
            self.xdatcar_path = str(HERE / "test" / "XDATCAR")
        elif (HERE / "XDATCAR").exists():
            self.xdatcar_path = str(HERE / "XDATCAR")
        if (HERE / "test" / "OUTCAR").exists():
            self.outcar_path = str(HERE / "test" / "OUTCAR")
        elif (HERE / "OUTCAR").exists():
            self.outcar_path = str(HERE / "OUTCAR")
        self._update_file_label()

    def _on_page_changed(self, idx):
        names = ["轨迹浏览", "分析曲线", "GIF 设置", "GIF 预览", "日志"]
        if 0 <= idx < len(names):
            self.page_title.setText(names[idx])
            for b in self.nav_group.buttons():
                if b.text() == names[idx]:
                    b.setChecked(True)
        # 离开预览页就暂停动画, 省 CPU
        mv = getattr(self, "preview_movie", None)
        if mv is not None:
            if idx == 3:
                mv.start()
                QTimer.singleShot(0, self._fit_preview)
            else:
                mv.stop()
        if idx == 4:
            self._refresh_summary()

    # ==================================================================
    # 窗口交互: 拖动 / 缩放
    # ==================================================================
    def _edges_at(self, pos):
        m = self.RESIZE_MARGIN
        r = self.rect()
        edges = None
        for cond, e in ((pos.x() <= m, Qt.Edge.LeftEdge),
                        (pos.x() >= r.width() - m, Qt.Edge.RightEdge),
                        (pos.y() <= m, Qt.Edge.TopEdge),
                        (pos.y() >= r.height() - m, Qt.Edge.BottomEdge)):
            if cond:
                edges = e if edges is None else (edges | e)
        return edges

    def mouseMoveEvent(self, event):  # noqa: N802
        edges = self._edges_at(event.position().toPoint())
        cursor = Qt.ArrowCursor
        if edges is not None:
            left = bool(edges & Qt.Edge.LeftEdge)
            right = bool(edges & Qt.Edge.RightEdge)
            top = bool(edges & Qt.Edge.TopEdge)
            bottom = bool(edges & Qt.Edge.BottomEdge)
            if (left and top) or (right and bottom):
                cursor = Qt.SizeFDiagCursor
            elif (right and top) or (left and bottom):
                cursor = Qt.SizeBDiagCursor
            elif left or right:
                cursor = Qt.SizeHorCursor
            else:
                cursor = Qt.SizeVerCursor
        self.setCursor(QCursor(cursor))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self.windowHandle():
            edges = self._edges_at(event.position().toPoint())
            if edges is not None:
                self.windowHandle().startSystemResize(edges)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self.setCursor(QCursor(Qt.ArrowCursor))
        super().mouseReleaseEvent(event)

    # ==================================================================
    # 文件选择 / 元素配色
    # ==================================================================
    def _pick_xdatcar(self):
        start = self.xdatcar_path or str(HERE)
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 XDATCAR 轨迹文件", start,
            "VASP 轨迹 (XDATCAR*);;所有文件 (*)")
        if path:
            self.xdatcar_path = path
            self._update_file_label()
            # 自动找同目录的 OUTCAR
            if not self.outcar_path:
                cand = Path(path).with_name("OUTCAR")
                if cand.is_file():
                    self.outcar_path = str(cand)
                    self._update_file_label()
            self._log(f"[信息] 已选择 XDATCAR: {path}")

    def _pick_outcar(self):
        start = self.outcar_path or self.xdatcar_path or str(HERE)
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 OUTCAR 文件", start,
            "VASP 输出 (OUTCAR*);;所有文件 (*)")
        if path:
            self.outcar_path = path
            self._update_file_label()
            self._log(f"[信息] 已选择 OUTCAR: {path}")

    def _update_file_label(self):
        xname = Path(self.xdatcar_path).name if self.xdatcar_path else "(未选)"
        oname = Path(self.outcar_path).name if self.outcar_path else "(未选, 仅轨迹)"
        self.lbl_files.setText(f"XDATCAR: {xname}   ·   OUTCAR: {oname}")
        self.lbl_files.setToolTip(
            f"XDATCAR: {self.xdatcar_path or '(未选)'}\n"
            f"OUTCAR: {self.outcar_path or '(未选, 仅轨迹)'}")

    def _open_color_dialog(self):
        if not self.trajectory:
            self._toast("提示", "请先加载轨迹, 再设置元素配色。")
            return
        els = sorted(set(self.trajectory[0].get_chemical_symbols()))
        scale = self.sl_radius.value()
        uniform = self._num(self.e_radius, "统一半径", float)
        base_colors = {el: (self._vesta_colors.get(el) or "#B8B8B8") for el in els}
        base_radii = {}
        for el in els:
            if uniform is not None:
                base_radii[el] = float(uniform)
            else:
                base_radii[el] = self._vesta_radii.get(el, 0.5) * scale
        cur_colors = {el: (self.color_overrides.get(el)
                           or self._vesta_colors.get(el) or "#B8B8B8")
                      for el in els}
        cur_radii = {el: self.radius_overrides.get(el, base_radii[el])
                     for el in els}
        dlg = ElementColorDialog(cur_colors, cur_radii, base_colors,
                                 base_radii, els, self)
        if dlg.exec() == QDialog.Accepted:
            colors = dlg.result_colors()
            radii = dlg.result_radii()
            self.color_overrides = {
                el: colors[el] for el in els
                if colors.get(el) and colors[el].lower() != base_colors[el].lower()}
            self.radius_overrides = {
                el: radii[el] for el in els
                if abs(radii.get(el, 0.0) - base_radii[el]) > 1e-6}
            self._log(f"[信息] 自定义配色 {len(self.color_overrides)} 个, "
                      f"自定义半径 {len(self.radius_overrides)} 个")
            self._reload_viewer()

    def effective_colors(self):
        """内置 VESTA 配色 + 用户自定义覆盖。"""
        base = dict(self._vesta_colors)
        base.update(self.color_overrides)
        return base

    # ==================================================================
    # 数据加载
    # ==================================================================
    def _load_clicked(self):
        if self.worker and self.worker.is_alive():
            self._toast("提示", "当前有任务在运行, 请稍候。")
            return
        xdatcar = self.xdatcar_path
        if not xdatcar or not os.path.isfile(xdatcar):
            self._toast("提示", "请先点「选择 XDATCAR」选择轨迹文件。")
            return
        outcar = self.outcar_path or None
        if outcar and not os.path.isfile(outcar):
            self._toast("提示", f"找不到 OUTCAR: {outcar}")
            return
        pairs = self._pairs()
        cfg = {
            "radius_scale": self.sl_radius.value(),
            "uniform_radius": self._num(self.e_radius, "统一半径", float),
            "color_overrides": dict(self.color_overrides),
            "radius_overrides": dict(self.radius_overrides),
        }
        self.cancel_evt.clear()
        self._loading = True
        self.btn_load2.setEnabled(False)
        self.status.setText("正在加载轨迹与 OUTCAR…")
        self.worker = threading.Thread(
            target=self._load_worker, args=(xdatcar, outcar, pairs, cfg),
            daemon=True)
        self.worker.start()

    def _load_worker(self, xdatcar, outcar, pairs, cfg):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(self.q)
        try:
            base_colors, radii = self._vesta_maps()
            eff = dict(base_colors)
            eff.update(cfg.get("color_overrides", {}))
            result, frames = vp.build_analysis(
                xdatcar, outcar, pairs=pairs,
                progress=lambda msg, i=0, n=1: self.q.put(("log", msg)))
            elem_map = core.make_element_map(
                frames[:1], eff, radii, cfg["radius_scale"],
                cfg["uniform_radius"], cfg.get("radius_overrides"))
            self.q.put(("loaded", result, frames, elem_map,
                        (base_colors, radii)))
        except Exception:
            self.q.put(("error", traceback.format_exc()))
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    def _vesta_maps(self):
        """内置 VESTA 经典配色/半径 (不再读取 .vesta 文件)。"""
        ns = types.SimpleNamespace(use_vesta=True, vesta=None,
                                   no_vesta_file=True)
        core.resolve_vesta(ns)
        return dict(ns._vesta_colors), dict(ns._vesta_radii)

    def _on_loaded(self, result, frames, elem_map, maps):
        self._loading = False
        self.btn_load2.setEnabled(True)
        self.trajectory = frames
        self.analysis = result
        self.elem_map = elem_map
        self._vesta_colors, self._vesta_radii = maps
        self.n_frames = len(frames)

        # 原子选择下拉框
        symbols = frames[0].get_chemical_symbols()
        self.cb_atom_a.clear()
        self.cb_atom_b.clear()
        for i, s in enumerate(symbols):
            self.cb_atom_a.addItem(f"{i:>4}  {s}")
            self.cb_atom_b.addItem(f"{i:>4}  {s}")
        self.cb_atom_b.setCurrentIndex(min(1, len(symbols) - 1))

        self.analysis_view.set_result(result)

        try:
            self.frame_indices = vp.select_frames(
                frames, self.e_index.text().strip() or ":",
                self._num(self.e_stride, "抽帧", int),
                self._num(self.e_max, "最多帧数", int))
        except ValueError as exc:
            self._log(f"[警告] {exc}; 已使用全部帧")
            self.frame_indices = list(range(len(frames)))
        self._update_framecount()
        self._reload_viewer()

        info = f"已加载 {len(frames)} 帧 · {len(symbols)} 原子"
        if result.has_force:
            info += "\n力收敛: 可用"
        if result.has_energy:
            info += " · 能量: 可用"
        self.lbl_dataload.setText(info)
        if result.warnings:
            for w in result.warnings:
                self._log("[警告] " + w)
        self.status.setText(f"加载完成: {len(frames)} 帧, {len(symbols)} 原子")
        self._log(f"[完成] {info.replace(chr(10), ' ')}")

    def _reload_viewer(self):
        if not self.trajectory:
            return
        if not self.frame_indices:
            self.frame_indices = list(range(len(self.trajectory)))
        try:
            elem_map = core.make_element_map(
                self.trajectory[:1], self.effective_colors(),
                self._vesta_radii, self.sl_radius.value(),
                self._num(self.e_radius, "统一半径", float),
                dict(self.radius_overrides))
            self.elem_map = elem_map
            frames = [self.trajectory[i] for i in self.frame_indices]
            self.viewer.load_trajectory(
                frames, elem_map,
                style=self.seg_style.value(),
                show_cell=self.sw_cell.isChecked(),
                bg=self.cb_bg.currentText(),
                zoom=self.sl_zoom.value(),
                view=self.seg_view.value(),
                pan=(self.sl_panx.value(), self.sl_pany.value()),
                fps=int(self._num(self.e_fps, "帧率", float, 5)))
            self._viewer_ready = True
            self._log("[信息] 3D 窗口已刷新")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[错误] 刷新 3D 窗口失败: {exc}")

    def _on_viewer_frame(self, i):
        if self.frame_indices and i < len(self.frame_indices):
            self.analysis_view.set_current(self.frame_indices[i])

    def _on_camera_captured(self, slot, view, css_w):
        self.captured[slot] = (view, css_w)
        self.lbl_cap.setText("已捕获视角 A \u2713")
        self.status.setText(f"已捕获视角 A (画布宽 {css_w}px)")
        self._log(f"[信息] 捕获视角 A (画布宽 {css_w}px) — 可在「视角与显示」选择"
                  f"「捕获视角 A」, 或作为「绕轴旋转」的基准")
        # 注意: 此处不能用模态对话框, 它会挂起 WebEngine 页面, 导致后续
        #       视角捕获 / JS 调用返回空。

    # ==================================================================
    # 原子对
    # ==================================================================
    def _pairs(self):
        out = []
        for r in range(self.pair_list.count()):
            item = self.pair_list.item(r)
            data = item.data(Qt.UserRole)
            if data:
                out.append(tuple(data))
        return out

    def _add_pair(self):
        if not self.trajectory:
            self._toast("提示", "请先加载轨迹。")
            return
        a = self.cb_atom_a.currentIndex()
        b = self.cb_atom_b.currentIndex()
        if a < 0 or b < 0 or a == b:
            self._toast("提示", "请选择两个不同的原子。")
            return
        self._append_pair(a, b)

    def _append_pair(self, a, b):
        key = vp.pair_key(a, b)
        for r in range(self.pair_list.count()):
            if self.pair_list.item(r).data(Qt.UserRole) == (a, b):
                return
        sym = self.trajectory[0].get_chemical_symbols()
        item = QListWidgetItem(f"[{a}] {sym[a]}  —  [{b}] {sym[b]}")
        item.setData(Qt.UserRole, (int(a), int(b)))
        self.pair_list.addItem(item)
        self._compute_pair(a, b)

    def _compute_pair(self, a, b):
        """立即计算该原子对的键长曲线并刷新。"""
        if self.analysis is None or self.trajectory is None:
            return
        key = vp.pair_key(a, b)
        try:
            self.analysis.bonds[key] = vp.bond_length_series(
                self.trajectory, a, b, mic=True)
            self.analysis.pair_indices[key] = (int(a), int(b))
            self.analysis_view.replot()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[警告] 键长计算失败 ({key}): {exc}")

    def _remove_pair(self):
        for item in self.pair_list.selectedItems():
            data = item.data(Qt.UserRole)
            if data and self.analysis is not None:
                key = vp.pair_key(*data)
                self.analysis.bonds.pop(key, None)
                self.analysis.pair_indices.pop(key, None)
            self.pair_list.takeItem(self.pair_list.row(item))
        if self.analysis is not None:
            self.analysis_view.replot()

    def _auto_pairs(self):
        if not self.trajectory:
            self._toast("提示", "请先加载轨迹。")
            return
        pairs = vp.detect_bonds(self.trajectory[0], max_pairs=6)
        if not pairs:
            self._toast("提示", "首帧未检测到明显的化学键。")
            return
        for a, b, _d in pairs:
            self._append_pair(a, b)
        self._log(f"[信息] 自动检测到 {len(pairs)} 个原子对")

    # ==================================================================
    # 参数收集
    # ==================================================================
    def _text(self, edit):
        return edit.text().strip()

    def _num(self, edit, name, cast=float, default=None):
        s = edit.text().strip()
        if not s:
            return default
        try:
            return cast(s)
        except ValueError:
            raise ValueError(f"「{name}」需要是数字，当前为“{s}”")

    def _update_framecount(self, *_):
        """实时显示将要渲染的帧数 (默认不抽帧)。"""
        lbl = getattr(self, "lbl_framecount", None)
        if lbl is None:
            return
        if not self.trajectory:
            lbl.setText("尚未加载轨迹")
            return
        total = len(self.trajectory)
        try:
            idx = vp.select_frames(
                self.trajectory, self._text(self.e_index) or ":",
                self._num(self.e_stride, "抽帧", int),
                self._num(self.e_max, "最多帧数", int))
        except ValueError as exc:
            lbl.setText(str(exc))
            return
        n = len(idx)
        lbl.setText(f"将渲染 {n} 帧 / 共 {total} 帧 · "
                    + ("不抽帧" if n == total else "已抽帧"))

    def collect_args(self, preview=False):
        path = self.xdatcar_path
        if not path:
            raise ValueError("请先点「选择 XDATCAR」选择轨迹文件")
        out = self._text(self.e_out)
        if not out:
            raise ValueError("请先指定输出文件")

        width = self._num(self.e_width, "画布宽", int, 500)
        height = self._num(self.e_height, "画布高", int, None) or width

        mode = self.seg_viewmode.value()
        view_start = None
        if mode == "capture_a":
            if "A" not in self.captured:
                raise ValueError("尚未捕获视角 A, 请先在「轨迹浏览」页点「设为视角 A」")
            view_start = self.captured["A"][0]

        args = types.SimpleNamespace(
            file=path,
            out=out,
            index=self._text(self.e_index) or ":",
            stride=self._num(self.e_stride, "抽帧", int),
            max_frames=self._num(self.e_max, "最多帧数", int),
            repeat=None,

            style=self.seg_style.value(),
            radius=self._num(self.e_radius, "统一半径", float),
            radius_scale=self.sl_radius.value(),
            vesta=None,
            use_vesta=True,
            no_vesta_file=True,
            color_overrides=dict(self.color_overrides),
            radius_overrides=dict(self.radius_overrides),

            cell=self.sw_cell.isChecked(),
            cell_color="#888888",
            bg=self.cb_bg.currentText() or "white",

            width=width,
            height=height,
            gif_width=width,
            scale=self._num(self.e_scale, "高清倍率", float, 1.0),
            zoom=self.sl_zoom.value(),
            pan_x=self.sl_panx.value(),
            pan_y=self.sl_pany.value(),
            view=self.seg_view.value(),
            rot=None,
            spin=0.0,

            fps=self._num(self.e_fps, "帧率", float, 5.0),
            colors=self._num(self.e_colors, "颜色数", int, 256),
            pingpong=self.sw_pingpong.isChecked(),
            loop=(0 if self.sw_loop.isChecked() else None),

            browser=None if self.cb_browser.currentText() == "auto"
            else self.cb_browser.currentText(),
            show_browser=False,
            keep_frames=self.sw_keep.isChecked(),
            frames_dir=None,

            views=None,
            view_start=view_start,
            rot_axis=self.cb_rotaxis.currentData() or "",
            rot_total=self._num(self.e_rotang, "旋转角度", float, 0.0),

            # 叠加与数据
            overlay_numbers=self.sw_numbers.isChecked(),
            overlay_diff=self.sw_diff.isChecked(),
            overlay_curves=self.sw_curves.isChecked(),
            overlay_specs=self.analysis_view.selected_specs()
            if self.sw_curves.isChecked() else [],
            panel_pos=self.seg_panelpos.value(),
            panel_height=int(self.sl_panelh.value()),
            panel_width=self._num(self.e_panelw, "面板宽度", int, 380),
            numbers_corner=({"左上": "top-left", "右上": "top-right",
                             "左下": "bottom-left", "右下": "bottom-right"}
                            .get(self.cb_corner.currentText(), "top-left")),
            frames_override=None,
            analysis=self.analysis,
            outcar=self.outcar_path or None,
            pairs=self._pairs(),
        )
        # 左右布局: 轨迹只占左侧, 右侧留给曲线与数值
        args.render_width = args.width
        if args.panel_pos == "right" and args.overlay_curves:
            pw = max(200, min(int(args.panel_width or 380), args.width - 240))
            # 轨迹区过窄会让模型被水平裁切, 因此限制面板宽度:
            # 保证轨迹区宽度 >= 0.75 x 画布高度 (且不小于 240px)。
            min_rw = max(240, int(round(args.height * 0.75)))
            if args.width - pw < min_rw:
                pw = max(120, args.width - min_rw)
            args.panel_width = pw
            args.render_width = max(160, args.width - pw)
        elif args.panel_pos == "right":
            args.render_width = args.width
        return args

    # ==================================================================
    # 任务: 预览 / 生成
    # ==================================================================
    def _start(self, preview=False):
        if self.worker and self.worker.is_alive():
            self._toast("提示", "已有任务在运行, 请等待或点击「停止」。")
            return
        if self.trajectory is None:
            self._toast("提示", "请先在「轨迹浏览」页点击「开始加载 / 分析」。")
            return
        try:
            args = self.collect_args(preview)
        except ValueError as exc:
            self._toast("参数有误", str(exc))
            return

        self.cancel_evt.clear()
        self.log.clear()
        self.progress.setValue(0)
        # 先释放预览占用的文件句柄 (Windows 下否则重写同一个 GIF 会失败)
        self._stop_preview()
        self._set_running(True, preview)
        if not preview:
            self.progress_dlg.begin("正在生成 GIF")
        self.worker = threading.Thread(target=self._run, args=(args, preview),
                                       daemon=True)
        self.worker.start()

    def _stop(self):
        self.cancel_evt.set()
        self.status.setText("正在停止…")
        try:
            self.progress_dlg.set_status("正在停止, 请稍候…")
        except Exception:
            pass

    def _set_running(self, running, preview=False):
        self.btn_run.setEnabled(not running)
        self.btn_load2.setEnabled(not running)
        if running:
            self.status.setText("预览中…" if preview else "渲染中…")

    def _open_output(self):
        target = self.last_output or self._text(self.e_out)
        if not target or not os.path.exists(target):
            self._toast("提示", "还没有可打开的输出文件。")
            return
        try:
            os.startfile(target)
        except AttributeError:
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, target])

    def _run(self, args, preview):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(self.q)
        frames_dir = None
        ok = False
        try:
            vesta_path = core.resolve_vesta(args)
            # 应用用户在「元素配色」中的自定义覆盖
            overrides = getattr(args, "color_overrides", None) or {}
            if overrides:
                args._vesta_colors.update(overrides)
                self.q.put(("log", f"自定义配色: {len(overrides)} 个元素"))
            if args.use_vesta:
                self.q.put(("log", f"VESTA 配色: {vesta_path or '(内置经典配色)'}"))
            else:
                self.q.put(("log", "已禁用 VESTA 配色"))

            # 1) 轨迹与帧索引
            indices = vp.select_frames(self.trajectory, args.index, args.stride,
                                       args.max_frames)
            images = [self.trajectory[i] for i in indices]
            if args.repeat and tuple(args.repeat) != (1, 1, 1):
                images = [a.repeat(args.repeat) for a in images]
            self.q.put(("log", f"准备渲染 {len(images)} 帧 (共 {len(self.trajectory)} 帧)"))

            single_png = (not preview) and args.out.lower().endswith(".png")
            if preview or single_png:
                images = images[:1]
                indices = indices[:1]

            # 2) 分析结果 (叠加用)
            analysis = args.analysis
            if (args.overlay_numbers or args.overlay_curves) and analysis is None:
                self.q.put(("log", "正在计算分析曲线…"))
                analysis, _ = vp.build_analysis(
                    args.file, args.outcar, pairs=args.pairs,
                    frames=self.trajectory)

            # 3) 渲染截图
            if args.keep_frames:
                frames_dir = Path(os.path.splitext(args.out)[0] + "_frames")
                frames_dir.mkdir(parents=True, exist_ok=True)
                cleanup = False
            else:
                frames_dir = Path(tempfile.mkdtemp(prefix="3dmol-frames-"))
                cleanup = True

            paths = core.render_pngs(
                images, args, frames_dir,
                progress=lambda i, n: self.q.put(("progress", i, n)),
                cancel=self.cancel_evt.is_set)
            if not paths:
                self.q.put(("log", "没有生成任何帧"))
                return

            # 4) 逐帧标注
            annotate = None
            if (args.overlay_numbers or args.overlay_curves) and analysis is not None:
                annotate = self._make_annotator(args, analysis, indices, paths)

            if preview:
                base = os.path.splitext(args.out)[0]
                png = (base + ".png") if base.endswith("_preview") \
                    else (base + "_preview.png")
                im = Image.open(paths[0]).convert("RGB")
                if annotate:
                    im = annotate(0, im)
                im.save(png)
                self.q.put(("preview", png))
                ok = True
            elif single_png:
                im = Image.open(paths[0]).convert("RGB")
                if annotate:
                    im = annotate(0, im)
                im.save(args.out)
                self.q.put(("done", args.out, 1, args.fps,
                            os.path.getsize(args.out) / 1e6))
                ok = True
            elif self.cancel_evt.is_set() and len(paths) < len(images):
                self.q.put(("log", "已取消, 未生成 GIF"))
            else:
                duration = max(1, int(round(1000.0 / args.fps)))
                gif_width = args.gif_width or args.width
                n = core.build_gif(paths, args.out, duration, args.loop,
                                   args.colors, args.pingpong, gif_width,
                                   annotate=annotate)
                size = os.path.getsize(args.out) / 1e6
                self.q.put(("done", args.out, n, args.fps, size))
                ok = True

            if cleanup:
                shutil.rmtree(frames_dir, ignore_errors=True)
            else:
                self.q.put(("log", f"逐帧 PNG 保存在: {frames_dir}"))
        except SystemExit as exc:
            self.q.put(("error", str(exc)))
        except Exception:
            self.q.put(("error", traceback.format_exc()))
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            self.q.put(("idle", ok))

    def _make_annotator(self, args, analysis, indices, paths):
        """根据实际渲染像素尺寸构建曲线底板 + 逐帧标注函数。"""
        fw, fh = Image.open(paths[0]).size
        # fw 是「轨迹部分」的像素宽 (左右布局时不含右侧面板)
        rd_w = int(getattr(args, "render_width", 0) or args.width)
        ratio = fw / float(max(1, rd_w))          # 实际像素比 (通常 1, --scale 2 时为 2)
        panel = None
        specs = list(args.overlay_specs)
        if args.overlay_curves and len(specs) > 8:
            self.q.put(("log", f"[信息] 曲线较多 ({len(specs)}), GIF 面板仅显示前 8 条"))
            specs = specs[:8]
        if args.overlay_curves and specs:
            if args.panel_pos == "right":
                pw = max(120, int(round(int(args.panel_width or 380) * ratio)))
                panel = go.CurvePanel(
                    specs, width=pw, height=fh,
                    dpi=100 * ratio, max_cols=1, theme="light")
            else:
                panel_h = max(60, int(round(args.panel_height * ratio)))
                panel = go.CurvePanel(
                    specs, width=fw, height=panel_h,
                    dpi=100 * ratio, max_cols=4,
                    theme="light" if args.bg.lower() in ("white", "#f2f2f7")
                    else "dark")
        ann = go.FrameAnnotator(
            panel=panel, show_numbers=args.overlay_numbers,
            position=args.panel_pos, theme="light",
            numbers_corner=args.numbers_corner)
        n_total = analysis.n_frames
        # 数值颜色 = 对应曲线颜色 (最大力 = 红, 能量 = 蓝, RMS = 绿)
        num_colors = {
            "force": go.PANEL_COLORS[1],
            "energy": go.PANEL_COLORS[0],
            "rms": go.PANEL_COLORS[2],
            "force_diff": go.PANEL_COLORS[6],
            "energy_diff": go.PANEL_COLORS[5],
        }

        def annotate(k, im):
            fi = indices[k] if k < len(indices) else 0
            lines = go.format_frame_lines(
                fi, n_total,
                vp.frame_value(analysis.max_force, fi),
                vp.frame_value(analysis.energies, fi),
                vp.frame_value(analysis.rms, fi),
                vp.frame_value(analysis.energy_diff, fi),
                vp.frame_value(analysis.force_diff, fi),
                show_force=args.overlay_numbers,
                show_energy=args.overlay_numbers,
                show_rms=args.overlay_numbers,
                show_diff=args.overlay_diff,
                colors=num_colors)
            mi = fi if panel is not None else fi
            return ann.annotate(im, mi, lines)

        return annotate

    # ==================================================================
    # 消息轮询
    # ==================================================================
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "progress":
                    i, n = msg[1], msg[2]
                    self.progress.setValue(int(100 * i / max(n, 1)))
                    self.status.setText(f"渲染 {i}/{n}")
                    if self.progress_dlg.isVisible():
                        self.progress_dlg.set_progress(i, n)
                elif kind == "loaded":
                    self._on_loaded(*msg[1:])
                elif kind == "done":
                    _, path, n, fps, size = msg
                    self.last_output = path
                    self.progress_dlg.finish()
                    is_png = path.lower().endswith(".png")
                    self.status.setText(f"完成: {Path(path).name}  {n} 帧  {size:.2f} MB")
                    self._log(f"[完成] {path}  {n} 帧, {size:.2f} MB")
                    self.progress.setValue(100)
                    if not is_png:
                        # 生成完直接跳到预览页
                        self._load_gif_preview(path)
                        self.pages.setCurrentIndex(3)
                    self._toast("完成",
                                f"{'PNG 图片' if is_png else 'GIF 动画'}已生成\n\n{path}\n\n"
                                f"{n} 帧 · {size:.2f} MB" +
                                ("" if is_png else f" · {fps:g} fps"),
                                QMessageBox.Information)
                elif kind == "preview":
                    self._show_preview(msg[1])
                elif kind == "error":
                    self.progress_dlg.finish()
                    self.status.setText("出错")
                    self._log("[错误] " + str(msg[1]))
                    self._toast("出错", str(msg[1])[-1500:], QMessageBox.Critical)
                elif kind == "idle":
                    self.progress_dlg.finish()
                    self._set_running(False)
                    if self._loading:
                        self._loading = False
                        self.btn_load2.setEnabled(True)
                    if not msg[1]:
                        self.status.setText("已停止")
        except queue.Empty:
            pass

    # ------------------------------------------------------------------
    def _log(self, text):
        if text:
            self.log.appendPlainText(str(text))

    def _toast(self, title, text, icon=QMessageBox.Information):
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()

    # ------------------------------------------------------------------
    def _show_preview(self, path):
        self.last_output = path
        win = QWidget()
        win.setWindowTitle("预览 - " + Path(path).name)
        win.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        win.setAttribute(Qt.WA_TranslucentBackground)
        outer = QVBoxLayout(win)
        outer.setContentsMargins(14, 14, 14, 14)
        frame = QFrame()
        frame.setObjectName("Window")
        shadow = QGraphicsDropShadowEffect(win)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        frame.setGraphicsEffect(shadow)
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 14, 14, 14)

        pix = QPixmap(path)
        # 上限按**设备像素**算, 并标上 dpr: 否则 QLabel 会按逻辑尺寸画,
        # 在高 DPI 屏上又被放大 1.25 倍, 白白插值一次。
        dpr = float(self.devicePixelRatioF() or 1.0)
        max_w, max_h = int(640 * dpr), int(820 * dpr)
        if pix.width() > max_w or pix.height() > max_h:
            pix = pix.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pix.setDevicePixelRatio(dpr)
        img = QLabel()
        img.setPixmap(pix)
        img.setAlignment(Qt.AlignCenter)
        lay.addWidget(img)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_save = QPushButton("保存为 PNG")
        btn_save.setObjectName("Secondary")
        btn_save.setCursor(Qt.PointingHandCursor)

        def save():
            dst, _ = QFileDialog.getSaveFileName(
                win, "保存图片", str(HERE / "frame.png"), "PNG 图片 (*.png)")
            if dst:
                shutil.copyfile(path, dst)
                self._log(f"[保存] {dst}")

        btn_save.clicked.connect(save)
        row.addWidget(btn_save)
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("Primary")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(win.close)
        row.addWidget(btn_close)
        lay.addLayout(row)

        win.show()
        self._preview_win = win

    # ------------------------------------------------------------------
    def closeEvent(self, event):  # noqa: N802
        try:
            self.viewer.shutdown()
        except Exception:
            pass
        super().closeEvent(event)


class _QueueWriter:
    def __init__(self, q):
        self.q = q
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(("log", line))

    def flush(self):
        if self.buf:
            self.q.put(("log", self.buf))
            self.buf = ""


# ==========================================================================
# ==========================================================================
def run_selftest(argv):
    """命令行自检 (用于验证打包后的 exe 是否具备完整运行环境)。

    用法: VASP轨迹可视化.exe --selftest <XDATCAR> <输出.gif> [OUTCAR]
    结果写入 <输出>.log 并返回退出码 (0 成功)。
    """
    import types as _types
    log = []

    def L(s):
        log.append(str(s))
        try:
            print(s)
        except Exception:
            pass

    try:
        xdatcar = argv[0]
        out = argv[1]
        outcar = argv[2] if len(argv) > 2 else None
        L(f"frozen={getattr(sys, 'frozen', False)} BASE={BASE_DIR}")
        L(f"3dmol candidates:")
        for c in core.THREEDMOL_CANDIDATES:
            L(f"   {c} exists={c.exists()}")
        js = core.ensure_3dmol_js()
        L(f"3dmol js bytes={len(js)}")
        res, frames = vp.build_analysis(xdatcar, outcar, pairs=[(0, 1)])
        L(f"frames={len(frames)} energy={res.has_energy} force={res.has_force} "
          f"rms={float(res.rms[-1]):.4f}")
        a = _types.SimpleNamespace(
            style="ballstick", radius=None, radius_scale=0.45, vesta=None,
            use_vesta=True, cell=True, cell_color="#888888", bg="white",
            width=200, height=240, gif_width=200, scale=1.0, zoom=1.0,
            view="right", rot=None, spin=0.0, colors=64, pingpong=False, loop=0,
            browser=None, show_browser=False, keep_frames=False, frames_dir=None,
            views=None, view_start=None, rot_axis="", rot_total=0.0,
            pan_x=0.0, pan_y=0.0)
        core.resolve_vesta(a)
        fd = Path(tempfile.mkdtemp(prefix="selftest-"))
        paths = core.render_pngs(frames[:2], a, fd)
        L(f"rendered={len(paths)} size={Image.open(paths[0]).size}")
        specs = [go.CurveSpec("force", "F", np.arange(len(res.max_force)),
                              res.max_force, "#FF3B30", "eV/A")]
        panel = go.CurvePanel(specs, width=Image.open(paths[0]).size[0],
                              height=80, dpi=100)
        ann = go.FrameAnnotator(panel=panel, show_numbers=True, position="bottom")
        n = core.build_gif(paths, out, 100, 0, 64, False, 200,
                           annotate=lambda k, im: ann.annotate(im, k, [f"frame {k}"]))
        L(f"gif={n} exists={os.path.exists(out)} size={os.path.getsize(out)}")
        L("SELFTEST OK")
        rc = 0
    except Exception:
        L("SELFTEST FAILED\n" + traceback.format_exc())
        rc = 1
    try:
        Path(out).with_suffix(Path(out).suffix + ".log").write_text(
            "\n".join(log), encoding="utf-8")
    except Exception:
        pass
    return rc


def run_gui_smoke(argv):
    """`--selftest-gui <XDATCAR> <截图.png> [OUTCAR]`

    真正把界面跑起来: 加载轨迹 -> 等 3D 窗口 (QtWebEngine/WebGL) 就绪并绘制
    -> 截图 -> 用**像素统计**判断 3D 画面上真的有东西 (无法目视时的自动化检查)
    -> 退出。用于验证打包后的 QtWebEngine 是否正常。
    结果同时写到 <截图>.log。
    """
    log = []

    def L(s):
        log.append(str(s))
        try:
            print(s)
        except Exception:
            pass

    xdatcar, shot = argv[0], argv[1]
    outcar = argv[2] if len(argv) > 2 else None
    rc = 1
    app = QApplication(sys.argv)
    app.setApplicationName("VASP 轨迹可视化 selftest")
    apply_theme(app, LIGHT)
    win = MainWindow()
    win.resize(1500, 900)
    win.show()
    state = {"t": 0, "phase": 0}

    def done(ok, why=""):
        nonlocal rc
        rc = 0 if ok else 1
        L(f"GUI SELFTEST {'OK' if ok else 'FAILED'} {why}")
        try:
            Path(shot).with_suffix(Path(shot).suffix + ".log").write_text(
                "\n".join(log), encoding="utf-8")
        except Exception:
            pass
        app.exit(rc)

    def tick():
        state["t"] += 1
        if state["t"] > 90:
            done(False, "超时: 轨迹或 3D 窗口未就绪")
            return
        if state["phase"] == 0:
            if win.trajectory:
                L(f"轨迹已加载: {len(win.trajectory)} 帧")
                state["phase"] = 1
                state["t"] = 0
                QTimer.singleShot(4000, tick)
                return
        elif state["phase"] == 1:
            if not getattr(win, "_viewer_ready", False):
                done(False, "3D 窗口未就绪 (_viewer_ready=False)")
                return
            L("3D 窗口已就绪, 等 WebGL 绘制")
            state["phase"] = 2
            state["t"] = 0
            QTimer.singleShot(4000, tick)
            return
        else:
            analyze()
            return
        QTimer.singleShot(1000, tick)

    def analyze():
        try:
            win.pages.setCurrentIndex(0)
            app.processEvents()
            win.grab().save(shot)
            v = win.viewer
            pm = v.grab()
            pm.save(str(Path(shot).with_name(Path(shot).stem + "_3d.png")))
            img = pm.toImage().convertToFormat(QImage.Format_RGB32)
            w, h = img.width(), img.height()
            colors, white, total = {}, 0, 0
            for y in range(0, h, 3):
                for x in range(0, w, 3):
                    c = img.pixel(x, y) & 0xFFFFFF
                    total += 1
                    r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
                    if r > 250 and g > 250 and b > 250:
                        white += 1
                    else:
                        colors[c] = colors.get(c, 0) + 1
            nonwhite = total - white
            frac = nonwhite / max(1, total)
            ncol = len(colors)
            L(f"3D 区域 {w}x{h}, 采样 {total} 点, 非白 {nonwhite} ({frac:.1%}), "
              f"不同颜色 {ncol}")
            ok = frac > 0.02 and ncol > 20
            done(ok, f"非白像素 {frac:.1%}, 颜色数 {ncol}"
                 if ok else "3D 画面几乎是空白")
        except Exception:
            L(traceback.format_exc())
            done(False, "分析截图时出错")

    win.viewer.ready.connect(lambda: L("viewer.ready 信号已收到"))
    win.xdatcar_path = xdatcar
    win.outcar_path = outcar
    QTimer.singleShot(500, win._load_clicked)
    QTimer.singleShot(1500, tick)
    app.exec()
    return rc


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest-gui":
        sys.exit(run_gui_smoke(sys.argv[2:]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        sys.exit(run_selftest(sys.argv[2:]))
    try:
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    except Exception:
        pass
    # Windows 任务栏图标
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "vasp.trajectory.studio.1")
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("VASP 轨迹可视化")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    apply_theme(app, LIGHT)

    ico = BASE_DIR / "assets" / "app.ico"
    if ico.is_file():
        app.setWindowIcon(QIcon(str(ico)))

    def hook(exc_type, exc, tb):
        _log_error("未捕获异常", "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            box = QMessageBox()
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle("运行错误")
            box.setText("程序出现异常，详情见 gui_error.log")
            box.setInformativeText("".join(traceback.format_exception_only(exc_type, exc)))
            box.exec()
        except Exception:
            pass

    sys.excepthook = hook

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        fatal_error("程序异常", traceback.format_exc())
