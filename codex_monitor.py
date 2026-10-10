#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codex_monitor.py — 监控 Codex（桌面端/app-server）会话，自动处理"中止"与致命错误。

会话链（relay）处理：
  Codex 桌面端每次"续接"一个线程会新建一个中继线程（标题形如
  `codex://threads/<父线程id> 在当前会话中继续...`）。本脚本按链监控：
  每轮把每个会话解析到当前"活动叶子线程"（沿标题引用向前走到最新），
  只对活线程做中止判定与发送，避免给已废弃的旧线程乱发"继续"；
  计数（连续失败/405-503）按会话（链根）累计，跨中继延续。

动态监控（自动模式，无 --thread 时）：
  - 运行中某会话被归档（活动叶子线程 archived=1）→ 自动停止监控，不再发送；
  - 监控数不足 N 时，自动补入最近活跃的未归档真实会话（按链去重）；
  - 仅"停止但未归档"的会话保留在监控中（保证"全部停止 20 分钟关机"可用）。

中止判定（避免误伤正在自动重试/输出的会话）：
  - 活动叶子线程最后轮次状态为 failed / interrupted（来自 thread_history_1.sqlite）；
  - 且会话自最后活动（threads.updated_at_ms）起持续空闲满"本次等待时长"秒；
    活动时间戳仍在前进的会话（如瞬时错误后的自动重试）不会触发；
  - 触发发送前再次读库二次确认：状态仍为中止且仍无活动，才执行
    `codex queue --thread <id> --message "继续"`。

等待节奏（连续失败退避，每次带 ±jitter 秒随机）：
  同一会话连续失败第 1、2 次等待 1 分钟，第 3 次 3 分钟，第 4 次起 5 分钟
  （默认 --backoff-minutes 1,1,3,5,5）。中间只要有"成功完成"的一轮
  （状态 completed）就清零，下一次失败重新从 1 分钟开始。

关机规则：
  1) 405 / 503 错误：同一会话"连续触发 3 次"才关机（每次 = 出现一条新的含
     405/503 的失败轮次；中间成功完成一轮，或出现其它错误，则计数清零）。
     前 2 次照常发送"继续"重试，第 3 次二次确认后直接关机。
  2) 所有被监控会话全部停止：全部会话（当前活动叶子）均无活动持续满
     --all-stopped-minutes 分钟（默认 20，0=关闭该规则）→ 二次确认后关机。
  关机指令可用 --shutdown-cmd 调整（默认 shutdown /s /f /t 10）。

用法：
  python codex_monitor.py                        # 自动取最近活跃的 2 个会话（按链去重）
  python codex_monitor.py --thread <UUID1> --thread <UUID2>   # 指定会话
  python codex_monitor.py --dry-run --once       # 自检：只检测不发送/不关机，跑一轮后退出
  更多参数见 --help
