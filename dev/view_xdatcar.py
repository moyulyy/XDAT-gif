#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
用 ASE 的 view() GUI 窗口查看 VASP XDATCAR 轨迹。

运行环境: D:\\miniconda3\\envs\\chem_env
    D:\\miniconda3\\envs\\chem_env\\python.exe view_xdatcar.py

示例:
    python view_xdatcar.py                          # 查看当前目录的 XDATCAR
    python view_xdatcar.py -f MD/XDATCAR            # 指定文件
    python view_xdatcar.py --stride 10              # 每 10 帧取一帧
    python view_xdatcar.py --index -1               # 只看最后一帧
    python view_xdatcar.py --index 0:100:2          # 帧切片
    python view_xdatcar.py --repeat 2 2 1           # 2x2x1 超胞
    python view_xdatcar.py --list                   # 只列出帧数, 不打开 GUI
"""

import argparse
import os
import sys
import traceback

from ase.io import read
from ase.visualize import view


def parse_index(text):
    """把命令行里的 index 参数转成 ASE 能用的形式。

    'all'/'：'-> ':'；'-1' -> 最后一个；'0:100:2' -> 切片；
    '5' -> 第 5 帧。
    """
    text = text.strip()
    if text.lower() in ("all", ":", "::"):
        return ":"
    if ":" in text:
        return text
    return int(text)


def load_trajectory(path, index=":", stride=None, max_frames=None):
    """读取 XDATCAR, 返回 list[ase.Atoms]。"""
    if not os.path.isfile(path):
        raise SystemExit(f"[错误] 找不到文件: {path}")

    # 注意: XDATCAR 必须用 'vasp-xdatcar' 格式, 否则只会读到第一帧
    images = read(path, index=":", format="vasp-xdatcar")
    if not isinstance(images, list):
        images = [images]

    n_total = len(images)
    print(f"[信息] {path}: 共 {n_total} 帧, 每帧 {len(images[0])} 个原子")

    # 先按 --index 选帧
    if isinstance(index, int):
        selected = [images[index]]
    elif index == ":":
        selected = images
    elif ":" in index:
        parts = [int(p) if p.strip() else None for p in index.split(":")]
        selected = images[slice(*parts)]
    else:
        selected = [images[int(index)]]

    # 再按 --stride 抽帧 (仅当显示全部帧时)
    if (isinstance(index, str) and index == ":") and stride and stride > 1:
        selected = selected[::stride]

    if max_frames:
        selected = selected[:max_frames]

    print(f"[信息] 准备显示 {len(selected)} 帧")
    return selected


def main():
    parser = argparse.ArgumentParser(
        description="使用 ASE view() GUI 查看 XDATCAR 轨迹",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-f", "--file", default="XDATCAR",
        help="轨迹文件路径 (默认: XDATCAR)",
    )
    parser.add_argument(
        "-i", "--index", default=":",
        help="显示的帧: ':' 全部(默认), '-1' 最后一帧, '0:100:2' 切片, '5' 第 5 帧",
    )
    parser.add_argument(
        "-s", "--stride", type=int, default=None,
        help="隔多少帧取一帧 (仅在 --index 为全部时生效), 例如 10",
    )
    parser.add_argument(
        "-n", "--max-frames", type=int, default=None,
        help="最多显示多少帧 (防止帧数过多卡顿)",
    )
    parser.add_argument(
        "-r", "--repeat", type=int, nargs=3, metavar=("A", "B", "C"),
        default=None, help="超胞重复, 如 -r 2 2 1",
    )
    parser.add_argument(
        "--viewer", default="ase",
        help="viewer 类型 (默认 'ase', 即 ASE GUI; 也可用 'ngl' 等)",
    )
    parser.add_argument(
        "--block", action="store_true",
        help="阻塞等待, 关闭 GUI 后脚本才退出",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="只打印帧信息, 不打开 GUI",
    )
    args = parser.parse_args()

    index = parse_index(args.index)
    images = load_trajectory(
        args.file, index=index, stride=args.stride, max_frames=args.max_frames
    )

    if args.list:
        print("[信息] --list 模式, 不打开 GUI")
        return

    # 打开 ASE GUI (viewer='ase' 会启动 ASE GUI 窗口/浏览器页面)
    print(f"[信息] 正在用 viewer='{args.viewer}' 打开 GUI ...")
    try:
        handle = view(images, viewer=args.viewer, repeat=args.repeat)
    except Exception:
        traceback.print_exc()
        print(
            "\n[提示] 若 GUI 启动失败, 可尝试:\n"
            "  1) 直接命令行运行:  python -m ase gui " + args.file + "\n"
            "  2) 安装依赖:        pip install flask\n"
            "  3) 指定其他 viewer: --viewer ngl", file=sys.stderr,
        )
        sys.exit(1)

    if args.block and hasattr(handle, "wait"):
        handle.wait()
        print("[信息] GUI 已关闭。")
    else:
        # GUI 是独立子进程, 数据已经在启动时通过管道发送完毕, 这里可以直接退出
        print("[信息] 已启动 GUI 窗口。")


if __name__ == "__main__":
    main()
