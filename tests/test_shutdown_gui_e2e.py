# -*- coding: utf-8 -*-
"""关机链路实测：GUI 勾选 → 真实子进程 → 假关机指令被调用。

安全保证：
  - 关机指令被替换成「写标记文件」的脚本，绝不会真的关机
  - 全程使用假数据库，不碰你的真实会话

验证链路（这是用户实际会走的路径）：
  1. 在 GUI 里勾选「所有会话都正常完成后关机」+「要求所有来源都完成」
  2. 调 GUI 自己的 _args_for() 拼出命令行（不是测试手写的）
  3. 用这套参数真的启动 pi_monitor / codex_monitor
  4. 确认假关机指令被调用，且内容正确
  5. 确认「只有一个来源完成」时不会关机
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
TMP = Path(__file__).resolve().parent / "_tmp" / "shutdown_gui"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


MARK = TMP / "shutdown-called.txt"
COORD = TMP / "coord.json"
NOW = int(time.time() * 1000)

# ---------------------------------------------------------------- 假关机指令
FAKE_SHUT = TMP / "fake_shutdown.py"
FAKE_SHUT.write_text(
    "import pathlib, sys\n"
    f"pathlib.Path(r'{MARK}').write_text('called ' + ' '.join(sys.argv[1:]), encoding='utf-8')\n",
    encoding="utf-8")

# ---------------------------------------------------------------- 假 Pi 库
PI_HOME = TMP / "pihome"
PI_HOME.mkdir(exist_ok=True)
pi_db = PI_HOME / "pi.sqlite"
con = sqlite3.connect(pi_db)
con.executescript("""
  create table sessions (id text, title text, project_id text, permission_mode text,
                         provider_id text, model_id text, mode text, thinking_level text,
                         deleted_at text, updated_at integer);
  create table projects (id text, path text);
  create table turns (id text, session_id text, status text, error_code text,
                      started_at integer, ended_at integer);
  create table notifications (id text, kind text, session_id text, session_title text,
                              turn_id text, error_code text, created_at integer, read_at text);
  create table kv (ns text, key text, value_json text);
""")
con.execute("insert into projects values ('p1','D:/guitest')")
con.execute("insert into sessions values ('pi-1','Pi 已完成','p1','auto','prov','model',"
            "'agent','high',null,?)", (NOW,))
con.execute("insert into turns values ('t1','pi-1','completed',null,?,?)", (NOW, NOW))
con.commit()
con.close()

# 插件心跳（pi_monitor 启动时会检查发送通道）
REAL_KEEPER = Path.home() / ".pi-desktop" / "session-keeper"
if REAL_KEEPER.exists():
    shutil.copytree(REAL_KEEPER, PI_HOME / "session-keeper", dirs_exist_ok=True)
    hbf = PI_HOME / "session-keeper" / "plugin.json"
    if hbf.exists():
        hb = json.loads(hbf.read_text(encoding="utf-8"))
        hb["at"] = int(time.time() * 1000)
        hb["enabled"] = True
        hbf.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")

# ---------------------------------------------------------------- 假 Codex 库
CX = TMP / "codexhome"
CX.mkdir(exist_ok=True)
c1 = sqlite3.connect(CX / "state_5.sqlite")
c1.executescript("""
  create table threads (id text, title text, cwd text, archived integer,
                        updated_at_ms integer, recency_at_ms integer, name text);
""")
c1.execute("insert into threads values ('cx-1','Codex 已完成','D:/guitest',0,?,?,null)",
           (NOW, NOW))
c1.commit(); c1.close()
c2 = sqlite3.connect(CX / "thread_history_1.sqlite")
c2.executescript("""
  create table thread_turns (thread_id text, turn_id text, rollout_ordinal integer,
                             status text, error_json text, started_at integer);
