# -*- coding: utf-8 -*-
"""Codex 400 续接端到端实测。

做法：
  - 造假 Codex 数据库（threads + thread_turns），让某个会话连续出现 HTTP 400
  - 用 CODEX_CLI_PATH 指向一个「假 codex」，它把每次调用记到文件里
    （这样既能验证「真的调用了 codex queue/exec」，又不会真跑 agent、不花 token）
  - 跑 codex_monitor.py，观察：
      连续 3 次 400 → 二次确认 → 调用 codex exec 开新会话（带 codex://threads/ 深链）
  - 全程 dry-run 无法验证真实调用，所以这里用真跑 + 假可执行文件

不涉及关机（本测试把关机规则全部关掉）。
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
TMP = Path(__file__).resolve().parent / "_tmp" / "cx400"
if TMP.exists():
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


# ---------------------------------------------------------------- 假 codex
CALLS = TMP / "codex-calls.jsonl"
FAKE = TMP / "codex.exe"          # .exe 后缀 → 走 exe 路径（不套 cmd /c）
FAKE.write_text(
    "#!python\n"
    "import json, sys, pathlib\n"
    f"rec = {{'argv': sys.argv[1:], 'cwd': str(pathlib.Path.cwd())}}\n"
    f"with open(r'{CALLS}', 'a', encoding='utf-8') as f:\n"
    "    f.write(json.dumps(rec, ensure_ascii=False) + '\\n')\n"
    "sys.exit(0)\n",
    encoding="utf-8")

# 造一个能直接被 CreateProcess 调用的 exe：用 python 打包一个最小转发器太慢，
# 改成 .cmd 包装（monitor 会自动套 cmd /c）
FAKE_CMD = TMP / "codex.cmd"
FAKE_CMD.write_text(
    "@echo off\r\n"
    f'"{sys.executable}" "%~dp0codex_fwd.py" %*\r\n',
    encoding="utf-8")

# ---------------------------------------------------------------- 假数据库
NOW = int(time.time() * 1000)
CX = TMP / "codexhome"
CX.mkdir(exist_ok=True)
THREADS = CX / "state_5.sqlite"
TURNS = CX / "thread_history_1.sqlite"
for p in (THREADS, TURNS):
    if p.exists():
        p.unlink()

# 现在 TURNS 已定义，可以生成「假 codex」了（它要往库里插新失败）
(TMP / "codex_fwd.py").write_text(
    "import json, sys, pathlib, sqlite3, time\n"
    f"rec = {{'argv': sys.argv[1:], 'cwd': str(pathlib.Path.cwd())}}\n"
    f"with open(r'{CALLS}', 'a', encoding='utf-8') as f:\n"
    "    f.write(json.dumps(rec, ensure_ascii=False) + '\\n')\n"
    "# 模拟「发了继续之后又撞一次 400」：追加一个新的失败 turn。\n"
    "# 400 连续计数只在出现新 turn 时累加，所以必须真的产生新失败。\n"
    "try:\n"
    f"    w = sqlite3.connect(r'{TURNS}')\n"
    "    t = int(time.time() * 1000)\n"
    "    n = w.execute('select count(*) from thread_turns').fetchone()[0]\n"
    "    w.execute('insert into thread_turns values (?,?,?,?,?,?)',\n"
    "              ('aaaabbbb-1111-2222-3333-444455556666', 'gen%d' % n, 100 + n,\n"
    "               'failed',\n"
    "               '{\"error\":{\"message\":\"bad response status code 400\",\"code\":400}}',\n"
    "               t))\n"
    "    w.commit(); w.close()\n"
    "except Exception:\n"
    "    pass\n"
    "sys.exit(0)\n",
    encoding="utf-8")

c1 = sqlite3.connect(THREADS)
c1.executescript("""
  create table threads (id text, title text, cwd text, archived integer,
                        updated_at_ms integer, recency_at_ms integer, name text);