"""

import argparse
import datetime
import glob
import json
import os
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# 跨来源关机协调（与 pi_monitor 共用同一个模块与协调文件）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import shutdown_coord as coord_mod

# Codex 的「inProgress 是不是残留」判定（被打断的 turn 会永久停在 inProgress）
import codex_activity

# 打包成 exe 后强制 stdout/stderr 用 UTF-8，避免 GUI 端读取中文日志乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
DB_THREADS = CODEX_HOME / "state_5.sqlite"
DB_TURNS = CODEX_HOME / "thread_history_1.sqlite"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
RELAY_RE = re.compile(
    r"codex://threads/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# 「服务端不可用（503）」HTTP 码：用于「近期活跃会话全部 503 → 关机」。
# Codex 的 turns 里存了完整错误 JSON，所以这里能精确匹配 503。
ALL_503_CODES = {"503"}


# 「监控脚本存活」心跳：写进 Pi 插件目录，插件面板据此显示监控是否在跑。
# 为什么放在 Pi 的数据目录：面板是 Pi 插件，只能读到自己数据目录下的文件。
KEEPER_DIR = Path(os.environ.get("PI_DESKTOP_DATA_DIR",
                                 HOME / ".pi-desktop")) / "session-keeper"


def write_monitor_heartbeat(dry_run, poll_seconds):
    """写「监控脚本存活」心跳，供插件面板显示「监控脚本：运行中」。

    为什么需要：插件只是执行器，它自己看不出有没有人在监控。
    缺了这个文件，用户很容易以为「插件装了就等于生效了」——
    结果会话报错后没人处理，事后还查不出原因。

    poll_seconds 一并写进去：面板据此判断心跳是否过期，
    而不是写死一个阈值（用户可能把轮询间隔调得很长）。
    """
    try:
        KEEPER_DIR.mkdir(parents=True, exist_ok=True)
        path = KEEPER_DIR / "monitor-codex.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "source": "codex",
            "at": int(time.time() * 1000),
            "pid": os.getpid(),
            "dryRun": bool(dry_run),
            "pollSeconds": float(poll_seconds or 0),
        }, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass  # 心跳写不进去不该影响监控本身

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_ms(ms):
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


def ro_connect(db: Path):
    return sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=10)


def find_codex():
    """解析 codex 可执行文件（优先真实 exe，避免 cmd/ps1 包装带来的子进程问题）。"""
    cands = []
    env = os.environ.get("CODEX_CLI_PATH")
    if env:
        cands.append(env)
    for name in ("codex", "codex.cmd", "codex.exe"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    npm_glob = str(
        HOME / "AppData/Roaming/npm/node_modules/@openai/codex/node_modules/@openai"
              "/codex-win32-x64/vendor/*/bin/codex.exe"
    )
    cands += glob.glob(npm_glob)
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def get_thread(con, tid):
    return con.execute(
        "select id, title, cwd, updated_at_ms, recency_at_ms, coalesce(name,'') from threads where id=?",
        (tid,),
    ).fetchone()


def resolve_root(con, tid):
    """沿标题中的 `codex://threads/<父id>` 引用向前追溯，返回会话链根。
    若某个父线程已被删除，则锚定到最深的存在线程（避免解析出幽灵 ID）。"""
    cur, last = tid, tid
    seen = set()
    while cur not in seen:
        seen.add(cur)
        row = get_thread(con, cur)
        if row is None:
            return last
        last = cur
        m = RELAY_RE.search(row[1] or "")
        if not m:
            return cur
        cur = m.group(1)
    return last


def resolve_leaf(con, root):
    """沿标题引用向前收集整条链，返回其中"未归档且最近活跃"的成员作为活动叶子；
    全链均归档时返回链尾；链尾若已被删除则由调用方过滤。"""
    cur, seen = root, set()
    members = []
    while cur not in seen:
        seen.add(cur)
        members.append(cur)
        rows = con.execute(
            "select id from threads where title like ? order by recency_at_ms desc limit 1",
            (f"%codex://threads/{cur}%",),
        ).fetchall()
        if not rows:
            break
        cur = rows[0][0]
    best = None
    for m in reversed(members):  # 从链尾向前；recency 相同（平局）时取更靠链尾的
        r = con.execute("select archived, recency_at_ms from threads where id=?", (m,)).fetchone()
        if r is None or r[0] != 0:
            continue
        if best is None or (r[1] or 0) > best[1]:
            best = (m, r[1] or 0)
    return best[0] if best else members[-1]


def sync_conversations(scon, convs, state, f, explicit, auto_n):
    """动态同步监控集合（自动模式）：
    - 已归档（活动叶子线程 archived=1）的会话停止监控，不再发送；
    - 监控数不足 N 时，从最近活跃的未归档真实会话中补足（按链去重）。
    仅"停止但未归档"的会话保留（保证"全部停止 20 分钟关机"规则可用）。
    显式 --thread 模式只剔除归档，不补足。"""
    refresh_names(scon, convs, f)  # Codex 端重命名后刷新显示名
    removed = []
    for cv in convs:
        r = scon.execute("select archived from threads where id=?", (cv["leaf"],)).fetchone()
        if r is not None and r[0] == 1:
            removed.append(cv)
    for cv in removed:
        log(f"[{cv['name']}] 会话已归档（{cv['leaf'][:8]}…），停止监控", f)
        convs.remove(cv)
        state.pop(cv["root"], None)
    if explicit or len(convs) >= auto_n:
        return
    have = {cv["root"] for cv in convs}
    try:
        fresh = pick_conversations(scon, [], max(auto_n * 2, 1))
    except Exception:
        fresh = []
    for fcv in fresh:
        if len(convs) >= auto_n:
            break
        if fcv["root"] in have:
            continue
        log(f"[{fcv['name']}] 检测到新的活跃会话，加入监控（活动线程 {fcv['leaf'][:8]}…）", f)
        convs.append(fcv)
        state[fcv["root"]] = {"seen": None, "target": None, "sent_episode": None,
                              "last_upd_ms": None, "last_queue": 0.0, "recent": [],
                              "fail_count": 0, "consecutive_503": 0, "consecutive_400": 0}
        have.add(fcv["root"])


def clean_title(title):
    """对话名：取线程标题，去掉中继引用前缀行（codex://threads/...），压缩空白。"""
    if not title:
        return ""
    lines = [ln.strip() for ln in title.splitlines() if ln.strip()]
    out = [ln for ln in lines if not RELAY_RE.search(ln)]
    return " ".join(out)[:40]


def build_name(leaf_row, root_row):
    """会话显示名 = 项目目录名 / 对话名。
    对话名优先取叶子线程的 name 字段（Codex 桌面端左侧显示的重命名），
    若为空或仍是自动生成的截断标题（含 codex://threads），则回退到链根标题。"""
    leaf_id, title, cwd = (leaf_row[0], leaf_row[1], leaf_row[2]) if leaf_row else (None, None, None)
    leaf_name = (leaf_row[5] or "").strip() if leaf_row and len(leaf_row) > 5 else ""
    project = ""
    if cwd:
        project = Path(cwd.replace("\\\\?\\", "")).name
    conv = ""
    if leaf_name and "codex://threads" not in leaf_name:
        conv = leaf_name
    else:
        conv = clean_title(root_row[1] if root_row else None)
    if project and conv:
        return f"{project} / {conv}"
    return project or conv or (leaf_id[:8] if leaf_id else "?")


