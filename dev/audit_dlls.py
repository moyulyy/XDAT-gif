"""审计 PyInstaller 分析结果: 哪些 DLL 来自 base conda 而不是 chem_env (版本可能不匹配)。"""
import collections
import pathlib
import sys

BS = chr(92)
toc = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                   else "build/VASP_Trajectory/Analysis-00.toc")
s = toc.read_text(encoding="utf-8", errors="replace")
env, base, other = [], [], []
for chunk in s.split("'"):
    if "miniconda3" not in chunk:
        continue
    p = chunk.replace(BS * 2, BS)
    if "envs" + BS + "chem_env" in p:
        env.append(p)
    elif p.lower().startswith("d:" + BS + "miniconda3"):
        base.append(p)
    else:
        other.append(p)
print("env   :", len(set(env)))
print("base  :", len(set(base)))
print("other :", len(set(other)))
print()
print("== 来自 base 的文件 (可能是错的版本) ==")
for n, c in sorted(collections.Counter(p.split(BS)[-1] for p in base).items()):
    print(f"   {n}  x{c}")
print()
print("== 其中带 Library 的完整路径 ==")
for p in sorted(set(base)):
    if BS + "Library" + BS in p:
        print("   ", p)
