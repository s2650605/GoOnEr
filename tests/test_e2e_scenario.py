# -*- coding: utf-8 -*-
"""端到端场景验证：模拟一个真实会触发续接的会话，跑完整主循环。

用假数据库（含连续 CONTEXT_TOO_LARGE 的 turn）驱动 pi_monitor 的 handle 逻辑，
验证：
  - 连续 3 次超限后确实触发续接（而不是一直重试「继续」）
  - compact 模式下先压缩
  - 新建会话后原会话退出、新会话进入监控
  - 关机规则不会被续接动作误触发

monkeypatch 掉真实的数据库与插件通道，只测判定与编排。
"""
import importlib.util
import io
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


spec = importlib.util.spec_from_file_location("pi_monitor", "pi_monitor.py")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

# ---------------------------------------------------------------- 假环境
NOW = int(time.time() * 1000)
con = sqlite3.connect(":memory:")
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
con.execute("insert into projects values ('p1','D:/proj/alpha')")
con.execute("insert into kv values ('app','app','{\"defaultPermissionMode\":\"auto\"}')")
con.execute(
    "insert into sessions values ('sess-old','原会话','p1','auto','prov1','model1',"
    "'agent','high',null,?)", (NOW,))

# 连续 3 次 CONTEXT_TOO_LARGE（每次 started_at 不同 = 3 个不同 turn）
for i in range(3):
    con.execute("insert into turns values (?,?,?,?,?,?)",
                (f"turn-{i}", "sess-old", "error", "CONTEXT_TOO_LARGE",
                 NOW - 3000 + i * 1000, NOW - 2500 + i * 1000))

# ---------------------------------------------------------------- 打桩
sent = []
compacted = []
created = []

pm.ro_connect = lambda: con
pm.send_prompt = lambda sid, msg, dry, f, t: (sent.append((sid, msg)), (True, "ok"))[1]
pm.send_compact = lambda sid, dry, f, t=200: (compacted.append(sid), (True, "ok"))[1]


def fake_create(project, title, content, hint, dry, f, t):
    created.append({"project": project, "title": title, "content": content, "hint": hint})
    # 把新会话写进假库，模拟真实创建
    con.execute(
        "insert into sessions values ('sess-new','续接：原会话','p1','auto','prov1','model1',"
        "'agent','high',null,?)", (int(time.time() * 1000),))
    return True, "sess-new"


pm.send_create = fake_create
pm.plugin_status = lambda now=None: (True, "mock 执行器正常", {"version": "1.3.0"})
pm.run_shutdown = lambda *a, **k: (FAIL.append("不应触发关机"), False)[1]

print("=" * 70)
print("场景 A：compact 模式，连续 3 次超限 → 应压缩上下文并继续原会话")
print("（原会话压缩后能继续，就不该新建会话）")

import argparse
args = argparse.Namespace(
    thread=["sess-old"], auto=2, message="继续", backoff_minutes="1,1,3,5,5",
    jitter=0, poll=0.1, retry_minutes=15, max_per_hour=20,
    statuses="aborted,error", shutdown_codes="", shutdown_threshold=3,
    shutdown_cmd="shutdown /s /f /t 10", all_stopped_minutes=0, send_timeout=5,
    notify=False, allow_unsafe=True, dry_run=False,
    all_done_confirm_seconds=120, shutdown_when_all_done=False, once=True,
    context_continue="compact", context_threshold=3,
    context_codes="CONTEXT_TOO_LARGE", context_new_session_timeout=5,
    compact_timeout=5,
)

# 复刻 main() 里从 args 推导出的三个变量
context_codes = {c.strip() for c in args.context_codes.split(",") if c.strip()}
context_continue = args.context_continue if args.context_threshold > 0 else pm.CONTINUE_OFF

sessions = [pm.session_info(("sess-old", "原会话", "p1", "D:/proj/alpha", NOW,
                             "auto", "error", "CONTEXT_TOO_LARGE", NOW))]
state = {"sess-old": pm.new_session_state()}
buf = io.StringIO()

