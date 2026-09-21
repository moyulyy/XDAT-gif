#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
VASP 轨迹 / 收敛信息解析与曲线计算。

本模块把 test/cgv.sh 的 shell/awk 逻辑翻译成 Python:

    cgv.sh                              ->  本模块
    ---------------------------------------------------------------
    awk '/E0/' OSZICAR (第 5 列)         ->  parse_outcar -> energies
    awk '/POSITION/,/drift/' OUTCAR      ->  parse_outcar -> forces
    按 CONTCAR 的 F/T 标记把固定原子置零   ->  read_selective_flags + max_force
    force = sqrt(x^2+y^2+z^2), 取每步最大  ->  force_series(max)
    一阶差分 (delta)                     ->  first_difference

另外还提供 XDATCAR 轨迹相关曲线:
    * 指定原子对的键长 (含周期性最小镜像)
    * 相对首帧的原子位移 RMS
    * 力收敛 / 能量的一阶差分

所有曲线都以「帧索引」为横坐标 (0-based), 便于 GIF 逐帧叠加。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

# --------------------------------------------------------------------------
# 正则
# --------------------------------------------------------------------------
TOTEN_RE = re.compile(r"free\s+energy\s+TOTEN\s*=\s*([-+0-9.eEdD]+)")
TOTEN_RE2 = re.compile(r"energy\(sigma->0\)\s*=\s*([-+0-9.eEdD]+)")
FREE_ENERGIE_RE = re.compile(r"FREE\s+ENERGIE\s+OF\s+THE\s+ION-ELECTRON", re.IGNORECASE)
POSITION_RE = re.compile(r"^\s*POSITION\s+TOTAL-FORCE", re.IGNORECASE)
FLOAT_RE = re.compile(r"^[-+]?\d*\.?\d+(?:[eEdD][-+]?\d+)?$")


def _to_float(token: str) -> float:
    return float(token.replace("D", "E").replace("d", "e"))


# ==========================================================================
# OUTCAR: 能量 + 力
# ==========================================================================
@dataclass
class OutcarData:
    """OUTCAR 中与收敛相关的数据。"""

    energies: np.ndarray = field(default_factory=lambda: np.array([]))
    forces: List[np.ndarray] = field(default_factory=list)      # 每步 (N,3)
    positions: List[np.ndarray] = field(default_factory=list)   # 每步 (N,3)
    n_atoms: int = 0

    # 逐帧标量 (在 finalseries 中填充)
    max_force: np.ndarray = field(default_factory=lambda: np.array([]))
    mean_force: np.ndarray = field(default_factory=lambda: np.array([]))
    rms_force: np.ndarray = field(default_factory=lambda: np.array([]))

    @property
    def n_steps(self) -> int:
        return len(self.forces)


