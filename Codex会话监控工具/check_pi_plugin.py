#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_pi_plugin.py — 检查 Pi 插件状态（版本、来源、自动重载是否生效）

用途：装完插件后跑一次，确认「运行中的版本」和「磁盘上的版本」是否一致。
若不一致，说明需要重载一次（见输出提示）。

用法：
  python check_pi_plugin.py
"""
import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PI_HOME = Path(os.environ.get("PI_DESKTOP_DATA_DIR", Path.home() / ".pi-desktop"))
REG = PI_HOME / "plugins" / "registry.json"
HEARTBEAT = PI_HOME / "session-keeper" / "plugin.json"
PLUGIN_ID = "local.session-keeper"


def main():
    print("=" * 66)
    print("Pi 插件状态检查")
    print("=" * 66)

    # 1) 注册表信息
    entry = None
    if REG.exists():
        try:
            for e in json.loads(REG.read_text(encoding="utf-8")):
                if e.get("id") == PLUGIN_ID:
                    entry = e
                    break
        except Exception as ex:
            print(f"注册表读取失败：{ex!r}")
    if entry is None:
        print(f"\n✗ 未在注册表中找到 {PLUGIN_ID}")
        print("  → 请先执行 python install_pi_plugin.py")
        return 1

    src = entry.get("source")
    path = Path(str(entry.get("path") or ""))
    print(f"\n插件目录 : {path}")
    print(f"来源     : {src}"
          f"{'  （支持文件改动自动重载）' if src == 'dev' else '  （改文件不会生效，需重装）'}")

    # 2) 磁盘上的版本
    disk_ver = "?"
    mf = path / "manifest.json"
    if mf.exists():
        try:
            disk_ver = json.loads(mf.read_text(encoding="utf-8")).get("version", "?")
        except Exception:
            pass
    print(f"磁盘版本 : {disk_ver}")

    # 3) 运行中的版本（心跳）
    print()
    if not HEARTBEAT.exists():
        print("✗ 没有心跳文件 → 插件未被 Pi 加载")
        print("  → 打开 Pi → 左侧栏底部「扩展」→ 加载本地插件 / 安装插件包")
        return 2

    try:
        hb = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"✗ 心跳无法解析：{ex!r}")
        return 2

    run_ver = hb.get("version", "?")
    age = (time.time() * 1000 - (hb.get("at") or 0)) / 1000
    print(f"运行版本 : {run_ver}")
    print(f"心跳年龄 : {age:.0f} 秒"
          f"{'  （正常）' if age < 30 else '  （过期，Pi 可能没运行）'}")
    print(f"执行器   : {'已启用' if hb.get('enabled') else '已关闭'}"
          f" / {'运行中' if hb.get('running', True) else '已停止'}")
    if hb.get("lastError"):
        print(f"最近错误 : {hb['lastError']}")

    # 4) 结论
    print()
    print("=" * 66)
    if age > 30:
        print("⚠ Pi 似乎没有运行（心跳过期）。请先打开 PI-Desktop。")
    elif run_ver == disk_ver:
        print(f"✓ 一切正常：插件 {run_ver} 已加载并运行。")
        if src == "dev":
            print("  已启用自动重载 —— 以后改代码无需任何操作。")
    else:
        print(f"⚠ 版本不一致：磁盘 {disk_ver}，运行中 {run_ver}")
        print()
        print("  需要在 Pi 里重载一次：")
        print("    左侧栏底部「扩展」→ 找到「会话续跑」→ 点「重载插件」")
        print()
        print("  重载后本脚本再跑一次，看到「✓ 一切正常」即可。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