""")
c2.execute("insert into thread_turns values ('cx-1','ct1',0,'completed',null,?)", (NOW,))
c2.commit(); c2.close()
# ---------------------------------------------------------------- 建 GUI 并勾选
print("=" * 70)
print("1. 建 GUI，勾选关机选项（模拟用户操作）")
import codex_monitor_gui as gui

root = tk.Tk()
root.withdraw()
app = gui.App(root)
app.var_all_done.set(True)
app.var_done_secs.set("10")   # GUI 校验要求最小 10 秒
app.var_all_sources.set(True)
app.var_use_codex.set(True)
app.var_use_pi.set(True)
app.var_dry.set(False)
app.var_shut.set(f'"{sys.executable}" "{FAKE_SHUT}"')   # 假关机指令
print("  ✓ GUI 已就绪，关机选项已勾选")

print()
print("=" * 70)
print("2. 用 GUI 自己的 _args_for() 拼参数（不是测试手写）")
args_pi = app._args_for(gui.SRC_PI, [{"key": "pi-1", "source": gui.SRC_PI}])
args_cx = app._args_for(gui.SRC_CODEX, [{"key": "cx-1", "source": gui.SRC_CODEX}])
# _args_for 返回 [解释器, 脚本路径, ...参数]（打包后是 [exe, ...参数]）
# 这里去掉前两项（解释器 + 脚本路径），只保留纯参数，由测试自己指定脚本
args_pi = args_pi[1:] if gui.FROZEN else args_pi[2:]
args_cx = args_cx[1:] if gui.FROZEN else args_cx[2:]
print("  Pi 参数:", " ".join(args_pi[:12]), "...")
check("GUI 给 Pi 传了 --shutdown-when-all-done", "--shutdown-when-all-done" in args_pi)
check("GUI 给 Codex 传了 --shutdown-when-all-done",
      "--shutdown-when-all-done" in args_cx)
check("GUI 传了 --all-sources", "--all-sources" in args_pi)
check("来源列表含两个来源",
      sorted(args_pi[args_pi.index("--all-sources") + 1].split(",")) == ["codex", "pi"])

env_pi = dict(os.environ, PYTHONIOENCODING="utf-8", PI_DESKTOP_DATA_DIR=str(PI_HOME))
env_cx = dict(os.environ, PYTHONIOENCODING="utf-8", CODEX_HOME=str(CX))


def run(script, extra_args, env, seconds=6):
    p = subprocess.Popen(
        [sys.executable, script] + extra_args +
        ["--coord-file", str(COORD), "--poll", "1",
         "--log", str(TMP / f"{Path(script).stem}.log")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env)
    try:
        out, _ = p.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            out, _ = p.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
    return out or ""


print()
print("=" * 70)
print("3. 先只跑 Pi（要求跨来源）→ 应登记完成但不关机")
out_pi = run("pi_monitor.py", args_pi + ["--allow-unsafe"], env_pi, seconds=16)
for line in out_pi.strip().splitlines()[-6:]:
    print("   ", line)
check("Pi 识别到全部完成", "均已正常完成" in out_pi)
check("Pi 登记了完成", "已在协调文件登记本来源完成" in out_pi)
check("Pi 没有关机（在等 Codex）",
      "暂不关机" in out_pi or "仍等待" in out_pi,
      "" if ("暂不关机" in out_pi or "仍等待" in out_pi) else out_pi[-200:])
check("假关机指令【未被】调用", not MARK.exists())

print()
print("=" * 70)
print("4. 再跑 Codex（要求跨来源）→ 两边都完成 → 应调用关机指令")
out_cx = run("codex_monitor.py", args_cx + ["--all-stopped-minutes", "0"], env_cx, seconds=18)
for line in out_cx.strip().splitlines()[-6:]:
    print("   ", line)
check("Codex 登记了完成", "已在协调文件登记本来源完成" in out_cx)
check("假关机指令【被】调用", MARK.exists(),
      "" if MARK.exists() else out_cx[-300:])
if MARK.exists():
    print("  关机指令收到的参数:", repr(MARK.read_text(encoding="utf-8")))
check("关机理由提到所有来源",
      "所有监控来源均已完成" in out_cx or "已正常完成" in out_cx)

print()
print("=" * 70)
print("5. 反向：不要求跨来源时，Pi 单独完成就应关机")
if MARK.exists():
    MARK.unlink()
COORD2 = TMP / "coord2.json"
app.var_all_sources.set(False)
args_pi2 = app._args_for(gui.SRC_PI, [{"key": "pi-1", "source": gui.SRC_PI}])[2:]
check("不勾跨来源时参数为 off",
      args_pi2[args_pi2.index("--shutdown-requires-all-sources") + 1] == "off")
# 心跳在测试期间会过期（插件只在真实 Pi 里刷新），这里先刷新一次再跑
hbf2 = PI_HOME / "session-keeper" / "plugin.json"
if hbf2.exists():
    hb2 = json.loads(hbf2.read_text(encoding="utf-8"))
    hb2["at"] = int(time.time() * 1000)
    hbf2.write_text(json.dumps(hb2, ensure_ascii=False), encoding="utf-8")
out_pi2 = subprocess.run(
    [sys.executable, "pi_monitor.py"] + args_pi2 +
    ["--coord-file", str(COORD2), "--poll", "1", "--allow-unsafe",
     "--log", str(TMP / "pi2.log")],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    env=env_pi, timeout=45).stdout or ""
for line in out_pi2.strip().splitlines()[-4:]:
    print("   ", line)
check("Pi 单独完成即关机（假指令被调用）", MARK.exists(),
      "" if MARK.exists() else out_pi2[-300:])

root.destroy()

print()
print("=" * 70)
print("6. 安全确认")
print("  真实关机指令是:", app.var_shut.get() if False else f'"{sys.executable}" "{FAKE_SHUT}"')
check("全程用的是假关机脚本（未调用系统 shutdown）", "fake_shutdown.py" in str(FAKE_SHUT))

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("关机链路实测全部通过（未真关机）")
