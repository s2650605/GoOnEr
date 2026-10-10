# -*- coding: utf-8 -*-
"""「报 400 的会话直接新建会话」的回归测试。

为什么要跑真实 main()：这条规则的价值全在「什么时候触发」——
Pi 的 turns.error_code 把 400 / 503 / 5xx 都记成 PROVIDER_ERROR，
只有 Pi 日志里的真实状态码能区分。所以必须验证：

  A. 日志里对得上时间的 400 → 立刻新建会话（不等次数、不压缩）
  B. 日志里是 503（服务商挂了）→ 不走这条路（否则会开出一串新会话）
  C. 上下文超长（CONTEXT_TOO_LARGE）→ 不走这条路（留给先压缩）
  D. 命令行关掉开关 → 不走这条路
  E. 插件面板关掉开关 → 不走这条路
  G. 连续次数已达到、日志里是 503 → 不续接（不压缩、不新建会话）
  H. 连续次数已达到、日志里不是 503 → 仍然续接（不能误伤）

做法：造一个假 Pi 数据目录（pi.sqlite + 日志），patch 掉发送函数，
跑一轮真实 main()（--once），看它到底调了哪个发送动作。
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(__file__).resolve().parent / "_tmp" / "direct400"
os.chdir(ROOT)

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


spec = importlib.util.spec_from_file_location("pi_monitor", ROOT / "pi_monitor.py")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

NOW = int(time.time() * 1000)
SID = "sess-400"

SCHEMA = """
  create table sessions (id text, title text, project_id text, permission_mode text,
                         provider_id text, model_id text, mode text, thinking_level text,
                         deleted_at text, updated_at integer);
  create table projects (id text, path text);
  create table turns (id text, session_id text, status text, error_code text,
                      started_at integer, ended_at integer);
  create table session_todo (session_id text, position integer, content text,
                             status text, priority text, updated_at integer);
  create table messages (mid text, id text, session_id text, turn_id text, seq integer,
                         role text, tool_name text, is_error integer, text text,
                         created_at integer, streaming integer);
  create table notifications (id text, kind text, session_id text, session_title text,
                              turn_id text, error_code text, created_at integer, read_at text);
  create table kv (ns text, key text, value_json text);
