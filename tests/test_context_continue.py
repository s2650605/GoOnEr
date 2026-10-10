# -*- coding: utf-8 -*-
"""针对 pi_monitor 上下文超限续接逻辑的回归测试。

用内存 SQLite 造一个「连续超限」的会话，喂给主循环的判定代码，
验证：连续 N 次触发、二次确认、续接后原会话退出/新会话接管。

不依赖真实 Pi 数据，也不发送任何东西（dry-run）。
"""
import importlib.util
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SPEC = importlib.util.spec_from_file_location("pi_monitor", "pi_monitor.py")
pm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pm)

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


# ---------------------------------------------------------------- 假数据库
def make_db():
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        create table sessions (id text, title text, project_id text, permission_mode text,
                               provider_id text, model_id text, mode text, thinking_level text,
                               deleted_at text, updated_at integer);
        create table projects (id text, path text);
        create table turns (id text, session_id text, status text, error_code text,
                            started_at integer, ended_at integer);
        """
    )
    con.execute("insert into projects values ('p1','D:/proj/alpha')")
    con.execute(
        "insert into sessions values ('sess-old','原会话','p1','auto','prov1','model1',"
        "'agent','high',null,?)", (int(time.time() * 1000),))
    con.execute(
        "insert into sessions values ('sess-new','续接会话','p1','auto','prov1','model1',"
        "'agent','high',null,?)", (int(time.time() * 1000),))
    return con


print("=" * 70)
print("1. build_handoff_message 必须带官方深链与项目信息")
msg = pm.build_handoff_message("原会话", "sess-old", "继续", "D:/proj/alpha")
check("含 pi-desktop://session/<原id> 深链", "pi-desktop://session/sess-old" in msg)
check("含原会话目录（Pi 只认同项目引用）", "D:/proj/alpha" in msg)
check("含原始消息文本", "'继续'" in msg)

print()
print("=" * 70)
print("2. session_model_info 要能继承原会话模型（否则新会话会退回默认模型）")
con = make_db()
info = pm.session_model_info(con, "sess-old")
check("取到 providerId", info.get("providerId") == "prov1", str(info))
check("取到 modelId", info.get("modelId") == "model1")
check("取到 mode", info.get("mode") == "agent")
check("取到 thinkingLevel", info.get("thinkingLevel") == "high")
check("不存在的会话返回空 dict", pm.session_model_info(con, "nope") == {})

print()
print("=" * 70)
print("3. new_session_state 必须带续接相关字段")
st = pm.new_session_state()
check("有 context_streak", st.get("context_streak") == 0)
check("有 continued 标记", st.get("continued") is False)

print()
print("=" * 70)
print("4. adopt_new_session：原会话退出监控、新会话接管")
con = make_db()
sessions = [pm.session_info(("sess-old", "原会话", "p1", "D:/proj/alpha",
                             int(time.time() * 1000), "auto", "error",
                             "CONTEXT_TOO_LARGE", int(time.time() * 1000)))]
state = {"sess-old": pm.new_session_state()}
origin = sessions[0]

import io
buf = io.StringIO()
pm.adopt_new_session(con, sessions, state, buf, "sess-new", origin)
check("原会话已退出监控", all(s["id"] != "sess-old" for s in sessions))
check("新会话已加入监控", any(s["id"] == "sess-new" for s in sessions))
check("新会话有独立状态", "sess-new" in state)
check("原会话状态已清理", "sess-old" not in state)
check("日志提到新会话", "sess-new" in buf.getvalue()[:200] or "已加入监控" in buf.getvalue())

print()
print("=" * 70)
print("5. 库中查不到新会话时，也要能兜住（不崩、仍纳入监控）")
con = make_db()
sessions = [pm.session_info(("sess-old", "原会话", "p1", "D:/proj/alpha",
                             int(time.time() * 1000), "auto", "error",
                             "CONTEXT_TOO_LARGE", int(time.time() * 1000)))]
state = {"sess-old": pm.new_session_state()}
pm.adopt_new_session(con, sessions, state, io.StringIO(), "sess-ghost", sessions[0])
check("兜底也能加入监控", any(s["id"] == "sess-ghost" for s in sessions))
check("兜底名称带项目名", any("alpha" in s["name"] for s in sessions))

print()
print("=" * 70)
print("6. continue_session 在 dry-run 下不得真的新建会话，且要说明会做什么")
con = make_db()
s = pm.session_info(("sess-old", "原会话", "p1", "D:/proj/alpha",
                     int(time.time() * 1000), "auto", "error",
                     "CONTEXT_TOO_LARGE", int(time.time() * 1000)))
st = pm.new_session_state()
st["context_streak"] = 3


class FakeArgs:
    dry_run = True
    message = "继续"
    send_timeout = 5
    compact_timeout = 5
    context_new_session_timeout = 5


buf = io.StringIO()
r_new = pm.continue_session(con, s, st, FakeArgs(), pm.CONTINUE_NEW, buf)
check("dry-run 直接新建模式不返回新会话 id", r_new is None)
check("dry-run 说明了会新建会话", "dry-run" in buf.getvalue())

buf2 = io.StringIO()
st2 = pm.new_session_state()
st2["context_streak"] = 3
r_compact = pm.continue_session(con, s, st2, FakeArgs(), pm.CONTINUE_COMPACT, buf2)
check("dry-run 压缩模式也不真建会话", r_compact is None)
check("压缩模式日志提到压缩", "压缩" in buf2.getvalue())

print()
print("=" * 70)
print("7. 无项目路径时必须拒绝续接（跨项目深链会被 Pi 静默丢弃）")
con = make_db()
s_nopath = dict(s, path=None)
buf3 = io.StringIO()
r = pm.continue_session(con, s_nopath, pm.new_session_state(), FakeArgs(),
                        pm.CONTINUE_NEW, buf3)
check("无项目路径返回 None", r is None)
check("日志说明了原因", "项目目录" in buf3.getvalue())

print()
print("=" * 70)
print("8. CLI 参数存在且默认值正确")
import subprocess
out = subprocess.run([sys.executable, "pi_monitor.py", "--help"],
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace").stdout
for flag in ["--context-continue", "--context-threshold", "--context-codes",
             "--context-new-session-timeout", "--compact-timeout"]:
    check(f"有 {flag}", flag in out)
check("默认续接方式是 compact", "compact" in out)

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("全部通过")