def refresh_names(scon, convs, f):
    """Codex 端重命名会话后刷新显示名：每轮重读 root/leaf 标题，变化则更新并打日志。"""
    for cv in convs:
        leaf_row = get_thread(scon, cv["leaf"])
        if leaf_row is None:
            continue
        root_row = get_thread(scon, cv["root"])
        name = build_name(leaf_row, root_row)
        if name != cv["name"]:
            old = cv["name"]
            cv["name"] = name
            log(f"[{old}] 会话在 Codex 端重命名 → [{name}]", f)


def pick_conversations(con, explicit, auto_n):
    """选择要监控的会话（按链去重）：返回 [{root, leaf, title, cwd, name}]。
    自动选择只取有工作目录的真实会话，避免把应用内部瞬态线程当成会话。
    显式 --thread 指定时监控全部指定会话（不再被 auto_n 截断）。
    name = 项目目录名 / 对话根标题，用于区分同一项目下的多个对话。"""
    convs = {}
    if explicit:
        cands = [(t, 0) for t in explicit]
    else:
        cands = con.execute(
            "select id, recency_at_ms from threads where archived=0 "
            "and cwd is not null and trim(cwd) != '' "
            "order by recency_at_ms desc limit ?", (auto_n * 4,)
        ).fetchall()
    for tid, rec in cands:
        if not tid:
            continue
        root = resolve_root(con, tid)
        cur = convs.get(root)
        if cur is None or (rec or 0) > cur[1]:
            convs[root] = (tid, rec or 0)
    top_n = len(convs) if explicit else auto_n
    top = sorted(convs.items(), key=lambda kv: kv[1][1], reverse=True)[:top_n]
    out = []
    for root, _ in top:
        leaf = resolve_leaf(con, root)
        if get_thread(con, leaf) is None:
            continue  # 防御：链解析出已不存在的线程，跳过
        row = get_thread(con, leaf)
        root_row = get_thread(con, root)
        out.append({"root": root, "leaf": leaf, "title": row[1] if row else None,
                    "cwd": row[2] if row else None, "name": build_name(row, root_row)})
    return out


def last_turn(tcon, thread_id):
    return tcon.execute(
        "select turn_id, rollout_ordinal, status, error_json, started_at "
        "from thread_turns where thread_id=? order by rollout_ordinal desc limit 1",
        (thread_id,),
    ).fetchone()


def thread_updated(scon, thread_id):
    """返回会话最后活动时间（epoch 毫秒），来自 threads.updated_at_ms。"""
    row = scon.execute(
        "select updated_at_ms from threads where id=?", (thread_id,)
    ).fetchone()
    return row[0] if row else None


def err_message(err_json):
    if not err_json:
        return ""
    try:
        d = json.loads(err_json)
        if isinstance(d, dict):
            return d.get("message") or json.dumps(d, ensure_ascii=False)[:300]
    except Exception:
        pass
    return str(err_json)[:300]


def has_http_code(err_json, codes):
    """错误信息中是否包含指定 HTTP 错误码（如 405 / 503），词边界匹配（只匹配错误记录）。"""
    if not err_json or not codes:
        return False
    pat = r"\b(" + "|".join(re.escape(c) for c in codes) + r")\b"
    return re.search(pat, err_json) is not None


def queue(codex, thread_id, message, dry_run, f):
    """执行 codex queue，返回 (是否成功, 输出摘要)。"""
    args = [codex, "queue", "--thread", thread_id, "--message", message]
    if codex.lower().endswith((".cmd", ".bat")):
        args = ["cmd", "/c"] + args
    if dry_run:
        log(f"[dry-run] 不实际发送，将执行: {args}", f)
        return True, ""
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=120,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "queue 命令超时(120s)"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    return p.returncode == 0, (out or err)


def spawn_continue_session(codex, cwd, thread_id, dry_run, f):
    """连续 400 后：在当前项目(cwd)下用 codex exec 开新会话，把原会话深度链接作为首条消息。
    后台启动，不阻塞监控循环；新会话会被动态同步自动加入监控。"""
    clean_cwd = (cwd or "").replace("\\\\?\\", "").replace("\\?\\", "")
    deep_link = f"codex://threads/{thread_id}"
    prompt = f"请继续这个会话的任务：{deep_link}"
    args = [codex, "exec", "-C", clean_cwd, prompt]
    if codex.lower().endswith((".cmd", ".bat")):
        args = ["cmd", "/c"] + args
    if dry_run:
        log(f"[dry-run] 连续 400，将在项目 {clean_cwd} 下开新会话继续: {args}", f)
        return
    log(f"连续 400，在项目 {clean_cwd} 下开启新会话继续任务（深度链接 {deep_link}）", f)
    try:
        log_path = str(Path(f.name).with_suffix(".continue.log"))
        lf = open(log_path, "a", encoding="utf-8")
        popen_cwd = clean_cwd if clean_cwd and os.path.isdir(clean_cwd) else None
        subprocess.Popen(
            args, stdout=lf, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, cwd=popen_cwd)
        log(f"新会话已在后台启动（输出追加到 {log_path}）", f)
    except Exception as e:
        log(f"开启新会话失败: {e!r}", f)