"""


def iso_z(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + \
        ".%03dZ" % (ms % 1000)


def build_home(tag, error_code, log_http, log_offset_ms):
    """造假数据目录：会话最后一个回合失败 + 日志里一条带真实状态码的失败记录。"""
    d = TMP / tag
    if d.exists():
        shutil.rmtree(d)
    (d / "logs" / "app").mkdir(parents=True)
    db = d / "pi.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.execute("insert into projects values ('p1','D:/proj/alpha')")
    con.execute("insert into sessions values (?,?,?,?,?,?,?,?,null,?)",
                (SID, "原会话", "p1", "auto", "prov1", "model1", "agent", "high", NOW))
    con.execute("insert into turns values ('t1',?,?,?,?,?)",
                (SID, "error", error_code, NOW, NOW))
    con.execute("insert into session_todo values (?,0,?,?,?,?)",
                (SID, "把剩下的活干完", "pending", "high", NOW))
    con.execute("insert into messages values ('m1','i1',?,null,1,'user',null,0,?,?,0)",
                (SID, "继续《原会话》未完成的任务", NOW))
    con.commit()
    con.close()
    line = json.dumps({
        "event": "agent.turn.failed", "sessionId": SID, "code": error_code,
        "ts": iso_z(NOW + log_offset_ms),
        "data": {"message": f"{log_http}: {{'error': 'x'}}"},
    }, ensure_ascii=False)
    (d / "logs" / "app" / "session.log").write_text(line + "\n", encoding="utf-8")
    return d, db


def run_case(tag, error_code, log_http, log_offset_ms, extra_args=(), hb_extra=None):
    """跑一轮真实 main()，返回 (调用记录, 日志文本)。"""
    d, db = build_home(tag, error_code, log_http, log_offset_ms)
    calls = {"prompt": [], "compact": [], "create": []}
    logfile = d / "monitor.log"

    hb = {"version": "1.9.0"}
    hb.update(hb_extra or {})

    pm.DB = db
    pm.PI_LOG_DIR = d / "logs" / "app"
    pm.KEEPER_DIR = d / "session-keeper"
    pm.ro_connect = lambda: sqlite3.connect(
        db.as_uri() + "?mode=ro", uri=True, timeout=10)
    pm.plugin_status = lambda now=None: (True, "ok", hb)
    pm.send_prompt = lambda sid, msg, dry, f, t: (calls["prompt"].append(sid), True, "ok")[1:]
    pm.send_compact = lambda sid, dry, f, t=200: (calls["compact"].append(sid),
                                                  False, "AGENT_BUSY")[1:]
    pm.send_create = lambda project, title, content, hint, dry, f, t: (
        calls["create"].append({"project": project, "title": title, "content": content,
                                "hint": hint}), True, "new-sess-1")[1:]
    pm.run_shutdown = lambda *a, **k: False

    sys.argv = ["pi_monitor.py", "--thread", SID, "--once",
                "--poll", "0.2", "--backoff-minutes", "0", "--jitter", "0",
                "--send-timeout", "2", "--context-new-session-timeout", "2",
                "--allow-unsafe", "--log", str(logfile)] + list(extra_args)
    try:
        pm.main()
    except SystemExit as e:
        print(f"    (main 退出码 {e.code})")
    except Exception as e:
        check(f"{tag}: main 不应抛异常", False, repr(e))
    return calls, logfile.read_text(encoding="utf-8") if logfile.exists() else ""


print("=" * 74)
print("A. 日志里是「对得上时间的 400」→ 立刻新建会话（不等次数、不压缩）")
calls, txt = run_case("a400", "PROVIDER_ERROR", 400, 0)
check("调用了新建会话", len(calls["create"]) == 1, str(calls))
check("没有先压缩上下文", not calls["compact"], str(calls["compact"]))
check("没有走普通「继续」重试", not calls["prompt"], str(calls["prompt"]))
check("新会话建在原项目目录",
      calls["create"] and calls["create"][0]["project"] == "D:/proj/alpha",
      str(calls["create"][:1]))
check("交接消息带原会话深链",
      calls["create"] and f"pi-desktop://session/{SID}" in calls["create"][0]["content"])
check("日志写明「检测到真实 HTTP 400」", "检测到真实 HTTP 400" in txt)
check("启动日志写明该规则已开", "报 400 直接新建会话：开启" in txt)

print()
print("=" * 74)
print("B. 日志里是 503（服务商挂了）→ 不能当成 400 去开新会话")
calls, txt = run_case("b503", "PROVIDER_ERROR", 503, 0)
check("没有新建会话", not calls["create"], str(calls))
check("按普通报错处理（发了「继续」）", len(calls["prompt"]) == 1, str(calls["prompt"]))
check("日志里没有「检测到真实 HTTP 400」", "检测到真实 HTTP 400" not in txt)

print()
print("=" * 74)
print("C. 上下文超长（CONTEXT_TOO_LARGE）→ 留给「先压缩」那条路")
calls, txt = run_case("cctx", "CONTEXT_TOO_LARGE", 400, 0)
check("没有立刻新建会话", not calls["create"], str(calls))
check("没有立刻压缩（要连续 3 次才压缩）", not calls["compact"], str(calls["compact"]))

print()
print("=" * 74)
print("D. 命令行关掉开关 → 不走这条路")
calls, txt = run_case("doff", "PROVIDER_ERROR", 400, 0,
                      extra_args=["--open-new-on-400", "off"])
check("没有新建会话", not calls["create"], str(calls))
check("日志写明该规则已关", "报 400 直接新建会话：关闭" in txt)

print()
print("=" * 74)
print("E. 插件面板关掉开关 → 不走这条路")
calls, txt = run_case("eplugin", "PROVIDER_ERROR", 400, 0,
                      hb_extra={"openNewOn400": False})
check("没有新建会话", not calls["create"], str(calls))
check("日志写明该规则已关", "报 400 直接新建会话：关闭" in txt)

print()
print("=" * 74)
print("F. 旧日志记录（3 小时前的 400，对不上当前回合）→ 不走这条路")
calls, txt = run_case("fstale", "PROVIDER_ERROR", 400, -3 * 3600 * 1000)
check("没有新建会话", not calls["create"], str(calls))
check("按普通报错处理（发了「继续」）", len(calls["prompt"]) == 1, str(calls["prompt"]))

print()
print("=" * 74)
print("G. 连续次数已达到、日志里是 503 → 不续接（不压缩、不新建会话）")
calls, txt = run_case("g503streak", "PROVIDER_ERROR", 503, 0,
                      extra_args=["--context-threshold", "1"])
check("没有新建会话", not calls["create"], str(calls))
check("没有压缩上下文", not calls["compact"], str(calls["compact"]))
check("按普通报错处理（发了「继续」）", len(calls["prompt"]) == 1, str(calls["prompt"]))
check("日志写明「真实 HTTP 503」不续接", "（服务商不可用）" in txt)
check("启动日志写明 503 不续接", "真实 HTTP 503 不续接" in txt)

print()
print("=" * 74)
print("G2. 同上，但方式是「直接新建会话」→ 503 时也不新建")
calls, txt = run_case("g503new", "PROVIDER_ERROR", 503, 0,
                      extra_args=["--context-threshold", "1",
                                  "--context-continue", "new"])
check("没有新建会话", not calls["create"], str(calls))
check("按普通报错处理（发了「继续」）", len(calls["prompt"]) == 1, str(calls["prompt"]))

print()
print("=" * 74)
print("H. 连续次数已达到、日志里不是 503 → 仍然续接（不能误伤）")
calls, txt = run_case("hctx", "CONTEXT_TOO_LARGE", 400, 0,
                      extra_args=["--context-threshold", "1"])
check("compact 模式先压缩上下文", len(calls["compact"]) == 1, str(calls))
check("压缩失败后新建会话", len(calls["create"]) == 1, str(calls))
check("没有被当成 503 拦下", "（服务商不可用）" not in txt)

calls, txt = run_case("hstale503", "PROVIDER_ERROR", 503, -3 * 3600 * 1000,
                      extra_args=["--context-threshold", "1"])
check("旧 503 记录（对不上当前回合）不拦续接", len(calls["compact"]) == 1, str(calls))

print()
print("=" * 74)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("「报 400 直接新建会话」测试全部通过")
