# -*- coding: utf-8 -*-
"""验证 GUI 拼出的命令行能被对应监控脚本真正接受（两侧参数对齐）。

GUI 和监控脚本是两份独立代码，最容易出的错是：
  1) GUI 传了脚本不认识的参数（argparse 直接报错退出）
  2) 开关关了却仍传参，或开着却没传（静默失效）
  3) 把某个来源专有的参数错传给另一个来源

这里对每个来源分别用「它自己的」参数表解析，并真跑一次 Pi 的完整参数。
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 测试固化在 tests/ 下，被脚本在项目根；临时文件放 tests/_tmp
ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(__file__).resolve().parent / "_tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(ROOT)

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


def parser_for(path):
    """按源码里的 ap.add_argument 调用，重建该脚本的参数表。

    只关心「参数名是否被脚本接受」，所以统一用 nargs='?' 收集值。
    """
    src = Path(path).read_text(encoding="utf-8")
    ap = argparse.ArgumentParser()
    names = []
    for m in re.finditer(r'ap\.add_argument\(\s*"(--[a-z0-9-]+)"', src):
        if m.group(1) not in names:
            names.append(m.group(1))
    for n in names:
        ap.add_argument(n, nargs="?")
    return ap, set(names)


pi_parser, pi_flags = parser_for("pi_monitor.py")
cx_parser, cx_flags = parser_for("codex_monitor.py")

print("=" * 70)
print("0. 两个脚本各自声明的参数（供下面的断言对照）")
print(f"  pi_monitor   : {len(pi_flags)} 个")
print(f"  codex_monitor: {len(cx_flags)} 个")

# 从 GUI 源码里抽出两个分支各自传的参数名，确保覆盖完整
gui_src = Path("codex_monitor_gui.py").read_text(encoding="utf-8")
gui_flags = set(re.findall(r'"(--[a-z0-9-]+)"', gui_src))

print()
print("=" * 70)
print("1. GUI 传的每个参数，都必须被对应来源的脚本认识")
pi_only = {"--statuses", "--notify", "--context-continue", "--context-threshold",
           "--context-codes", "--context-new-session-timeout", "--compact-timeout",
           "--shutdown-when-all-done", "--all-done-confirm-seconds",
           "--open-new-on-400",
           "--active-within"}
for flag in sorted(gui_flags):
    if flag in ("--bad400-threshold",):
        check(f"{flag} 属于 Codex 且被 codex_monitor 认识", flag in cx_flags)
    elif flag in pi_only:
        check(f"{flag} 属于 Pi 且被 pi_monitor 认识", flag in pi_flags)
    else:
        # 共用参数：两边都该有
        check(f"{flag} 两边都认识", flag in pi_flags and flag in cx_flags)

print()
print("=" * 70)
print("2. Pi 专有参数不得出现在 Codex 命令行（否则 codex_monitor 会直接报错退出）")
args_codex = [
    "--auto", "2", "--poll", "10", "--message", "继续",
    "--backoff-minutes", "1,1,3,5,5", "--jitter", "3",
    "--max-per-hour", "20", "--retry-minutes", "15",
    "--shutdown-threshold", "3", "--all-stopped-minutes", "0",
    "--shutdown-cmd", "shutdown /s /f /t 10",
    "--shutdown-codes", "405,503", "--bad400-threshold", "3",
]
try:
    _, unknown = cx_parser.parse_known_args(args_codex)
    check("Codex 参数被 codex_monitor 全部识别", not unknown, str(unknown))
except SystemExit:
    check("Codex 参数解析不报错", False)
for flag in pi_only:
    check(f"Codex 命令行不含 {flag}", flag not in args_codex)

print()
print("=" * 70)
print("3. Pi 专有参数必须被 pi_monitor 认识")
args_pi = [
    "--auto", "2", "--poll", "10", "--message", "继续",
    "--backoff-minutes", "1,1,3,5,5", "--jitter", "3",
    "--max-per-hour", "20", "--retry-minutes", "15",
    "--shutdown-threshold", "3", "--all-stopped-minutes", "0",
    "--shutdown-cmd", "shutdown /s /f /t 10",
    "--statuses", "aborted,error",
    "--shutdown-codes", "STREAM_FAILED,PROVIDER_ERROR",
    "--notify",
    "--context-continue", "compact", "--context-threshold", "3",
    "--open-new-on-400", "on",
    "--shutdown-when-all-done", "--all-done-confirm-seconds", "120",
]
try:
    _, unknown = pi_parser.parse_known_args(args_pi)
    check("Pi 参数被 pi_monitor 全部识别", not unknown, str(unknown))
except SystemExit:
    check("Pi 参数解析不报错", False)

print()
print("=" * 70)
print("4. 端到端：用 Pi 分支的完整参数真跑一次（dry-run，不发送不关机）")
import os
env = dict(os.environ, PYTHONIOENCODING="utf-8")
run_args = [sys.executable, "pi_monitor.py"] + args_pi + \
           ["--dry-run", "--once", "--auto", "1",
            "--log", str(TMP_DIR / "e2e.log")]
p = subprocess.run(run_args, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", env=env, timeout=120)
check("退出码为 0", p.returncode == 0, f"rc={p.returncode}")
out = p.stdout or ""
check("启动了监控", "pi_monitor 启动" in out)
check("打印了上下文续接配置", "上下文超限续接" in out)
check("没有 argparse 报错", "unrecognized arguments" not in out and "invalid choice" not in out)
if p.returncode != 0:
    print("  --- stderr ---")
    print("  " + (p.stderr or "")[:1500])
    print("  --- stdout ---")
    print("  " + out[:1500])

print()
print("=" * 70)
print("5. 关闭续接时，Pi 命令行必须显式带 off（不能靠省略）")
run_args_off = [sys.executable, "pi_monitor.py", "--context-continue", "off",
                "--dry-run", "--once", "--auto", "1",
                "--log", str(TMP_DIR / "e2e_off.log")]
p2 = subprocess.run(run_args_off, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", env=env, timeout=120)
check("off 模式退出码 0", p2.returncode == 0, f"rc={p2.returncode}")
check("日志显示续接已关闭", "上下文超限续接：关闭" in (p2.stdout or ""))

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("全部通过")
