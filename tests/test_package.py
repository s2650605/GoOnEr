# -*- coding: utf-8 -*-
"""检查打包产物是否完整（先跑 python build.py，再跑这个）。

为什么单独放个文件、而不是把检查写进工作流里：Git Bash 传中文路径容易出问题，
Python 在 Windows 上原生支持 Unicode 路径；而且放成文件本机就能先跑一遍，
不必等 CI 才知道检查本身写错了。

用法：
    python build.py
    python tests/test_package.py
"""
import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import build as build_mod  # noqa: E402  （复用它的清单，避免两处口径漂移）

PKG = ROOT / "Codex会话监控工具"
ZIP = ROOT / "Codex会话监控工具.zip"
WIN = PKG / "Windows"

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


def subsystem(exe):
    """读 PE 头的子系统：3=控制台，2=纯窗口。"""
    with open(exe, "rb") as f:
        head = f.read(0x400)
    e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
    if head[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", head, e_lfanew + 24 + 68)[0]


print("=" * 70)
print("1. 分发包目录与 exe")
check("分发包目录存在", PKG.is_dir(), str(PKG))
for name, _script, contents, _windowed in build_mod.APPS:
    check(f"{name}.exe 存在", (WIN / f"{name}.exe").is_file())
    check(f"{name} 的内容目录 {contents}/ 存在", (WIN / contents).is_dir())

print()
print("=" * 70)
print("2. exe 真能跑起来")
if os.name == "nt":
    for name, _script, _contents, windowed in build_mod.APPS:
        exe = WIN / f"{name}.exe"
        if not exe.is_file():
            continue
        if windowed:
            # 窗口程序跑起来会弹窗抢焦点，只静态看子系统：必须是 2（无控制台）
            check(f"{name}.exe 是窗口程序（子系统=2）", subsystem(exe) == 2,
                  f"subsystem={subsystem(exe)}")
            continue
        # exe 里的 Python 强制 stdout 用 UTF-8，这里也要按 UTF-8 解，
        # 不能用默认的本地编码（GBK），否则读取线程会直接抛 UnicodeDecodeError
        proc = subprocess.run([str(exe), "--help"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        check(f"{name}.exe --help 正常", proc.returncode == 0 and "usage" in out.lower(),
              f"exit={proc.returncode}")
else:
    print("  （非 Windows：跳过 exe 运行检查）")

print()
print("=" * 70)
print("3. Mac 目录的文件集与 build.py 声明一致")
mac = PKG / "Mac"
for name in build_mod.MAC_SOURCES:
    check(f"Mac/{name} 在", (mac / name).is_file())
check("Mac/启动.command 在", (mac / "启动.command").is_file())
extra = sorted(p.name for p in mac.glob("*.py")
               if p.name not in build_mod.MAC_SOURCES)
check("Mac/ 没有多余脚本", not extra, str(extra))

print()
print("=" * 70)
print("4. 分发包 zip 结构")
if not ZIP.is_file():
    check("zip 存在", False, str(ZIP))
else:
    with zipfile.ZipFile(ZIP) as z:
        names = set(z.namelist())
    need = ([f"Windows/{n}.exe" for n, _s, _c, _w in build_mod.APPS]
            + [f"Windows/{c}" for _n, _s, c, _w in build_mod.APPS]
            + [f"Mac/{n}" for n in build_mod.MAC_SOURCES]
            + ["Mac/启动.command", "使用说明.txt"] + build_mod.PKG_FILES)
    missing = [n for n in need
               if n not in names and not any(x.startswith(n + "/") for x in names)]
    check("zip 里该有的都在", not missing, f"缺 {missing}")
    check("旧 exe 备份没混进 zip",
          not any(n.startswith("_旧版onefile备份") for n in names))
    check("没混进 __pycache__", not any("__pycache__" in n for n in names))
    print(f"  （zip 条目 {len(names)} 个）")

print()
print("=" * 70)
if FAIL:
    print(f"有 {len(FAIL)} 项未通过：")
    for name in FAIL:
        print("  ✗ " + name)
    sys.exit(1)
print("打包产物检查全部通过")
