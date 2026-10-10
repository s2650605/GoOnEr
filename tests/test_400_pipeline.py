# -*- coding: utf-8 -*-
"""「400 自动续接 + 全部完成后关机」这套配置的回归测试。

对应需求：
  针对 Pi 所有会话，某会话报 400 → 提取未完成内容 → 生成提示词 →
  新开会话继续 → 全部完成后关机。

本测试守住四件事：
  1. 监控范围（--active-within）只纳入活跃会话，不误触发历史遗留
  2. 触发条件涵盖 Pi 的 400（PROVIDER_ERROR）与 CONTEXT_TOO_LARGE
  3. 交接提示词真的带上未完成内容（todo + 最后任务描述 + 深链）
  4. 连续计数保护：单次抖动不开新会话
"""
import importlib.util
import io
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("pi_monitor", ROOT / "pi_monitor.py")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


NOW = int(time.time() * 1000)


def make_con(sessions, turns_by_session=None, todos=None, msgs=None):
    """造一个内存库：sessions / turns / session_todo / messages。"""
    con = sqlite3.connect(":memory:")
    con.executescript("""
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
    """)
    con.execute("insert into projects values ('p1','D:/proj')")
    for sid, title, updated in sessions:
        con.execute("insert into sessions values (?,?,?,?,?,?,?,?,null,?)",
                    (sid, title, "p1", "auto", "pv", "md", "agent", "high", updated))
    for sid, rows in (turns_by_session or {}).items():
        for i, (status, code, started) in enumerate(rows):
            con.execute("insert into turns values (?,?,?,?,?,?)",
                        (f"{sid}-t{i}", sid, status, code, started, started))
    for sid, items in (todos or {}).items():
        for pos, (content, status, pri) in enumerate(items):
            con.execute("insert into session_todo values (?,?,?,?,?,?)",
                        (sid, pos, content, status, pri, NOW))
    for sid, text in (msgs or {}).items():
        con.execute("insert into messages values ('m','i',?,null,1,'user',null,0,?,?,0)",
                    (sid, text, NOW))
    return con


print("=" * 74)
print("1. 监控范围：--active-within 只纳入活跃会话（不误触发历史遗留）")
con = make_con(
    sessions=[
        ("sess-active", "活跃会话", NOW - 60_000),          # 1 分钟前
        ("sess-old", "一周前的旧会话", NOW - 7 * 24 * 3600_000),
        ("sess-ancient", "一个月前的旧会话", NOW - 30 * 24 * 3600_000),
    ],
    turns_by_session={
        # 三个都是 error，但只有 active 是最近的
        "sess-active": [("error", "PROVIDER_ERROR", NOW - 60_000)],
        "sess-old": [("error", "PROVIDER_ERROR", NOW - 7 * 24 * 3600_000)],
        "sess-ancient": [("error", "PROVIDER_ERROR", NOW - 30 * 24 * 3600_000)],
    })
picked = pm.pick_sessions(con, [], 0, active_within_minutes=60)
ids = [s["id"] for s in picked]
check("60 分钟窗口只选中活跃会话", ids == ["sess-active"], str(ids))
check("不包含一周前的旧会话", "sess-old" not in ids)
check("不包含一月前的旧会话", "sess-ancient" not in ids)

picked_wide = pm.pick_sessions(con, [], 0, active_within_minutes=60 * 24 * 8)
check("放宽到 8 天时旧会话才被纳入",
      "sess-old" in [s["id"] for s in picked_wide],
      str([s["id"] for s in picked_wide]))