""")
c1.execute("insert into threads values ('aaaabbbb-1111-2222-3333-444455556666',"
           "'400 测试会话','D:/e2e400',0,?,?,null)", (NOW, NOW))
c1.commit()
c1.close()

# 连续 3 个 400 的 turn（每个 started_at 不同 = 3 个独立失败轮次）
ERR400 = json.dumps({"error": {"message": "bad response status code 400",
                               "code": 400}}, ensure_ascii=False)
c2 = sqlite3.connect(TURNS)
c2.executescript("""
  create table thread_turns (thread_id text, turn_id text, rollout_ordinal integer,
                             status text, error_json text, started_at integer);
""")
for i in range(3):
    c2.execute("insert into thread_turns values (?,?,?,?,?,?)",
               ("aaaabbbb-1111-2222-3333-444455556666", f"t{i}", i, "failed", ERR400,
                NOW - 3000 + i * 1000))
c2.commit()
c2.close()

env = dict(os.environ, PYTHONIOENCODING="utf-8", CODEX_HOME=str(CX))
# 关键：把 codex 指向假的可执行文件
env["CODEX_CLI_PATH"] = str(FAKE_CMD)

print("=" * 70)
print("准备：假 Codex 库（3 个连续 400）+ 假 codex 可执行文件")
print("  数据库:", CX)
print("  假 codex:", FAKE_CMD)
print("  调用记录:", CALLS)
print()

# ---------------------------------------------------------------- 跑监控
LOG = TMP / "cx400.log"
proc = subprocess.Popen(
    [sys.executable, "codex_monitor.py",
     "--thread", "aaaabbbb-1111-2222-3333-444455556666",
     "--bad400-threshold", "3",
     "--poll", "1",
     "--max-per-hour", "5",
     "--backoff-minutes", "0",
     "--jitter", "0",
     "--shutdown-codes", "",          # 关掉关机，避免误触发
     "--all-stopped-minutes", "0",
     "--all-sources", "codex",
     "--shutdown-when-all-done", "off",
     "--log", str(LOG)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace", env=env)

print("=" * 70)
print("跑 codex_monitor（最多 12 秒，看是否触发 400 续接）")
try:
    out, _ = proc.communicate(timeout=14)
except subprocess.TimeoutExpired:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()

for line in (out or "").strip().splitlines()[-16:]:
    print("   ", line)

print()
print("=" * 70)
print("结论")
check("日志出现「400 连续触发」", "400 已连续触发" in (out or "")
      or "400 连续" in (out or ""), (out or "")[-200:])
check("日志出现「开新会话」", "开新会话" in (out or ""))
check("确实调用了假 codex", CALLS.exists())

calls = []
if CALLS.exists():
    calls = [json.loads(l) for l in CALLS.read_text(encoding="utf-8").splitlines() if l.strip()]
print(f"  假 codex 被调用 {len(calls)} 次：")
for c in calls:
    print("    argv:", c["argv"], "| cwd:", c["cwd"])

exec_calls = [c for c in calls if c["argv"] and c["argv"][0] == "exec"]
queue_calls = [c for c in calls if c["argv"] and c["argv"][0] == "queue"]
check("有 codex exec 调用（开新会话）", len(exec_calls) >= 1, f"{len(exec_calls)} 次")
check("exec 带了 -C 指定项目目录",
      any("-C" in c["argv"] for c in exec_calls),
      str([c["argv"] for c in exec_calls]))
check("exec 的首条消息含 codex://threads/ 深链",
      any(any("codex://threads/aaaabbbb" in a for a in c["argv"]) for c in exec_calls),
      str([c["argv"] for c in exec_calls]))
check("exec 的 -C 指向原会话项目目录",
      any("D:/e2e400" in c["argv"] for c in exec_calls),
      str([c["argv"] for c in exec_calls]))
check("触发了 queue（重试发送继续）", len(queue_calls) >= 1, f"{len(queue_calls)} 次")

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("Codex 400 续接实测全部通过")
