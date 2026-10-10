#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_pi_plugin.py — 一键把插件更新到 Pi 里，并检查是否需要重载

背景：
  Pi 的插件注册表由 Pi 自己管理。通过插件页安装的插件（source=installed）
  不会监听文件改动，所以更新代码后需要让 Pi 重新加载一次。

本脚本做三件事：
  1. 把插件源码同步到 Pi 的插件目录
  2. 更新注册表里的版本号
  3. 检查运行版本是否一致，并给出最短操作

用法：
  python update_pi_plugin.py            # 同步 + 检查
  python update_pi_plugin.py --status   # 只看状态
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = Path.home()
PI_HOME = Path(os.environ.get("PI_DESKTOP_DATA_DIR", HOME / ".pi-desktop"))
PLUGIN_ID = "local.session-keeper"
REG = PI_HOME / "plugins" / "registry.json"
DEST = PI_HOME / "plugins" / "installed" / PLUGIN_ID
HEARTBEAT = PI_HOME / "session-keeper" / "plugin.json"

HERE = Path(__file__).resolve().parent
# 源码可能在两处：开发目录 pi-session-keeper/ 或交付目录 Pi插件/
SOURCE = HERE / "pi-session-keeper"
if not SOURCE.exists():
    SOURCE = HERE / "Pi插件" / PLUGIN_ID


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def show_status():
    """打印状态，返回 (磁盘版本, 运行版本, 是否需要重载)。"""
    print("=" * 66)
    print("Pi 插件状态")
    print("=" * 66)

    entry = None
    for e in (read_json(REG, []) or []):
        if e.get("id") == PLUGIN_ID:
            entry = e
            break
    if entry is None:
        print(f"\n✗ 注册表里没有 {PLUGIN_ID} → 请先安装")
        return None, None, True

    disk_ver = (read_json(DEST / "manifest.json", {}) or {}).get("version", "?")
    print(f"\n插件目录   : {DEST}")
    print(f"来源       : {entry.get('source')}")
    print(f"注册表版本 : {entry.get('version', '?')}")
    print(f"磁盘版本   : {disk_ver}")

    run_ver, age = None, None
    if HEARTBEAT.exists():
        hb = read_json(HEARTBEAT, {}) or {}
        run_ver = hb.get("version")
        age = (time.time() * 1000 - (hb.get("at") or 0)) / 1000
        print(f"运行版本   : {run_ver}")
        print(f"心跳年龄   : {age:.0f} 秒"
              f"{'  （正常）' if age < 30 else '  （过期，Pi 可能没运行）'}")
    else:
        print("运行版本   : 无心跳 → 插件未被 Pi 加载")

    need_reload = (run_ver != disk_ver)
    print()
    if age is not None and age > 30:
        print("⚠ Pi 似乎没运行，请先打开 PI-Desktop")
    elif not need_reload:
        print(f"✓ 已是最新：{run_ver} 正在运行")
    else:
        print(f"⚠ 需要重载：运行中 {run_ver}，磁盘 {disk_ver}")
    print("=" * 66)
    return disk_ver, run_ver, need_reload


def sync():
    """把源码同步到 Pi 插件目录，并更新注册表版本。"""
    if not SOURCE.exists():
        print(f"✗ 找不到插件源码：{SOURCE}")
        print("  请把本脚本放在含 pi-session-keeper/ 或 Pi插件/ 的目录下运行。")
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    copied = []
    for rel in ("manifest.json", "main.js", "renderer/index.html"):
        src = SOURCE / rel
        if not src.exists():
            continue
        dst = DEST / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    print(f"已同步 {len(copied)} 个文件 → {DEST}")
    for c in copied:
        print(f"   · {c}")

    # 更新注册表版本（source 由 Pi 自己管理，不动它）
    ver = (read_json(DEST / "manifest.json", {}) or {}).get("version", "?")
    entries = read_json(REG, [])
    if isinstance(entries, list):
        shutil.copy2(REG, REG.with_name("registry.json.bak-update"))
        for e in entries:
            if e.get("id") == PLUGIN_ID:
                e["version"] = ver
                e["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        REG.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"注册表版本已更新为 {ver}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="更新 Pi「会话续跑」插件")
    ap.add_argument("--status", action="store_true", help="只查看状态，不同步")
    args = ap.parse_args()

    if not args.status:
        rc = sync()
        if rc:
            return rc
        print()

    _disk, _run, need = show_status()
    if need:
        print()
        print("还需要在 Pi 里做一步（Pi 不会自动重载已安装的插件）：")
        print()
        print("  已安装（installed 来源）的插件没有「重载」按钮，")
        print("  但「安装插件包」会覆盖安装并立即重新加载。做法：")
        print()
        print("    1. 打开 PI-Desktop → 左侧栏底部「扩展」")
        print("    2. 右上角「更多操作 ⋯」→「安装插件包」")
        print(f"    3. 选中这个文件：{HERE / 'local.session-keeper-1.9.2.piplug'}")
        print("       （若根目录没有，用 pi-session-keeper/dist/ 下的同名文件）")
        print("    4. 权限确认页三项全部允许")
        print()
        print("  装完运行 python update_pi_plugin.py --status 确认版本已一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