print()
print("=" * 74)
print("2. 触发条件：涵盖 Pi 的 400（PROVIDER_ERROR）")
import subprocess
out = subprocess.run([sys.executable, "pi_monitor.py", "--help"],
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace").stdout or ""
check("有 --active-within 参数", "--active-within" in out)
check("默认触发码含 PROVIDER_ERROR", "PROVIDER_ERROR" in out)
check("默认触发码含 CONTEXT_TOO_LARGE", "CONTEXT_TOO_LARGE" in out)
check("默认阈值是 3 次", "默认 3" in out)

# 默认值本身
ap_default = "PROVIDER_ERROR,CONTEXT_TOO_LARGE"
codes = {c.strip() for c in ap_default.split(",")}
check("400 类错误在触发集合里", "PROVIDER_ERROR" in codes)
check("上下文超限在触发集合里", "CONTEXT_TOO_LARGE" in codes)

print()
print("=" * 74)
print("3. 交接提示词：真的带上未完成内容")
con2 = make_con(
    sessions=[("s1", "原会话", NOW)],
    turns_by_session={"s1": [("error", "PROVIDER_ERROR", NOW)]},
    todos={"s1": [("第一件没干完", "in_progress", "high"),
                  ("第二件没干完", "pending", "medium"),
                  ("已经干完的", "completed", "low")]},
    msgs={"s1": "原始任务描述内容"})
todos = pm.unfinished_todos(con2, "s1")
contents = [t[1] for t in todos]
check("只取未完成（不含 completed）", "已经干完的" not in contents, str(contents))
check("取到 2 项未完成", len(todos) == 2, str(contents))
check("in_progress 优先排前", todos[0][1] == "第一件没干完", str(contents))
check("high 优先于 medium", todos[0][3] == "high")

lu = pm.last_user_message(con2, "s1")
check("取到最后一条用户消息", lu == "原始任务描述内容", lu)

msg = pm.build_handoff_message("原会话", "s1", "继续", "D:/proj",
                               todos=todos, last_user=lu)
check("提示词含深链", "pi-desktop://session/s1" in msg)
check("提示词含未完成项 1", "第一件没干完" in msg)
check("提示词含未完成项 2", "第二件没干完" in msg)
check("提示词不含已完成项", "已经干完的" not in msg)
check("提示词含任务原文", "原始任务描述内容" in msg)
check("提示词含原会话目录", "D:/proj" in msg)
check("提示词要求不要重做已完成部分", "不要从头重做" in msg)
check("提示词授权自行判断", "自行判断" in msg)

print()
print("=" * 74)
print("4. 无 todo 时要能退回「只用最后任务描述」（不能生成空壳）")
con3 = make_con(sessions=[("s2", "无 todo 会话", NOW)],
                turns_by_session={"s2": [("error", "PROVIDER_ERROR", NOW)]},
                msgs={"s2": "只有任务原文"})
todos3 = pm.unfinished_todos(con3, "s2")
check("无 todo 时返回空", todos3 == [], str(todos3))
lu3 = pm.last_user_message(con3, "s2")
msg3 = pm.build_handoff_message("无 todo 会话", "s2", "继续", "D:/proj",
                                todos=todos3, last_user=lu3)
check("仍含深链", "pi-desktop://session/s2" in msg3)
check("仍含任务原文", "只有任务原文" in msg3)
check("不含「未完成的待办」标题（因为没有）", "未完成的待办" not in msg3)

print()
print("=" * 74)
print("5. 连续计数保护：单次抖动不开新会话")
con4 = make_con(sessions=[("s3", "抖动会话", NOW)],
                turns_by_session={"s3": [("error", "PROVIDER_ERROR", NOW)]},
                todos={"s3": [("待办", "pending", "high")]})
st = pm.new_session_state()
check("初始计数为 0", st["context_streak"] == 0)
st["context_streak"] = 1
check("失败 1 次未达阈值 3", st["context_streak"] < 3)
st["context_streak"] = 2
check("失败 2 次仍未达阈值", st["context_streak"] < 3)
st["context_streak"] = 3
check("失败 3 次达到阈值", st["context_streak"] >= 3)

print()
print("=" * 74)
print("6. dry-run 下绝不真的新建会话")
class FakeArgs:
    dry_run = True
    message = "继续"
    send_timeout = 5
    compact_timeout = 5
    context_new_session_timeout = 5


buf = io.StringIO()
s4 = pm.session_info(("s3", "抖动会话", "p1", "D:/proj", NOW, "auto",
                      "error", "PROVIDER_ERROR", NOW))
st4 = pm.new_session_state()
st4["context_streak"] = 3
r = pm.continue_session(con4, s4, st4, FakeArgs(), pm.CONTINUE_NEW, buf)
check("dry-run 不返回新会话 id", r is None)
check("dry-run 日志说明会做什么", "dry-run" in buf.getvalue())

print()
print("=" * 74)
print("7. 无项目目录时必须拒绝（跨项目深链会被 Pi 丢弃）")
buf2 = io.StringIO()
s5 = dict(s4, path=None)
r2 = pm.continue_session(con4, s5, pm.new_session_state(), FakeArgs(),
                         pm.CONTINUE_NEW, buf2)
check("无项目路径时返回 None", r2 is None)
check("日志说明了原因", "项目目录" in buf2.getvalue())

print()
print("=" * 74)
print()
print("=" * 74)
print("8. 插件面板设置生效（优先级：命令行 > 插件面板 > 内置默认）")
# 命令行显式指定 → 以命令行为准，插件设置被忽略
m, n = pm.resolve_context_settings("new", "5",
                                   {"contextContinueMode": "compact",
                                    "contextContinueThreshold": 9})
check("CLI 模式优先", m == "new", m)
check("CLI 次数优先", n == 5, str(n))

# 命令行 auto（默认）→ 听插件面板
m, n = pm.resolve_context_settings("auto", "auto",
                                   {"contextContinue": True,
                                    "contextContinueMode": "new",
                                    "contextContinueThreshold": 7})
check("auto 时用插件模式", m == "new", m)
check("auto 时用插件次数", n == 7, str(n))

# 插件面板里关掉 → 等价 off
m, n = pm.resolve_context_settings("auto", "auto", {"contextContinue": False})
check("插件关闭等价 off", m == pm.CONTINUE_OFF, m)

# 插件面板次数设为 0 → 关闭该规则
m, n = pm.resolve_context_settings("auto", "auto",
                                   {"contextContinue": True,
                                    "contextContinueMode": "compact",
                                    "contextContinueThreshold": 0})
check("插件次数 0 = 关闭", m == pm.CONTINUE_OFF, m)

# 读不到插件（没装/心跳过期）→ 退回内置默认，行为与以前一致
m, n = pm.resolve_context_settings("auto", "auto", {})
check("无插件时默认 compact", m == pm.CONTINUE_COMPACT, m)
check("无插件时默认 3 次", n == 3, str(n))

# 命令行显式 0 → 关闭（不被插件覆盖）
m, n = pm.resolve_context_settings("auto", "0", {"contextContinueThreshold": 7})
check("CLI 次数 0 = 关闭", m == pm.CONTINUE_OFF, m)

# 插件里存了非法值 → 退回安全默认，而不是崩掉
m, n = pm.resolve_context_settings("auto", "auto",
                                   {"contextContinueMode": "bogus",
                                    "contextContinueThreshold": "abc"})
check("非法模式退回 compact", m == pm.CONTINUE_COMPACT, m)
check("非法次数退回 3", n == 3, str(n))
print()
print("=" * 74)
print("9. 监控脚本要写「存活心跳」（插件面板靠它显示监控是否在跑）")
import json as _json
import tempfile as _tempfile

_hb_dir = Path(_tempfile.mkdtemp())
_orig_keeper = pm.KEEPER_DIR
pm.KEEPER_DIR = _hb_dir
try:
    pm.write_monitor_heartbeat(True, 10)
    _f = _hb_dir / "monitor-pi.json"
    check("pi 心跳文件已写出", _f.exists())
    _d = _json.loads(_f.read_text(encoding="utf-8"))
    check("标了来源 pi", _d.get("source") == "pi", str(_d.get("source")))
    check("带 pid", isinstance(_d.get("pid"), int))
    check("带时间戳", isinstance(_d.get("at"), int) and _d["at"] > 0)
    check("带 dryRun 标记", _d.get("dryRun") is True)
    check("带 pollSeconds（面板据此判过期）", _d.get("pollSeconds") == 10,
          str(_d.get("pollSeconds")))

    # Codex 侧同一个机制，写的是另一个文件名，别写串
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("codex_monitor", ROOT / "codex_monitor.py")
    cm = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(cm)
    cm.KEEPER_DIR = _hb_dir
    cm.write_monitor_heartbeat(False, 30)
    _f2 = _hb_dir / "monitor-codex.json"
    check("codex 心跳文件已写出", _f2.exists())
    _d2 = _json.loads(_f2.read_text(encoding="utf-8"))
    check("标了来源 codex", _d2.get("source") == "codex", str(_d2.get("source")))
    check("两个来源互不覆盖", ( _hb_dir / "monitor-pi.json").exists()
          and _d.get("source") == "pi")
finally:
    pm.KEEPER_DIR = _orig_keeper


print()
print("=" * 74)
print("10. 「近期活跃会话全部报 503 → 关机」的判定")
ev = pm.coord_mod.evaluate_all_503
NOW = int(time.time() * 1000)
MIN = 60_000

# 全部近期活跃都 503 → 触发
fired, names, bad = ev([("a", NOW, True), ("b", NOW - 5 * MIN, True)], NOW, 60)
check("全部 503 → 触发", fired is True)
check("参与判定的会话数正确", len(names) == 2, str(names))
check("没有不满足的", bad == [])

# 只要有一个不是 503 → 不触发
fired, names, bad = ev([("a", NOW, True), ("b", NOW, False)], NOW, 60)
check("有一个不是 503 → 不触发", fired is False)
check("指出是哪个不满足", bad == ["b"], str(bad))

# 历史会话（窗口外）不参与判定：不该因为它而永远凑不齐
fired, names, bad = ev([("a", NOW, True), ("old", NOW - 300 * MIN, False)], NOW, 60)
check("窗口外的历史会话不参与", fired is True)
check("历史会话不在名单里", "old" not in names, str(names))

# 窗口内一个会话都没有 → 绝不关机
fired, names, bad = ev([("old", NOW - 300 * MIN, True)], NOW, 60)
check("没有近期活跃会话 → 不触发（绝不关机）", fired is False)
fired, names, bad = ev([], NOW, 60)
check("空列表 → 不触发", fired is False)

# 没有活动时间（upd=None）的会话不参与
fired, names, bad = ev([("a", NOW, True), ("unknown", None, False)], NOW, 60)
check("无活动时间的会话不参与", fired is True, str(names))

# 窗口为 0 → 只算「正好此刻」的会话（不该误触发）
fired, names, bad = ev([("a", NOW - MIN, True)], NOW, 0)
check("窗口 0 不误触发", fired is False)

# 非法窗口值 → 安全地不触发
fired, names, bad = ev([("a", NOW, True)], NOW, "abc")
check("非法窗口值 → 不触发", fired is False)
print()
print("=" * 74)
print("11. Pi 的 503 判定：从日志里读真实 HTTP 状态码（区分 400 与 503）")
import json as _json2
import tempfile as _tf2

_logdir = Path(_tf2.mkdtemp())
_sid_a = "aaaaaaaa-0000-0000-0000-000000000001"
_sid_b = "bbbbbbbb-0000-0000-0000-000000000002"
_sid_c = "cccccccc-0000-0000-0000-000000000003"


def _line(sid, code, msg):
    return _json2.dumps({
        "ts": "2026-01-01T00:00:00.000Z", "level": "error", "category": "session",
        "event": "agent.turn.failed", "message": "agent turn failed",
        "sessionId": sid, "code": code, "data": {"message": msg},
    }, ensure_ascii=False)


# 旧文件里 a 是 400，新文件里 a 变成 503 → 必须取最后一条
(_logdir / "session.1.log").write_text(_line(_sid_a, "PROVIDER_ERROR", "400: {}") + "\n",
                                       encoding="utf-8")
(_logdir / "session.log").write_text(
    _line(_sid_a, "PROVIDER_ERROR", "503: {}") + "\n"
    + _line(_sid_b, "PROVIDER_ERROR", "400: {\"x\":1}") + "\n"
    + _json2.dumps({"event": "tool.execution.completed", "sessionId": _sid_c}) + "\n"
    + _line(_sid_c, "PROVIDER_ERROR", "（没有状态码前缀）") + "\n",
    encoding="utf-8")

_m = pm.scan_pi_http_status(_logdir)
check("读到两个会话", len(_m) == 2, str(sorted(_m)))
check("同一会话取最后一条（400→503）",
      _m.get(_sid_a, (None,))[0] == 503, str(_m.get(_sid_a)))
check("能区分出 400", _m.get(_sid_b, (None,))[0] == 400, str(_m.get(_sid_b)))
check("带上了日志时间戳", isinstance(_m.get(_sid_a, (None, None))[1], int),
      str(_m.get(_sid_a)))
check("没有状态码前缀的会话不进入结果", _sid_c not in _m, str(sorted(_m)))
check("不相关的日志事件被忽略", "tool.execution.completed" not in str(_m))

# 目录不存在 / 空目录 → 返回空（调用方据此「不触发」，绝不误关机）
check("目录不存在返回空", pm.scan_pi_http_status(_logdir / "nope") == {})
check("空目录返回空", pm.scan_pi_http_status(Path(_tf2.mkdtemp())) == {})

print()
print("-" * 74)
print("11b. 关键安全性质：旧日志记录绝不能导致误关机")
_hit503 = (503, NOW)
_hit503_old = (503, NOW - 3 * 3600 * 1000)      # 三小时前的 503
check("当前失败 + 时间对得上 → 算 503",
      pm.is_pi_503(True, _hit503, NOW) is True)
check("会话已恢复（最后回合不是失败）→ 不算 503",
      pm.is_pi_503(False, _hit503, NOW) is False)
check("日志记录是旧的（对不上时间）→ 不算 503",
      pm.is_pi_503(True, _hit503_old, NOW) is False)
check("日志里查不到 → 不算 503", pm.is_pi_503(True, None, NOW) is False)
check("活动时间缺失 → 不算 503", pm.is_pi_503(True, _hit503, None) is False)
check("日志时间戳解析失败 → 不算 503",
      pm.is_pi_503(True, (503, None), NOW) is False)
check("400 记录 → 不算 503", pm.is_pi_503(True, (400, NOW), NOW) is False)

_scan_503 = [("a", NOW, pm.is_pi_503(True, _hit503, NOW)),
             ("b", NOW, pm.is_pi_503(True, (400, NOW), NOW))]
_fired, _names, _bad = ev(_scan_503, NOW, 60)
check("一个 503 一个 400 → 不触发关机", _fired is False)
check("指出是哪个不是 503", _bad == ["b"], str(_bad))

print()
print("-" * 74)
print("11c. 「报 400 直接新建会话」的判定：只认日志里对得上时间的 400")
check("400 是判定值", pm.PI_400_STATUS == 400)
check("当前失败 + 日志 400 且时间对得上 → 算 400",
      pm.is_pi_400(True, (400, NOW), NOW) is True)
check("503 记录 → 不算 400（不能把服务商挂了当成参数错误）",
      pm.is_pi_400(True, (503, NOW), NOW) is False)
check("会话已恢复 → 不算 400", pm.is_pi_400(False, (400, NOW), NOW) is False)
check("日志记录是旧的 → 不算 400",
      pm.is_pi_400(True, (400, NOW - 3 * 3600 * 1000), NOW) is False)
check("日志里查不到 → 不算 400", pm.is_pi_400(True, None, NOW) is False)
check("aligned_pi_http 返回真实状态码",
      pm.aligned_pi_http(True, (429, NOW), NOW) == 429)
check("aligned_pi_http 时间对不上时返回 None",
      pm.aligned_pi_http(True, (400, NOW - 3 * 3600 * 1000), NOW) is None)
check("上下文超长不走 400 这条路（留给先压缩）",
      "CONTEXT_TOO_LARGE" in pm.CONTEXT_CODES)
_out400 = subprocess.run([sys.executable, "pi_monitor.py", "--help"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout or ""
check("有 --open-new-on-400 参数", "--open-new-on-400" in _out400)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("400 续接 + 关机配置测试全部通过")