def parse_outcar(path: str | Path,
                 movable_mask: Optional[np.ndarray] = None,
                 progress: Optional[Callable[[int, int], None]] = None,
                 expected_atoms: Optional[int] = None) -> OutcarData:
    """流式解析 OUTCAR。

    能量取 `free  energy   TOTEN  = ... eV` (等价于 OSZICAR 的 E0 行)。
    力取 `POSITION  TOTAL-FORCE` 数据块的最后 3 列。

    movable_mask: True = 该方向可动。可以是 (N,) 或 (N,3) 的 bool 数组。
        与 cgv.sh 一致: 固定方向的分量置 0 (逐分量, 而非整原子置零)。
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到 OUTCAR: {path}")

    energies: List[float] = []
    forces: List[np.ndarray] = []
    positions: List[np.ndarray] = []

    cur_pos: List[Tuple[float, float, float]] = []
    cur_force: List[Tuple[float, float, float]] = []
    in_block = False
    skipped_header = False
    # OUTCAR 里 `free  energy   TOTEN` 每个电子步都会打印; 只有紧跟在
    # `FREE ENERGIE OF THE ION-ELECTRON SYSTEM` 之后的那条才是该离子步能量
    # (等价于 OSZICAR 的 E0 行, 也正是 cgv.sh 使用的量)。
    in_free_energie = False

    total_size = max(path.stat().st_size, 1)
    read_bytes = 0
    n_atoms_hint = expected_atoms or 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            read_bytes += len(line)
            if progress is not None and in_block is False:
                progress(read_bytes, total_size)

            if FREE_ENERGIE_RE.search(line):
                in_free_energie = True
                continue

            if "free" in line and "energy" in line and "TOTEN" in line:
                m = TOTEN_RE.search(line)
                if m and in_free_energie:
                    energies.append(_to_float(m.group(1)))
                    in_free_energie = False
                    continue
                in_free_energie = False

            if POSITION_RE.match(line):
                in_block = True
                skipped_header = False
                cur_pos = []
                cur_force = []
                continue

            if in_block:
                if not skipped_header:
                    skipped_header = True        # 跳过 ---- 分隔线
                    continue
                parts = line.split()
                if len(parts) >= 6 and all(FLOAT_RE.match(p) for p in parts[:6]):
                    vals = [_to_float(p) for p in parts[:6]]
                    cur_pos.append((vals[0], vals[1], vals[2]))
                    cur_force.append((vals[3], vals[4], vals[5]))
                    # 原子数已知时, 收满即结束该块
                    if n_atoms_hint and len(cur_force) >= n_atoms_hint:
                        positions.append(np.asarray(cur_pos, dtype=float))
                        forces.append(np.asarray(cur_force, dtype=float))
                        in_block = False
                    continue
                # 数据结束
                if cur_force:
                    positions.append(np.asarray(cur_pos, dtype=float))
                    forces.append(np.asarray(cur_force, dtype=float))
                    if not n_atoms_hint:
                        n_atoms_hint = len(cur_force)
                in_block = False
                # 该行本身可能又是能量行, 不 continue, 继续走下一轮判断
    # 文件结束时可能仍在块中
    if in_block and cur_force:
        positions.append(np.asarray(cur_pos, dtype=float))
        forces.append(np.asarray(cur_force, dtype=float))

    data = OutcarData(
        energies=np.asarray(energies, dtype=float),
        forces=forces,
        positions=positions,
        n_atoms=int(n_atoms_hint) if n_atoms_hint else (
            len(forces[0]) if forces else 0),
    )

    # 固定方向力置零 (cgv.sh 的 temp.fix/temp.fixx 处理, 逐分量)
    if movable_mask is not None and len(movable_mask) and forces:
        mask = np.asarray(movable_mask, dtype=bool)
        if mask.ndim == 1:
            mask = np.repeat(mask[:, None], 3, axis=1)
        for k, f in enumerate(forces):
            if f.shape == mask.shape:
                forces[k] = f * mask
    _fill_force_stats(data)
    return data


def _fill_force_stats(data: OutcarData) -> None:
    """每步 max / mean / rms 力 (cgv.sh 里 force=sqrt(x^2+y^2+z^2) 取最大)。"""
    max_list, mean_list, rms_list = [], [], []
    for f in data.forces:
        if f is None or len(f) == 0:
            max_list.append(np.nan)
            mean_list.append(np.nan)
            rms_list.append(np.nan)
            continue
        mag = np.sqrt(np.sum(f ** 2, axis=1))
        max_list.append(float(np.max(mag)))
        mean_list.append(float(np.mean(mag)))
        rms_list.append(float(np.sqrt(np.mean(mag ** 2))))
    data.max_force = np.asarray(max_list, dtype=float)
    data.mean_force = np.asarray(mean_list, dtype=float)
    data.rms_force = np.asarray(rms_list, dtype=float)


# ==========================================================================
# OSZICAR: 能量 (cgv.sh 的 awk '/E0/' 数据源)
# ==========================================================================
def parse_oszicar(path: str | Path) -> Tuple[np.ndarray, np.ndarray]:
    """读取 OSZICAR 的离子步能量。

    完全对应 cgv.sh 的:

        awk '/E0/{ ... print $0 }' OSZICAR        # 第 1 列=步号, 第 5 列=E0
        plot 'temp.e' u 1:5                       # 即 E0 (eV)

    返回 (steps, energies)。文件不存在时返回两个空数组。
    """
    path = Path(path)
    if not path.is_file():
        return np.array([], dtype=int), np.array([], dtype=float)

    steps: List[int] = []
    energies: List[float] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "E0" not in line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                step = int(parts[0])
            except ValueError:
                continue
            val: Optional[float] = None
            for k, tok in enumerate(parts):
                if tok.startswith("E0="):
                    raw = (parts[k + 1] if (tok.endswith("=") and k + 1 < len(parts))
                           else tok.split("=", 1)[1])
                    if raw:
                        try:
                            val = _to_float(raw)
                        except ValueError:
                            val = None
                    break
            if val is None:
                # 退化情形: 按 cgv.sh 的 `u 1:5` 取第 5 列
                try:
                    val = _to_float(parts[4])
                except ValueError:
                    continue
            steps.append(step)
            energies.append(val)
    return np.asarray(steps, dtype=int), np.asarray(energies, dtype=float)


# ==========================================================================
# 选择性动力学: 固定原子标记
# ==========================================================================
def read_selective_flags(path: str | Path) -> Optional[np.ndarray]:
    """读取 POSCAR/CONTCAR 的 Selective dynamics 标记。

    返回 (N,3) 的 bool 数组, True = 该原子在该方向上可动 (T)。
    没有 Selective dynamics 时返回 None。

    注意: cgv.sh 是把 `$4=="F"` 的那个分量置零 (逐分量),
    所以这里也保留每个方向单独的 T/F。
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    if len(lines) < 8:
        return None

    idx = 5  # 第 6 行: 元素符号
    idx += 1
    counts = lines[idx].split()
    try:
        counts = [int(c) for c in counts]
    except ValueError:
        return None

    idx += 1
    selective = False
    header = lines[idx].strip().lower()
    if header.startswith("s"):          # Selective dynamics
        selective = True
        idx += 1
    if not selective:
        return None

    total = sum(counts)
    flags: List[List[bool]] = []
    for line in lines[idx + 1: idx + 1 + total]:
        parts = line.split()
        if len(parts) < 6:
            continue
        row = [p.upper().startswith("T") for p in parts[3:6]]
        flags.append(row)
    if len(flags) != total:
        return None
    return np.asarray(flags, dtype=bool)