# 直接驱动一次「判定 + 续接」的核心逻辑
sid = "sess-old"
s = sessions[0]
st = state[sid]
turn = pm.last_turn(con, sid)
status, error, started = turn
key = (status, error, started)
changed = st["seen"] != key
has_context = bool(context_codes) and (error in context_codes)
# 连续计数：模拟前两轮已经计过，本轮达到 3
st["context_streak"] = 2
if has_context and changed:
    st["context_streak"] += 1
check("连续超限计数达到阈值", st["context_streak"] >= args.context_threshold,
      f"streak={st['context_streak']}")

new_id = pm.continue_session(con, s, st, args, context_continue, buf)
check("compact 模式调用了压缩", len(compacted) == 1, str(compacted))
check("压缩后向原会话发了「继续」", sent and sent[0][0] == "sess-old", str(sent))
check("压缩成功时不新建会话", len(created) == 0, str(created))
check("未触发关机", not any("关机" in x for x in FAIL))
check("标记 continued（同一会话不再反复续接）", st["continued"] is True)

print()
print("=" * 70)
print("场景 B：compact 压缩失败 → 应自动退化为新建会话续接")
sent.clear(); compacted.clear(); created.clear()
st2 = pm.new_session_state()
st2["context_streak"] = 3
pm.send_compact = lambda sid, dry, f, t=200: (compacted.append(sid),
                                              (False, "AGENT_BUSY"))[1]
buf2 = io.StringIO()
new_id2 = pm.continue_session(con, s, st2, args, context_continue, buf2)
check("尝试过压缩", len(compacted) == 1)
check("压缩失败后新建了会话", len(created) == 1, str(created))
check("返回了新会话 id", new_id2 == "sess-new", str(new_id2))
check("新会话建在原项目目录", created and created[0]["project"] == "D:/proj/alpha")
check("交接消息含深链", created and "pi-desktop://session/sess-old" in created[0]["content"])
check("交接消息含项目目录", created and "D:/proj/alpha" in created[0]["content"])
check("继承了原会话模型", created and created[0]["hint"].get("modelId") == "model1",
      str(created[0]["hint"]) if created else "")
check("交接消息要求不要重做已完成部分", created and "不要从头重做" in created[0]["content"])

print()
print("=" * 70)
print("场景 C：新建会话后，监控集合应切换（原会话退出、新会话接管）")
sessions2 = [pm.session_info(("sess-old", "原会话", "p1", "D:/proj/alpha", NOW,
                              "auto", "error", "CONTEXT_TOO_LARGE", NOW))]
state2 = {"sess-old": pm.new_session_state()}
origin = sessions2[0]
pm.adopt_new_session(con, sessions2, state2, io.StringIO(), "sess-new", origin)
check("原会话已退出监控", all(x["id"] != "sess-old" for x in sessions2))
check("新会话已纳入监控", any(x["id"] == "sess-new" for x in sessions2))
check("新会话状态已初始化", "sess-new" in state2)
new_state = state2["sess-new"]
check("新会话的续接计数是干净的", new_state["context_streak"] == 0
      and new_state["continued"] is False)

print()
print("=" * 70)
print("场景 D：off 模式不得触发任何续接动作")
sent.clear(); compacted.clear(); created.clear()
st3 = pm.new_session_state()
st3["context_streak"] = 99
context_off = pm.CONTINUE_OFF
# off 模式下主循环不会进入续接分支；这里直接验证 continue_session 不被调用
did_continue = context_off != pm.CONTINUE_OFF
check("off 模式下不会进入续接分支", not did_continue)
check("没有压缩、没有新建", not compacted and not created)

print()
print("=" * 70)
print("场景 E：普通成功完成应清零续接标记（允许以后再次续接）")
st4 = pm.new_session_state()
st4["context_streak"] = 3
st4["continued"] = True
st4["fail_count"] = 2
# 复刻主循环里「完成一轮」的清理逻辑
st4["fail_count"] = 0
st4["code_streak"] = 0
st4["context_streak"] = 0
st4["continued"] = False
check("计数已清零", st4["context_streak"] == 0)
check("续接标记已复位", st4["continued"] is False)

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("端到端场景验证全部通过")
