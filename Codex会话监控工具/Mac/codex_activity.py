#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判断 Codex 会话「到底还在不在跑」。

为什么需要：Codex 的 turn 被中途打断时（应用被关掉、崩溃、进程被杀），
thread_history 里那条记录的 status 会**永远停在 inProgress**、completed_at 一直是
NULL —— Codex 不会回头把它改成 interrupted。于是：

  · 会话列表把几天前的会话一直显示成「进行中」（实测本机有 14 天前的残留）；
  · 「全部完成后关机」要求每条 turn 都是 completed，会被这条残留记录卡住永不触发。

怎么判断（两个信号取较新的一个，距今超过 STALE_MINUTES 就算残留）：

  1. 该会话的 rollout 文件（~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl）
     的最后写入时间 —— 真在跑的 turn 会不停往里写；
  2. turn 的 started_at（拿不到 rollout 文件时兜底）。

只在 status == "inProgress" 时才问这个模块：其它状态（completed / failed /
interrupted）本来就有明确结论，不需要猜。
"""
import os
import time
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SESSIONS_DIR = CODEX_HOME / "sessions"
# 多久没动静就算「这条 inProgress 是残留」。真在跑的 turn 几秒就写一次 rollout，
# 给到 15 分钟是为了容忍超长的单次模型调用，不至于把真在跑的会话误判成残留。
STALE_MINUTES = 15


def _to_seconds(ts):
    """时间戳统一成秒：数据库里是秒，但留一手防它哪天改毫秒。"""
    if ts is None:
        return None
    ts = float(ts)
    return ts / 1000.0 if ts > 1e11 else ts


def rollout_mtime(thread_id, sessions_dir=None):
    """该会话 rollout 文件的最后写入时间（epoch 秒）；找不到返回 None。"""
    if not thread_id:
        return None
    root = Path(sessions_dir) if sessions_dir else SESSIONS_DIR
    if not root.is_dir():
        return None
    best = None
    try:
        for p in root.glob(f"**/*{thread_id}*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if best is None or m > best:
                best = m
    except OSError:
        return None
    return best


def last_activity(thread_id, started_at, sessions_dir=None):
    """最近一次活动时间（epoch 秒）；没有任何证据时返回 None。"""
    stamps = [_to_seconds(rollout_mtime(thread_id, sessions_dir)),
              _to_seconds(started_at)]
    stamps = [t for t in stamps if t]
    return max(stamps) if stamps else None


def is_stale_in_progress(thread_id, started_at, now=None, stale_minutes=STALE_MINUTES,
                         sessions_dir=None):
    """status 是 inProgress 的这条记录，是不是「早就没人跑了」的残留。

    started_at 传 thread_turns.started_at。没有任何时间证据时返回 False ——
    宁可当成「还在跑」，也不乱判成残留。
    """
    now = time.time() if now is None else now
    latest = last_activity(thread_id, started_at, sessions_dir)
    if latest is None:
        return False
    return (now - latest) > stale_minutes * 60


def stale_minutes_text(thread_id, started_at, now=None, sessions_dir=None):
    """给日志用：这条残留记录已经多久没活动了（人话，如「14 天」）。"""
    now = time.time() if now is None else now
    latest = last_activity(thread_id, started_at, sessions_dir)
    if latest is None:
        return "很久"
    mins = max(0.0, (now - latest) / 60.0)
    if mins < 90:
        return f"{mins:.0f} 分钟"
    hours = mins / 60.0
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


if __name__ == "__main__":
    import sys
    tid = sys.argv[1] if len(sys.argv) > 1 else ""
    started = float(sys.argv[2]) if len(sys.argv) > 2 else None
    print(f"rollout 最后写入：{rollout_mtime(tid)}")
    print(f"最近活动：{last_activity(tid, started)}")
    print(f"算残留吗：{is_stale_in_progress(tid, started)}（{stale_minutes_text(tid, started)}没活动）")
