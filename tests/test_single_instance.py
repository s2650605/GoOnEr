# -*- coding: utf-8 -*-
"""验证「控制台只能开一个」这道闸门真的生效。

背景：打包成 onefile 时，双击 exe 会有两个进程（解包器 + 真身），
用户看到的「打开一次开了 2 个」就是这么来的；而重复双击更糟 ——
两个控制台各起一套监控脚本，同一会话会被发两次「继续」。
所以这里验两件事：
  1. 闸门必须跨进程：别人占着时，新进程必须拿不到「第一个」的身份；
  2. 占用进程退出后闸门必须自动释放：否则第二次开工具会被上一次挡住（死锁）。

为什么每项都用新进程验：CreateMutexW 只要调用过就会持有句柄，
同一进程再问必然是「已存在」——在同进程内自测会把测试写法的问题
误判成产品的 bug（第一版就是这么错的）。

不验的东西：窗口能不能拉到前台（FindWindow 的成败取决于窗口标题与桌面会话，
在无人值守的测试里不稳定，只要求它不抛异常）。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


def new_process(*code):
    """在一个全新进程里跑几行代码，返回 (stdout, stderr)。"""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s');" % str(ROOT) + "".join(code)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout.strip(), proc.stderr.strip()


try:
    import codex_monitor_gui as gui
except Exception as e:
    print(f"导入 GUI 失败：{e!r}")
    sys.exit(2)

print("=" * 70)
print("单实例闸门")
print("=" * 70)

if os.name != "nt":
    # 闸门本身是 Windows 命名互斥体；非 Windows 上按设计放行，不该拦人。
    handle, first = gui.single_instance_guard()
    check("非 Windows：不拦人（first=True）", first is True)
    check("非 Windows：focus_existing_console 不抛异常",
          gui.focus_existing_console() is None)
else:
    # 1) 占位进程先占住闸门
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s');"
         "import codex_monitor_gui as g;"
         "print('HOLDER', g.single_instance_guard()[1], flush=True);"
         "sys.stdin.read()" % str(ROOT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace")
    try:
        line = holder.stdout.readline().strip()
        check("占位进程拿到第一个身份（first=True）", line == "HOLDER True", line)

        # 2) 别人占着时，新进程必须被判为「已有实例」
        out, err = new_process(
            "import codex_monitor_gui as g;"
            "print('PROBE', g.single_instance_guard()[1])")
        check("同时再开一个进程：拿不到第一个身份（first=False）",
              out == "PROBE False", out or err)

        check("focus_existing_console 不抛异常",
              gui.focus_existing_console() is None)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    # 3) 占位进程退出 → 闸门自动释放（换新进程问，才是真的问内核）
    out, err = new_process(
        "import codex_monitor_gui as g;"
        "print('PROBE', g.single_instance_guard()[1])")
    check("占用进程退出后可再次拿到第一个身份（不留死锁）",
          out == "PROBE True", out or err)

    # 4) 本进程自己拿闸门：全新进程应当能拿到
    handle, first = gui.single_instance_guard()
    check("本进程首次调用拿到第一个身份（first=True）", first is True,
          f"first={first}")

    # 5) 找窗口靠标题，标题必须是同一个常量
    src = open("codex_monitor_gui.py", encoding="utf-8").read()
    check("窗口标题统一走 CONSOLE_TITLE（找已有窗口靠它）",
          "CONSOLE_TITLE = " in src and "root.title(CONSOLE_TITLE)" in src)

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("单实例闸门验证全部通过")
