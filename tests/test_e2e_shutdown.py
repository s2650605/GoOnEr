# -*- coding: utf-8 -*-
"""端到端实测：两个真实进程同时跑，验证不会误关机。

这是本次改动的核心风险点 —— 「Pi 完成了但 Codex 还在跑就关机」。
前面的测试都是直接调协调模块；这里真的启动 pi_monitor 和 codex_monitor
两个进程（用假数据库 + 假关机指令），看它们是否会互相等待。

注意「全部完成」需要至少两轮：
  第 1 轮 发现全部完成 → 记下起始时间；
  第 2 轮 发现已观察满 N 秒 → 才关机。
所以这里不能加 --once（那只跑一轮），而是让进程跑几秒再停。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
TMP = Path(__file__).resolve().parent / "_tmp"
TMP.mkdir(parents=True, exist_ok=True)

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


COORD = TMP / "e2e-coord.json"
SHUT_MARK = TMP / "e2e-shutdown-called.txt"
for p in (COORD, SHUT_MARK):
    if p.exists():
        p.unlink()

NOW = int(time.time() * 1000)

# ---------------------------------------------------------------- 假数据库
PI_DB = TMP / "e2e-pi.sqlite"
if PI_DB.exists():
    PI_DB.unlink()
con = sqlite3.connect(PI_DB)
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
con.execute("insert into projects values ('p1','D:/e2e')")
con.execute("insert into sessions values ('pi-1','Pi 任务','p1','auto','prov','model',"
            "'agent','high',null,?)", (NOW,))
con.execute("insert into turns values ('t1','pi-1','completed',null,?,?)", (NOW, NOW))
con.commit()
con.close()

CX_DIR = TMP / "e2e-codex"
CX_DIR.mkdir(exist_ok=True)
CX_THREADS = CX_DIR / "state_5.sqlite"
CX_TURNS = CX_DIR / "thread_history_1.sqlite"
for p in (CX_THREADS, CX_TURNS):
    if p.exists():
        p.unlink()
c1 = sqlite3.connect(CX_THREADS)
# 列名必须与真实 Codex 库一致（codex_monitor 会查 updated_at_ms / recency_at_ms / name）
c1.executescript("""
  create table threads (id text, title text, cwd text, archived integer,
                        updated_at_ms integer, recency_at_ms integer, name text);
""")
c1.execute("insert into threads values ('cx-1','Codex 任务','D:/e2e',0,?,?,null)",
           (NOW, NOW))
c1.commit()
c1.close()
c2 = sqlite3.connect(CX_TURNS)
c2.executescript("""
  create table thread_turns (thread_id text, turn_id text, rollout_ordinal integer,
                             status text, error_json text, started_at integer);