def find_constraint_file(xdatcar: str | Path, outcar: Optional[str | Path] = None) -> Optional[Path]:
    """在轨迹/OUTCAR 所在目录寻找 CONTCAR / POSCAR 以读取固定原子标记。"""
    base = Path(outcar).parent if outcar else Path(xdatcar).parent
    for name in ("CONTCAR", "POSCAR"):
        p = base / name
        if p.is_file():
            return p
    return None


# ==========================================================================
# XDATCAR 轨迹曲线
# ==========================================================================
def _frac_positions(atoms) -> np.ndarray:
    """笛卡尔 -> 分数坐标 (行向量晶格)。"""
    cell = np.asarray(atoms.get_cell(), dtype=float)
    pos = np.asarray(atoms.get_positions(), dtype=float)
    try:
        inv = np.linalg.inv(cell)
    except np.linalg.LinAlgError:
        return pos
    return pos @ inv


def bond_length_series(frames: Sequence, i: int, j: int, mic: bool = True) -> np.ndarray:
    """逐帧 i-j 键长 (Å)。mic=True 使用周期性最小镜像。"""
    out = []
    for atoms in frames:
        try:
            out.append(float(atoms.get_distance(i, j, mic=mic)))
        except Exception:
            out.append(np.nan)
    return np.asarray(out, dtype=float)


