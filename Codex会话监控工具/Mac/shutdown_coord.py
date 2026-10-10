#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shutdown_coord.py — 「全部完成后关机」的跨来源协调。

为什么需要单独一个模块：
  监控 Codex 和监控 Pi 是两个独立进程，各自只看得到自己那部分会话。
  如果各自判定「我这边全完成了」就关机，那么同时监控两个来源时，
  先跑完的那个就会把机器关掉 —— 而另一个来源的任务还在跑。
  这正是「活没干完就关机」，必须避免。

做法（纯文件，无端口、无守护进程）：
  每个进程在协调文件里只登记自己那一份状态，互不覆盖：
      {"sources": {"pi": {"done": true, "at": 1791...}, "codex": {...}}}
  判定关机时：
    · 不要求跨来源：自己这边完成即关机（与旧行为一致）；
    · 要求跨来源：--all-sources 里列出的每个来源都登记为 done，
      且登记没有过期，才关机。
  登记过期（默认 10 分钟没刷新）视为「那个进程可能已经不在了」，
  此时不关机并给出提示 —— 宁可多等，也不要误关。

  登记由进程每轮刷新，所以只要进程活着就不会过期；过期只意味着进程死了。

写文件用「临时文件 + 改名」，避免两个进程同时读到半个文件。
"""
import json
import os
import shlex
import time
import uuid
from pathlib import Path

# 协调文件位置：与两个监控脚本的打包方式无关，用固定的家目录位置。
# 不放在 Pi 的插件目录里，因为只监控 Codex 时那个目录可能根本不存在。
DEFAULT_COORD_DIR = Path.home() / ".session-monitor"
DEFAULT_COORD_FILE = DEFAULT_COORD_DIR / "shutdown-coord.json"

# 登记多久没刷新就视为过期（秒）。进程每轮都会刷新，所以只有进程死了才会过期。
DEFAULT_TTL_SECONDS = 600

def default_coord_file():
    return str(DEFAULT_COORD_FILE)


class ShutdownCoordinator:
    """跨来源的「全部完成」登记与判定。"""

    def __init__(self, source, all_sources=None, coord_file=None,
                 require_all=False, ttl_seconds=DEFAULT_TTL_SECONDS):
        self.source = str(source or "unknown").strip() or "unknown"
        # 本次运行实际监控的来源集合（含自己）；空表示只有自己
        self.all_sources = [s for s in (all_sources or []) if s] or [self.source]
        if self.source not in self.all_sources:
            self.all_sources.append(self.source)
        self.path = Path(coord_file or default_coord_file())
        self.require_all = bool(require_all)
        self.ttl = float(ttl_seconds)
        self._warned_stale = set()

    # ------------------------------------------------------------ 读写
    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sources"), dict):
                return data
        except Exception:
            pass
        return {"sources": {}}

    def _write(self, data):
        """原子写：先写临时文件再改名，避免并发读到半个文件。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except Exception:
            return False

    def _update(self, mutate):
        data = self._read()
        data.setdefault("sources", {})
        mutate(data["sources"])
        data["updatedAt"] = int(time.time() * 1000)
        return self._write(data)

    # ------------------------------------------------------------ 对外
    def mark_done(self):
        """登记「本来源已全部完成」，并刷新时间戳。"""
        def mutate(sources):
            sources[self.source] = {"done": True,
                                    "at": int(time.time() * 1000)}
        return self._update(mutate)

    def clear_done(self):
        """撤销本来源的完成登记（有会话重新活动时调用）。"""
        def mutate(sources):
            sources.pop(self.source, None)
        return self._update(mutate)

    def _fresh(self, entry, now_ms):
        """登记是否仍然有效（没过期）。"""
        if not isinstance(entry, dict) or entry.get("done") is not True:
            return False
        at = entry.get("at") or 0
        return (now_ms - at) <= self.ttl * 1000

    def evaluate(self):
        """判断现在是否可以关机。

        返回 (可以关机, 说明)。说明用于写日志，无论可否都给出理由。
        """
        if not self.require_all:
            return True, "未要求跨来源，本来源全部完成即可关机"

        now_ms = time.time() * 1000
        data = self._read()
        sources = data.get("sources") or {}
        pending, stale = [], []
        for name in self.all_sources:
            entry = sources.get(name)
            if self._fresh(entry, now_ms):
                continue
            # 自己这份刚写过，一定是新的；其余来源过期说明那个进程可能不在了
            if entry is not None and not self._fresh(entry, now_ms):
                stale.append(name)
            pending.append(name)

        if not pending:
            names = "、".join(self.all_sources)
            return True, f"所有监控来源均已完成（{names}）"

        detail = "、".join(pending)
        if stale:
            msg = (f"仍等待这些来源完成：{detail}"
                   f"（其中 {'、'.join(stale)} 的登记已过期，"
                   f"可能那个监控进程已退出 → 为安全起见不关机）")
        else:
            msg = f"仍等待这些来源完成：{detail}"
        return False, msg


def resolve_setting(cli_value, plugin_value, fallback):
    """CLI 显式值优先，否则用插件设置，最后退回内置默认。

    CLI 用 "auto" 表示「没指定，听插件的」——这样「在插件面板里勾一下」
    就能生效，不必再去改命令行。
    """
    if cli_value not in (None, "", "auto"):
        return cli_value
    if plugin_value is not None:
        return plugin_value
    return fallback


def parse_on_off(value, fallback=False):
    """把 on/off/true/false/1/0 之类的写法统一成布尔。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if text in ("on", "true", "1", "yes", "y"):
        return True
    if text in ("off", "false", "0", "no", "n"):
        return False
    return fallback


def evaluate_all_503(scan, now_ms, window_minutes):
    """判断「近期活跃会话是否全部报 503」。

    scan: [(名字, 最后活动毫秒, 是否503)]，包含所有被监控会话。

    只有「窗口内有活动」的会话参与判定 —— 历史会话不参与，
    否则几天前的旧错误会永远凑不齐「全部」，规则等于失效。

    返回 (是否触发, 参与判定的会话名列表, 其中不是 503 的名字列表)。
    没有任何近期活跃会话时返回 (False, [], [])：无人可判，绝不关机。
    """
    try:
        window_ms = max(0.0, float(window_minutes)) * 60_000
    except (TypeError, ValueError):
        return False, [], []
    recent = [(str(n), bool(ok)) for (n, u, ok) in scan
              if u is not None and (now_ms - u) <= window_ms]
    if not recent:
        return False, [], []
    bad = [n for n, ok in recent if not ok]
    return (not bad), [n for n, _ in recent], bad




def split_shutdown_cmd(cmd):
    """把关机指令拆成参数列表（跨平台）。

    为什么不能直接用 shlex.split：在 Windows 下它按 POSIX 规则处理反斜杠，
    会把 `C:\\path\\to\\x.exe` 变成 `C:pathtox.exe`，导致自定义关机指令
    （尤其是带路径的脚本）执行失败 —— 而失败时调用方往往仍以为关机已触发。
    这里按平台选择规则：Windows 用 posix=False 并剥掉外层引号，其余用 POSIX。
    """
    text = str(cmd or "").strip()
    if not text:
        return []
    if os.name == "nt":
        parts = shlex.split(text, posix=False)
        # posix=False 会把引号原样保留，这里剥掉（含前后成对的引号）
        out = []
        for part in parts:
            if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"'):
                part = part[1:-1]
            out.append(part)
        return out
    return shlex.split(text)
