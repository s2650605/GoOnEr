# -*- coding: utf-8 -*-
"""验证主循环真的会走完「连续 N 次超限 → 续接」这条路。

关键点：context_streak 只在出现**新的**失败 turn 时才累加
（与 Codex 的 400 计数同一口径）。所以「连续 3 次」意味着：
监控发「继续」→ 会话又撞上下文上限（新 turn）→ 再发 → 再撞 …
累计到第 3 个失败 turn 时才触发续接。

因此这里必须跑多轮，而不是 --once 一轮。做法：
  - 用短轮询、零退避，让循环快速迭代
  - fake 的 send_prompt 每被调用一次，就往库里插一个新的
    CONTEXT_TOO_LARGE turn（模拟「又撞了一次」）
  - fake 的 send_create 触发后抛 KeyboardInterrupt 结束循环
"""
import importlib.util
import os
import sqlite3
import sys
import time
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


spec = importlib.util.spec_from_file_location("pi_monitor", "pi_monitor.py")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

DB = TMP_DIR / "test_mainloop2.sqlite"
if DB.exists():
    DB.unlink()

NOW = int(time.time() * 1000)
boot = sqlite3.connect(DB)
boot.executescript("""
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
boot.execute("insert into projects values ('p1','D:/proj/alpha')")
boot.execute("insert into sessions values ('sess-old','原会话','p1','auto','prov1','model1',"
             "'agent','high',null,?)", (NOW,))
boot.execute("insert into turns values ('t0','sess-old','error','CONTEXT_TOO_LARGE',?,?)",
             (NOW, NOW))
boot.commit()
boot.close()

sent, compacted, created = [], [], []
turn_seq = [0]


def write_turn():
    turn_seq[0] += 1
    w = sqlite3.connect(DB)
    t = int(time.time() * 1000)
    w.execute("insert into turns values (?,?,?,?,?,?)",
              (f"t{turn_seq[0]}", "sess-old", "error", "CONTEXT_TOO_LARGE", t, t))
    w.commit()
    w.close()


def fake_prompt(sid, msg, dry, f, t):
    sent.append(sid)
    # 模拟：发了「继续」，会话又撞一次上下文上限（产生新的失败 turn）
    write_turn()
    return True, "ok"


def fake_compact(sid, dry, f, t=200):
    compacted.append(sid)
    return False, "AGENT_BUSY"   # 压缩失败 → 应退化为新建会话


def fake_create(project, title, content, hint, dry, f, t):
    created.append({"project": project, "content": content, "hint": hint})
    # 记录完就中断循环（测试用）
    raise KeyboardInterrupt


pm.DB = DB
pm.ro_connect = lambda: sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=10)
pm.send_prompt = fake_prompt
pm.send_compact = fake_compact
pm.send_create = fake_create
pm.plugin_status = lambda now=None: (True, "ok", {"version": "1.3.0"})
pm.run_shutdown = lambda *a, **k: False

sys.argv = [
    "pi_monitor.py", "--thread", "sess-old",
    "--context-continue", "compact", "--context-threshold", "3",
    "--poll", "0.2", "--backoff-minutes", "0", "--jitter", "0",
    "--send-timeout", "2", "--compact-timeout", "2",
    "--context-new-session-timeout", "2", "--allow-unsafe",
    "--log", str(TMP_DIR / "mainloop2.log"),
]

print("=" * 70)
print("跑完整 main() 多轮，直到续接触发")
print("=" * 70)
try:
    pm.main()
except SystemExit as e:
    print(f"  (main 退出码 {e.code})")
except KeyboardInterrupt:
    print("  (测试主动中断循环)")
except Exception as e:
    check("main 不应抛异常", False, repr(e))

print()
print("=" * 70)
print("结论")
print(f"  发过「继续」次数 : {len(sent)}")
print(f"  压缩调用次数     : {len(compacted)}")
print(f"  新建会话次数     : {len(created)}")

check("先按退避重试了几轮（说明计数在累加）", len(sent) >= 1, f"sent={len(sent)}")
check("达到阈值后调用了压缩", len(compacted) >= 1, f"compacted={len(compacted)}")
check("压缩失败后新建了会话", len(created) >= 1, f"created={len(created)}")
check("新会话建在原项目目录",
      created and created[0]["project"] == "D:/proj/alpha",
      str(created[0]["project"]) if created else "")
check("交接消息含原会话深链",
      created and "pi-desktop://session/sess-old" in created[0]["content"])
check("继承了原会话模型",
      created and created[0]["hint"].get("modelId") == "model1",
      str(created[0]["hint"]) if created else "")
check("重试次数与阈值一致（3 次超限才触发）", len(sent) <= 3, f"sent={len(sent)}")

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("主循环接入验证通过")