def rms_displacement_series(frames: Sequence, ref: int = 0, mic: bool = True) -> np.ndarray:
    """相对参考帧 (默认首帧) 的原子位移 RMS:

        RMS_k = sqrt( mean_i | r_i^k - r_i^ref |^2 )

    mic=True 时按最小镜像计算, 避免周期性边界跳变。
    """
    n = len(frames)
    if n == 0:
        return np.array([])
    ref = max(0, min(ref, n - 1))
    ref_atoms = frames[ref]
    ref_frac = _frac_positions(ref_atoms) if mic else None
    ref_pos = np.asarray(ref_atoms.get_positions(), dtype=float)

    out = np.zeros(n, dtype=float)
    for k, atoms in enumerate(frames):
        if k == ref:
            out[k] = 0.0
            continue
        if mic:
            frac = _frac_positions(atoms)
            dfrac = frac - ref_frac
            dfrac -= np.round(dfrac)          # 最小镜像
            cell = np.asarray(atoms.get_cell(), dtype=float)
            d = dfrac @ cell
        else:
            d = np.asarray(atoms.get_positions(), dtype=float) - ref_pos
        out[k] = float(np.sqrt(np.mean(np.sum(d ** 2, axis=1))))
    return out


def max_atom_displacement_series(frames: Sequence, ref: int = 0, mic: bool = True) -> np.ndarray:
    """每帧中「位移最大的单个原子」的位移 (Å), 用于参考。"""
    n = len(frames)
    if n == 0:
        return np.array([])
    ref = max(0, min(ref, n - 1))
    ref_frac = _frac_positions(frames[ref]) if mic else None
    ref_pos = np.asarray(frames[ref].get_positions(), dtype=float)
    out = np.zeros(n, dtype=float)
    for k, atoms in enumerate(frames):
        if mic:
            frac = _frac_positions(atoms)
            dfrac = frac - ref_frac
            dfrac -= np.round(dfrac)
            d = dfrac @ np.asarray(atoms.get_cell(), dtype=float)
        else:
            d = np.asarray(atoms.get_positions(), dtype=float) - ref_pos
        out[k] = float(np.max(np.sqrt(np.sum(d ** 2, axis=1)))) if len(d) else 0.0
    return out


def first_difference(y: np.ndarray) -> np.ndarray:
    """一阶差分: d[0]=nan, d[i]=y[i]-y[i-1]。长度与输入一致。"""
    y = np.asarray(y, dtype=float)
    d = np.full_like(y, np.nan)
    if len(y) > 1:
        d[1:] = np.diff(y)
    return d


