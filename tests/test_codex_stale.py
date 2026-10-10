# -*- coding: utf-8 -*-
"""验证「inProgress 残留记录」的识别。

背景：Codex 的 turn 被中途打断时（应用关掉、崩溃、进程被杀），thread_history 里那条
记录会永久停在 inProgress、completed_at 为 NULL。于是会话列表把几天前的会话一直显示
成「进行中」，而「全部完成后关机」也被这条残留卡住永不触发。

这里分三层验证：
  1. 判定逻辑本身（用临时 sessions 目录造 rollout 文件，不依赖本机 Codex）；
  2. GUI 的状态映射与「进行中」集合（残留不能算进行中，否则会被「选中所有进行中」选中）；
  3. 真实数据抽样（本机 Codex 库里有这种残留时，必须被认出来；没有就跳过）。
"""
import os
import sqlite3
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import codex_activity as ca  # noqa: E402

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


def make_rollout(root, thread_id, age_minutes):
    """在临时 sessions 目录里造一个 rollout 文件，mtime = 现在 - age_minutes。"""
    day = Path(root) / "2026" / "01" / "02"
    day.mkdir(parents=True, exist_ok=True)
    p = day / f"rollout-2026-01-02T00-00-00-{thread_id}.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    t = time.time() - age_minutes * 60
    os.utime(p, (t, t))
    return p


NOW = time.time()
TID = "11111111-2222-3333-4444-555555555555"

print("=" * 70)
print("1. 判定逻辑（临时 sessions 目录）")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)

    # 1a. rollout 文件很久没动 + turn 也是很久前开始的 → 残留
    make_rollout(root, TID, age_minutes=60 * 24 * 14)      # 14 天前
    check("14 天没活动的 inProgress 算残留",
          ca.is_stale_in_progress(TID, NOW - 60 * 60 * 24 * 14, now=NOW, sessions_dir=root))

    # 1b. rollout 文件刚写过（哪怕 turn 是很久前开始的）→ 还在跑
    make_rollout(root, TID, age_minutes=0.2)
    check("rollout 刚写过就不算残留（rollout 信号优先）",
          not ca.is_stale_in_progress(TID, NOW - 60 * 60 * 24 * 14, now=NOW, sessions_dir=root))

    # 1c. 找不到 rollout 文件时用 started_at 兜底
    other = "99999999-8888-7777-6666-555555555555"
    check("没有 rollout 文件、turn 刚开始 → 不算残留",
          not ca.is_stale_in_progress(other, NOW - 60, now=NOW, sessions_dir=root))
    check("没有 rollout 文件、turn 是 3 小时前的 → 算残留",
          ca.is_stale_in_progress(other, NOW - 3 * 3600, now=NOW, sessions_dir=root))

    # 1d. 完全没有时间证据 → 一律当成还在跑（宁可漏判，不误判）
    check("没有任何时间证据 → 不判残留",
          not ca.is_stale_in_progress(other, None, now=NOW, sessions_dir=root))

    # 1e. 毫秒时间戳也要能处理（防它哪天改单位）
    check("started_at 是毫秒时判定一致",
          ca.is_stale_in_progress(other, (NOW - 3 * 3600) * 1000, now=NOW, sessions_dir=root))

    # 1f. 阈值边界：15 分钟以内不算残留
    check("14 分钟没活动不算残留",
          not ca.is_stale_in_progress(other, NOW - 14 * 60, now=NOW, sessions_dir=root))
    check("16 分钟没活动算残留",
          ca.is_stale_in_progress(other, NOW - 16 * 60, now=NOW, sessions_dir=root))

    # 1g. 人话时长（用没有 rollout 文件的会话，避免被上面刚 touch 过的文件干扰）
    txt = ca.stale_minutes_text(other, NOW - 60 * 60 * 24 * 14, now=NOW, sessions_dir=root)
    check("时长文案按天显示", txt.endswith("天"), txt)
    txt2 = ca.stale_minutes_text(other, NOW - 3 * 3600, now=NOW, sessions_dir=root)
    check("时长文案按小时显示", txt2.endswith("小时"), txt2)

print()
print("=" * 70)
print("2. GUI：残留不算「进行中」")
try:
    import codex_monitor_gui as gui
    check("状态表里有「已停止(残留)」", gui.STATUS_CN.get(gui.STALE_INPROGRESS) == "已停止(残留)")
    check("「进行中」集合不含残留状态", gui.STALE_INPROGRESS not in gui.RUNNING_STATUSES,
          str(sorted(gui.RUNNING_STATUSES)))
    check("inProgress 仍在「进行中」集合里", "inProgress" in gui.RUNNING_STATUSES)
except Exception as e:
    check(f"GUI 检查未能运行：{e!r}", False)

print()
print("=" * 70)
print("3. 真实数据抽样")
db = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "thread_history_1.sqlite"
if not db.exists():
    print("  （本机没有 Codex 库，跳过）")
else:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    stale_row = con.execute(
        "select thread_id, started_at from thread_turns where status='inProgress' "
        "and completed_at is null order by started_at desc limit 1").fetchone()
    done_row = con.execute(
        "select thread_id, started_at from thread_turns where status='completed' "
        "order by started_at desc limit 1").fetchone()
    con.close()
    if stale_row:
        tid, started = stale_row
        check("库里最新的 inProgress 残留被认出来",
              ca.is_stale_in_progress(tid, started),
              f"{tid[:8]}… 已 {ca.stale_minutes_text(tid, started)}没活动")
        try:
            root = tk.Tk()
            root.withdraw()
            app = gui.App(root)
            check("GUI 对这条会话不再显示 inProgress",
                  app._codex_status(tid) == gui.STALE_INPROGRESS,
                  app._codex_status(tid))
            if done_row:
                dtid = done_row[0]
                check("正常完成的会话状态不受影响",
                      app._codex_status(dtid) == "completed", app._codex_status(dtid))
            root.destroy()
        except Exception as e:
            check(f"GUI 真实数据检查未能运行：{e!r}", False)
    else:
        print("  （库里没有 inProgress 残留，跳过）")

print()
print("=" * 70)
if FAIL:
    print(f"有 {len(FAIL)} 项未通过：")
    for name in FAIL:
        print("  ✗ " + name)
    sys.exit(1)
print("inProgress 残留识别检查全部通过")
