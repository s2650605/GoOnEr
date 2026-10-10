#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pi_monitor.py — 监控 PI-Desktop 会话，自动处理"中止"，按退避节奏发送"继续"。

与 codex_monitor.py 的差别（重要）：
  Codex 有命令行入口（codex queue --thread <id> --message "继续"），
  PI-Desktop 没有：host-core 只接受 Electron 主进程的 stdio 握手，
  外部进程无法直接给会话投递消息。因此本脚本把"发送"交给一个 Pi 插件
  （local.session-keeper），通过文件投递指令：

    指令目录  <PI_DESKTOP_DATA_DIR>/session-keeper/commands/<id>.json
    结果目录  <PI_DESKTOP_DATA_DIR>/session-keeper/results/<id>.json
    心跳文件  <PI_DESKTOP_DATA_DIR>/session-keeper/plugin.json

  插件内部调用官方 host 操作 agent/prompt（"Send a prompt to a session's
  Agent"），这是唯一被官方允许、且不依赖重启参数的外部发送通道。

判定逻辑（读 pi.sqlite，只读）：
  - 会话最后一个 turn 的状态属于"中止"（默认 aborted,error）；
  - 且会话自最后活动（sessions.updated_at）起持续空闲满"本次等待时长"；
    仍在活动的会话（模型正在输出、自动重试）不会触发；
  - 触发发送前再读一次库二次确认：状态仍是中止、且仍无活动，才发送。

等待节奏（连续失败退避，每次带 ±jitter 秒随机）：
  同一会话连续失败第 1、2 次等 1 分钟，第 3 次 3 分钟，第 4 次起 5 分钟
  （默认 --backoff-minutes 1,1,3,5,5）。中间只要成功完成一轮
  （状态 completed/complete）就清零。

关机规则（两条独立开关，默认都不勾选）：
  1) 特定错误码连续 N 次（默认 3 次，按 turns.error_code 判断，如 STREAM_FAILED）；
  2) 所有被监控会话全部无活动持续满 N 分钟（默认 20 分钟）。

上下文超限续接（Pi 端独有；Codex 端对应的是「400 续接」）：
  同一个会话一旦上下文超限（turns.error_code = CONTEXT_TOO_LARGE），
  再发「继续」几乎必然继续超限 —— 重试本身是白费的。因此这里做的是
  「换一个能继续的方式」，而不是反复重试：

    压缩优先（默认）：先调 agent/compact 压缩该会话的上下文，再发「继续」；
                      若压缩后仍连续超限，则退化为「新建会话续接」。
    直接新建会话   ：立刻在同项目目录下新建会话（session/create），
                      首条消息带 pi-desktop://session/<原id> 深链，
                      由 Pi 自动把原会话最近若干条消息附给新会话。
    关闭           ：不处理（仍按普通中止逻辑重试）。

  触发条件与 Codex 一致：同一会话连续 N 次（默认 3 次）出现上下文超限错误码，
  二次确认后执行。执行后原会话退出监控，新会话自动纳入监控。

  为什么新会话必须建在原项目的同一目录下：Pi 只接受「同项目」的会话引用，
  跨项目的 pi-desktop://session/<id> 会被静默跳过，交接内容就丢了。

用法：
  python pi_monitor.py                          # 自动取最近活跃的 2 个会话
  python pi_monitor.py --thread <UUID> ...      # 指定会话
  python pi_monitor.py --dry-run --once         # 自检：只检测不发送/不关机
  更多参数见 --help
