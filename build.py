#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包：Windows 出三个 exe、两个平台的源码归位，最后拼出分发包 zip。

为什么要有这个脚本：PyInstaller 的参数（onedir、内容目录名 `_pi`/`_codex`/`_gui`、
哪个 exe 带 `--windowed`）以前只存在于「上次是怎么打的」这种口头知识里，换台机器
就丢。这里固化成一条命令，CI（.github/workflows/build.yml）也直接调它。

分发包结构（与《使用说明》里的路径一致）：

    Codex会话监控工具/
      使用说明.txt、check/install/update_pi_plugin.py、local.session-keeper-*.piplug
      Mac/       ← 根目录源码 + 启动.command（Mac 上直接跑，不需要编译）
      Windows/   ← PyInstaller 打出来的 exe + _pi/_codex/_gui
      Pi插件/local.session-keeper/   ← pi-session-keeper/ 的源码副本

源码只认根目录这一份：`Codex会话监控工具/` 里的 .py 与 使用说明.txt 都是本脚本
从根目录复制过去的产物，不要再手工改那边。

用法：
    python build.py             # 编译 exe（仅 Windows）+ 归位源码 + 打 zip
    python build.py --no-exe    # 跳过编译，只归位源码 + 打 zip（Mac/Linux 上也能跑）
    python build.py --out DIR   # 换输出目录（默认 Codex会话监控工具/）
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "Codex会话监控工具"
ZIP_NAME = "Codex会话监控工具.zip"

# 只留在本地、不进分发包的顶层目录：上一版 onefile exe 的备份（几十 MB，仅回退用）
SKIP_IN_ZIP = {"_旧版onefile备份"}

# 要打的三个 exe：(名字, 入口脚本, 内容目录名, 是否 --windowed)
# 内容目录名沿用现版布局；GUI 是纯窗口程序（无控制台），另两个要控制台看日志。
APPS = [
    ("pi_monitor", "pi_monitor.py", "_pi", False),
    ("codex_monitor", "codex_monitor.py", "_codex", False),
    ("codex_monitor_gui", "codex_monitor_gui.py", "_gui", True),
]

# Mac 包里的源码 = 根目录这几份（沿用现版 Mac 目录的文件集，不多不少）
MAC_SOURCES = [
    "codex_monitor.py", "codex_monitor_gui.py", "pi_monitor.py", "pi_archive.py",
    "codex_activity.py", "shutdown_coord.py", "check_pi_plugin.py", "update_pi_plugin.py",
]

# 分发包根目录里的文件
PKG_FILES = ["使用说明.txt", "check_pi_plugin.py", "install_pi_plugin.py",
             "update_pi_plugin.py"]

PLUGIN_SRC = ROOT / "pi-session-keeper"
PLUGIN_DST_REL = Path("Pi插件") / "local.session-keeper"


def log(msg):
    print(msg, flush=True)


def build_exes(out_dir):
    """用 PyInstaller 打三个 exe，按现版布局摊到 <out>/Windows/ 下。"""
    if os.name != "nt":
        raise SystemExit("打包 exe 只能在 Windows 上做（PyInstaller 不支持交叉编译）；"
                         "只想归位源码+打 zip 请加 --no-exe")
    win = out_dir / "Windows"
    win.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pyi-build-"))
    try:
        for name, script, contents, windowed in APPS:
            cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                   "--distpath", str(work / "dist"), "--workpath", str(work / "build"),
                   "--specpath", str(work), "--contents-directory", contents,
                   "--name", name]
            if windowed:
                cmd.append("--windowed")
            cmd.append(str(ROOT / script))
            log(f"  · 编译 {name}.exe{'（--windowed）' if windowed else ''}")
            subprocess.run(cmd, cwd=str(ROOT), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            built = work / "dist" / name
            shutil.copy2(built / f"{name}.exe", win / f"{name}.exe")
            dst = win / contents
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(built / contents, dst)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log(f"  · exe 已就位：{win}")


def sync_sources(out_dir):
    """把根目录源码复制进分发包（Mac/、包根、插件源码）。"""
    mac = out_dir / "Mac"
    mac.mkdir(parents=True, exist_ok=True)
    for name in MAC_SOURCES:
        shutil.copy2(ROOT / name, mac / name)
    for name in PKG_FILES:
        shutil.copy2(ROOT / name, out_dir / name)
    for piplug in sorted(ROOT.glob("local.session-keeper-*.piplug")):
        shutil.copy2(piplug, out_dir / piplug.name)
    dst = out_dir / PLUGIN_DST_REL
    if dst.exists():
        shutil.rmtree(dst)
    if PLUGIN_SRC.is_dir():
        shutil.copytree(PLUGIN_SRC, dst,
                        ignore=shutil.ignore_patterns("dist", "__pycache__"))
    log(f"  · 源码已归位：{out_dir}")


def make_zip(out_dir, zip_path):
    """打分发包 zip：条目名与现版一致（zip 根下直接是 Windows/、Mac/、文档）。"""
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out_dir.rglob("*")):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
                continue
            if p.name == ZIP_NAME:
                continue
            rel = p.relative_to(out_dir)
            if rel.parts and rel.parts[0] in SKIP_IN_ZIP:
                continue
            z.write(p, rel)
            n += 1
    size = zip_path.stat().st_size / 1048576
    log(f"  · 分发包：{zip_path}（{n} 个文件，{size:.1f} MB）")


def main():
    ap = argparse.ArgumentParser(description="打包 Codex/Pi 会话监控工具")
    ap.add_argument("--no-exe", action="store_true",
                    help="跳过 PyInstaller（非 Windows 上只能这样）")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="分发包目录（默认 Codex会话监控工具/）")
    ap.add_argument("--no-zip", action="store_true", help="不打 zip")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"打包目录：{out_dir}")

    if args.no_exe:
        log("  · 跳过 exe 编译（--no-exe）")
    else:
        build_exes(out_dir)
    sync_sources(out_dir)

    if not args.no_zip:
        make_zip(out_dir, out_dir.parent / ZIP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