# ==========================================================================
# 自动检测原子对
# ==========================================================================
def detect_bonds(atoms, max_pairs: int = 12, scale: float = 1.2,
                 min_dist: float = 0.4) -> List[Tuple[int, int, float]]:
    """在给定帧中按共价半径检测化学键, 返回 [(i, j, 距离), ...]。"""
    try:
        from ase.data import covalent_radii
    except Exception:
        covalent_radii = None
    numbers = atoms.get_atomic_numbers()
    dist = atoms.get_all_distances(mic=True)
    n = len(atoms)
    pairs: List[Tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            d = float(dist[i, j])
            if d < min_dist:
                continue
            if covalent_radii is not None:
                thr = scale * (covalent_radii[numbers[i]] + covalent_radii[numbers[j]])
            else:
                thr = 1.9
            if d <= thr:
                pairs.append((i, j, d))
    pairs.sort(key=lambda t: t[2])
    return pairs[:max_pairs]


# ==========================================================================
# 综合结果
# ==========================================================================
@dataclass
class AnalysisResult:
    n_frames: int = 0
    energies: np.ndarray = field(default_factory=lambda: np.array([]))
    energy_diff: np.ndarray = field(default_factory=lambda: np.array([]))
    max_force: np.ndarray = field(default_factory=lambda: np.array([]))
    mean_force: np.ndarray = field(default_factory=lambda: np.array([]))
    force_diff: np.ndarray = field(default_factory=lambda: np.array([]))
    rms: np.ndarray = field(default_factory=lambda: np.array([]))
    max_disp: np.ndarray = field(default_factory=lambda: np.array([]))
    bonds: Dict[str, np.ndarray] = field(default_factory=dict)
    pair_indices: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    has_outcar: bool = False
    has_energy: bool = False
    has_force: bool = False
    energy_source: str = ""
    warnings: List[str] = field(default_factory=list)


def build_analysis(xdatcar: str | Path,
                   outcar: Optional[str | Path] = None,
                   pairs: Optional[Sequence[Tuple[int, int]]] = None,
                   ref_index: int = 0,
                   mic: bool = True,
                   max_frames_hint: Optional[int] = None,
                   progress: Optional[Callable[[str, int, int], None]] = None,
                   frames: Optional[list] = None,
                   ) -> Tuple[AnalysisResult, list]:
    """读取 XDATCAR (+OUTCAR) 并计算全部曲线。

    返回 (AnalysisResult, frames)。frames 为 ASE Atoms 列表, 供复用。
    frames 参数可传入已读取的轨迹, 避免重复读盘。
    """
    from ase.io import read

    def emit(msg, i=0, n=1):
        if progress:
            progress(msg, i, n)

    xdatcar = Path(xdatcar)
    if not xdatcar.is_file():
        raise FileNotFoundError(f"找不到 XDATCAR: {xdatcar}")

    if frames is None:
        emit("读取 XDATCAR 轨迹…")
        frames = read(str(xdatcar), index=":", format="vasp-xdatcar")
    if not isinstance(frames, list):
        frames = [frames]
    if max_frames_hint:
        frames = frames[:max_frames_hint]
    n = len(frames)
    res = AnalysisResult(n_frames=n)
    if n == 0:
        return res, frames

    # ---- 轨迹类曲线 ----
    emit("计算 RMS 位移…")
    res.rms = rms_displacement_series(frames, ref=ref_index, mic=mic)
    res.max_disp = max_atom_displacement_series(frames, ref=ref_index, mic=mic)

    if pairs:
        emit("计算键长曲线…")
        for a, b in pairs:
            key = pair_key(a, b)
            try:
                res.bonds[key] = bond_length_series(frames, a, b, mic=mic)
                res.pair_indices[key] = (int(a), int(b))
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"原子对 {key} 计算失败: {exc}")

    # ---- OUTCAR 类曲线 ----
    if outcar:
        op = Path(outcar)
        if op.is_file():
            emit("解析 OUTCAR 能量与力…")
            # cgv.sh 的能量来自 OSZICAR 的 E0; 有 OSZICAR 时优先使用它
            osz_path = None
            for cand in (op.with_name("OSZICAR"), xdatcar.with_name("OSZICAR")):
                if cand.is_file():
                    osz_path = cand
                    break
            cmask = None
            cfile = find_constraint_file(xdatcar, op)
            if cfile is not None:
                cmask = read_selective_flags(cfile)
                if cmask is not None:
                    n_atoms_movable = int(np.all(cmask, axis=1).sum())
                    emit(f"发现固定原子标记: {cfile.name} "
                         f"(完全可动 {n_atoms_movable}/{len(cmask)} 个原子, "
                         f"逐方向掩码)")
            od = parse_outcar(op, movable_mask=cmask,
                              expected_atoms=len(frames[0]))
            res.has_outcar = True
            if od.n_steps:
                res.max_force = od.max_force
                res.mean_force = od.mean_force
                res.force_diff = first_difference(od.max_force)
                res.has_force = True
            # 能量: 优先 OSZICAR 的 E0 (= cgv.sh), 否则回退 OUTCAR 的 TOTEN
            _, e0 = parse_oszicar(osz_path) if osz_path else (np.array([]), np.array([]))
            if len(e0):
                res.energies = e0
                res.energy_source = f"{osz_path.name}:E0"
                emit(f"能量取自 {osz_path.name} 的 E0 (与 cgv.sh 一致): {len(e0)} 步")
            elif len(od.energies):
                res.energies = od.energies
                res.energy_source = "OUTCAR:TOTEN"
            if len(res.energies):
                res.energy_diff = first_difference(res.energies)
                res.has_energy = True
            # 对齐检查
            if res.has_energy and len(res.energies) != n:
                res.warnings.append(
                    f"OUTCAR 能量步数 ({len(res.energies)}) 与 XDATCAR 帧数 ({n}) 不一致, "
                    f"曲线按较短者显示")
            if res.has_force and len(res.max_force) != n:
                res.warnings.append(
                    f"OUTCAR 力的步数 ({len(res.max_force)}) 与 XDATCAR 帧数 ({n}) 不一致")
        else:
            res.warnings.append(f"找不到 OUTCAR: {op}")
    else:
        res.warnings.append("未指定 OUTCAR, 力/能量曲线不可用")
    return res, frames


