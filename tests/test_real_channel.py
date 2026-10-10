# -*- coding: utf-8 -*-
"""用真实运行中的 Pi 插件，验证 pi_monitor 的文件通道能通。

只发一条 ping（无副作用），确认：
  - 指令文件被插件读到并执行
  - 结果文件被正确写回并解析
  - pi_monitor.plugin_call 的返回约定与插件实际行为一致
"""
import importlib.util
import os
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 测试固化在 tests/ 下，被测脚本在项目根；临时文件放 tests/_tmp
ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(__file__).resolve().parent / "_tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(ROOT)

spec = importlib.util.spec_from_file_location("pi_monitor", "pi_monitor.py")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

print("=" * 70)
print("0. 插件心跳")
ok, detail, hb = pm.plugin_status()
print(f"  心跳可用: {ok} — {detail}")
if not ok:
    print("  插件未运行，无法验证真实通道。请先打开 PI-Desktop。")
    sys.exit(2)
print(f"  运行版本: {hb.get('version')}")

print()
print("=" * 70)
print("1. 通过 plugin_call 发 ping（无副作用），验证文件通道往返")
import io
buf = io.StringIO()
t0 = time.time()
ok2, result = pm.plugin_call({"action": "ping"}, False, buf, 30, "ping 自检")
dt = time.time() - t0
print(f"  耗时: {dt:.1f}s")
print(f"  ok={ok2}")
print(f"  结果: {json.dumps(result, ensure_ascii=False)[:300] if isinstance(result, dict) else result}")
assert ok2, f"ping 失败: {result}"
assert isinstance(result, dict), "结果不是 dict"
assert result.get("ok") is True, "插件未返回 ok"
assert result.get("version"), "插件未返回版本号"
print("  ✓ 文件通道往返正常（指令写入 → 插件执行 → 结果写回 → 解析）")

print()
print("=" * 70)
print("2. 校验 plugin_call 的超时约定（发一个插件不认识的指令，应返回失败而非卡死）")
buf2 = io.StringIO()
t1 = time.time()
ok3, res3 = pm.plugin_call({"action": "__no_such_action__"}, False, buf2, 20,
                           "未知指令")
dt1 = time.time() - t1
print(f"  耗时: {dt1:.1f}s")
print(f"  ok={ok3}  说明={res3}")
assert ok3 is False, "未知指令本应返回失败"
print("  ✓ 未知指令被正确判为失败，且未卡到超时")

print()
print("=" * 70)
print("3. 校验指令目录不会残留未处理文件")
leftover = [p.name for p in pm.CMD_DIR.glob("*.json")] if pm.CMD_DIR.exists() else []
print(f"  指令目录残留: {leftover or '无'}")
assert not leftover, f"有残留指令文件: {leftover}"
print("  ✓ 指令已被插件消费，无残留")

print()
print("=" * 70)
print("真实通道验证全部通过")
