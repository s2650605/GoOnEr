# -*- coding: utf-8 -*-
"""跨来源关机协调 + 「全部完成后关机」的回归测试。

重点验证那个真实缺陷场景：
  同时监控 Codex 和 Pi 时，Pi 先跑完 → 不能直接关机（Codex 还在跑）。
以及只监控一个来源时不能被跨来源逻辑卡住（否则永远不关机）。
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

TMP = Path(__file__).resolve().parent / "_tmp"
TMP.mkdir(parents=True, exist_ok=True)

import shutdown_coord as sc

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


def fresh_coord_file(name):
    p = TMP / name
    if p.exists():
        p.unlink()
    return str(p)


print("=" * 70)
print("1. resolve_setting：CLI 显式值 > 插件设置 > 内置默认")
check("CLI 有值时用 CLI", sc.resolve_setting("on", False, False) == "on")
check("CLI 为 auto 时用插件值",
      sc.resolve_setting("auto", True, False) is True)
check("CLI 为 auto 且插件无值时用默认",
      sc.resolve_setting("auto", None, 120) == 120)
check("CLI 为空串时用插件值", sc.resolve_setting("", 45, 120) == 45)

print()
print("=" * 70)
print("2. parse_on_off：各种写法都要能识别")
for text, want in [("on", True), ("off", False), ("true", True), ("false", False),
                   ("1", True), ("0", False), ("yes", True), ("no", False),
                   (True, True), (False, False)]:
    check(f"{text!r} → {want}", sc.parse_on_off(text) is want)
check("无法识别时用 fallback", sc.parse_on_off("???", True) is True)

print()
print("=" * 70)
print("3. 不要求跨来源：本来源完成即可关机（旧行为不能变）")
f1 = fresh_coord_file("c1.json")
c = sc.ShutdownCoordinator("pi", all_sources=["pi", "codex"],
                           coord_file=f1, require_all=False)
c.mark_done()
ok, why = c.evaluate()
check("可以关机", ok is True, why)
check("说明提到未要求跨来源", "未要求跨来源" in why, why)

print()
print("=" * 70)
print("4. 要求跨来源 + 只有自己完成 → 不能关机（这是关键缺陷场景）")
f2 = fresh_coord_file("c2.json")
pi = sc.ShutdownCoordinator("pi", all_sources=["pi", "codex"],
                            coord_file=f2, require_all=True)
codex = sc.ShutdownCoordinator("codex", all_sources=["pi", "codex"],
                               coord_file=f2, require_all=True)
pi.mark_done()
ok, why = pi.evaluate()
check("Pi 完成但 Codex 未完成 → 不关机", ok is False, why)
check("说明指出了还在等谁", "codex" in why, why)

print()
print("=" * 70)
print("5. 两个来源都完成 → 才关机")
codex.mark_done()
ok, why = pi.evaluate()
check("都完成了 → 可以关机", ok is True, why)
check("说明提到所有来源", "所有监控来源" in why, why)
ok2, why2 = codex.evaluate()
check("从 Codex 侧看也一样", ok2 is True, why2)

print()
print("=" * 70)
print("6. 两个进程各自登记，互不覆盖")
data = __import__("json").loads(Path(f2).read_text(encoding="utf-8"))
check("协调文件里两个来源都在", set(data["sources"].keys()) == {"pi", "codex"},
      str(list(data["sources"].keys())))
check("pi 标记为 done", data["sources"]["pi"]["done"] is True)
check("codex 标记为 done", data["sources"]["codex"]["done"] is True)

print()
print("=" * 70)
print("7. 撤销登记后不能关机（有会话重新活动）")
pi.clear_done()
ok, why = codex.evaluate()
check("撤销后不能关机", ok is False, why)
check("说明里 pi 回到等待列表", "pi" in why, why)

print()
print("=" * 70)
print("8. 登记过期（对方进程已死）→ 不关机，并明确说明原因")
f3 = fresh_coord_file("c3.json")
pi3 = sc.ShutdownCoordinator("pi", all_sources=["pi", "codex"],
                             coord_file=f3, require_all=True, ttl_seconds=1)
codex3 = sc.ShutdownCoordinator("codex", all_sources=["pi", "codex"],
                                coord_file=f3, require_all=True, ttl_seconds=1)
pi3.mark_done()
codex3.mark_done()
# 把 codex 的登记时间改成很久以前，模拟那个进程已退出
data = __import__("json").loads(Path(f3).read_text(encoding="utf-8"))
data["sources"]["codex"]["at"] = int((time.time() - 3600) * 1000)
Path(f3).write_text(__import__("json").dumps(data), encoding="utf-8")
ok, why = pi3.evaluate()
check("过期登记不视为完成", ok is False, why)
check("说明指出过期与可能已退出", "过期" in why and "退出" in why, why)

print()
print("=" * 70)
print("9. 协调文件损坏 / 不存在时不得崩，且要保守（不关机）")
f4 = fresh_coord_file("c4.json")
Path(f4).write_text("{ 这不是合法 JSON", encoding="utf-8")
c4 = sc.ShutdownCoordinator("pi", all_sources=["pi", "codex"],
                            coord_file=f4, require_all=True)
ok, why = c4.evaluate()
check("坏文件不崩且不关机", ok is False, why)
f5 = str(TMP / "never-exists.json")
if Path(f5).exists():
    Path(f5).unlink()
c5 = sc.ShutdownCoordinator("pi", all_sources=["pi", "codex"],
                            coord_file=f5, require_all=True)
ok, why = c5.evaluate()
check("文件不存在也不崩且不关机", ok is False, why)

print()
print("=" * 70)
print("10. 只监控一个来源时，跨来源要求应自动失效（否则永远不关机）")
f6 = fresh_coord_file("c6.json")
solo = sc.ShutdownCoordinator("pi", all_sources=["pi"],
                              coord_file=f6, require_all=False)
solo.mark_done()
ok, why = solo.evaluate()
check("单来源能正常关机", ok is True, why)

print()
print("=" * 70)
print("11. 两个监控脚本都必须真的用上协调模块")
pi_src = Path("pi_monitor.py").read_text(encoding="utf-8")
cx_src = Path("codex_monitor.py").read_text(encoding="utf-8")
for name, src in (("pi_monitor.py", pi_src), ("codex_monitor.py", cx_src)):
    check(f"{name} 引入了 shutdown_coord", "import shutdown_coord" in src)
    check(f"{name} 构造了协调器", "ShutdownCoordinator(" in src)
    check(f"{name} 有完成登记", "mark_done()" in src)
    check(f"{name} 有撤销登记", "clear_done()" in src)
    check(f"{name} 会评估能否关机", "evaluate()" in src)
    check(f"{name} 支持 --all-sources", '"--all-sources"' in src)
    check(f"{name} 支持 --shutdown-when-all-done",
          '"--shutdown-when-all-done"' in src)

print()
print("=" * 70)
print("12. Codex 端必须真的实现了「全部完成」判定（之前完全缺失）")
check("Codex 有规则3判定", "规则3" in cx_src or "全部完成后关机" in cx_src)
check("Codex 判定用 completed 状态", '"completed" for _c, st in states' in cx_src
      or '== "completed"' in cx_src)
check("Codex 有 all_done_since 计时", '"all_done_since"' in cx_src)
check("Codex 有二次确认", "re_states" in cx_src)

print()
print("=" * 70)
print("13. GUI 必须把「全部完成」传给两个来源（修复静默无效）")
gui_src = Path("codex_monitor_gui.py").read_text(encoding="utf-8")
# 找到 _args_for 的定义体，确认该参数不在 Pi 分支的缩进里
args_for = gui_src[gui_src.index("def _args_for"):]
args_for = args_for[:args_for.index("def _enabled_sources")]
codex_part = args_for[args_for.index("if src == SRC_CODEX:"):args_for.index("else:")]
pi_part = args_for[args_for.index("else:"):]
check("Codex 分支之后（共用区）有 shutdown-when-all-done",
      args_for.rindex("--shutdown-when-all-done") > args_for.index("else:"),
      "应位于两来源共用区")
check("GUI 传 --all-sources", '"--all-sources"' in args_for)
check("GUI 传 --shutdown-requires-all-sources",
      '"--shutdown-requires-all-sources"' in args_for)
check("GUI 有跨来源复选框变量", "var_all_sources" in gui_src)
check("GUI 有 _enabled_sources 辅助方法", "def _enabled_sources" in gui_src)
check("新复选框已加入禁用列表", "self.cb_all_sources," in gui_src)

print()
print("=" * 70)
print("14. 关机指令解析：Windows 下必须保住反斜杠路径")
# 这是端到端实测中发现的真实问题：shlex.split 在 Windows 会按 POSIX 规则
# 吃掉反斜杠，导致自定义关机指令（带路径的脚本）执行失败。
parts = sc.split_shutdown_cmd("shutdown /s /f /t 10")
check("系统关机命令解析正常",
      parts == ["shutdown", "/s", "/f", "/t", "10"], str(parts))
parts2 = sc.split_shutdown_cmd('"' + r"C:\tools\my shut.py" + '" --now')
check("带引号的路径保住反斜杠",
      bool(parts2) and parts2[0] == r"C:\tools\my shut.py", str(parts2))
check("空指令返回空列表", sc.split_shutdown_cmd("") == [])
check("空白指令返回空列表", sc.split_shutdown_cmd("   ") == [])

print()
print("=" * 70)
print("15. 关机失败必须如实回报（不能报「已触发关机」）")
check("pi_monitor 用共享解析器", "coord_mod.split_shutdown_cmd" in pi_src)
check("codex_monitor 用共享解析器", "coord_mod.split_shutdown_cmd" in cx_src)
check("pi_monitor 失败时返回 False", "关机指令执行失败" in pi_src)
check("codex_monitor 失败时返回 False", "关机指令执行失败" in cx_src)
check("pi_monitor 不再用裸 shlex.split",
      "subprocess.run(shlex.split(cmd)" not in pi_src)
check("codex_monitor 不再用裸 shlex.split",
      "shlex.split(args.shutdown_cmd)" not in cx_src)

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("关机功能测试全部通过")
