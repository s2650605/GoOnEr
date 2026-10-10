#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install_pi_plugin.py — 把「会话续跑」插件装进 PI-Desktop（无需重启 Pi）。

背景：
  Pi 的插件注册表由 host-core 在启动时读取，直接改 registry.json 不会热加载。
  官方允许的加载入口是「开发插件」通道（plugin/loadDev），它由 Pi 的插件页面
  触发。本脚本负责把插件文件放到 Pi 认得的目录，并给出最短的操作指引。

用法：
  python install_pi_plugin.py            # 安装/更新插件文件
  python install_pi_plugin.py --check    # 只检查当前安装与运行状态
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
PLUGINS = PI_HOME / "plugins"
INSTALLED = PLUGINS / "installed"
REGISTRY = PLUGINS / "registry.json"
HEARTBEAT = PI_HOME / "session-keeper" / "plugin.json"
PLUGIN_ID = "local.session-keeper"

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "pi-session-keeper"


def check():
    """打印当前安装与运行状态。"""
    print(f"Pi 数据目录：{PI_HOME}")
    print(f"插件源目录  ：{SOURCE}  ({'存在' if SOURCE.exists() else '缺失'})")
    dest = INSTALLED / PLUGIN_ID
    print(f"已安装目录  ：{dest}  ({'存在' if dest.exists() else '缺失'})")

    reg_ok = False
    if REGISTRY.exists():
        try:
            entries = json.loads(REGISTRY.read_text(encoding="utf-8"))
            reg_ok = any(e.get("id") == PLUGIN_ID for e in entries)
        except Exception:
            pass
    print(f"注册表登记  ：{'已登记' if reg_ok else '未登记'}")

    if HEARTBEAT.exists():
        try:
            hb = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
            age = time.time() * 1000 - (hb.get("at") or 0)
            if age > 30_000:
                print(f"执行器心跳  ：已过期（{age / 1000:.0f} 秒前），Pi 可能没在运行")
            else:
                print(f"执行器心跳  ：正常（插件 v{hb.get('version')}，PID {hb.get('pid')}，"
                      f"enabled={hb.get('enabled')}）")
        except Exception as e:
            print(f"执行器心跳  ：无法解析（{e!r}）")
    else:
        print("执行器心跳  ：不存在 → 插件尚未被 Pi 加载")
    return reg_ok


def install():
    """把插件文件复制到 Pi 的插件目录，并登记到注册表。"""
    if not SOURCE.exists():
        print(f"找不到插件源目录：{SOURCE}")
        print("请在与本脚本同级的 pi-session-keeper/ 目录下运行。")
        return 1

    dest = INSTALLED / PLUGIN_ID
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SOURCE, dest)
    print(f"已复制插件文件 → {dest}")

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    entries = []
    if REGISTRY.exists():
        try:
            entries = json.loads(REGISTRY.read_text(encoding="utf-8"))
        except Exception:
            entries = []
        if not isinstance(entries, list):
            entries = []
        # 备份一次，方便回退
        shutil.copy2(REGISTRY, REGISTRY.with_suffix(".json.bak-session-keeper"))

    entry = {
        "id": PLUGIN_ID,
        "name": manifest["name"],
        "version": manifest["version"],
        "enabled": True,
        "scope": {"mode": "global", "projects": []},
        "source": "dev",
        "bundled": False,
        "status": "ready",
        "permissions": manifest["permissions"],
        "path": str(dest),
        "capabilities": ["panel", "commands", "services"],
        "description": manifest["description"],
        "autoUpdate": False,
        "ui": {
            "panel": manifest["ui"]["panel"],
            "width": manifest["ui"]["width"],
            "height": manifest["ui"]["height"],
            "title": manifest["ui"]["title"],
            "resizable": manifest["ui"]["resizable"],
        },
    }
    replaced = False
    for i, item in enumerate(entries):
        if isinstance(item, dict) and item.get("id") == PLUGIN_ID:
            entries[i] = {**item, **entry}
            replaced = True
            break
    if not replaced:
        entries.append(entry)
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已在注册表{'更新' if replaced else '新增'}登记：{PLUGIN_ID}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="安装/检查 Pi「会话续跑」插件")
    ap.add_argument("--check", action="store_true", help="只检查状态，不安装")
    args = ap.parse_args()

    if not args.check:
        rc = install()
        if rc:
            return rc
        print()

    reg_ok = check()
    print()
    if reg_ok and HEARTBEAT.exists():
        print("插件已就绪，可以直接用监控控制台监控 Pi 会话。")
        return 0

    print("=" * 68)
    print("还需要在 PI-Desktop 里做一步（Pi 不会热加载注册表）：")
    print("=" * 68)
    print(f"  1. 打开 PI-Desktop → 插件页（插件中心 / Plugins）")
    print(f"  2. 选择「从文件夹加载开发插件」（Load dev plugin from folder）")
    print(f"  3. 选择这个目录：")
    print(f"       {INSTALLED / PLUGIN_ID}")
    print(f"  4. 权限确认页里勾选全部三项（缺 desktop.control 就无法发送）：")
    print(f"       ui.panel / background.service / desktop.control")
    print()
    print("  装好后本脚本再跑一次 `python install_pi_plugin.py --check`，")
    print("  看到「执行器心跳：正常」即表示通道已通。")
    print()
    print("  提示：也可以用控制台里的“模拟运行”先只做检测，不影响任何会话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