"""

import argparse
import datetime
import json
import os
import random
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

# 跨来源关机协调（同目录，打包时一起带上）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import shutdown_coord as coord_mod
# PI-Desktop 的「已归档会话」表：归档状态不在 pi.sqlite 里，见 pi_archive.py
import pi_archive

# 打包成 exe 后强制 stdout/stderr 用 UTF-8，避免 GUI 端读取中文日志乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = Path.home()
# PI-Desktop 的数据目录；应用自身也用这个环境变量
PI_HOME = Path(os.environ.get("PI_DESKTOP_DATA_DIR", HOME / ".pi-desktop"))
DB = PI_HOME / "pi.sqlite"
# 插件执行器的文件通道
KEEPER_DIR = PI_HOME / "session-keeper"
CMD_DIR = KEEPER_DIR / "commands"
RES_DIR = KEEPER_DIR / "results"
HEARTBEAT = KEEPER_DIR / "plugin.json"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 会话/turn 状态的中文名，日志与 GUI 共用口径
STATUS_CN = {
    "running": "进行中",
    "queued": "排队中",
    "complete": "已完成",
    "completed": "已完成",
    "error": "已失败",
    "aborted": "已停止",
    "interrupted": "已停止",
}
# 视为"已正常完成一轮"的状态：用于清零连续失败计数
DONE_STATUSES = {"complete", "completed"}

# 「上下文超限」错误码：同一个会话重发必然再超限，需要压缩或换新会话才能继续
CONTEXT_CODES = {"CONTEXT_TOO_LARGE"}

# 「真实 HTTP 状态码」判定用：Pi 的 turns.error_code 把 400 / 503 / 5xx 都记成
# PROVIDER_ERROR，光看它区分不了；真实状态码只在 Pi 日志里（见 scan_pi_http_status）。
# 两个用途：400 → 直接新建会话续接；503 → 「全部近期活跃会话都报 503」关机。
# Codex 侧不依赖日志（它的错误 JSON 里带 HTTP 码），按 400 / 503 精确匹配。
PI_400_STATUS = 400
PI_503_STATUS = 503
# 日志时间戳与「该会话最后一个回合」的时间差容忍度（毫秒）。
# 超过这个差值说明日志里那条失败记录不是当前这个回合的（可能日志缺失/轮转），
# 此时不认它 —— 宁可不动作，也不要拿旧记录做决定（旧记录会把机器关掉）。
PI_503_MATCH_MS = 5 * 60 * 1000
PI_LOG_DIR = PI_HOME / "logs" / "app"

def aligned_pi_http(failed, hit, upd_ms, match_ms=PI_503_MATCH_MS):
    """该会话「当前这个失败回合」在 Pi 日志里的真实 HTTP 状态码；判不出来返回 None。

    failed: 会话最后一个回合是否失败
    hit:    scan_pi_http_status 给出的 (状态码, 日志时间戳毫秒) 或 None
    upd_ms: 该会话最后活动时间（毫秒）

    两条都必须满足，缺一不可：
      1) 当前最后一个回合确实是失败（否则它早就恢复了）；
      2) 日志里那条失败记录的时间与这个回合对得上（否则是旧记录）。
    宁可判成「判不出来」（不动作），也不要拿旧记录做决定。
    """
    if not (failed and hit and upd_ms is not None and hit[1] is not None):
        return None
    if abs(hit[1] - upd_ms) > match_ms:
        return None
    return hit[0]

def is_pi_503(failed, hit, upd_ms, match_ms=PI_503_MATCH_MS):
    """该会话当前是否「正处于 503 失败」。宁可判成「不是 503」，也不拿旧记录关机。"""
    return aligned_pi_http(failed, hit, upd_ms, match_ms) == PI_503_STATUS

def is_pi_400(failed, hit, upd_ms, match_ms=PI_503_MATCH_MS):
    """该会话当前是否「正处于 400 失败」（用于「报 400 直接新建会话续接」）。"""
    return aligned_pi_http(failed, hit, upd_ms, match_ms) == PI_400_STATUS
PI_LOG_TAIL_BYTES = 4 * 1024 * 1024
_HTTP_PREFIX_RE = re.compile(r"^(\d{3})\b")
# 续接模式
CONTINUE_OFF = "off"        # 不处理
CONTINUE_COMPACT = "compact"  # 先压缩上下文，压不动再新建会话
CONTINUE_NEW = "new"        # 直接新建会话续接


def scan_pi_http_status(log_dir=None, tail_bytes=PI_LOG_TAIL_BYTES):
    """扫描 Pi 日志，返回 {session_id: HTTP 状态码}（每个会话取最后一条失败记录）。

    为什么要读日志：turns.error_code 把 400 和 503 都记成 PROVIDER_ERROR，
    只看它无法区分「参数错误」和「服务商挂了」。真实状态码只在日志里：

        {"event":"agent.turn.failed","sessionId":"…","code":"PROVIDER_ERROR",
         "data":{"message":"503: {…}"}}

    message 的前缀就是 HTTP 状态码（实测 503/400/429/502/401/500 等）。

    读不到就返回空：调用方把「无法确认」当作「不是 503」——
    宁可漏触发（不关机），也不要凭猜测关机。
    """
    out = {}
    d = Path(log_dir) if log_dir else PI_LOG_DIR
    try:
        files = sorted(d.glob("session*.log"))   # session.1.log 在前，session.log 在后
    except Exception:
        return out
    for p in files:
        try:
            size = p.stat().st_size
            with open(p, "rb") as fh:
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                    fh.readline()          # 丢掉可能被截断的半行
                raw = fh.read()
        except Exception:
            continue
        for line in raw.decode("utf-8", "replace").splitlines():
            if "agent.turn.failed" not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            sid = str(o.get("sessionId") or "")
            if not sid:
                continue
            msg = str((o.get("data") or {}).get("message") or "")
            m = _HTTP_PREFIX_RE.match(msg)
            if not m:
                continue
            # 连同时间戳一起返回：调用方要拿它和「该会话最后一个回合」对齐，
            # 否则一个早已恢复正常的会话会被它几小时前的 503 记录误判成还在报 503。
            out[sid] = (int(m.group(1)), _parse_iso_ms(o.get("ts")))
    return out

def _parse_iso_ms(ts):
    """把日志里的 ISO 时间戳转成 epoch 毫秒；解析不了返回 None。"""
    try:
        return int(datetime.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def write_monitor_heartbeat(dry_run, poll_seconds):
    """写「监控脚本存活」心跳，供插件面板显示「监控脚本：运行中」。

    为什么需要：插件只是执行器，它自己看不出有没有人在监控。
    缺了这个文件，用户很容易以为「插件装了就等于生效了」——
    结果会话报错后没人处理，事后还查不出原因（这次就是这么踩的）。

    poll_seconds 一并写进去：面板据此判断心跳是否过期，
    而不是写死一个阈值（用户可能把轮询间隔调得很长）。
    """
    try:
        KEEPER_DIR.mkdir(parents=True, exist_ok=True)
        path = KEEPER_DIR / "monitor-pi.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "source": "pi",
            "at": int(time.time() * 1000),
            "pid": os.getpid(),
            "dryRun": bool(dry_run),
            "pollSeconds": float(poll_seconds or 0),
        }, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass  # 心跳写不进去不该影响监控本身


def resolve_context_settings(cli_mode, cli_threshold, hb):
    """解析「报错/上下文超限续接」的生效设置。

    优先级：命令行显式值 > 插件面板（心跳文件）> 内置默认。
    CLI 用 "auto" 表示「没指定，听插件的」——这样在插件面板里勾一次就生效。

    返回 (模式, 连续次数)。模式为 off/compact/new；
    次数 <= 0 时模式强制为 off（0 = 关闭该规则）。
    """
    hb = hb or {}
    # 插件面板的「启用」开关关闭时等价于 off；命令行显式指定时仍以命令行为准。
    plugin_on = hb.get("contextContinue") is not False
    mode = coord_mod.resolve_setting(
        cli_mode,
        hb.get("contextContinueMode") if plugin_on else CONTINUE_OFF,
        CONTINUE_COMPACT,
    )
    if mode not in (CONTINUE_OFF, CONTINUE_COMPACT, CONTINUE_NEW):
        mode = CONTINUE_COMPACT

    raw = coord_mod.resolve_setting(cli_threshold,
                                    hb.get("contextContinueThreshold"), 3)
    try:
        threshold = int(float(raw))
    except (TypeError, ValueError):
        threshold = 3
    threshold = max(0, threshold)
    if threshold <= 0:
        mode = CONTINUE_OFF
    return mode, threshold


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_str(ms):
    """epoch 毫秒 → 本地 HH:MM:SS。"""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")
    except Exception:
        return str(ms)


def log(msg, f=None):
    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    if f:
        try:
            f.write(line + "\n")
            f.flush()
        except Exception:
            pass


def ro_connect():
    """只读打开 Pi 数据库。"""
    return sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=10)


# ---------------------------------------------------------------- 会话读取


def session_rows(con, limit=200):
    """读取未删除的会话（带项目路径与最后 turn 状态）。"""
    return con.execute(
        """
        select s.id, coalesce(s.title,''), s.project_id, p.path, s.updated_at,
               s.permission_mode,
               (select t.status from turns t where t.session_id = s.id
                 order by t.started_at desc limit 1) as last_status,
               (select t.error_code from turns t where t.session_id = s.id
                 order by t.started_at desc limit 1) as last_error,
               (select t.started_at from turns t where t.session_id = s.id
                 order by t.started_at desc limit 1) as last_started
        from sessions s
        left join projects p on p.id = s.project_id
        where s.deleted_at is null
        order by s.updated_at desc
        limit ?
        """,
        (limit,),
    ).fetchall()


def project_name(path):
    if not path:
        return "（无项目）"
    return Path(str(path).replace("\\\\?\\", "")).name or "（无项目）"


def build_name(title, path):
    """会话显示名 = 项目目录名 / 会话标题。"""
    proj = project_name(path)
    conv = " ".join((title or "").split())[:40]
    if proj and conv:
        return f"{proj} / {conv}"
    return proj or conv or "?"


def pick_sessions(con, explicit, auto_n, active_within_minutes=0):
    """选择要监控的会话。

    三种模式：
      1) explicit 指定了 --thread → 只监控这些
      2) active_within_minutes > 0 → 监控「最近 N 分钟内有活动」的全部会话
         （这是「监控所有会话」的安全实现：不会去碰几天前的历史遗留会话）
      3) 否则 → 取最近活跃的 auto_n 个
    """
    rows = session_rows(con)
    # 已归档的会话不纳入监控（「已删除」由 session_rows 的 deleted_at 过滤掉）
    archived, _note = pi_archive.archived_ids()
    if archived:
        rows = [r for r in rows if r[0] not in archived]
    by_id = {r[0]: r for r in rows}
    out = []
    if explicit:
        for sid in explicit:
            if sid in archived:
                continue
            r = by_id.get(sid)
            if r is None:
                # 指定了库里查不到的会话：仍纳入监控，只是信息不全
                out.append({"id": sid, "name": sid[:8], "title": "", "path": None,
                            "updated_at": None, "status": "", "error": None})
            else:
                out.append(session_info(r))
    elif active_within_minutes > 0:
        cutoff = time.time() * 1000 - active_within_minutes * 60_000
        for r in rows:
            if r[4] is not None and r[4] >= cutoff:
                out.append(session_info(r))
    else:
        for r in rows[:auto_n]:
            out.append(session_info(r))
    return out


def session_info(row):
    sid, title, _pid, path, updated, perm, status, err, _started = row
    return {
        "id": sid,
        "name": build_name(title, path),
        "title": title,
        "path": path,
        "updated_at": updated,
        "permission_mode": perm or "",
        "status": status or "",
        "error": err,
    }

def session_exists(con, session_id):
    """会话是否仍然存在且未被删除。"""
    row = con.execute(
        "select 1 from sessions where id=? and deleted_at is null", (session_id,)
    ).fetchone()
    return row is not None


# 权限模式的中文名（与 Pi 界面一致）
PERMISSION_CN = {
    "auto": "全自动",
    "accept-edits": "自动接受编辑",
    "ask": "每次询问",
    "inherit": "跟随默认",
}
# 只有「全自动」才不会在半夜弹确认；其余模式都可能卡住等用户
UNATTENDED_SAFE_MODES = {"auto"}


def check_unattended(con, sessions, f):
    """检查会话能否无人值守跑通宵，返回不安全会话列表。

    为什么需要这个：Pi 的权限模式决定工具操作要不要问用户。
      auto          全自动：不询问，直接跑（睡觉时唯一安全的模式）
      accept-edits  自动接受编辑，但其它高风险操作仍会问
      ask           每次询问
      inherit       跟随默认（默认可能是"每次询问"）
    只要不是 auto，半夜弹出确认就会一直卡到天亮 —— 这恰恰是
    「挂机续跑」要解决的问题，所以启动时必须明确警告。
    """
    unsafe = []
    for s in sessions:
        mode = s.get("permission_mode") or ""
        # 解析 inherit：它跟随应用默认权限模式，实际生效的是默认值
        if mode == "inherit":
            row = con.execute(
                "select value_json from kv where ns='app' and key='app' limit 1"
            ).fetchone()
            try:
                default_mode = (json.loads(row[0]) if row and row[0] else {}).get(
                    "defaultPermissionMode", "")
            except Exception:
                default_mode = ""
            effective = default_mode or "inherit"
            shown = f"跟随默认（{PERMISSION_CN.get(effective, effective)}）"
        else:
            effective = mode
            shown = PERMISSION_CN.get(mode, mode or "未知")
        if effective in UNATTENDED_SAFE_MODES:
            continue
        unsafe.append((s, shown, effective))
    return unsafe


def warn_unattended(con, sessions, args, f):
    """启动时警告「这些会话半夜会卡住」，并给出解决办法。"""
    unsafe = check_unattended(con, sessions, f)
    if not unsafe:
        log("无人值守检查：所有被监控会话都是「全自动」模式，半夜不会弹确认 ✓", f)
        return True
    log("=" * 60, f)
    log(f"无人值守警告：{len(unsafe)} 个会话不是「全自动」模式，", f)
    log("=" * 60, f)
    for s, eff in unsafe:
        log(f"  · {s['name']}（{s['id'][:8]}…）当前权限模式：{eff}", f)
    log("", f)
    log("解决办法（任选其一）：", f)
    log("  1. 在 Pi 里打开该会话 → 权限模式改成「全自动」，再启动本监控", f)
    log("  2. 用「会话续跑」插件面板的「全部切到全自动」按钮批量设置", f)
    log("  3. 只做检测不发消息：加 --dry-run", f)
    log("  4. 明确接受可能卡住：加 --allow-unsafe", f)
    log("", f)
    if not getattr(args, "allow_unsafe", False) and not args.dry_run:
        log("监控退出（未发送任何消息）。确认要带着这个风险继续，请加 --allow-unsafe。", f)
        return False
    log("（已按 --allow-unsafe / dry-run 继续，但请知悉半夜可能卡住）", f)
    return True


def refresh_names(con, sessions, f):
    """会话在 Pi 端被重命名后刷新显示名（每轮重读标题，变化则更新并打日志）。"""
    for s in sessions:
        row = con.execute(
            "select coalesce(title,''), p.path from sessions s "
            "left join projects p on p.id = s.project_id where s.id=?",
            (s["id"],),
        ).fetchone()
        if row is None:
            continue
        name = build_name(row[0], row[1])
        if name != s["name"]:
            old = s["name"]
            s["name"] = name
            s["title"] = row[0]
            s["path"] = row[1]
            log(f"[{old}] 会话在 Pi 端重命名 → [{name}]", f)


def continue_session(con, s, st, args, context_continue, f):
    """上下文超限续接：压缩上下文，或新建会话并把任务交接过去。

    返回新会话的 id（已成功新建并纳入监控）或 None（未新建）。

    为什么不是简单重发「继续」：CONTEXT_TOO_LARGE 是确定性错误 ——
    同一个会话的上下文已经超出模型上限，再发一次只会再超限一次，
    徒增 token 消耗。所以这里只有两条真正有用的路：
      1) 压缩上下文（agent/compact），把历史压掉一部分后还能继续用原会话；
      2) 新建会话，靠深链把「要干什么」带过去。

    compact 模式会先试 1)，压缩成功就发「继续」让原会话接着跑；
    只有当压缩本身失败（或压缩后仍再次超限）才退到 2)。
    """
    name, sid = s["name"], s["id"]
    project = s.get("path") or ""

    if context_continue == CONTINUE_COMPACT:
        log(f"[{name}] 尝试压缩上下文（连续超限第 {st['context_streak']} 次）…", f)
        ok, detail = send_compact(sid, args.dry_run, f, args.compact_timeout)
        if ok:
            log(f"[{name}] 上下文压缩成功，发送「继续」让原会话接着跑", f)
            sent, send_detail = send_prompt(sid, args.message, args.dry_run, f,
                                            args.send_timeout)
            st["context_streak"] = 0
            st["sent_key"] = None
            st["target"] = None
            if sent:
                log(f"[{name}] 压缩后已发送 {args.message!r} ✓（原会话继续，未新建会话）", f)
                st["continued"] = True   # 同一会话不再反复压缩续接
                return None
            log(f"[{name}] 压缩成功但发送失败：{send_detail} → 转为新建会话续接", f)
        else:
            log(f"[{name}] 上下文压缩失败：{detail} → 转为新建会话续接", f)

    # —— 新建会话续接 ——
    if not project:
        log(f"[{name}] 无法新建会话续接：库里查不到该项目目录（{sid[:8]}…）", f)
        return None

    # 提取「还要干什么」：todo 清单为主，最后一条用户消息兜底
    todos = unfinished_todos(con, sid)
    last_user = last_user_message(con, sid)
    if todos:
        log(f"[{name}] 提取到 {len(todos)} 项未完成待办，将一并交接", f)
    else:
        log(f"[{name}] 该会话没有未完成待办，改用最后一条任务描述交接", f)
    content = build_handoff_message(name, sid, args.message, project,
                                    todos=todos, last_user=last_user)
    # 标题带「续接」前缀：便于识别这是自动续接出来的会话，也便于去重
    title = f"续接：{' '.join((s.get('title') or name).split())[:40]}"
    model_hint = session_model_info(con, sid)
    if model_hint:
        log(f"[{name}] 新会话继承原会话模型："
            f"{model_hint.get('providerId', '?')[:8]}… / {model_hint.get('modelId', '?')}", f)
    else:
        log(f"[{name}] 未取到原会话模型信息，新会话将使用应用默认模型", f)

    ok, detail = send_create(project, title, content, model_hint,
                             args.dry_run, f, args.context_new_session_timeout)
    if not ok:
        log(f"[{name}] 新建会话失败：{detail}", f)
        return None
    if args.dry_run:
        log(f"[{name}] [dry-run] 将新建会话并把任务交接过去（项目 {project}）", f)
        # 记账：dry-run 下也算「本轮已按新建会话处理」，否则会继续走普通重试
        # 逻辑再报一次「已发送」，日志与真实行为对不上。
        st["continued"] = True
        return None

    log(f"[{name}] 已新建会话 {detail[:8]}… 并投递交接消息（深链指向原会话 {sid[:8]}…）", f)
    st["context_streak"] = 0
    st["continued"] = True
    return detail


def adopt_new_session(con, sessions, state, f, new_id, origin):
    """把续接新建的会话纳入监控，并让原会话退出监控。

    为什么要退出原会话：它的上下文已经确定超限，继续盯着只会不断触发续接；
    任务已经交给新会话，盯新会话才是有效的。
    """
    sessions[:] = [s for s in sessions if s["id"] != origin["id"]]
    state.pop(origin["id"], None)
    if any(s["id"] == new_id for s in sessions):
        return
    rows = {r[0]: r for r in session_rows(con)}
    row = rows.get(new_id)
    if row is not None:
        info = session_info(row)
    else:
        # 刚建的会话可能还没落进这次查询结果，用已知信息先兜住
        info = {"id": new_id, "name": f"{project_name(origin.get('path'))} / 续接会话",
                "title": "", "path": origin.get("path"), "updated_at": None,
                "permission_mode": origin.get("permission_mode", ""),
                "status": "", "error": None}
    sessions.append(info)
    state[new_id] = new_session_state()
    log(f"[{info['name']}] 新会话已加入监控（{new_id[:8]}…），原会话退出监控", f)


def sync_sessions(con, sessions, state, f, explicit, auto_n, active_within=0):
    """动态同步监控集合。

    两种模式：
      A) 固定数量（auto_n）：会话失效时剔除，数量不足则补入最近活跃的；
      B) 活跃窗口（active_within>0）：把所有「最近 N 分钟有活动」的会话都纳入，
         持续吸纳新出现的活跃会话 —— 这就是「监控所有会话」的实现方式，
         同时天然排除了几天前的历史遗留会话。

    显式 --thread 模式只剔除失效会话，不补足。
    """
    refresh_names(con, sessions, f)
    removed = [s for s in sessions if not session_exists(con, s["id"])]
    for s in removed:
        log(f"[{s['name']}] 会话已被删除，停止监控（{s['id'][:8]}…）", f)
        sessions.remove(s)
        state.pop(s["id"], None)
    # 已归档 = 用户明确不想再被自动打扰：和「已删除」一样退出监控。
    # 归档状态只在 PI-Desktop 的 Local Storage 里（见 pi_archive.py）。
    archived, _note = pi_archive.archived_ids()
    for s in [x for x in sessions if x["id"] in archived]:
        log(f"[{s['name']}] 会话已归档，停止监控（{s['id'][:8]}…）", f)
        sessions.remove(s)
        state.pop(s["id"], None)
    if explicit:
        return

    have = {s["id"] for s in sessions}

    if active_within > 0:
        # 活跃窗口模式：把窗口内的活跃会话全部纳入（含续接新开出来的会话）
        try:
            fresh = pick_sessions(con, [], 0, active_within_minutes=active_within)
        except Exception:
            fresh = []
        for fc in fresh:
            if fc["id"] in have:
                continue
            log(f"[{fc['name']}] 检测到活跃会话，加入监控（{fc['id'][:8]}…）", f)
            sessions.append(fc)
            state[fc["id"]] = new_session_state()
            have.add(fc["id"])
        return

    if len(sessions) >= auto_n:
        return
    try:
        fresh = pick_sessions(con, [], max(auto_n * 2, 1))
    except Exception:
        fresh = []
    for fc in fresh:
        if len(sessions) >= auto_n:
            break
        if fc["id"] in have:
            continue
        log(f"[{fc['name']}] 检测到新的活跃会话，加入监控（{fc['id'][:8]}…）", f)
        sessions.append(fc)
        state[fc["id"]] = new_session_state()
        have.add(fc["id"])


def last_turn(con, session_id):
    """返回 (status, error_code, started_at)。"""
    row = con.execute(
        "select status, error_code, started_at from turns where session_id=? "
        "order by started_at desc limit 1",
        (session_id,),
    ).fetchone()
    return row


def new_session_state():
    """单个会话的监控状态。

    seen/target/sent_key 等用于普通的「中止 → 等待 → 发送继续」节奏；
    context_streak/continued 用于上下文超限续接：前者累计连续超限次数，
    后者标记「这个会话已经续接过」，避免同一会话被反复续接。
    """
    return {"seen": None, "target": None, "sent_key": None,
            "last_upd": None, "last_send": 0.0, "recent": [],
            "fail_count": 0, "code_streak": 0,
            "context_streak": 0, "continued": False}


def session_model_info(con, session_id):
    """读取原会话的 provider/model/mode/thinking，用于让新会话继承同样的配置。

    继承很重要：不指定模型时，新建的会话会退回应用默认模型，可能和原会话
    完全不是同一个（比如原会话用强模型跑复杂任务，默认模型却是个轻量版）。
    """
    try:
        row = con.execute(
            "select provider_id, model_id, mode, thinking_level from sessions where id=?",
            (session_id,),
        ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    provider_id, model_id, mode, thinking = row
    info = {}
    if provider_id and model_id:
        info["providerId"] = provider_id
        info["modelId"] = model_id
    if mode in ("agent", "plan", "goal"):
        info["mode"] = mode
    if thinking:
        info["thinkingLevel"] = thinking
    return info


def session_updated(con, session_id):
    """会话最后活动时间（epoch 毫秒），来自 sessions.updated_at。"""
    row = con.execute("select updated_at from sessions where id=?", (session_id,)).fetchone()
    return row[0] if row else None


def recent_notifications(con, since_ms):
    """读取 Pi 自己记录的任务完成/失败通知（notifications 表）。

    Pi 在每个 turn 结束时会写一条 task.completed / task.failed，
    外部工具据此可以「知道会话刚跑完」——这正是 Codex 侧没有的信息。
    """
    try:
        return con.execute(
            "select id, kind, session_id, session_title, error_code, created_at "
            "from notifications where created_at > ? order by created_at asc",
            (since_ms,),
        ).fetchall()
    except Exception:
        return []


def notify_desktop(title, body, dry_run, f):
    """弹一条系统通知（Windows 用 toast 脚本，Mac 用 osascript）。

    失败不影响监控主流程，只记日志。
    """
    if dry_run:
        log(f"[dry-run] 将弹通知：{title} — {body}", f)
        return
    try:
        if os.name == "nt":
            # 用 PowerShell 发 Windows 通知（无需第三方模块）
            safe_title = title.replace("'", "''")
            safe_body = body.replace("'", "''")
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] > $null; "
                "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                "$x = $t.GetElementsByTagName('text'); "
                f"$x.Item(0).AppendChild($t.CreateTextNode('{safe_title}')) > $null; "
                f"$x.Item(1).AppendChild($t.CreateTextNode('{safe_body}')) > $null; "
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'会话监控').Show([Windows.UI.Notifications.ToastNotification]::new($t));"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW,
            )
        else:
            script = (
                f'display notification "{body}" with title "{title}"'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
    except Exception as e:
        log(f"弹通知失败（不影响监控）：{e!r}", f)


# ---------------------------------------------------------------- 插件执行器


def plugin_status(now=None):
    """读取插件心跳，判断执行器是否可用。

    返回 (ok, 说明, 详情)。心跳过期（超过 30 秒没更新）视为不可用，
    避免 Pi 退出后脚本仍以为能发送。
    """
    if not HEARTBEAT.exists():
        return False, (
            f"未找到插件执行器心跳：{HEARTBEAT}\n"
            "  → 请安装并启用插件 local.session-keeper（见 使用说明.txt 的【Pi 插件】一节）"
        ), {}
    try:
        data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"心跳文件无法解析：{e!r}", {}
    ts = data.get("at") or 0
    age = (now or time.time() * 1000) - ts
    if age > 30_000:
        return False, f"插件执行器心跳已过期（{age / 1000:.0f} 秒未更新），Pi 可能已退出", data
    if data.get("running") is False:
        return False, "插件执行器已停止（心跳显示 running=false）", data
    if data.get("enabled") is False:
        return False, "插件执行器已被关闭（请在插件面板里勾选启用）", data
    return True, f"执行器正常（插件 v{data.get('version', '?')}，PID {data.get('pid', '?')}）", data


def send_prompt(session_id, content, dry_run, f, timeout_s=90):
    """通过插件把 content 投递给会话，返回 (是否成功, 摘要)。

    实现：写一条指令文件 → 轮询结果文件 → 清理。
    不做任何重试（节奏由本脚本的退避逻辑统一控制）。
    """
    if dry_run:
        log(f"[dry-run] 不实际发送，将投递 agent/prompt 给 {session_id[:8]}…：{content!r}", f)
        return True, "dry-run"
    try:
        CMD_DIR.mkdir(parents=True, exist_ok=True)
        RES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"创建指令目录失败：{e!r}"

    cmd_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    cmd_file = CMD_DIR / f"{cmd_id}.json"
    res_file = RES_DIR / f"{cmd_id}.json"
    payload = {"id": cmd_id, "action": "prompt", "sessionId": session_id, "content": content}

    # 先写临时文件再改名，避免插件读到半个文件
    tmp = cmd_file.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cmd_file)
    except Exception as e:
        return False, f"写入指令失败：{e!r}"

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if res_file.exists():
            try:
                result = json.loads(res_file.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(0.5)
                continue
            try:
                res_file.unlink()
            except Exception:
                pass
            if result.get("ok"):
                return True, json.dumps(result.get("result"), ensure_ascii=False)[:200]
            return False, f"{result.get('code') or ''} {result.get('error') or '未知错误'}".strip()
        time.sleep(0.5)

    # 超时：清掉残留指令，避免插件稍后又执行一次
    try:
        cmd_file.unlink()
    except Exception:
        pass
    return False, f"等待插件结果超时（{timeout_s} 秒）"


def send_abort(session_id, dry_run, f, timeout_s=60):
    """通过插件中止会话的当前 turn。"""
    if dry_run:
        log(f"[dry-run] 不实际中止 {session_id[:8]}…", f)
        return True, "dry-run"
    cmd_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    cmd_file = CMD_DIR / f"{cmd_id}.json"
    res_file = RES_DIR / f"{cmd_id}.json"
    payload = {"id": cmd_id, "action": "abort", "sessionId": session_id}
    tmp = cmd_file.with_suffix(".json.tmp")
    try:
        CMD_DIR.mkdir(parents=True, exist_ok=True)
        RES_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cmd_file)
    except Exception as e:
        return False, f"写入指令失败：{e!r}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if res_file.exists():
            try:
                result = json.loads(res_file.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(0.5)
                continue
            try:
                res_file.unlink()
            except Exception:
                pass
            return bool(result.get("ok")), str(result.get("error") or "ok")
        time.sleep(0.5)
    try:
        cmd_file.unlink()
    except Exception:
        pass
    return False, f"等待插件结果超时（{timeout_s} 秒）"

def plugin_call(payload, dry_run, f, timeout_s, what):
    """把一条指令交给插件执行，返回 (ok, 结果 dict 或错误说明)。

    与 send_prompt / send_abort 相同的文件通道，但把「写指令 → 等结果」这段
    抽出来给新动作（compact / create）复用，避免同一套逻辑抄第三、第四遍。
    """
    if dry_run:
        log(f"[dry-run] 不实际执行 {what}：{json.dumps(payload, ensure_ascii=False)[:200]}", f)
        return True, {"dryRun": True}
    try:
        CMD_DIR.mkdir(parents=True, exist_ok=True)
        RES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"创建指令目录失败：{e!r}"

    cmd_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    cmd_file = CMD_DIR / f"{cmd_id}.json"
    res_file = RES_DIR / f"{cmd_id}.json"
    payload = dict(payload, id=cmd_id)

    tmp = cmd_file.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cmd_file)
    except Exception as e:
        return False, f"写入指令失败：{e!r}"

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if res_file.exists():
            try:
                result = json.loads(res_file.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(0.5)
                continue
            try:
                res_file.unlink()
            except Exception:
                pass
            if result.get("ok"):
                return True, result
            return False, f"{result.get('code') or ''} {result.get('error') or '未知错误'}".strip()
        time.sleep(0.5)

    # 超时：清掉残留指令，避免插件稍后又执行一次
    try:
        cmd_file.unlink()
    except Exception:
        pass
    return False, f"等待插件结果超时（{timeout_s} 秒）"


def send_compact(session_id, dry_run, f, timeout_s=200):
    """让插件压缩该会话的上下文（agent/compact）。"""
    ok, detail = plugin_call({"action": "compact", "sessionId": session_id},
                             dry_run, f, timeout_s, f"压缩上下文 {session_id[:8]}…")
    return ok, detail


def send_create(project_path, title, content, model_hint, dry_run, f, timeout_s=120):
    """让插件在指定项目下新建会话，并把 content 作为首条消息投递。

    返回 (ok, 新会话 id 或错误说明)。
    """
    payload = {"action": "create", "projectPath": project_path, "title": title,
               "content": content}
    if model_hint:
        payload.update(model_hint)
    ok, detail = plugin_call(payload, dry_run, f, timeout_s, f"在 {project_path} 新建会话")
    if not ok:
        return False, detail
    if dry_run:
        return True, "dry-run"
    new_id = str(detail.get("sessionId") or "").strip()
    if not new_id:
        return False, "插件未返回新会话 id"
    return True, new_id


def unfinished_todos(con, session_id, limit=30):
    """读该会话未完成的 todo（pending / in_progress），按优先级+顺序返回。

    为什么用 todo：它是「这个会话还要干什么」最准的来源 —— 是 agent 自己在
    执行过程中维护的清单，比从聊天记录里猜进度可靠得多。
    """
    try:
        rows = list(con.execute(
            "select position, content, status, priority from session_todo "
            "where session_id=? and status in ('pending','in_progress') "
            "order by case priority when 'high' then 0 when 'medium' then 1 "
            "else 2 end, position limit ?",
            (session_id, limit)))
    except Exception:
        return []
    return rows


def last_user_message(con, session_id, limit=600):
    """该会话最后一条用户消息（任务原文），截断到 limit 字符。

    用途：todo 有时不全（比如还没建清单就挂了），原文能兜住「到底要做什么」。
    """
    try:
        row = con.execute(
            "select text from messages where session_id=? and role='user' "
            "order by seq desc limit 1", (session_id,)).fetchone()
    except Exception:
        return ""
    if not row or not row[0]:
        return ""
    text = " ".join(str(row[0]).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def build_handoff_message(session_name, session_id, message, project_path,
                          todos=None, last_user=""):
    """构造交接给新会话的首条消息。

    三块内容：
      1) pi-desktop://session/<原id> 深链 —— Pi 会自动把原会话最近若干条消息
         的摘录附进新会话第一轮（官方机制，不需要我们自己搬运整份历史）
      2) 未完成的 todo 清单 —— 「还要干什么」最准的来源
      3) 原会话最后一条用户消息 —— 兜住任务原文，防止 todo 不全

    只给「未完成」的部分，不搬已完成的工作，避免新会话重做一遍。
    """
    link = f"pi-desktop://session/{session_id}"
    parts = [
        f"请继续这个会话未完成的任务：{link}",
        "",
        f"原会话标题：{session_name}",
        f"原会话目录：{project_path}",
        "",
    ]

    if todos:
        parts.append(f"原会话未完成的待办（共 {len(todos)} 项，来自原会话的 todo 清单）：")
        for _pos, content, status, priority in todos:
            mark = "进行中" if status == "in_progress" else "待办"
            pri = f"[{priority}] " if priority else ""
            parts.append(f"  - （{mark}）{pri}{content}")
        parts.append("")

    if last_user:
        parts.append("原会话最后一条任务描述（供参考）：")
        parts.append(f"  {last_user}")
        parts.append("")

    parts += [
        "说明：原会话因为连续报错（服务端 400 一类）无法继续，已由监控工具交接给你。",
        "上面的链接会带出原会话最近的消息摘录；请结合它、待办清单和你自己核对到的"
        "实际状态继续推进，不要从头重做已经完成的部分。",
        "若信息不足以判断进度，请先查看当前目录下的文件和记录再动手；"
        "遇到需要决策的地方自行判断处理，不必逐项询问。",
        f"（本条由会话监控工具自动生成，原文：{message!r}）",
    ]
    return "\n".join(parts)



# ------------------------------------------------- Pi 通知与等待确认提醒


def handle_notifications(con, ctl, args, f):
    """读 Pi 的 task.completed / task.failed 通知，弹系统提醒。

    这是 Pi 侧独有的能力（Codex 没有这种记录）：会话一跑完 Pi 就写一条，
    所以能第一时间知道"某个会话结束了"，而不是靠轮询猜。
    """
    if not getattr(args, "notify", False):
        return
    since = ctl.get("last_notif_ms") or (time.time() * 1000 - 60_000)
    for _id, kind, session_id, title, error_code, created_at in recent_notifications(con, since):
        ctl["last_notif_ms"] = max(ctl.get("last_notif_ms") or 0, created_at)
        name = " ".join((title or session_id or "").split())[:40] or (session_id or "")[:8]
        if kind == "task.completed":
            log(f"[{name}] 会话已完成一轮（Pi 通知）", f)
            notify_desktop("会话已完成", name, args.dry_run, f)
        elif kind == "task.failed":
            log(f"[{name}] 会话执行失败（Pi 通知）：{error_code or '未知错误'}", f)
            notify_desktop("会话执行失败", f"{name}：{error_code or '未知错误'}", args.dry_run, f)


def warn_waiting_confirm(con, sessions, ctl, args, f):
    """检测"会话正在等工具确认"并提醒。

    这种会话不会自己继续，退避发送也没用 —— 只有人去点一下才行，
    所以要单独提醒，避免以为工具失灵。
    """
    if not getattr(args, "notify", False):
        return
    warned = ctl.setdefault("waiting_warned", set())
    for s in sessions:
        row = con.execute(
            "select kind from notifications where session_id=? order by created_at desc limit 1",
            (s["id"],),
        ).fetchone()
        waiting = bool(row) and (row[0] or "") in ("task.waiting", "task.permission")
        if waiting and s["id"] not in warned:
            warned.add(s["id"])
            log(f"[{s['name']}] 会话正在等待工具确认，需要你手动点一下才能继续", f)
            notify_desktop("会话等待确认", f"{s['name']} 需要你确认工具权限", args.dry_run, f)
        elif not waiting:
            warned.discard(s["id"])

# ---------------------------------------------------------------- 关机


def run_shutdown(reason, cmd, dry_run, f):
    if dry_run:
        log(f"[dry-run] 检测到 {reason}，将执行关机指令：{cmd}（不实际执行）", f)
        return False
    log(f"检测到 {reason}，执行关机指令：{cmd}", f)
    try:
        argv = coord_mod.split_shutdown_cmd(cmd)
        if not argv:
            log("关机指令为空，未执行", f)
            return False
        subprocess.run(argv, timeout=30, creationflags=CREATE_NO_WINDOW)
        log("关机指令已执行，系统即将关机", f)
        return True
    except Exception as e:
        # 执行失败必须如实回报，否则调用方会以为已经关机了
        log(f"关机指令执行失败：{e!r} → 未关机，请检查「关机指令」设置", f)
        return False


# ---------------------------------------------------------------- 主流程


def main():
    ap = argparse.ArgumentParser(
        description="PI-Desktop 会话监控：中止后按退避节奏发送「继续」；可选错误码连续 N 次或全部停止 N 分钟后关机")
    ap.add_argument("--thread", action="append", default=[],
                    help="指定监控的会话 UUID（可重复；默认按最近活跃自动取 N 个）")
    ap.add_argument("--auto", type=int, default=2,
                    help="未指定 --thread 时监控的会话数量（默认 2）")
    # 「监控所有会话」的安全实现：只监控最近 N 分钟内有活动的会话。
    # 直接监控全部会误触发几天前的历史遗留 error 会话（实测 58 个里只有 2 个是真的）。
    ap.add_argument("--active-within", type=float, default=0,
                    help="只监控最近 N 分钟内有活动的会话（0=关闭，默认 0）。"
                         "设为正数即为「监控所有活跃会话」模式")
    ap.add_argument("--message", default="继续", help="要发送的消息（默认：继续）")
    ap.add_argument("--backoff-minutes", default="1,1,3,5,5",
                    help="连续失败第 1..N 次的等待分钟数（逗号分隔，最后一个值对后续生效；默认 1,1,3,5,5）")
    ap.add_argument("--jitter", type=float, default=3, help="等待时长随机 ± 秒数（默认 3）")
    ap.add_argument("--poll", type=float, default=10, help="轮询间隔秒数（默认 10）")
    ap.add_argument("--retry-minutes", type=float, default=15,
                    help="发送后仍持续中止（未被处理）时多久重试（默认 15 分钟）")
    ap.add_argument("--max-per-hour", type=int, default=20,
                    help="每会话每小时最多发送次数，0 表示不限（默认 20）")
    ap.add_argument("--statuses", default="aborted,error",
                    help="视为中止的 turn 状态，逗号分隔（默认 aborted,error）")
    ap.add_argument("--shutdown-codes", default="",
                    help="这些 turns.error_code 连续触发 N 次后关机，逗号分隔（空串=关闭该规则）")
    ap.add_argument("--shutdown-threshold", type=int, default=3,
                    help="错误码连续触发多少次后关机（默认 3）")
    ap.add_argument("--shutdown-cmd", default="shutdown /s /f /t 10",
                    help="关机指令（默认 shutdown /s /f /t 10）")
    ap.add_argument("--all-stopped-minutes", type=float, default=0,
                    help="所有被监控会话全部无活动持续满该分钟数后关机（0=关闭该规则，默认 0）")
    # 「近期活跃会话全部报 503」→ 关机。
    # 与上面按单会话连续计数的规则互补：服务商整体挂掉时，
    # 每个会话各报一次 503，单会话计数永远到不了阈值，这条规则才管用。
    ap.add_argument("--shutdown-when-all-503", nargs="?", const="on",
                    default="auto", choices=["auto", "on", "off"],
                    help="所有「近期活跃」的被监控会话都报 503 时关机。"
                         "auto（默认）=听插件面板设置；on=开启；off=关闭")
    ap.add_argument("--all-503-window-minutes", default="auto",
                    help="判定「近期活跃」的时间窗口（分钟，默认 60）。"
                         "auto（默认）=听插件面板设置；历史会话不参与判定")
    ap.add_argument("--send-timeout", type=float, default=90,
                    help="等待插件执行结果的超时秒数（默认 90）")
    ap.add_argument("--notify", action="store_true",
                    help="会话完成/失败/等待工具确认时弹系统通知")
    ap.add_argument("--allow-unsafe", action="store_true",
                    help="允许监控非「全自动」权限模式的会话（半夜可能卡在确认上）")
    ap.add_argument("--log", default=str(Path(__file__).resolve().parent / "pi_monitor.log"),
                    help="日志文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只检测不发送/不关机")
    # 「全部正常完成后关机」：默认 auto = 读插件面板里的设置。
    # 这样睡前在插件面板勾一次就够了，不必再改命令行。
    ap.add_argument("--shutdown-when-all-done", nargs="?", const="on",
                    default="auto", choices=["auto", "on", "off"],
                    help="所有被监控会话都「正常完成」后关机。"
                         "auto（默认）=听插件面板里的设置；on/off=强制开关")
    ap.add_argument("--all-done-confirm-seconds", default="auto",
                    help="「全部正常完成」后再观察多少秒才关机（默认 auto=读插件设置，内置 120）")
    ap.add_argument("--all-sources", default="",
                    help="本次同时监控的来源，逗号分隔（如 pi,codex）。"
                         "用于跨来源协调：要求所有来源都完成才关机")
    ap.add_argument("--shutdown-requires-all-sources", nargs="?", const="on",
                    default="auto", choices=["auto", "on", "off"],
                    help="是否要求所有监控来源都完成才关机。"
                         "auto（默认）=听插件设置；on=要求；off=各自独立判定")
    ap.add_argument("--coord-file", default="",
                    help="跨来源协调文件路径（默认 ~/.session-monitor/shutdown-coord.json）")
    # —— 上下文超限续接（Pi 端独有；Codex 侧对应 --bad400-threshold）——
    ap.add_argument("--context-continue", default="auto",
                    choices=["auto", "off", "compact", "new"],
                    help="报错/上下文超限时的续接方式："
                         "auto（默认）=听插件面板设置；"
                         "compact=先压缩上下文、压不动再新建会话；"
                         "new=直接新建会话续接；off=不处理")
    ap.add_argument("--context-threshold", default="auto",
                    help="连续触发多少次后执行续接。"
                         "auto（默认）=听插件面板设置；0=关闭该规则")
    # Pi 的「400」映射：status 400/422 → PROVIDER_ERROR（上下文超限时是 CONTEXT_TOO_LARGE）。
    # 所以默认把这两个都算作「需要新开会话续接」的错误。
    # 注意 PROVIDER_ERROR 也涵盖 5xx —— Pi 的 turns 表不存 HTTP 码，无法更精确区分。
    ap.add_argument("--context-codes", default="PROVIDER_ERROR,CONTEXT_TOO_LARGE",
                    help="视为「需要续接」的 turns.error_code，逗号分隔。"
                         "Pi 的 400 对应 PROVIDER_ERROR（默认同时含 CONTEXT_TOO_LARGE）")
    ap.add_argument("--context-new-session-timeout", type=float, default=120,
                    help="等待插件新建会话结果的超时秒数（默认 120）")
    ap.add_argument("--compact-timeout", type=float, default=200,
                    help="等待插件压缩上下文结果的超时秒数（默认 200）")
    # 「报 400 直接新建会话」：只有日志里的真实状态码能区分 400 与 503/5xx，
    # 所以这里按日志判定，而不是按 turns.error_code（那会把 503 也算成 400）。
    ap.add_argument("--open-new-on-400", nargs="?", const="on", default="auto",
                    help="Pi 会话真实报 400（读日志里的 HTTP 状态码，时间对齐）时"
                         "直接新建会话续接：不等连续次数、不先压缩。"
                         "auto（默认）=听插件面板设置；on/off 显式开关")
    ap.add_argument("--once", action="store_true", help="只检测一轮后退出（用于自检）")
    args = ap.parse_args()

    stop_statuses = {s.strip() for s in args.statuses.split(",") if s.strip()}
    shutdown_codes = [c.strip() for c in args.shutdown_codes.split(",") if c.strip()]
    context_codes = {c.strip() for c in args.context_codes.split(",") if c.strip()}
    backoff = []
    for x in args.backoff_minutes.split(","):
        x = x.strip()
        if x:
            try:
                backoff.append(float(x))
            except ValueError:
                pass
    if not backoff:
        backoff = [1.0, 1.0, 3.0, 5.0, 5.0]

    def wait_for(n):
        """第 n 次连续失败的等待秒数（分钟档 × 60 ± jitter 随机）。"""
        idx = min(max(1, n) - 1, len(backoff) - 1)
        base = backoff[idx] * 60.0
        return random.uniform(max(0.0, base - args.jitter), base + args.jitter)

    def tier_of(n):
        return f"{backoff[min(max(1, n) - 1, len(backoff) - 1)]:g} 分钟档"

    # —— 关机策略：CLI 显式值 > 插件面板设置 > 内置默认 ——
    # 插件设置来自心跳文件（插件每 3 秒刷新一次）。插件没装/没跑时读不到，
    # 此时退回内置默认，行为与以前一致，不会因为读不到就崩掉。
    _, _detail, hb = plugin_status()
    hb = hb or {}

    def setting_bool(cli_value, hb_key, fallback):
        plugin_value = hb.get(hb_key)
        if plugin_value is None:
            plugin_value = None
        return coord_mod.parse_on_off(
            coord_mod.resolve_setting(cli_value, plugin_value, fallback), fallback)

    def setting_seconds(cli_value, hb_key, fallback):
        raw = coord_mod.resolve_setting(cli_value, hb.get(hb_key), fallback)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return float(fallback)
        return max(0.0, value)

    shutdown_all_done = setting_bool(args.shutdown_when_all_done,
                                     "shutdownWhenAllDone", False)
    all_done_seconds = setting_seconds(args.all_done_confirm_seconds,
                                       "allDoneConfirmSeconds", 120.0)
    require_all_sources = setting_bool(args.shutdown_requires_all_sources,
                                       "shutdownRequiresAllSources", True)
    # —— 报错/上下文超限续接：CLI 显式值 > 插件面板 > 内置默认 ——
    # 与关机策略同一套优先级，所以「睡前在插件面板勾一次」就够，不必再改命令行。
    context_continue, context_threshold = resolve_context_settings(
        args.context_continue, args.context_threshold, hb)

    # —— 真 400 → 直接新建会话：CLI 显式值 > 插件面板 > 内置默认（默认开） ——
    open400_new = setting_bool(args.open_new_on_400, "openNewOn400", True)

    # —— 「全部近期活跃会话报 503」关机：CLI 显式值 > 插件面板 > 内置默认 ——
    all_503_on = setting_bool(args.shutdown_when_all_503,
                              "shutdownWhenAll503", False)
    all_503_window_min = setting_seconds(args.all_503_window_minutes,
                                         "all503WindowMinutes", 60.0)
    monitored_sources = [x.strip() for x in args.all_sources.split(",") if x.strip()]
    # 只监控一个来源（或没告知）时，跨来源要求没有意义
    if len(monitored_sources) <= 1:
        require_all_sources = False

    coord = coord_mod.ShutdownCoordinator(
        source="pi",
        all_sources=monitored_sources or ["pi"],
        coord_file=args.coord_file or None,
        require_all=require_all_sources,
    )

    if not DB.exists():
        print(f"找不到 PI-Desktop 数据库：{DB}\n请确认 PI_DESKTOP_DATA_DIR（当前 {PI_HOME}）")
        sys.exit(1)

    f = open(args.log, "a", encoding="utf-8")
    log(f"pi_monitor 启动 | 数据目录：{PI_HOME}", f)
    log(f"消息：{args.message!r} | 等待节奏：{args.backoff_minutes} 分钟±{args.jitter}s（成功一轮后清零）| "
        f"轮询：{args.poll}s | 中止状态：{sorted(stop_statuses)} | 每小时上限：{args.max_per_hour}", f)
    log(f"关机规则：{shutdown_codes or '无'}-连续{args.shutdown_threshold}次 | "
        f"全部停止 {args.all_stopped_minutes} 分钟 | 指令：{args.shutdown_cmd} | dry_run：{args.dry_run}", f)
    # 把关机策略的实际生效值写进日志：这些值可能来自插件面板，
    # 只写命令行参数的话，出了问题会看不出到底用的是哪套设置。
    if shutdown_all_done:
        src_note = ("插件面板设置" if args.shutdown_when_all_done == "auto"
                    else "命令行")
        log(f"全部完成后关机：开启（观察 {all_done_seconds:.0f} 秒，来自{src_note}）", f)
        if require_all_sources:
            log(f"跨来源要求：开启 —— 需 {'、'.join(coord.all_sources)} 全部完成才关机"
                f"（协调文件 {coord.path}）", f)
        else:
            log(f"跨来源要求：关闭 —— 本来源全部完成即可关机"
                f"（本次来源 {monitored_sources or ['pi']}）", f)
    else:
        log("全部完成后关机：关闭", f)
    if all_503_on:
        src_note = ("命令行" if args.shutdown_when_all_503 != "auto"
                    else "插件面板/内置默认")
        # 说清判定依据：真实 HTTP 状态码来自 Pi 日志，读不到就不算 503（宁可不关）。
        probe = scan_pi_http_status()
        log(f"「全部报 503」关机：开启（近期活跃窗口 {all_503_window_min:g} 分钟，"
            f"来自{src_note}）", f)
        log(f"  Pi 状态码来源：{PI_LOG_DIR}（已读到 {len(probe)} 个会话的最近状态；"
            f"读不到的会话按「不是 503」处理，不会误关机）", f)
    else:
        log("「全部报 503」关机：关闭", f)
    if context_continue == CONTINUE_OFF:
        log("报错/上下文超限续接：关闭", f)
    else:
        mode_cn = ("先压缩上下文、压不动再新建会话"
                   if context_continue == CONTINUE_COMPACT else "直接新建会话续接")
        # 写出生效值与来源：这些值可能来自插件面板，
        # 只写命令行的话，出了问题会看不出到底用的哪套设置。
        ctx_src = "命令行" if args.context_continue != "auto" else "插件面板/内置默认"
        log(f"报错/上下文超限续接：{mode_cn}（连续 {context_threshold} 次 "
            f"{sorted(context_codes)} 后触发，来自{ctx_src}；"
            f"真实 HTTP 503 不续接、不新建会话）", f)

    if context_continue == CONTINUE_OFF:
        log("报 400 直接新建会话：关闭（报错续接总开关已关）", f)
    elif open400_new:
        src_note = ("命令行" if args.open_new_on_400 != "auto" else "插件面板/内置默认")
        log(f"报 400 直接新建会话：开启（按日志里的真实 HTTP 400 判定，"
            f"不等次数、不压缩，来自{src_note}）", f)
    else:
        log("报 400 直接新建会话：关闭（400 仍按普通报错续接规则处理）", f)
    # 监控范围：让日志明确说出「这次到底盯了哪些会话」，便于核对设置是否生效
    if args.active_within > 0:
        log(f"监控范围：最近 {args.active_within:g} 分钟内有活动的全部会话"
            f"（新出现的活跃会话会自动纳入）", f)
    elif args.thread:
        log(f"监控范围：指定的 {len(args.thread)} 个会话", f)
    else:
        log(f"监控范围：最近活跃的 {args.auto} 个会话", f)

    # 发送通道自检：没有插件执行器就明确报错，而不是静默什么都不做
    ok, detail, hb = plugin_status()
    if ok:
        log(f"发送通道：{detail}", f)
    else:
        if not args.dry_run:
            log("监控退出（未发送任何消息）。可用 --dry-run 先只看检测结果。", f)
            f.close()
            sys.exit(3)

    try:
        con = ro_connect()
        sessions = pick_sessions(con, args.thread, args.auto,
                                 active_within_minutes=args.active_within)
    except Exception as e:
        log(f"解析会话失败：{e!r}", f)
        sessions = [{"id": t, "name": t[:8], "title": "", "path": None,
                     "updated_at": None, "permission_mode": "", "status": "",
                     "error": None} for t in args.thread]
        con = None
    arch_ids, arch_note = pi_archive.archived_ids()
    log(f"归档过滤：{arch_note}", f)
    for t in (args.thread or []):
        if t in arch_ids:
            log(f"会话 {t[:8]}… 已在 PI-Desktop 里归档，按规则跳过监控"
                f"（想监控就先取消归档）", f)

    if not sessions:
        log("未找到可监控的会话，退出", f)
        f.close()
        sys.exit(2)

    # 无人值守检查：非「全自动」权限模式的会话半夜会卡在确认上，必须提前警告
    if con is not None:
        try:
            if not warn_unattended(con, sessions, args, f):
                con.close()
                f.close()
                sys.exit(4)
        except Exception as e:
            log(f"无人值守检查失败（继续运行）：{e!r}", f)
        finally:
            con.close()

    for s in sessions:
        log(f"监控会话：{s['name']}（{s['id'][:8]}…，状态 {STATUS_CN.get(s['status'], s['status'] or '未知')}）"
            f" | {s['path'] or ''}", f)

    # seen: (status, error, started_at) 上一轮看到的 turn；target: 本次中止需空闲满的秒数
    state = {}
    for s in sessions:
        state[s["id"]] = new_session_state()

    # 关机/计时状态：shutdown 表示已触发关机；all_stopped_* 与 all_done_* 是
    # 「全部停止」「全部完成」两条规则的计时与已触发标记。
    ctl = {"shutdown": False, "all_stopped_since": None, "all_stopped_fired": False,
           "all_done_since": None, "all_done_fired": False,
           "all_503_fired": False, "all_503_logged": None}
    def handle():
        con = ro_connect()
        upds = {}
        try:
            # 动态同步：失效会话退出监控，数量不足时补入新的活跃会话，并刷新显示名
            sync_sessions(con, sessions, state, f, bool(args.thread), args.auto,
                          args.active_within)
            # —— Pi 独有的信息：会话刚跑完 / 刚失败，以及是否有工具在等你确认 ——
            handle_notifications(con, ctl, args, f)
            warn_waiting_confirm(con, sessions, ctl, args, f)
            # 供「近期活跃会话全部报 503」判定：本轮每个会话的 (名字, 最后活动, 是否503)
            # 是否 503 以日志里的真实 HTTP 状态码为准（turns.error_code 区分不了）。
            all503_scan = []
            # 「报错续接」开着时也要扫日志：503 判定靠它（见下面的 503 分支）。
            http_map = scan_pi_http_status() if (all_503_on or open400_new
                                                 or context_continue != CONTINUE_OFF) else {}
            for s in sessions:
                sid = s["id"]
                st = state[sid]
                turn = last_turn(con, sid)
                upd = session_updated(con, sid)
                upds[sid] = upd
                s["updated_at"] = upd

                if turn is None:
                    all503_scan.append((s["name"], upd, False))
                    st["seen"] = None
                    continue

                status, error, started = turn
                s["status"] = status or ""
                s["error"] = error
                key = (status, error, started)
                changed = st["seen"] != key
                now_ms = time.time() * 1000
                idle_s = (now_ms - upd) / 1000 if upd is not None else None
                failed = status in stop_statuses
                # 日志里对得上「当前这个回合」的真实 HTTP 状态码（判不出来是 None）
                http_now = aligned_pi_http(failed, http_map.get(sid), upd)
                # 只有「当前最后一个回合就是失败」且「日志里对得上时间的那条
                # 失败记录是 503」才算 503 —— 否则一个早已恢复的会话会被它
                # 几小时前的 503 记录误判成还在报 503（会把机器关掉）。
                all503_scan.append(
                    (s["name"], upd, is_pi_503(failed, http_map.get(sid), upd)))

                if not failed:
                    st["target"] = None
                    st["sent_key"] = None
                    if status in DONE_STATUSES:
                        if st["fail_count"] or st["code_streak"] or st["context_streak"]:
                            log(f"[{s['name']}] 会话成功完成，连续失败/错误码/超限计数清零", f)
                        st["fail_count"] = 0
                        st["code_streak"] = 0
                        st["context_streak"] = 0
                        # 真的跑完了一轮 = 有实际进展，允许以后再触发一次续接
                        st["continued"] = False
                    st["seen"] = key
                    if upd is not None:
                        st["last_upd"] = upd
                    continue

                # —— 错误码连续计数（仅新 turn 计一次）——
                has_code = bool(shutdown_codes) and (error in shutdown_codes)
                if has_code and changed:
                    st["code_streak"] += 1
                elif not has_code and changed and st["code_streak"]:
                    log(f"[{s['name']}] 出现非 {shutdown_codes} 错误，连续计数清零", f)
                    st["code_streak"] = 0

                if has_code and changed and st["code_streak"] >= args.shutdown_threshold:
                    log(f"[{s['name']}] 错误码 {error} 已连续触发 {st['code_streak']} 次，二次确认后关机", f)
                    re_turn = last_turn(con, sid)
                    if re_turn is not None and re_turn[0] in stop_statuses and re_turn[1] in shutdown_codes:
                        st["seen"] = key
                        if run_shutdown(f"会话 {s['name']} 连续触发 {error} {st['code_streak']} 次",
                                        args.shutdown_cmd, args.dry_run, f):
                            ctl["shutdown"] = True
                            return
                    else:
                        log(f"[{s['name']}] 关机二次确认未通过（状态已变化），本次不关机", f)

                # —— 真 400 → 直接新建会话续接（不等连续次数、不先压缩）——
                # 400 是确定性错误：同一会话重发「继续」还是 400，压缩上下文也救不了，
                # 只有换新会话有用。判定必须用日志里的真实状态码：turns.error_code 把
                # 400 与 503/5xx 都记成 PROVIDER_ERROR，只看它会把 503 也当成 400
                # 立刻开出一串新会话（服务商整体挂掉时最糟）。
                # CONTEXT_TOO_LARGE 不走这条路：那是上下文真超长，先压缩更省 token。
                # 「报错续接」总开关关闭时这条也不生效（不自动续接就是不续接）。
                if (open400_new and context_continue != CONTINUE_OFF
                        and http_now == PI_400_STATUS and changed
                        and error not in CONTEXT_CODES
                        and not st.get("continued")):
                    log(f"[{s['name']}] 检测到真实 HTTP 400（日志时间与当前回合对齐），"
                        f"直接新建会话续接", f)
                    re_turn = last_turn(con, sid)
                    if re_turn is not None and re_turn[0] in stop_statuses:
                        st["seen"] = key
                        new_id = continue_session(con, s, st, args, CONTINUE_NEW, f)
                        if new_id:
                            adopt_new_session(con, sessions, state, f, new_id, s)
                            return
                        if st.get("continued"):
                            continue
                    else:
                        log(f"[{s['name']}] 新建会话二次确认未通过（状态已变化），"
                            f"本次不触发", f)

                # —— 上下文超限连续计数（仅新 turn 计一次）——
                # 与上面的「关机错误码」是两回事：这里不关机，而是换一种方式继续。
                has_context = bool(context_codes) and (error in context_codes)
                if has_context and changed:
                    st["context_streak"] += 1
                elif not has_context and changed and st["context_streak"]:
                    log(f"[{s['name']}] 出现非上下文超限错误，超限连续计数清零", f)
                    st["context_streak"] = 0

                # 真实 HTTP 503（服务商整体不可用）：不续接、不新建会话。
                # 为什么：换新会话一样连不上，只会白烧 token，还会盖掉「全 503 关机」
                # 的信号。turns.error_code 把 503 也记成 PROVIDER_ERROR，只有日志里的
                # 真实状态码能区分（见 scan_pi_http_status）。
                is503 = (http_now == PI_503_STATUS)
                if (is503 and context_continue != CONTINUE_OFF and has_context
                        and changed and st["context_streak"] >= context_threshold):
                    log(f"[{s['name']}] 真实 HTTP 503（服务商不可用），不续接、"
                        f"不新建会话，按普通报错重试", f)

                # 连续 N 次上下文超限 → 二次确认后执行续接（压缩 / 新建会话）
                if (context_continue != CONTINUE_OFF and has_context and changed
                        and st["context_streak"] >= context_threshold
                        and not st.get("continued") and not is503):
                    log(f"[{s['name']}] 上下文超限已连续 {st['context_streak']} 次，"
                        f"二次确认后续接", f)
                    re_turn = last_turn(con, sid)
                    if (re_turn is not None and re_turn[0] in stop_statuses
                            and re_turn[1] in context_codes):
                        st["seen"] = key
                        new_id = continue_session(con, s, st, args, context_continue, f)
                        if new_id:
                            # 监控集合发生变化（原会话退出、新会话加入），
                            # 本轮就此结束；关机规则下一轮再判定。
                            adopt_new_session(con, sessions, state, f, new_id, s)
                            return
                        if st.get("continued"):
                            # 压缩成功并已发「继续」：本轮不再走普通发送逻辑
                            continue
                    else:
                        log(f"[{s['name']}] 续接二次确认未通过（状态已变化），本次不触发", f)

                if changed:
                    st["fail_count"] += 1
                    n = st["fail_count"]
                    st["target"] = wait_for(n)
                    st["sent_key"] = None
                    extra = (f"错误码 {error}（连续第 {st['code_streak']}/{args.shutdown_threshold} 次）"
                             if has_code else (f"错误码 {error}" if error else ""))
                    log(f"[{s['name']}] 检测到中止（{STATUS_CN.get(status, status)}{('，' + extra) if extra else ''}）；"
                        f"最后活动 {ts_str(upd) if upd else '?'}；连续失败第 {n} 次，"
                        f"{tier_of(n)}（等待 {st['target']:.1f}s）且无活动才发送", f)

                prev = st["last_upd"]
                if prev is not None and upd is not None and upd > prev and st["sent_key"] is not None:
                    log(f"[{s['name']}] 会话恢复活动（最后活动 {ts_str(upd)}），重置等待", f)
                    st["sent_key"] = None
                    st["target"] = wait_for(max(1, st["fail_count"]))

                # 兜底：发送后仍无活动（指令没被处理）→ 超时后允许重发
                if (st["sent_key"] == key and st["last_send"] > 0
                        and time.time() - st["last_send"] >= args.retry_minutes * 60):
                    st["sent_key"] = None
                    log(f"[{s['name']}] 发送后中止状态持续超过 {args.retry_minutes:.0f} 分钟，允许重试", f)

                # 触发发送：空闲满目标时长 → 发送前二次确认
                if (st["sent_key"] != key and st["target"] is not None
                        and idle_s is not None and idle_s >= st["target"]):
                    re_turn = last_turn(con, sid)
                    re_upd = session_updated(con, sid)
                    re_fail = re_turn is not None and re_turn[0] in stop_statuses
                    re_idle = (time.time() * 1000 - re_upd) / 1000 if re_upd is not None else None
                    if not (re_fail and re_idle is not None and re_idle >= st["target"]):
                        st["target"] = None
                        log(f"[{s['name']}] 发送前二次确认未通过"
                            f"（状态={'中止' if re_fail else '已恢复'}，空闲 "
                            f"{re_idle if re_idle is not None else '?'}s），取消本次发送", f)
                    else:
                        now = time.time()
                        st["recent"] = [t for t in st["recent"] if now - t < 3600]
                        if args.max_per_hour > 0 and len(st["recent"]) >= args.max_per_hour:
                            log(f"[{s['name']}] 已达每小时上限({args.max_per_hour})，本小时内不再发送", f)
                            st["sent_key"] = key
                        else:
                            ok_send, detail = send_prompt(sid, args.message, args.dry_run, f,
                                                          args.send_timeout)
                            st["last_send"] = now
                            st["recent"].append(now)
                            st["sent_key"] = key
                            if ok_send:
                                log(f"[{s['name']}] 二次确认通过，空闲 {idle_s:.0f}s ≥ {st['target']:.1f}s"
                                    f"（连续失败第 {st['fail_count']} 次），已发送 {args.message!r} ✓", f)
                            else:
                                log(f"[{s['name']}] 发送失败：{detail} → 2 分钟后重试", f)
                                st["last_send"] = 0.0
                                st["sent_key"] = None

                st["seen"] = key
                if upd is not None:
                    st["last_upd"] = upd

            # —— 规则4：近期活跃会话「全部报 503」→ 关机 ——
            # 与规则1（同一会话连续 N 次）互补：服务商整体挂掉时，
            # 每个会话各报一次 503，单会话计数永远到不了阈值，这条才管用。
            # 只算「近期活跃」（窗口内有活动）的会话：几天前的历史错误不参与判定，
            # 否则一堆旧会话会永远凑不齐「全部 503」，规则等于失效。
            if all_503_on and not ctl.get("all_503_fired"):
                fired, names, bad = coord_mod.evaluate_all_503(
                    all503_scan, time.time() * 1000, all_503_window_min)
                if fired:
                    ctl["all_503_fired"] = True
                    shown = "、".join(names[:3])
                    if len(names) > 3:
                        shown += f" 等 {len(names)} 个"
                    if run_shutdown(
                            f"近期活跃会话全部报 503（{shown}，"
                            f"窗口 {all_503_window_min:g} 分钟）",
                            args.shutdown_cmd, args.dry_run, f):
                        ctl["shutdown"] = True
                        return
                elif names and ctl.get("all_503_logged") != len(names):
                    # 只在与上次不同的规模下提示一次，避免每轮刷屏
                    ctl["all_503_logged"] = len(names)
                    log(f"「全部 503」关机规则：近期活跃 {len(names)} 个会话中，"
                        f"{len(bad)} 个不是 503（{'、'.join(bad[:3])}），不触发", f)

            # —— 规则2：所有被监控会话全部无活动满阈值 → 关机 ——
            if args.all_stopped_minutes > 0 and not ctl["all_stopped_fired"]:
                threshold_ms = args.all_stopped_minutes * 60_000
                now_ms2 = time.time() * 1000
                vals = [u for u in upds.values() if u is not None]
                all_stopped = bool(vals) and all(now_ms2 - u >= threshold_ms for u in vals)
                since = ctl["all_stopped_since"]
                if all_stopped:
                    if since is None:
                        ctl["all_stopped_since"] = time.time()
                        log(f"所有监控会话均无活动（已超 {args.all_stopped_minutes} 分钟），开始计时", f)
                    elif time.time() - since >= args.all_stopped_minutes * 60:
                        con2 = ro_connect()
                        try:
                            fresh = [session_updated(con2, s["id"]) for s in sessions]
                        finally:
                            con2.close()
                        vals2 = [u for u in fresh if u is not None]
                        still = bool(vals2) and all(now_ms2 - u >= threshold_ms for u in vals2)
                        if still:
                            ctl["all_stopped_fired"] = True
                            if run_shutdown(f"所有监控会话停止已超过 {args.all_stopped_minutes} 分钟",
                                            args.shutdown_cmd, args.dry_run, f):
                                ctl["shutdown"] = True
                        else:
                            log("「全部停止」关机二次确认未通过（有会话恢复），取消关机", f)
                            ctl["all_stopped_since"] = None
                else:
                    if since is not None:
                        log("监控会话恢复活动，取消「全部停止」计时", f)
                    ctl["all_stopped_since"] = None
            # —— 规则3：所有被监控会话都「正常完成」→ 关机 ——
            # 与「全部停止」的区别：这里要求每个会话最后一个 turn 的状态都是
            # 正常完成（complete/completed）。卡在等确认、失败、还在跑的会话
            # 都不算完成，所以不会出现「任务没干完却关机」。
            if shutdown_all_done and not ctl.get("all_done_fired"):
                states = []
                for s in sessions:
                    t = last_turn(con, s["id"])
                    states.append((s, t[0] if t else None))
                if states and all(st in DONE_STATUSES for _s, st in states):
                    if not ctl.get("all_done_since"):
                        ctl["all_done_since"] = time.time()
                        names = "、".join(s["name"] for s, _ in states)
                        log(f"所有被监控会话均已正常完成（{names}），"
                            f"持续 {all_done_seconds:.0f} 秒后关机", f)
                        # 登记本来源已完成，让同时运行的另一个来源知道
                        if coord is not None:
                            coord.mark_done()
                            log(f"已在协调文件登记本来源完成：{coord.path}", f)
                    elif (time.time() - ctl["all_done_since"]
                          >= all_done_seconds):
                        # 二次确认：重读一遍，避免刚好有新任务进来
                        con3 = ro_connect()
                        try:
                            re_states = [last_turn(con3, s["id"]) for s in sessions]
                        finally:
                            con3.close()
                        still = bool(re_states) and all(
                            t is not None and t[0] in DONE_STATUSES for t in re_states)
                        if not still:
                            log("「全部完成」关机二次确认未通过（有新活动），取消关机", f)
                            ctl["all_done_since"] = None
                            if coord is not None:
                                coord.clear_done()
                        else:
                            # 本来源确实完成了；若要求跨来源，还要等其他来源也完成
                            if coord is not None:
                                coord.mark_done()
                                ok_shutdown, why = coord.evaluate()
                            else:
                                ok_shutdown, why = True, "未启用跨来源协调"
                            if not ok_shutdown:
                                log(f"本来源已完成，但暂不关机：{why}", f)
                            else:
                                ctl["all_done_fired"] = True
                                reason = (f"所有监控会话已正常完成（共 {len(sessions)} 个）"
                                          if not coord or not coord.require_all
                                          else f"所有监控来源均已完成（{why}）")
                                if run_shutdown(reason, args.shutdown_cmd,
                                                args.dry_run, f):
                                    ctl["shutdown"] = True
                                    return
                else:
                    if ctl.get("all_done_since"):
                        pending = [s["name"] for s, st in states
                                   if st not in DONE_STATUSES]
                        log(f"仍有会话未完成（{'、'.join(pending[:3])}），"
                            f"取消「全部完成」计时", f)
                    ctl["all_done_since"] = None
                    # 有会话重新活动 → 撤销本来源的完成登记
                    if coord is not None:
                        coord.clear_done()
        finally:
            con.close()

    try:
        # 先写一次心跳，让插件面板立刻能看到「监控脚本已启动」，
        # 不必等第一个轮询周期。
        write_monitor_heartbeat(args.dry_run, args.poll)
        while True:
            try:
                handle()
            except Exception as e:
                log(f"检测异常：{e!r}（下轮自动重试）", f)
            write_monitor_heartbeat(args.dry_run, args.poll)
            if ctl["shutdown"]:
                log("已触发关机，监控退出", f)
                break
            if args.once:
                log("自检完成（--once），退出", f)
                break
            # 每轮确认执行器还在，避免 Pi 退出后静默失效
            ok_now, detail_now, _ = plugin_status()
            if not ok_now and not args.dry_run:
                log(f"发送通道失效：{detail_now} → 监控退出", f)
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出", f)
    finally:
        f.close()


if __name__ == "__main__":
    main()