def main():
    ap = argparse.ArgumentParser(description="Codex 会话监控：中止后按退避节奏发送“继续”，405/503 连续3次或全部停止20分钟时关机")
    ap.add_argument("--thread", action="append", default=[],
                    help="指定监控的会话 UUID（可重复；默认按最近活跃自动取 N 个会话，按中继链去重）")
    ap.add_argument("--auto", type=int, default=2,
                    help="未指定 --thread 时监控的会话数量（默认 2）")
    ap.add_argument("--message", default="继续", help="要发送的消息（默认：继续）")
    ap.add_argument("--backoff-minutes", default="1,1,3,5,5",
                    help="连续失败第 1、2、3、4、5+ 次的等待分钟数（逗号分隔，最后一个值对后续所有次数生效；默认 1,1,3,5,5）")
    ap.add_argument("--jitter", type=float, default=3, help="等待时长随机 ± 秒数（默认 3）")
    ap.add_argument("--poll", type=float, default=10, help="轮询间隔秒数（默认 10）")
    ap.add_argument("--retry-minutes", type=float, default=15,
                    help="发送后仍持续中止（队列未被处理）时多久重试（默认 15 分钟）")
    ap.add_argument("--max-per-hour", type=int, default=20,
                    help="每会话每小时最多发送次数，0 表示不限（默认 20）")
    ap.add_argument("--statuses", default="failed,interrupted",
                    help="视为中止的状态，逗号分隔（默认 failed,interrupted）")
    ap.add_argument("--shutdown-codes", default="405,503",
                    help="这些 HTTP 错误码连续触发 N 次后关机，逗号分隔（空串=关闭该规则，默认 405,503）")
    ap.add_argument("--shutdown-threshold", type=int, default=3,
                    help="405/503 连续触发多少次后关机（默认 3）")
    ap.add_argument("--shutdown-cmd", default="shutdown /s /f /t 10",
                    help="关机指令（默认 shutdown /s /f /t 10）")
    ap.add_argument("--all-stopped-minutes", type=float, default=20,
                    help="所有被监控会话全部无活动持续满该分钟数后关机（0=关闭该规则，默认 20）")
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
    ap.add_argument("--bad400-threshold", type=int, default=1,
                    help="连续多少次 bad response 400 后自动在当前项目下开新会话继续"
                         "（0=关闭该功能，默认 1：报 400 就直接开）")
    # 「全部正常完成后关机」：与 Pi 端口径一致 —— 每个会话最后一个 turn
    # 都是「正常完成」才关机；失败、卡住、还在跑的都不算完成。
    ap.add_argument("--shutdown-when-all-done", nargs="?", const="on",
                    default="off", choices=["auto", "on", "off"],
                    help="所有被监控会话都「正常完成」后关机"
                         "（auto=读 Pi 插件面板设置；on/off=强制开关）")
    ap.add_argument("--all-done-confirm-seconds", default="auto",
                    help="「全部正常完成」后再观察多少秒才关机（默认 auto，内置 120）")
    ap.add_argument("--all-sources", default="",
                    help="本次同时监控的来源，逗号分隔（如 pi,codex），用于跨来源协调")
    ap.add_argument("--shutdown-requires-all-sources", nargs="?", const="on",
                    default="auto", choices=["auto", "on", "off"],
                    help="是否要求所有监控来源都完成才关机（auto=读插件设置）")
    ap.add_argument("--coord-file", default="",
                    help="跨来源协调文件路径（默认 ~/.session-monitor/shutdown-coord.json）")
    ap.add_argument("--log", default=str(Path(__file__).resolve().parent / "codex_monitor.log"),
                    help="日志文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只检测不发送/不关机")
    ap.add_argument("--once", action="store_true", help="只检测一轮后退出（用于自检）")
    args = ap.parse_args()

    stop_statuses = set(s.strip() for s in args.statuses.split(",") if s.strip())
    shutdown_codes = [c.strip() for c in args.shutdown_codes.split(",") if c.strip()]
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

    # —— 关机策略：CLI 显式值 > Pi 插件面板设置 > 内置默认 ——
    # 设置统一放在 Pi 插件面板里（那里已经有开关界面），Codex 侧只读心跳。
    # 读不到时退回内置默认，不会因为 Pi 没开就崩掉。
    def read_plugin_settings():
        """读 Pi 插件的设置（来自它的心跳文件）。读不到就返回空 dict。"""
        try:
            hb = Path(os.environ.get("PI_DESKTOP_DATA_DIR",
                                     Path.home() / ".pi-desktop")) / "session-keeper" / "plugin.json"
            data = json.loads(hb.read_text(encoding="utf-8"))
            # 心跳过期（>30 秒没更新）说明 Pi 没在跑，此时设置不可信
            if (time.time() * 1000 - (data.get("at") or 0)) > 30_000:
                return {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    _hb = read_plugin_settings()

    def setting_bool(cli_value, hb_key, fallback):
        return coord_mod.parse_on_off(
            coord_mod.resolve_setting(cli_value, _hb.get(hb_key), fallback), fallback)

    def setting_seconds(cli_value, hb_key, fallback):
        raw = coord_mod.resolve_setting(cli_value, _hb.get(hb_key), fallback)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return float(fallback)

    shutdown_all_done = setting_bool(args.shutdown_when_all_done,
                                     "shutdownWhenAllDone", False)
    all_done_seconds = setting_seconds(args.all_done_confirm_seconds,
                                       "allDoneConfirmSeconds", 120.0)
    require_all_sources = setting_bool(args.shutdown_requires_all_sources,
                                       "shutdownRequiresAllSources", True)

    # —— 「全部近期活跃会话报 503」关机：CLI 显式值 > 插件面板 > 内置默认 ——
    all_503_on = setting_bool(args.shutdown_when_all_503,
                              "shutdownWhenAll503", False)
    all_503_window_min = setting_seconds(args.all_503_window_minutes,
                                         "all503WindowMinutes", 60.0)
    monitored_sources = [x.strip() for x in args.all_sources.split(",") if x.strip()]
    if len(monitored_sources) <= 1:
        require_all_sources = False

    coord = coord_mod.ShutdownCoordinator(
        source="codex",
        all_sources=monitored_sources or ["codex"],
        coord_file=args.coord_file or None,
        require_all=require_all_sources,
    )

    if not DB_THREADS.exists() or not DB_TURNS.exists():
        print(f"找不到 Codex 数据库：{DB_THREADS} / {DB_TURNS}\n请确认 CODEX_HOME（当前 {CODEX_HOME}）")
        sys.exit(1)

    codex = find_codex()
    if not codex:
        print("找不到 codex 可执行文件，请确认已安装并加入 PATH，或设置 CODEX_CLI_PATH 环境变量")
        sys.exit(1)

    f = open(args.log, "a", encoding="utf-8")
    log(f"codex_monitor 启动 | codex: {codex}", f)
    log(f"消息: {args.message!r} | 等待节奏: {args.backoff_minutes} 分钟±{args.jitter}s（成功一轮后清零）| "
        f"轮询: {args.poll}s | 中止状态: {sorted(stop_statuses)} | 每小时上限: {args.max_per_hour}", f)
    log(f"关机规则: {shutdown_codes or '无'}-连续{args.shutdown_threshold}次 | 全部停止 {args.all_stopped_minutes} 分钟 | "
        f"指令: {args.shutdown_cmd} | dry_run: {args.dry_run}", f)
    if all_503_on:
        src_note = ("命令行" if args.shutdown_when_all_503 != "auto"
                    else "插件面板/内置默认")
        log(f"「全部报 503」关机: 开启（近期活跃窗口 {all_503_window_min:g} 分钟，"
            f"来自{src_note}）", f)
    else:
        log("「全部报 503」关机: 关闭", f)
    if shutdown_all_done:
        src_note = ("Pi 插件面板设置" if args.shutdown_when_all_done == "auto"
                    else "命令行")
        log(f"全部完成后关机: 开启（观察 {all_done_seconds:.0f} 秒，来自{src_note}）", f)
        if require_all_sources:
            log(f"跨来源要求: 开启 —— 需 {'、'.join(coord.all_sources)} 全部完成才关机"
                f"（协调文件 {coord.path}）", f)
        else:
            log(f"跨来源要求: 关闭 —— 本来源全部完成即可关机"
                f"（本次来源 {monitored_sources or ['codex']}）", f)
    else:
        log("全部完成后关机: 关闭", f)
    if args.bad400_threshold > 0:
        log(f"400 续接规则: 连续 {args.bad400_threshold} 次 400 后在当前项目下开新会话继续", f)
    else:
        log("400 续接规则: 关闭", f)

    try:
        con = ro_connect(DB_THREADS)
        convs = pick_conversations(con, args.thread, args.auto)
        con.close()
    except Exception as e:
        log(f"解析会话失败: {e!r}（将按显式 --thread 处理）", f)
        convs = [{"root": t, "leaf": t, "title": None, "cwd": None, "name": t[:8]} for t in args.thread]

    if not convs:
        log("未找到可监控的会话，退出", f)
        f.close()
        sys.exit(2)

    for cv in convs:
        log(f"监控会话: {cv['name']}（活动线程 {cv['leaf'][:8]}…，链根 {cv['root'][:8]}…）| {cv['cwd'] or ''}", f)

    # seen: (turn_id, ordinal, status)；target: 本次中止需要空闲满的秒数（随机抖动）
    # fail_count: 连续失败次数（决定等待档位）；consecutive_503: 连续 405/503 次数（决定关机）
    # 状态按会话链根存储，跨中继线程延续；seen 在活动线程变化时重置
    state = {cv["root"]: {"seen": None, "target": None, "sent_episode": None,
                          "last_upd_ms": None, "last_queue": 0.0, "recent": [],
                          "fail_count": 0, "consecutive_503": 0, "consecutive_400": 0}
             for cv in convs}
    ctl = {"shutdown": False, "all_stopped_since": None, "all_stopped_fired": False,
           "all_done_since": None, "all_done_fired": False}

    def run_shutdown(reason, f):
        if args.dry_run:
            log(f"[dry-run] 检测到 {reason}，将执行关机指令: {args.shutdown_cmd}（不实际执行）", f)
            return False
        log(f"检测到 {reason}，执行关机指令: {args.shutdown_cmd}", f)
        try:
            argv = coord_mod.split_shutdown_cmd(args.shutdown_cmd)
            if not argv:
                log("关机指令为空，未执行", f)
                return False
            subprocess.run(argv, timeout=30, creationflags=CREATE_NO_WINDOW)
            log("关机指令已执行，系统即将关机", f)
            ctl["shutdown"] = True
            return True
        except Exception as e:
            # 执行失败必须如实回报，否则调用方会以为已经关机了
            log(f"关机指令执行失败: {e!r} → 未关机，请检查「关机指令」设置", f)
            return False

    def handle():
        tcon = ro_connect(DB_TURNS)
        scon = ro_connect(DB_THREADS)
        upds = {}
        try:
            # 动态同步：归档的会话退出监控，数量不足时补入新的活跃会话
            sync_conversations(scon, convs, state, f, bool(args.thread), args.auto)
            # 供「近期活跃会话全部报 503」判定：本轮每个会话的 (名字, 最后活动, 是否503)
            all503_scan = []
            for cv in convs:
                root = cv["root"]
                st = state[root]
                # 每轮跟随中继链，解析当前活动叶子线程
                leaf = resolve_leaf(scon, root)
                if leaf != cv["leaf"]:
                    log(f"[{cv['name']}] 跟随中继到新会话 {leaf[:8]}…（原 {cv['leaf'][:8]}…），继续监控", f)
                    cv["leaf"] = leaf
                    st["seen"] = None
                tid = cv["leaf"]
                cur = last_turn(tcon, tid)
                upd = thread_updated(scon, tid)
                upds[root] = upd
                prev_upd = st["last_upd_ms"]
                if cur is None:
                    all503_scan.append((cv["name"], upd, False))
                    st["seen"] = None
                    continue
                turn_id, ordinal, status, err_json, _started = cur
                all503_scan.append((cv["name"], upd,
                                    has_http_code(err_json, ALL_503_CODES)))
                episode = (turn_id, ordinal)
                fail = status in stop_statuses
                changed = (st["seen"] is None
                           or turn_id != st["seen"][0]
                           or ordinal != st["seen"][1]
                           or status != st["seen"][2])
                now_ms = time.time() * 1000
                idle_s = (now_ms - upd) / 1000 if upd is not None else None
                has_code = bool(shutdown_codes) and has_http_code(err_json, shutdown_codes)

                if not fail:
                    st["target"] = None
                    st["sent_episode"] = None
                    # 只有"成功完成一轮"才算恢复：清空连续失败计数与 405/503 计数
                    # （inProgress=正在重试，不清空，保证"连续触发3次"在真实流程中能累计）
                    if status == "completed":
                        if st["fail_count"] or st["consecutive_503"] or st["consecutive_400"]:
                            log(f"[{cv['name']}] 会话成功完成，连续失败/405-503/400 计数清零", f)
                        st["fail_count"] = 0
                        st["consecutive_503"] = 0
                        st["consecutive_400"] = 0
                    st["seen"] = cur
                    if upd is not None:
                        st["last_upd_ms"] = upd
                    continue

                # —— 405/503 连续触发计数（仅新失败轮次计一次）——
                if has_code and changed:
                    st["consecutive_503"] += 1
                elif not has_code and changed and st["consecutive_503"]:
                    log(f"[{cv['name']}] 出现非 405/503 失败，405/503 连续计数清零", f)
                    st["consecutive_503"] = 0

                n503 = st["consecutive_503"]
                # 第 N 次（N>=3）→ 二次确认后关机
                if has_code and changed and n503 >= args.shutdown_threshold:
                    log(f"[{cv['name']}] 405/503 已连续触发 {n503} 次，二次确认后关机", f)
                    re_turn = last_turn(tcon, tid)
                    if (re_turn is not None and re_turn[2] in stop_statuses
                            and has_http_code(re_turn[3], shutdown_codes)):
                        run_shutdown(f"会话 {cv['name']} 连续触发 {shutdown_codes} 错误 {n503} 次", f)
                        st["seen"] = cur
                        if upd is not None:
                            st["last_upd_ms"] = upd
                        return
                    log(f"[{cv['name']}] 关机二次确认未通过（状态已变化），本次不关机", f)

                # —— 400 连续触发计数（仅新失败轮次计一次）——
                has_400 = args.bad400_threshold > 0 and has_http_code(err_json, ["400"])
                if has_400 and changed:
                    st["consecutive_400"] += 1
                elif not has_400 and changed and st["consecutive_400"]:
                    log(f"[{cv['name']}] 出现非 400 失败，400 连续计数清零", f)
                    st["consecutive_400"] = 0

                n400 = st["consecutive_400"]
                # 连续 N 次 400 → 二次确认后在当前项目下开新会话继续
                if (has_400 and changed and n400 >= args.bad400_threshold
                        and not st.get("continued")):
                    log(f"[{cv['name']}] 400 已连续触发 {n400} 次，二次确认后开新会话继续", f)
                    re_turn = last_turn(tcon, tid)
                    if (re_turn is not None and re_turn[2] in stop_statuses
                            and has_http_code(re_turn[3], ["400"])):
                        spawn_continue_session(codex, cv["cwd"], cv["leaf"], args.dry_run, f)
                        st["consecutive_400"] = 0
                        st["continued"] = True
                        log(f"[{cv['name']}] 已开新会话续接，原会话停止监控", f)
                    else:
                        log(f"[{cv['name']}] 开新会话二次确认未通过（状态已变化），本次不触发", f)

                if changed:
                    st["fail_count"] += 1
                    n = st["fail_count"]
                    st["target"] = wait_for(n)
                    st["sent_episode"] = None
                    if has_code:
                        log(f"[{cv['name']}] 检测到 405/503 失败（连续第 {n503}/{args.shutdown_threshold} 次）: "
                            f"{err_message(err_json)}；最后活动 {ts_ms(upd) if upd else '?'}；"
                            f"连续失败第 {n} 次，{tier_of(n)}（等待 {st['target']:.1f}s）后发送“继续”重试", f)
                    else:
                        log(f"[{cv['name']}] 检测到中止候选({status}): {err_message(err_json)}；"
                            f"最后活动 {ts_ms(upd) if upd else '?'}；连续失败第 {n} 次，"
                            f"{tier_of(n)}（等待 {st['target']:.1f}s）且无活动才发送", f)
                # 活动恢复（自动重试/输出中）→ 取消已发送标记并重置等待（档位不变）
                if prev_upd is not None and upd is not None and upd > prev_upd:
                    if st["sent_episode"] is not None:
                        log(f"[{cv['name']}] 会话恢复活动（最后活动 {ts_ms(upd)}），重置等待", f)
                    st["sent_episode"] = None
                    st["target"] = wait_for(max(1, st["fail_count"]))
                # 兜底：发送后仍无活动（队列未被处理）→ 超时后允许重发
                if (st["sent_episode"] == episode and st["last_queue"] > 0
                        and time.time() - st["last_queue"] >= args.retry_minutes * 60):
                    st["sent_episode"] = None
                    log(f"[{cv['name']}] 发送后中止状态持续超过 {args.retry_minutes:.0f} 分钟，允许重试", f)

                # 触发发送：空闲满目标时长 → 发送前二次确认，仍中止且仍空闲才发
                if (st["sent_episode"] != episode and st["target"] is not None
                        and idle_s is not None and idle_s >= st["target"]):
                    re_turn = last_turn(tcon, tid)
                    re_upd = thread_updated(scon, tid)
                    re_fail = re_turn is not None and re_turn[2] in stop_statuses
                    re_idle = (time.time() * 1000 - re_upd) / 1000 if re_upd is not None else None
                    if not (re_fail and re_idle is not None and re_idle >= st["target"]):
                        st["target"] = None
                        log(f"[{cv['name']}] 发送前二次确认未通过"
                            f"（状态={'中止' if re_fail else '已恢复'}，空闲 "
                            f"{re_idle if re_idle is not None else '?'}s），取消本次发送", f)
                    else:
                        now = time.time()
                        st["recent"] = [t for t in st["recent"] if now - t < 3600]
                        if args.max_per_hour > 0 and len(st["recent"]) >= args.max_per_hour:
                            log(f"[{cv['name']}] 已达每小时上限({args.max_per_hour})，本小时内不再发送", f)
                            st["sent_episode"] = episode
                        else:
                            ok, detail = queue(codex, tid, args.message, args.dry_run, f)
                            st["last_queue"] = now
                            st["recent"].append(now)
                            st["sent_episode"] = episode
                            if ok:
                                log(f"[{cv['name']}] 二次确认通过，空闲 {idle_s:.0f}s ≥ {st['target']:.1f}s"
                                    f"（连续失败第 {st['fail_count']} 次），已发送 {args.message!r} ✓", f)
                            else:
                                log(f"[{cv['name']}] 发送失败: {detail[:200]} → 2 分钟后重试", f)
                                st["last_queue"] = 0.0
                                st["sent_episode"] = None

                st["seen"] = cur
                if upd is not None:
                    st["last_upd_ms"] = upd

            # —— 规则2：所有被监控会话全部无活动满阈值 → 关机 ——
            # —— 规则4：近期活跃会话「全部报 503」→ 关机 ——
            # 与规则1（同一会话连续 N 次）互补：服务商整体挂掉时，
            # 每个会话各报一次 503，单会话计数永远到不了阈值，这条才管用。
            # 只算「近期活跃」（窗口内有活动）的会话：几天前的历史错误不参与判定，
            # 否则一堆旧会话永远凑不齐「全部 503」，规则等于失效。
            if all_503_on and not ctl.get("all_503_fired"):
                fired, names, bad = coord_mod.evaluate_all_503(
                    all503_scan, time.time() * 1000, all_503_window_min)
                if fired:
                    ctl["all_503_fired"] = True
                    shown = "、".join(names[:3])
                    if len(names) > 3:
                        shown += f" 等 {len(names)} 个"
                    run_shutdown(f"近期活跃会话全部报 503（{shown}，"
                                 f"窗口 {all_503_window_min:g} 分钟）", f)
                    return
                elif names and ctl.get("all_503_logged") != len(names):
                    # 只在与上次不同的规模下提示一次，避免每轮刷屏
                    ctl["all_503_logged"] = len(names)
                    log(f"「全部 503」关机规则：近期活跃 {len(names)} 个会话中，"
                        f"{len(bad)} 个不是 503（{'、'.join(bad[:3])}），不触发", f)
            if args.all_stopped_minutes > 0 and not ctl.get("all_stopped_fired"):
                threshold_ms = args.all_stopped_minutes * 60_000
                now_ms2 = time.time() * 1000
                vals = [u for u in upds.values() if u is not None]
                all_stopped = bool(vals) and all(now_ms2 - u >= threshold_ms for u in vals)
                since = ctl.get("all_stopped_since")
                if all_stopped:
                    if since is None:
                        ctl["all_stopped_since"] = time.time()
                        log(f"所有监控会话均无活动（已超 {args.all_stopped_minutes} 分钟），开始计时；"
                            f"持续满 {args.all_stopped_minutes} 分钟将关机", f)
                    elif time.time() - since >= args.all_stopped_minutes * 60:
                        # 二次确认：重读所有会话最后活动时间
                        scon2 = ro_connect(DB_THREADS)
                        try:
                            fresh = {cv["root"]: thread_updated(scon2, cv["leaf"]) for cv in convs}
                        finally:
                            scon2.close()
                        vals2 = [u for u in fresh.values() if u is not None]
                        still = bool(vals2) and all(time.time() * 1000 - u >= threshold_ms for u in vals2)
                        if still:
                            ctl["all_stopped_fired"] = True
                            run_shutdown(f"所有监控会话停止已超过 {args.all_stopped_minutes} 分钟", f)
                        else:
                            log("“全部停止”关机二次确认未通过（有会话恢复），取消关机", f)
                            ctl["all_stopped_since"] = None
                else:
                    if since is not None:
                        log("监控会话恢复活动，取消“全部停止”计时", f)
                    ctl["all_stopped_since"] = None

            # —— 规则3：所有被监控会话都「正常完成」→ 关机 ——
            # 与「全部停止」的区别：这里要求每个会话最后一个 turn 都是
            # completed。失败、还在跑的都不算完成，所以不会「活没干完就关机」。
            if shutdown_all_done and not ctl.get("all_done_fired"):
                states = []
                for cv in convs:
                    t = last_turn(tcon, cv["leaf"])
                    states.append((cv, t[2] if t else None))
                    # 被中断的 turn 会永久停在 inProgress（completed_at 为 NULL），
                    # 这条残留永远等不到 completed →「全部完成」关机永远不触发。
                    # 只把它说清楚，不改判定：没真完成就不能关机。
                    if t is not None and t[2] == "inProgress" and \
                            codex_activity.is_stale_in_progress(cv["leaf"], t[4]):
                        if cv["root"] not in ctl.setdefault("stale_logged", set()):
                            ctl["stale_logged"].add(cv["root"])
                            log(f"[{cv['name']}] 最后一条 turn 停在 inProgress，"
                                f"且已 {codex_activity.stale_minutes_text(cv['leaf'], t[4])}"
                                f"没有活动 —— 这是被中断留下的残留记录，"
                                f"「全部完成」关机不会触发；想按「没活动就关机」"
                                f"请开「全部停止 N 分钟」规则", f)
                if states and all(st == "completed" for _c, st in states):
                    if not ctl.get("all_done_since"):
                        ctl["all_done_since"] = time.time()
                        names = "、".join(cv["name"] for cv, _ in states)
                        log(f"所有被监控会话均已正常完成（{names}），"
                            f"持续 {all_done_seconds:.0f} 秒后关机", f)
                        if coord is not None:
                            coord.mark_done()
                            log(f"已在协调文件登记本来源完成：{coord.path}", f)
                    elif time.time() - ctl["all_done_since"] >= all_done_seconds:
                        # 二次确认：重读一遍，避免刚好有新任务进来
                        tcon3 = ro_connect(DB_TURNS)
                        try:
                            re_states = [last_turn(tcon3, cv["leaf"]) for cv in convs]
                        finally:
                            tcon3.close()
                        still = bool(re_states) and all(
                            t is not None and t[2] == "completed" for t in re_states)
                        if not still:
                            log("「全部完成」关机二次确认未通过（有新活动），取消关机", f)
                            ctl["all_done_since"] = None
                            if coord is not None:
                                coord.clear_done()
                        else:
                            if coord is not None:
                                coord.mark_done()
                                ok_shutdown, why = coord.evaluate()
                            else:
                                ok_shutdown, why = True, "未启用跨来源协调"
                            if not ok_shutdown:
                                log(f"本来源已完成，但暂不关机：{why}", f)
                            else:
                                ctl["all_done_fired"] = True
                                reason = (f"所有监控会话已正常完成（共 {len(convs)} 个）"
                                          if not coord or not coord.require_all
                                          else f"所有监控来源均已完成（{why}）")
                                run_shutdown(reason, f)
                                if ctl["shutdown"]:
                                    return
                else:
                    if ctl.get("all_done_since"):
                        pending = [cv["name"] for cv, st in states
                                   if st != "completed"]
                        log(f"仍有会话未完成（{'、'.join(pending[:3])}），"
                            f"取消「全部完成」计时", f)
                    ctl["all_done_since"] = None
                    if coord is not None:
                        coord.clear_done()
        finally:
            tcon.close()
            scon.close()

    try:
        # 先写一次心跳，让插件面板立刻能看到「监控脚本已启动」。
        write_monitor_heartbeat(args.dry_run, args.poll)
        while True:
            try:
                handle()
            except Exception as e:
                log(f"检测异常: {e!r}（下轮自动重试）", f)
            write_monitor_heartbeat(args.dry_run, args.poll)
            if ctl["shutdown"]:
                log("已触发关机，监控退出", f)
                break
            if args.once:
                log("自检完成（--once），退出", f)
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出", f)
    finally:
        f.close()


if __name__ == "__main__":
    main()