""")
c2.execute("insert into thread_turns values ('cx-1','ct1',0,'completed',null,?)", (NOW,))
c2.commit()
c2.close()

# 假关机：写标记文件，而不是真关机
FAKE_SHUT = TMP / "fake_shutdown.py"
FAKE_SHUT.write_text(
    "import pathlib\n"
    f"pathlib.Path(r'{SHUT_MARK}').write_text('called', encoding='utf-8')\n",
    encoding="utf-8")

env = dict(os.environ, PYTHONIOENCODING="utf-8")
shut_cmd = f"{sys.executable} {FAKE_SHUT}"

# Pi 的假数据目录：PI_DESKTOP_DATA_DIR 同时用于数据库和插件心跳，
# 所以心跳也要一并准备，否则 pi_monitor 会因「发送通道不可用」提前退出。
pi_home = TMP / "e2e-pi-home"
if pi_home.exists():
    shutil.rmtree(pi_home, ignore_errors=True)
pi_home.mkdir(parents=True, exist_ok=True)
shutil.copy2(PI_DB, pi_home / "pi.sqlite")
REAL_KEEPER = Path.home() / ".pi-desktop" / "session-keeper"
fake_keeper = pi_home / "session-keeper"
if REAL_KEEPER.exists():
    shutil.copytree(REAL_KEEPER, fake_keeper, dirs_exist_ok=True)
    hb_file = fake_keeper / "plugin.json"
    if hb_file.exists():
        hb = json.loads(hb_file.read_text(encoding="utf-8"))
        hb["at"] = int(time.time() * 1000)   # 心跳必须新鲜（>30s 视为过期）
        hb["enabled"] = True
        hb_file.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
env_pi = dict(env, PI_DESKTOP_DATA_DIR=str(pi_home))


def run_monitor(script, args, run_env, seconds=6):
    """跑监控脚本若干秒后停掉，返回 (输出, 退出码)。

    不用 --once：观察窗口需要至少两轮才能触发。
    """
    proc = subprocess.Popen(
        [sys.executable, script] + args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=run_env)
    try:
        out, _ = proc.communicate(timeout=seconds)
        return out or "", proc.returncode
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        return out or "", proc.returncode


def tail(text, n=8):
    for line in text.strip().splitlines()[-n:]:
        print("   ", line)


print("=" * 70)
print("1. 只启动 Pi（要求跨来源）→ 应登记完成但不关机")
out, rc = run_monitor("pi_monitor.py", [
    "--thread", "pi-1",
    "--shutdown-when-all-done", "on",
    "--all-done-confirm-seconds", "0",
    "--all-sources", "pi,codex",
    "--shutdown-requires-all-sources", "on",
    "--coord-file", str(COORD),
    "--shutdown-cmd", shut_cmd,
    "--poll", "1",
    "--allow-unsafe",
    "--log", str(TMP / "e2e-pi.log"),
], env_pi, seconds=6)
tail(out, 10)
check("Pi 识别到全部完成", "均已正常完成" in out)
check("Pi 登记了完成", "已在协调文件登记本来源完成" in out)
check("Pi 因等待其他来源而没有关机",
      "暂不关机" in out or "仍等待" in out,
      "" if ("暂不关机" in out or "仍等待" in out) else out[-300:])
check("此时关机指令未被调用", not SHUT_MARK.exists())

print()
print("=" * 70)
print("2. 协调文件里应只有 pi 标记完成")
data = json.loads(COORD.read_text(encoding="utf-8"))
check("pi 已登记", data["sources"].get("pi", {}).get("done") is True,
      json.dumps(data, ensure_ascii=False))
check("codex 尚未登记", data["sources"].get("codex", {}).get("done") is not True)

print()
print("=" * 70)
print("3. 再启动 Codex（要求跨来源）→ 两边都完成，应关机")
codex_ok = True
env_cx = dict(env, CODEX_HOME=str(CX_DIR))
out2, rc2 = run_monitor("codex_monitor.py", [
    "--thread", "cx-1",
    "--shutdown-when-all-done", "on",
    "--all-done-confirm-seconds", "0",
    "--all-sources", "pi,codex",
    "--shutdown-requires-all-sources", "on",
    "--coord-file", str(COORD),
    "--shutdown-cmd", shut_cmd,
    "--all-stopped-minutes", "0",
    "--poll", "1",
    "--log", str(TMP / "e2e-cx.log"),
], env_cx, seconds=8)
tail(out2, 10)
if "找不到 codex 可执行文件" in out2 or "找不到 Codex 数据库" in out2:
    codex_ok = False
    print("  （本机缺 codex 可执行文件或数据库，跳过 Codex 进程实测）")
else:
    check("Codex 登记了完成", "已在协调文件登记本来源完成" in out2)
    check("两边都完成 → 执行了关机指令", SHUT_MARK.exists(),
          out2[-300:] if not SHUT_MARK.exists() else "")
    check("关机理由提到所有来源",
          "所有监控来源均已完成" in out2 or "已正常完成" in out2)

print()
print("=" * 70)
print("4. 反向验证：不要求跨来源时，Pi 单独完成就应关机")
if SHUT_MARK.exists():
    SHUT_MARK.unlink()
COORD2 = TMP / "e2e-coord2.json"
if COORD2.exists():
    COORD2.unlink()
out3, rc3 = run_monitor("pi_monitor.py", [
    "--thread", "pi-1",
    "--shutdown-when-all-done", "on",
    "--all-done-confirm-seconds", "0",
    "--all-sources", "pi,codex",
    "--shutdown-requires-all-sources", "off",
    "--coord-file", str(COORD2),
    "--shutdown-cmd", shut_cmd,
    "--poll", "1",
    "--allow-unsafe",
    "--log", str(TMP / "e2e-pi2.log"),
], env_pi, seconds=6)
tail(out3, 6)
check("不要求跨来源时执行了关机", SHUT_MARK.exists(),
      out3[-300:] if not SHUT_MARK.exists() else "")
check("日志说明跨来源已关闭", "跨来源要求：关闭" in out3)

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("端到端关机实测全部通过" + ("" if codex_ok else "（Codex 半程跳过）"))
