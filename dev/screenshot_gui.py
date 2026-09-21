# -*- coding: utf-8 -*-
"""给 README 截几张界面图 (轨迹浏览 / 分析曲线 / GIF 设置)。

用法:
    python dev\\screenshot_gui.py <XDATCAR> <OUTCAR> <输出目录>

会输出 gui_trajectory.png / gui_analysis.png / gui_gif.png。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer                      # noqa: E402
from PySide6.QtWidgets import QApplication             # noqa: E402

import xdatcar_gif_gui as g                            # noqa: E402


def main() -> int:
    xdatcar, outcar = sys.argv[1], sys.argv[2]
    outdir = Path(sys.argv[3])
    outdir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName("README 截图")
    g.apply_theme(app, g.LIGHT)
    win = g.MainWindow()
    win.resize(1500, 900)
    win.show()

    state = {"t": 0, "phase": 0}

    def tick():
        state["t"] += 1
        if state["t"] > 90:
            app.exit(1)
            return
        if state["phase"] == 0:
            if win.trajectory:
                state["phase"], state["t"] = 1, 0
                QTimer.singleShot(4000, tick)
                return
        elif state["phase"] == 1:
            if not getattr(win, "_viewer_ready", False):
                app.exit(1)
                return
            state["phase"], state["t"] = 2, 0
            QTimer.singleShot(4000, tick)
            return
        else:
            shoot()
            return
        QTimer.singleShot(1000, tick)

    def shoot():
        for idx, name in ((0, "gui_trajectory.png"),
                          (1, "gui_analysis.png"),
                          (2, "gui_gif.png")):
            win.pages.setCurrentIndex(idx)
            app.processEvents()
            win.grab().save(str(outdir / name))
            print("saved", outdir / name)
        app.exit(0)

    win.xdatcar_path = xdatcar
    win.outcar_path = outcar
    QTimer.singleShot(500, win._load_clicked)
    QTimer.singleShot(1500, tick)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
