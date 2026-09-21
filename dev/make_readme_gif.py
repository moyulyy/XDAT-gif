# -*- coding: utf-8 -*-
"""生成 README 首页演示动图 (docs/demo.gif)。

内容:
  * 用 samples/XDATCAR + samples/OUTCAR 的弛豫轨迹逐帧渲染 3D 结构;
  * 相机绕屏幕竖直轴匀速转一圈, 展示 3D 效果;
  * 自动裁掉晶胞真空带来的大片留白, 让结构充满画面;
  * 每帧叠加「最大力 / 能量」数值, 底部拼接收敛曲线底板并同步打点。

换样例数据后重新运行本脚本即可更新 README 动图:

    D:\\miniconda3\\envs\\chem_env\\python.exe dev\\make_readme_gif.py
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gif_overlay as go                    # noqa: E402
import vasp_parser as vp                    # noqa: E402
import xdatcar_to_gif_3dmol as core         # noqa: E402
from vasp_parser import build_analysis      # noqa: E402

XDATCAR = ROOT / "samples" / "XDATCAR"
OUTCAR = ROOT / "samples" / "OUTCAR"
OUT = ROOT / "docs" / "demo.gif"

RENDER_W = 640        # 渲染 CSS 边长 (物理像素通常为其 2 倍)
FPS = 12              # GIF 帧率
COLORS = 96           # GIF 调色板颜色数
FINAL_WIDTH = 680     # 输出 GIF 宽度
MARGIN = 24           # 裁剪留白 (源像素, 左右/底部)
MARGIN_TOP = 170      # 顶部多留白, 给左上角数值标注让位, 不遮挡水分子


def build_args() -> types.SimpleNamespace:
    """渲染参数: 正视图 + 小幅上移 + 绕屏幕竖直轴转一圈。"""
    args = types.SimpleNamespace(
        style="ballstick", radius=None, radius_scale=0.45,
        vesta=None, use_vesta=True, no_vesta_file=True,
        cell=False, cell_color="#888888", bg="white",
        width=RENDER_W, height=RENDER_W, scale=1.0, zoom=1.15,
        view="front", rot=None, spin=0.0,
        rot_axis="screen-v", rot_total=360.0,
        pan_x=0.0, pan_y=0.30,
        browser=None, show_browser=False,
        views=None, view_start=None, radius_overrides=None,
    )
    core.resolve_vesta(args)
    return args


def content_bbox(paths, margin: int = MARGIN, margin_top: int = 0):
    """所有帧非白像素的并集包围盒 (裁掉真空留白)。"""
    xs0 = ys0 = 10 ** 9
    xs1 = ys1 = -1
    for p in paths:
        im = Image.open(p).convert("RGB")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        diff = ImageChops.difference(im, bg).convert("L")
        box = diff.point(lambda v: 255 if v > 8 else 0).getbbox()
        if box:
            xs0, ys0 = min(xs0, box[0]), min(ys0, box[1])
            xs1, ys1 = max(xs1, box[2]), max(ys1, box[3])
    w, h = Image.open(paths[0]).size
    return (max(0, xs0 - margin), max(0, ys0 - margin_top),
            min(w, xs1 + margin), min(h, ys1 + margin))


def main() -> int:
    if not XDATCAR.is_file():
        raise SystemExit(f"[错误] 缺少样例文件: {XDATCAR}")
    outcar = str(OUTCAR) if OUTCAR.is_file() else None
    res, frames = build_analysis(str(XDATCAR), outcar, pairs=[(0, 1)])
    n = len(frames)
    print(f"[信息] 轨迹 {n} 帧, 元素 {frames[0].get_chemical_formula()}, "
          f"force={res.has_force} energy={res.has_energy}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    args = build_args()
    tmp = Path(tempfile.mkdtemp(prefix="readme-gif-"))
    try:
        paths = core.render_pngs(frames, args, tmp)
        box = content_bbox(paths, MARGIN, MARGIN_TOP)
        print(f"[信息] 裁剪区域 {box}")

        cropdir = tmp / "crop"
        cropdir.mkdir()
        cpaths = []
        for i, p in enumerate(paths):
            im = Image.open(p).convert("RGB").crop(box)
            cp = cropdir / f"crop_{i:05d}.png"
            im.save(cp)
            cpaths.append(cp)
        cw = Image.open(cpaths[0]).width

        # ---- 曲线底板: 最大力 (红) + 能量 (蓝) ----
        specs = [go.CurveSpec("force", "最大力 max|F|", np.arange(n),
                              np.asarray(res.max_force, dtype=float),
                              go.PANEL_COLORS[1], "eV/Å")]
        if res.has_energy:
            e = np.asarray(res.energies, dtype=float)
            specs.append(go.CurveSpec("energy", "能量 E (相对最低)",
                                      np.arange(n), e - np.nanmin(e),
                                      go.PANEL_COLORS[0], "eV"))
        panel = go.CurvePanel(specs, width=cw,
                              height=int(round(cw * 0.30)),
                              dpi=200, max_cols=len(specs), theme="light")
        ann = go.FrameAnnotator(panel=panel, show_numbers=True,
                                position="bottom", theme="light",
                                numbers_corner="top-left")
        colors = {"force": go.PANEL_COLORS[1], "energy": go.PANEL_COLORS[0]}

        def annotate(k: int, im: Image.Image) -> Image.Image:
            lines = go.format_frame_lines(
                k, n,
                vp.frame_value(res.max_force, k),
                vp.frame_value(res.energies, k),
                vp.frame_value(res.rms, k),
                show_force=True, show_energy=res.has_energy,
                show_rms=False, colors=colors)
            return ann.annotate(im, k, lines)

        frames_n = core.build_gif(cpaths, str(OUT),
                                  duration=max(1, round(1000 / FPS)),
                                  loop=0, colors=COLORS, pingpong=False,
                                  width=FINAL_WIDTH, annotate=annotate)
        size = OUT.stat().st_size / 1e6
        print(f"[完成] {OUT}  ({frames_n} 帧, {FPS} fps, {size:.2f} MB)")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