def pair_key(i: int, j: int) -> str:
    return f"{int(i)}-{int(j)}"


def frame_value(arr: np.ndarray, i: int) -> Optional[float]:
    """安全取第 i 帧的值, 越界或 nan 返回 None。"""
    if arr is None or i < 0 or i >= len(arr):
        return None
    v = float(arr[i])
    if np.isnan(v) or np.isinf(v):
        return None
    return v


def select_frames(frames: Sequence, index=":", stride: Optional[int] = None,
                  max_frames: Optional[int] = None) -> List[int]:
    """复刻 xdatcar_to_gif_3dmol.load_trajectory 的帧选择逻辑, 返回帧索引列表。"""
    n = len(frames)
    if isinstance(index, int):
        idx = [index % n]
    elif index in (":", None):
        idx = list(range(n))
        if stride and stride > 1:
            idx = idx[::stride]
    else:
        parts = [int(p) if str(p).strip() else None for p in str(index).split(":")]
        idx = list(range(n))[slice(*parts)]
    if max_frames:
        idx = idx[:max_frames]
    return idx


# ==========================================================================
# cgv.sh 风格的控制台收敛报告
# ==========================================================================
def convergence_summary(res: AnalysisResult, n_tail: int = 6) -> str:
    """紧凑的收敛摘要 (不是模拟 cgv.sh 的终端输出, 只给关键数字)。"""
    lines: List[str] = []
    if res.has_force and len(res.max_force):
        f = np.asarray(res.max_force, dtype=float)
        imax = int(np.nanargmax(f))
        lines.append(
            f"最大力   初始 {f[0]:.4f} → 最终 {f[-1]:.4f} eV/Å"
            f"   (峰值 {float(np.nanmax(f)):.4f} @ 第 {imax + 1} 步)")
        tail = f[-n_tail:]
        start = len(f) - len(tail) + 1
        lines.append("最后几步  " + "  ".join(
            f"{start + k}:{v:.4f}" for k, v in enumerate(tail)))
    if res.has_energy and len(res.energies):
        e = np.asarray(res.energies, dtype=float)
        lines.append(
            f"能量     初始 {e[0]:.6f} → 最终 {e[-1]:.6f} eV"
            f"   (Δ = {e[-1] - e[0]:+.6f} eV, 来源 {res.energy_source or 'OUTCAR'})")
    if not lines:
        return "（无 OUTCAR / OSZICAR 数据, 力与能量曲线不可用）"
    lines.append(f"步数     XDATCAR {res.n_frames} 帧"
                 + (f" · 力 {len(res.max_force)} 步" if res.has_force else "")
                 + (f" · 能量 {len(res.energies)} 步" if res.has_energy else ""))
    for w in res.warnings:
        lines.append("⚠ " + w)
    return "\n".join(lines)
