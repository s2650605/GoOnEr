#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会话监控控制台（Codex + Pi 统一界面，tkinter）

功能：
  - 启动/停止监控（codex_monitor.py / pi_monitor.py 子进程，关闭时自动终止、无残留）
  - 【会话列表】左侧按项目分组的会话树（可展开/收起），Codex 与 Pi 的会话一起列出，
    项目名前带 [Codex] / [Pi] 标记；点击某个会话即加入下方"要监控的对话"列表，
    再次点击可移出；可多选
  - 不选择任何会话时，两个来源各自按"监控会话数"自动选最近活跃的 N 个
  - 手动开关两条关机规则：错误码连续 N 次关机、全部停止 N 分钟关机
  - 设置：监控会话数、轮询间隔、退避节奏、±随机抖动、每小时发送上限、
    发送后重试间隔、消息内容、关机指令
  - 模拟运行(dry-run)：只检测不发送不关机
  - 实时日志显示（每行标注来源）

两个来源的差别（重要）：
  Codex 有命令行入口（codex queue --thread <id> --message），直接由 codex_monitor.py 发送。
  Pi（PI-Desktop）没有命令行入口，pi_monitor.py 把发送交给 Pi 插件
  local.session-keeper 执行；插件没装/没开时，Pi 监控会明确报错而不是静默失效。

启动：python codex_monitor_gui.py（或双击 start_codex_monitor_gui.bat）
"""
import datetime
import json
import os
import platform
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
# PI-Desktop 的「已归档会话」表：归档状态不在 pi.sqlite 里，见 pi_archive.py

# Codex 的「inProgress 是不是残留」判定：被打断的 turn 会永久停在 inProgress
import codex_activity
import pi_archive

# 控制台窗口标题：既用于显示，也用于「重复启动时把已有窗口拉到前台」
CONSOLE_TITLE = "会话监控控制台（Codex + Pi）"

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    HERE = os.path.dirname(sys.executable)
    MONITOR_CODEX = os.path.join(HERE, "codex_monitor.exe")
    MONITOR_PI = os.path.join(HERE, "pi_monitor.exe")
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    MONITOR_CODEX = os.path.join(HERE, "codex_monitor.py")
    MONITOR_PI = os.path.join(HERE, "pi_monitor.py")

SRC_CODEX = "codex"
SRC_PI = "pi"
SRC_LABEL = {SRC_CODEX: "Codex", SRC_PI: "Pi"}

# 按平台设置默认关机指令
_sysname = platform.system()
if _sysname == "Windows":
    DEFAULT_SHUTDOWN_CMD = "shutdown /s /f /t 10"
elif _sysname == "Darwin":
    DEFAULT_SHUTDOWN_CMD = "osascript -e 'tell application \"System Events\" to shut down'"
else:
    DEFAULT_SHUTDOWN_CMD = "shutdown -h now"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MAX_LINES = 5000

# —— Codex 数据位置 ——
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
DB_THREADS = CODEX_HOME / "state_5.sqlite"
DB_TURNS = CODEX_HOME / "thread_history_1.sqlite"
RELAY_RE = re.compile(
    r"codex://threads/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# —— Pi 数据位置 ——
PI_HOME = Path(os.environ.get("PI_DESKTOP_DATA_DIR", Path.home() / ".pi-desktop"))
PI_DB = PI_HOME / "pi.sqlite"
PI_HEARTBEAT = PI_HOME / "session-keeper" / "plugin.json"

# 「inProgress 但早就没动静」——Codex 被打断会留下这种永久 inProgress 的记录。
# 这是本工具自己造的状态（数据库里没有这个值），只用于显示与「选中所有进行中」的判定。
STALE_INPROGRESS = "staleInProgress"

# 状态中文名：Codex 与 Pi 的口径合并
STATUS_CN = {
    "inProgress": "进行中", "running": "进行中", "queued": "排队中",
    "completed": "已完成", "complete": "已完成",
    "failed": "已失败", "error": "已失败",
    "interrupted": "已停止", "aborted": "已停止",
    STALE_INPROGRESS: "已停止(残留)",
}

# 「进行中」的会话状态：从 STATUS_CN 派生，避免与列表显示的中文名两处口径漂移。
# Codex 是 inProgress，Pi 是 running。
RUNNING_STATUSES = {k for k, v in STATUS_CN.items() if v == "进行中"}


def ro_connect(db: Path):
    return sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=10)


def project_name(path):
    if not path:
        return "（无项目）"
    return Path(str(path).replace("\\\\?\\", "")).name or "（无项目）"


def clean_title(title):
    if not title:
        return ""
    lines = [ln.strip() for ln in title.splitlines() if ln.strip()]
    out = [ln for ln in lines if not RELAY_RE.search(ln)]
    return " ".join(out)[:40]


def pi_plugin_status():
    """读取 Pi 插件执行器心跳；返回 (是否可用, 说明)。"""
    if not PI_HEARTBEAT.exists():
        return False, f"未找到 Pi 插件执行器心跳：{PI_HEARTBEAT}"
    try:
        data = json.loads(PI_HEARTBEAT.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"心跳文件无法解析：{e!r}"
    age = time.time() * 1000 - (data.get("at") or 0)
    if age > 30_000:
        return False, f"插件执行器心跳已过期（{age / 1000:.0f} 秒未更新），Pi 可能已退出"
    if data.get("running") is False:
        return False, "插件执行器已停止"
    if data.get("enabled") is False:
        return False, "插件执行器已被关闭（请在 Pi 插件面板里勾选启用）"
    return True, f"执行器正常（插件 v{data.get('version', '?')}，PID {data.get('pid', '?')}）"


def plugin_shutdown_settings():
    """读 Pi 插件面板里的设置（心跳文件），作为 GUI 的初始值。

    为什么需要：GUI 的复选框和插件面板的设置是同一件事的两个入口。
    如果 GUI 固定从「未勾选」起步，用户睡前在插件里勾上了，
    一打开 GUI 又变成没勾（且启动时会传 off 覆盖掉插件设置），
    两边就静默打架了。这里以插件设置为准做初始值。

    覆盖：关机策略 + 报错/上下文超限续接（两者都在插件面板里配置）。
    """
    try:
        data = json.loads(PI_HEARTBEAT.read_text(encoding="utf-8"))
    except Exception:
        return {}
    age = time.time() * 1000 - (data.get("at") or 0)
    if age > 30_000:
        return {}   # 心跳过期说明 Pi 没在跑，此时插件设置不可信
    return data



def enable_dpi_awareness():
    """Windows 高 DPI：按真实像素渲染。

    为什么需要：tkinter 默认不是 DPI 感知的，系统缩放 125%/150% 时会把窗口
    整体位图拉伸 —— 字发虚，而且控件尺寸与字体对不上，长行尾巴容易被挤掉。
    必须在创建 Tk() 之前调用；非 Windows 或老系统失败都忽略（不影响运行）。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Win8.1+ 每显示器感知
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()        # 老系统退回
    except Exception:
        pass


def system_dpi():
    """系统 DPI（拿不到就按 96 算）。"""
    try:
        import ctypes
        try:
            return int(ctypes.windll.user32.GetDpiForSystem())
        except Exception:
            hdc = ctypes.windll.user32.GetDC(0)
            try:
                return int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSY
            finally:
                ctypes.windll.user32.ReleaseDC(0, hdc)
    except Exception:
        return 96


def apply_tk_scaling(root):
    """让 Tk 的字体缩放跟随系统 DPI。

    DPI 感知之后必须自己做这件事：否则 9pt 字在高 DPI 屏上会偏小、
    和系统里其它应用的观感不一致，排版也更容易挤。
    """
    try:
        root.tk.call("tk", "scaling", max(1.0, system_dpi() / 72.0))
    except Exception:
        pass

class App:
    def __init__(self, root):
        self.root = root
        self.procs = {}          # source -> Popen
        self.q = queue.Queue()
        self._sessions = []      # 全部会话 dict 列表（两个来源）
        self._monitored = []     # 已选要监控的会话
        root.title(CONSOLE_TITLE)
        apply_tk_scaling(root)
        # 全局中文字体（打包成 exe 后默认字体可能不支持中文）
        root.option_add("*Font", ("Microsoft YaHei UI", 9))
        root.minsize(720, 520)
        self._wrap_labels = []   # 需要按窗口宽度重新折行的说明文字

        # Treeview 的行高是主题初始化时按「当时的字体」算死的，而主题在 Tk() 创建时
        # 就加载了 —— 我们的 DPI 缩放改在它之后，行高就偏小：中文上下贴边、甚至被切。
        # 必须在建控件之前按当前字体显式给足行高。
        try:
            import tkinter.font as _tkfont
            _line = _tkfont.nametofont("TkDefaultFont").metrics("linespace")
            ttk.Style(root).configure("Treeview",
                                      rowheight=max(20, int(_line * 1.35) + 1))
        except Exception:
            pass
        self._build()
        self._refresh_sessions()
        # 内容填好后再定尺寸：状态文字（数据库路径/通道状态）这时才出现，
        # 提前量会量不到它们，窗口就会偏窄、行尾被挤出可视区。
        self._fit_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 首次布局落定（窗口真正映射）后再量一次：此时行高与容器高度才是真的
        self.root.after(150, self._fit_tree_height)
        self.root.after(100, self._drain)

    # ---------- 窗口与滚动 ----------
    def _fit_window(self):
        """窗口按内容需要的大小打开，并限制在屏幕内。

        为什么不能写死尺寸：系统缩放 125%/150% 时字更大，内容会比固定尺寸宽，
        行尾控件被压窄、文字被吞掉。这里先量内容再定尺寸；真放不下时由外层
        滚动条兜底（见 _build 里的 canvas）。
        """
        # 先按「宽屏两列」量一次：屏幕够宽时这才是想要的布局，
        # 否则会量到单列的窄宽度、把窗口开小，两列就永远用不上。
        self._set_layout(True)
        self.root.update_idletasks()
        need_w = self.body.winfo_reqwidth() + 30
        need_h = self.body.winfo_reqheight() + 30
        w = max(760, min(need_w, self.root.winfo_screenwidth() - 80))
        h = max(560, min(need_h, self.root.winfo_screenheight() - 140))
        self.root.geometry(f"{w}x{h}")
        # 定好尺寸后再按真实宽度决定要不要退回单列（屏幕窄时两列会被挤）
        self._set_layout(w >= self.COLUMN_MIN_WIDTH)

    def _on_body_configure(self, _event=None):
        """内容尺寸变化 → 更新滚动范围、把内容铺满视口，并让长说明重新折行。"""
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass
        # 宽和高都至少铺满视口：
        #  - 宽度：内容比视口宽时保持自然宽度，否则行尾控件会被压窄（吞字）；
        #  - 高度：窗口比内容高时让内容铺满，多出来的高度分给两列，
        #          会话列表因此跟着变长（见 _fit_tree_height）。
        try:
            need_w = self.body.winfo_reqwidth()
            need_h = self.body.winfo_reqheight()
            if need_w > 1:
                self.canvas.itemconfigure(
                    self._body_id,
                    width=max(self.canvas.winfo_width(), need_w),
                    height=max(self.canvas.winfo_height(), need_h))
        except Exception:
            pass
        width = self.body.winfo_width()
        if width > 1:
            for lbl in self._wrap_labels:
                try:
                    # 上限要卡住：不封顶的话，长说明会按窗口宽度一路撑开，
                    # 把整列（乃至整页）顶宽，白占地方还逼出横向滚动条。
                    cap = getattr(lbl, "_wrap_cap", 900)
                    lbl.configure(wraplength=min(cap, max(320, width - 90)))
                except Exception:
                    pass

    def _on_canvas_configure(self, event):
        """视口尺寸变化 → 内容跟着铺满（宽度不够时保持自然宽度，宁可出滚动条）。"""
        try:
            need_w = self.body.winfo_reqwidth()
            need_h = self.body.winfo_reqheight()
            self.canvas.itemconfigure(self._body_id,
                                      width=max(event.width, need_w),
                                      height=max(event.height, need_h))
        except Exception:
            pass

    def _on_wheel(self, event):
        """滚轮滚动整页；鼠标停在日志/列表上时交给它们自己滚。"""
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            w = None
        while w is not None:
            if w.winfo_class() in ("Text", "Listbox", "Treeview"):
                return
            w = getattr(w, "master", None)
        try:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        except Exception:
            pass

    def _fit_tree_height(self):
        """按可用高度自动加长会话列表，并且只显示整行（避免最后一行被切一半）。

        行高与表头高度都从实际渲染的第一行量出来（跟着字体/DPI 走）；
        还没绘制出来（启动瞬间）时退回样式里显式设好的行高。
        """
        try:
            avail = self.tree.master.winfo_height()
            if avail <= 1:
                return
            row_h = 0
            head = 0
            for iid in self.tree.get_children()[:1]:
                bbox = self.tree.bbox(iid)
                if bbox:
                    row_h = bbox[3]
                    head = bbox[1]      # 第一行的 y 偏移就是表头高度
            if row_h <= 0:
                try:
                    row_h = int(ttk.Style(self.root).lookup("Treeview", "rowheight")) or 22
                except Exception:
                    row_h = 22
            if head <= 0:
                head = 26
            rows = max(6, int((avail - head) // row_h))
            if rows != int(self.tree.cget("height")):
                self.tree.configure(height=rows)
        except Exception:
            pass

    # ---------- 两列 / 单列布局 ----------
    COLUMN_MIN_WIDTH = 1080   # 低于这个宽度退回单列（并排会把行挤窄、文字被吞）

    def _set_layout(self, wide):
        """切换两列/单列（幂等）。宽屏：左配置、右会话；窄屏：上下堆叠。"""
        if wide == self._wide:
            return
        self._wide = wide
        try:
            self.col_left.grid_forget()
            self.col_right.grid_forget()
            if wide:
                self.col_left.grid(row=0, column=0, sticky="new", padx=(0, 8))
                self.col_right.grid(row=0, column=1, sticky="nsew")
                self.cols.grid_rowconfigure(0, weight=1)
                self.cols.grid_columnconfigure(0, weight=1)
                self.cols.grid_columnconfigure(1, weight=1)
            else:
                self.col_left.grid(row=0, column=0, sticky="new")
                self.col_right.grid(row=1, column=0, sticky="new", pady=(8, 0))
                self.cols.grid_columnconfigure(0, weight=1)
                self.cols.grid_columnconfigure(1, weight=0)
        except Exception:
            pass

    def _apply_layout(self, _event=None):
        """窗口宽度变化时决定两列还是单列。"""
        try:
            width = self.root.winfo_width()
        except Exception:
            return
        if width < 200:      # 窗口还没真正映射出来（宽度是 1 之类），别急着改布局
            return
        self._set_layout(width >= self.COLUMN_MIN_WIDTH)

    # ---------- UI ----------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        # 整页可滚动：内容比窗口大时能滚到，不会被遮挡；横向滚动条是
        # 「某行比视口宽」时的兜底（正常不该出现）。
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        hbar = ttk.Scrollbar(outer, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        vbar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        frm = ttk.Frame(self.canvas, padding=10)
        self.body = frm
        self._body_id = self.canvas.create_window((0, 0), window=frm, anchor="nw")
        frm.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.root.bind("<MouseWheel>", self._on_wheel)

        # ---- 两列布局容器 ----
        # 宽屏：左列配置、右列会话与监控列表，控制行与日志横跨整页；窄屏退回单列。
        # 注意：控件必须一开始就建在对应列里 —— Tk 的 pack(in_=...) 不会真的改父级。
        self.cols = ttk.Frame(frm)
        # 两列区吸收「窗口比内容高」时多出来的高度 → 会话列表跟着变长
        self.cols.pack(fill="both", expand=True, **pad)
        self.col_left = ttk.Frame(self.cols)
        self.col_right = ttk.Frame(self.cols)

        # 基础参数
        g1 = ttk.LabelFrame(self.col_left, text="基础参数（修改需先停止监控）")
        g1.pack(fill="x", **pad)
        r1 = ttk.Frame(g1); r1.pack(fill="x", padx=6, pady=4)
        ttk.Label(r1, text="监控会话数").pack(side="left")
        self.var_auto = tk.StringVar(value="2")
        ttk.Entry(r1, textvariable=self.var_auto, width=5).pack(side="left", padx=4)
        ttk.Label(r1, text="轮询间隔(秒)").pack(side="left", padx=(14, 0))
        self.var_poll = tk.StringVar(value="10")
        ttk.Entry(r1, textvariable=self.var_poll, width=6).pack(side="left", padx=4)
        ttk.Label(r1, text="发送消息").pack(side="left", padx=(14, 0))
        self.var_msg = tk.StringVar(value="继续")
        ttk.Entry(r1, textvariable=self.var_msg, width=8).pack(side="left", padx=4)
        ttk.Label(r1, text="退避节奏(分钟)").pack(side="left", padx=(14, 0))
        self.var_backoff = tk.StringVar(value="1,1,3,5,5")
        ttk.Entry(r1, textvariable=self.var_backoff, width=12).pack(side="left", padx=4)
        r2 = ttk.Frame(g1); r2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(r2, text="±抖动(秒)").pack(side="left")
        self.var_jitter = tk.StringVar(value="3")
        ttk.Entry(r2, textvariable=self.var_jitter, width=5).pack(side="left", padx=4)
        ttk.Label(r2, text="每小时上限").pack(side="left", padx=(14, 0))
        self.var_maxph = tk.StringVar(value="20")
        ttk.Entry(r2, textvariable=self.var_maxph, width=5).pack(side="left", padx=4)
        ttk.Label(r2, text="重试(分钟)").pack(side="left", padx=(14, 0))
        self.var_retry = tk.StringVar(value="15")
        ttk.Entry(r2, textvariable=self.var_retry, width=5).pack(side="left", padx=4)

        # 关机规则
        g2 = ttk.LabelFrame(self.col_left, text="关机规则")
        g2.pack(fill="x", **pad)
        r3 = ttk.Frame(g2); r3.pack(fill="x", padx=6, pady=4)
        self.var_use503 = tk.BooleanVar(value=False)
        self.cb503 = ttk.Checkbutton(r3, text="错误码连续", variable=self.var_use503)
        self.cb503.pack(side="left")
        self.var_503n = tk.StringVar(value="3")
        ttk.Entry(r3, textvariable=self.var_503n, width=4).pack(side="left", padx=2)
        ttk.Label(r3, text="次后关机（Codex: 405,503 ／ Pi: STREAM_FAILED 等）").pack(side="left")
        self.var_use_all = tk.BooleanVar(value=False)
        self.cb_all = ttk.Checkbutton(r3, text="全部停止", variable=self.var_use_all)
        self.cb_all.pack(side="left", padx=(18, 2))
        self.var_allmin = tk.StringVar(value="20")
        ttk.Entry(r3, textvariable=self.var_allmin, width=5).pack(side="left")
        ttk.Label(r3, text="分钟后关机").pack(side="left")
        r4 = ttk.Frame(g2); r4.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(r4, text="关机指令").pack(side="left")
        self.var_shut = tk.StringVar(value=DEFAULT_SHUTDOWN_CMD)
        # 全部正常完成 → 关机（比「全部停止」精确：卡住/失败的会话不算完成）
        # 两个来源都支持：各自判定自己的会话，再由跨来源选项决定是否要等对方。
        r3b = ttk.Frame(g2); r3b.pack(fill="x", padx=6, pady=(0, 4))
        # 初始值跟随 Pi 插件面板的设置，避免「插件里勾了、GUI 一开又变没勾」
        _ps = plugin_shutdown_settings()
        self.var_all_done = tk.BooleanVar(
            value=_ps.get("shutdownWhenAllDone") is True)
        self.cb_all_done = ttk.Checkbutton(
            r3b, text="所有会话都「正常完成」后关机", variable=self.var_all_done)
        self.cb_all_done.pack(side="left")
        ttk.Label(r3b, text="（再观察").pack(side="left", padx=(8, 0))
        self.var_done_secs = tk.StringVar(
            value=str(_ps.get("allDoneConfirmSeconds") or 120))
        ttk.Entry(r3b, textvariable=self.var_done_secs, width=5).pack(side="left", padx=2)
        ttk.Label(r3b, text="秒确认；卡在等确认或失败的会话不算完成，不会误关机）").pack(side="left")
        r3c = ttk.Frame(g2); r3c.pack(fill="x", padx=6, pady=(0, 6))
        self.var_all_sources = tk.BooleanVar(
            value=_ps.get("shutdownRequiresAllSources") is not False)
        self.cb_all_sources = ttk.Checkbutton(
            r3c, text="要求所有监控来源都完成才关机", variable=self.var_all_sources)
        self.cb_all_sources.pack(side="left")
        ttk.Label(r3c, text="（同时监控 Codex 和 Pi 时：勾上=两边都完成才关机；"
                            "不勾=谁先完成谁关机）").pack(side="left", padx=(6, 0))
        # 近期活跃会话全部 503 → 关机（服务商整体挂掉时再等也没用）
        r3d = ttk.Frame(g2); r3d.pack(fill="x", padx=6, pady=(0, 6))
        self.var_all503 = tk.BooleanVar(
            value=_ps.get("shutdownWhenAll503") is True)
        self.cb_all503 = ttk.Checkbutton(
            r3d, text="近期活跃会话全部报 503 后关机",
            variable=self.var_all503)
        self.cb_all503.pack(side="left")
        ttk.Label(r3d, text="（活跃窗口").pack(side="left", padx=(8, 0))
        self.var_all503_win = tk.StringVar(
            value=str(_ps.get("all503WindowMinutes") or 60))
        self.ent_all503_win = ttk.Entry(r3d, textvariable=self.var_all503_win, width=5)
        self.ent_all503_win.pack(side="left", padx=2)
        ttk.Label(r3d, text="分钟；更早的历史会话不参与判定）").pack(side="left")
        ttk.Entry(r4, textvariable=self.var_shut).pack(side="left", fill="x", expand=True, padx=4)

        # 来源与通道状态
        g2b = ttk.LabelFrame(self.col_right, text="监控来源")
        g2b.pack(fill="x", **pad)
        r4b = ttk.Frame(g2b); r4b.pack(fill="x", padx=6, pady=4)
        self.var_use_codex = tk.BooleanVar(value=True)
        self.cb_codex = ttk.Checkbutton(r4b, text="Codex", variable=self.var_use_codex,
                                        command=self._refresh_sessions)
        self.cb_codex.pack(side="left")
        self.lbl_codex = ttk.Label(r4b, text="", foreground="#666")
        self.lbl_codex.pack(side="left", padx=(6, 18))
        self.var_use_pi = tk.BooleanVar(value=True)
        self.cb_pi = ttk.Checkbutton(r4b, text="Pi（PI-Desktop）", variable=self.var_use_pi,
                                     command=self._refresh_sessions)
        self.cb_pi.pack(side="left")
        self.lbl_pi = ttk.Label(r4b, text="", foreground="#666")
        self.lbl_pi.pack(side="left", padx=(6, 0))
        ttk.Button(r4b, text="重新检测通道", command=self._check_channels).pack(side="right")
        # Pi 独有：完成/失败/等待确认时弹系统通知
        # 单独占一行：这一项文字长，和上面的来源状态挤在同一行时会被压窄、
        # 尾巴被吞掉（高 DPI 缩放下尤其明显）。
        self.var_notify = tk.BooleanVar(value=False)
        r4b2 = ttk.Frame(g2b); r4b2.pack(fill="x", padx=6, pady=(0, 4))
        self.cb_notify = ttk.Checkbutton(
            r4b2, text="Pi 完成/失败/等待确认时弹通知", variable=self.var_notify)
        self.cb_notify.pack(side="left")

        # 上下文续接规则：Codex 按 HTTP 400，Pi 按 CONTEXT_TOO_LARGE
        # 两边机制不同（Codex 只能开新会话；Pi 还能先压缩上下文），
        # 所以分两个区域各自配置。
        g2c = ttk.LabelFrame(self.col_left, text="400 续接规则（仅 Codex）")
        g2c.pack(fill="x", **pad)
        r4c = ttk.Frame(g2c); r4c.pack(fill="x", padx=6, pady=4)
        self.var_use400 = tk.BooleanVar(value=True)
        self.cb400 = ttk.Checkbutton(r4c, text="连续", variable=self.var_use400)
        self.cb400.pack(side="left")
        self.var_400n = tk.StringVar(value="1")
        ttk.Entry(r4c, textvariable=self.var_400n, width=4).pack(side="left", padx=2)
        ttk.Label(r4c, text="次 400 错误后，在当前项目下开新会话继续任务").pack(side="left")

        # Pi 报错续接（Pi 的「400」= PROVIDER_ERROR；上下文超限 = CONTEXT_TOO_LARGE）
        # 初始值跟随插件面板设置，避免「插件里勾了、GUI 一开又变没勾」。
        g2d = ttk.LabelFrame(self.col_left, text="报错/400 续接（仅 Pi）")
        g2d.pack(fill="x", **pad)
        r4d = ttk.Frame(g2d); r4d.pack(fill="x", padx=6, pady=4)
        self.var_ctx_on = tk.BooleanVar(
            value=_ps.get("contextContinue") is not False)
        self.cb_ctx_on = ttk.Checkbutton(
            r4d, text="连续", variable=self.var_ctx_on, command=self._toggle_ctx)
        self.cb_ctx_on.pack(side="left")
        self.var_ctx_n = tk.StringVar(
            value=str(_ps.get("contextContinueThreshold") or 3))
        ttk.Entry(r4d, textvariable=self.var_ctx_n, width=4).pack(side="left", padx=2)
        ttk.Label(r4d, text="次报错后：").pack(side="left")
        self.var_ctx_mode = tk.StringVar(
            value="new" if _ps.get("contextContinueMode") == "new" else "compact")
        self.cb_ctx_mode = ttk.Combobox(
            r4d, textvariable=self.var_ctx_mode, width=26, state="readonly",
            values=["compact", "new"])
        self.cb_ctx_mode.pack(side="left", padx=4)
        r4d3 = ttk.Frame(g2d); r4d3.pack(fill="x", padx=6, pady=(2, 0))
        self.var_400_new = tk.BooleanVar(
            value=_ps.get("openNewOn400") is not False)
        self.cb_400_new = ttk.Checkbutton(
            r4d3, text="真报 400 时直接新建会话（不等次数、不压缩）",
            variable=self.var_400_new, command=self._toggle_ctx)
        self.cb_400_new.pack(side="left")
        r4d2 = ttk.Frame(g2d); r4d2.pack(fill="x", padx=6, pady=(0, 6))
        self.lbl_ctx_hint = ttk.Label(r4d2, text="", foreground="#666", wraplength=760,
                                      justify="left")
        self.lbl_ctx_hint.pack(side="left")
        self.lbl_ctx_hint._wrap_cap = 760
        self._wrap_labels.append(self.lbl_ctx_hint)
        self._toggle_ctx()

        # 监控范围（Pi 专有：活跃窗口模式）
        g2e = ttk.LabelFrame(self.col_left, text="监控范围（Pi：报错自动续接 + 全部完成后关机）")
        g2e.pack(fill="x", **pad)
        r4e = ttk.Frame(g2e); r4e.pack(fill="x", padx=6, pady=4)
        self.var_active_all = tk.BooleanVar(value=False)
        self.cb_active_all = ttk.Checkbutton(
            r4e, text="监控所有活跃会话（最近", variable=self.var_active_all,
            command=self._toggle_active_all)
        self.cb_active_all.pack(side="left")
        self.var_active_min = tk.StringVar(value="60")
        self.ent_active_min = ttk.Entry(r4e, textvariable=self.var_active_min, width=5)
        self.ent_active_min.pack(side="left", padx=2)
        ttk.Label(r4e, text="分钟内有活动的，全部纳入；之后新出现的活跃会话自动加入）"
                  ).pack(side="left")
        r4e2 = ttk.Frame(g2e); r4e2.pack(fill="x", padx=6, pady=(0, 6))
        lbl_all_hint = ttk.Label(r4e2, foreground="#666", wraplength=880, justify="left",
                  text="勾选后不必在下面逐个挑会话。为什么不直接「监控全部」："
                       "实测 307 个未归档会话里有 58 个最后状态是失败，"
                       "但只有 2 个是最近真出问题的 —— 直接监控全部会一次性"
                       "误触发 56 个旧会话、狂开新会话。时间窗口天然避开这个坑。"
                  )
        lbl_all_hint.pack(side="left")
        lbl_all_hint._wrap_cap = 880
        self._wrap_labels.append(lbl_all_hint)

        # 会话列表
        g6 = ttk.LabelFrame(self.col_right, text="会话列表（按项目分组，点击会话加入监控列表，再次点击移出）")
        g6.pack(fill="both", expand=True, **pad)
        r6 = ttk.Frame(g6); r6.pack(fill="x", padx=6, pady=(4, 2))
        self.btn_refresh = ttk.Button(r6, text="刷新列表", command=self._refresh_sessions)
        self.btn_refresh.pack(side="left")
        self.btn_expand = ttk.Button(r6, text="展开全部", command=lambda: self._expand_all(True))
        self.btn_expand.pack(side="left", padx=4)
        self.btn_collapse = ttk.Button(r6, text="收起全部", command=lambda: self._expand_all(False))
        self.btn_collapse.pack(side="left")
        self.btn_pick_running = ttk.Button(r6, text="选中所有进行中",
                                           command=self._select_running)
        self.btn_pick_running.pack(side="left", padx=4)
        self.lbl_sess = ttk.Label(r6, text="", foreground="#666")
        self.lbl_sess.pack(side="right")
        body6 = ttk.Frame(g6)
        # 关键：会话列表容器的尺寸不跟着内容走 —— 否则「行数变多 → 容器变高 →
        # 行数更多」会自己越滚越大。这里让容器只由外层（列/窗口）决定，
        # 再由 _fit_tree_height 按容器实际高度算出能放几整行。
        body6.pack_propagate(False)
        body6.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        # 容器一变高就把会话列表的行数重算一遍（见 _fit_tree_height）
        body6.bind("<Configure>", lambda _e: self._fit_tree_height())
        # 多列展示：会话名占树列（可伸缩），最后活动与状态各占一列 ——
        # 以前三样挤在一行文本里，长名字一撑就整行被裁掉，且宽屏空间全浪费。
        self.tree = ttk.Treeview(body6, show="tree headings", height=12,
                                 selectmode="browse", columns=("updated", "status"))
        self.tree.heading("#0", text="项目 / 会话", anchor="w")
        self.tree.heading("updated", text="最后活动", anchor="center")
        self.tree.heading("status", text="状态", anchor="center")
        self.tree.column("#0", width=430, minwidth=200, stretch=True, anchor="w")
        self.tree.column("updated", width=100, minwidth=80, stretch=False, anchor="center")
        self.tree.column("status", width=110, minwidth=90, stretch=False, anchor="center")
        sby6 = ttk.Scrollbar(body6, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sby6.set)
        sby6.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="x", expand=True)   # 高度由 _fit_tree_height 定
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        # 要监控的对话
        g5 = ttk.LabelFrame(self.col_right, text="要监控的对话（不选则两个来源各按“监控会话数”自动选最近活跃的）")
        g5.pack(fill="x", **pad)
        r5 = ttk.Frame(g5); r5.pack(fill="x", padx=6, pady=(4, 2))
        self.btn_rm = ttk.Button(r5, text="移除选中", command=self._remove_selected)
        self.btn_rm.pack(side="left")
        self.btn_clear = ttk.Button(r5, text="清空", command=self._clear_monitored)
        self.btn_clear.pack(side="left", padx=4)
        self.lbl_mon = ttk.Label(r5, text="", foreground="#666")
        self.lbl_mon.pack(side="right")
        body5 = ttk.Frame(g5)
        body5.pack(fill="x", padx=6, pady=(0, 6))
        self.lst_mon = tk.Listbox(body5, height=4, selectmode="extended",
                                  font=("Microsoft YaHei UI", 9), activestyle="none")
        sby5 = ttk.Scrollbar(body5, command=self.lst_mon.yview)
        self.lst_mon.configure(yscrollcommand=sby5.set)
        sby5.pack(side="right", fill="y")
        self.lst_mon.pack(side="left", fill="x", expand=True)

        # 控制
        g3 = ttk.Frame(frm)
        g3.pack(fill="x", **pad)
        self.var_dry = tk.BooleanVar(value=False)
        self.cb_dry = ttk.Checkbutton(g3, text="模拟运行(只检测不发送/不关机)", variable=self.var_dry)
        self.cb_dry.pack(side="left")
        self.btn_start = ttk.Button(g3, text="▶ 启动监控", command=self.start)
        self.btn_start.pack(side="left", padx=10)
        self.btn_stop = ttk.Button(g3, text="■ 停止监控", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")
        self.lbl_state = ttk.Label(g3, text="已停止", foreground="#999")
        self.lbl_state.pack(side="right")

        # 日志
        g4 = ttk.LabelFrame(frm, text="运行日志")
        g4.pack(fill="x", **pad)   # 日志保持固定行数，不再空占一大片高度
        top4 = ttk.Frame(g4)
        top4.pack(fill="x", padx=6, pady=(4, 0))
        self.var_wrap = tk.BooleanVar(value=False)
        ttk.Checkbutton(top4, text="自动换行", variable=self.var_wrap,
                        command=self._toggle_wrap).pack(side="left")
        body4 = ttk.Frame(g4)
        body4.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt = tk.Text(body4, height=12, wrap="none", font=("Microsoft YaHei UI", 9))
        sby = ttk.Scrollbar(body4, command=self.txt.yview)
        sbx = ttk.Scrollbar(body4, orient="horizontal", command=self.txt.xview)
        self.txt.configure(yscrollcommand=sby.set, xscrollcommand=sbx.set)
        sby.pack(side="right", fill="y")
        sbx.pack(side="bottom", fill="x")
        self.txt.pack(side="left", fill="both", expand=True)
        self._lines = 0

        # ---- 两列布局 ----
        # 控件已在上面直接建进两列（事后搬不动），这里只做状态与切换绑定。
        self._wide = None
        self._set_layout(True)     # 先按两列建好：_fit_window 量到的就是宽屏所需宽度
        self.root.bind("<Configure>", self._apply_layout)

        # 运行中禁用的控件
        self.inputs = []
        for w in (r1, r2, r3, r4):
            for c in w.winfo_children():
                if isinstance(c, ttk.Entry):
                    self.inputs.append(c)
        self.inputs += [self.cb503, self.cb_all, self.cb_all_done,
                        self.cb400, self.cb_notify, self.cb_dry,
                        self.cb_all_sources,
                        self.cb_all503, self.ent_all503_win,
                        self.cb_ctx_on, self.cb_ctx_mode,
                        self.cb_active_all, self.ent_active_min]
        self.sel_ctrls = [self.btn_refresh, self.btn_expand, self.btn_collapse,
                          self.btn_pick_running, self.tree, self.btn_rm,
                          self.btn_clear, self.lst_mon,
                          self.cb_codex, self.cb_pi]

    def _toggle_active_all(self):
        """勾选「监控所有活跃会话」后，会话列表的逐个挑选就不再必要。"""
        on = self.var_active_all.get()
        state = "normal" if on else "disabled"
        try:
            self.ent_active_min.configure(state=state)
        except Exception:
            pass
    # ---------- 上下文续接 ----------
    def _toggle_ctx(self):
        """勾选状态决定参数框可用性，并给出当前模式的说明。"""
        on = self.var_ctx_on.get()
        state = "readonly" if on else "disabled"
        try:
            self.cb_ctx_mode.configure(state=state)
            self.cb_400_new.configure(state="normal" if on else "disabled")
        except Exception:
            pass
        note400 = ("\n　另外：\n"
                   "　· 400（按 Pi 日志里的真实 HTTP 状态码判定）：真报时直接新建会话，"
                   "不受连续次数限制。\n"
                   "　· 503（服务商不可用）：两种方式都不续接、不新建会话，"
                   "只按普通报错重试。" if on else "")
        if not on:
            self.lbl_ctx_hint.config(
                text="已关闭：会话报错（含 Pi 的 400）时只按普通中止逻辑重试「继续」。")
        elif self.var_ctx_mode.get() == "new":
            self.lbl_ctx_hint.config(
                text="new：直接在同项目目录下新建会话，首条消息带原会话深链"
                     "（Pi 会自动附上原会话最近消息摘录）。" + note400)
        else:
            self.lbl_ctx_hint.config(
                text="compact：先压缩该会话上下文再发「继续」；压缩失败或压缩后仍超限，"
                     "才自动退化为新建会话。更省 token，但压缩本身要跑一轮模型。"
                     + note400)

    # ---------- 来源与通道 ----------
    def _check_channels(self):
        """检测两个来源的数据与发送通道，把结果写在界面上。"""
        if DB_THREADS.exists() and DB_TURNS.exists():
            self.lbl_codex.config(text=f"✓ 数据库就绪（{CODEX_HOME}）", foreground="#0a0")
        else:
            self.lbl_codex.config(text=f"✗ 找不到数据库：{CODEX_HOME}", foreground="#c00")
        ok, detail = pi_plugin_status()
        self.lbl_pi.config(text=("✓ " if ok else "✗ ") + detail, foreground="#0a0" if ok else "#c00")
        return ok, detail

    # ---------- 会话列表 ----------
    def _fmt_ts(self, ms):
        try:
            return datetime.datetime.fromtimestamp((ms or 0) / 1000).strftime("%m-%d %H:%M")
        except Exception:
            return "?"

    # ---- Codex ----
    def _codex_thread(self, con, tid):
        return con.execute(
            "select id, title, cwd, updated_at_ms, recency_at_ms, coalesce(name,'') from threads where id=?",
            (tid,),
        ).fetchone()

    def _codex_root(self, con, tid):
        """沿标题中的 codex://threads/<父id> 引用向前追溯，返回会话链根。"""
        cur, last = tid, tid
        seen = set()
        while cur not in seen:
            seen.add(cur)
            row = self._codex_thread(con, cur)
            if row is None:
                return last
            last = cur
            m = RELAY_RE.search(row[1] or "")
            if not m:
                return cur
            cur = m.group(1)
        return last

    def _codex_leaf(self, con, root):
        """沿标题引用向前收集整条链，返回其中未归档且最近活跃的成员。"""
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
        for m in reversed(members):
            r = con.execute("select archived, recency_at_ms from threads where id=?", (m,)).fetchone()
            if r is None or r[0] != 0:
                continue
            if best is None or (r[1] or 0) > best[1]:
                best = (m, r[1] or 0)
        return best[0] if best else members[-1]

    def _codex_status(self, tid):
        """会话最后一条 turn 的状态。

        Codex 被打断时会把 status 永久留在 inProgress（completed_at 为 NULL），
        几天前的会话也会一直显示成「进行中」。所以 inProgress 要先看有没有近期
        活动，没有就按「已停止(残留)」显示 —— 判定见 codex_activity.py。
        """
        if not DB_TURNS.exists():
            return ""
        try:
            con = ro_connect(DB_TURNS)
            try:
                r = con.execute(
                    "select status, started_at from thread_turns where thread_id=? "
                    "order by rollout_ordinal desc limit 1", (tid,)
                ).fetchone()
            finally:
                con.close()
        except Exception:
            return ""
        if not r:
            return ""
        status, started = r[0], r[1]
        if status == "inProgress" and codex_activity.is_stale_in_progress(tid, started):
            return STALE_INPROGRESS
        return status

    def _list_codex(self):
        out = []
        if not (DB_THREADS.exists() and DB_TURNS.exists()):
            return out
        try:
            con = ro_connect(DB_THREADS)
            try:
                cands = con.execute(
                    "select id, recency_at_ms from threads where archived=0 "
                    "and cwd is not null and trim(cwd) != '' "
                    "order by recency_at_ms desc limit 60"
                ).fetchall()
                convs = {}
                for tid, rec in cands:
                    if not tid:
                        continue
                    root = self._codex_root(con, tid)
                    if root not in convs or (rec or 0) > convs[root][1]:
                        convs[root] = (tid, rec or 0)
                for root, (tid, rec) in convs.items():
                    leaf = self._codex_leaf(con, root)
                    lrow = self._codex_thread(con, leaf)
                    if lrow is None:
                        continue
                    rrow = self._codex_thread(con, root)
                    leaf_name = (lrow[5] or "").strip()
                    conv = leaf_name if (leaf_name and "codex://threads" not in leaf_name) \
                        else clean_title(rrow[1] if rrow else None)
                    proj = project_name(lrow[2])
                    out.append({
                        "source": SRC_CODEX,
                        "key": root,
                        "leaf": leaf,
                        "project": proj,
                        "name": f"{proj} / {conv}" if proj and conv else (proj or conv or root[:8]),
                        "upd_ms": lrow[3],
                        "recency": rec or 0,
                        "status": self._codex_status(leaf),
                    })
            finally:
                con.close()
        except Exception:
            pass
        return out

    # ---- Pi ----
    def _list_pi(self):
        out = []
        if not PI_DB.exists():
            return out
        try:
            con = ro_connect(PI_DB)
            try:
                rows = con.execute(
                    """
                    select s.id, coalesce(s.title,''), p.path, s.updated_at,
                           (select t.status from turns t where t.session_id = s.id
                             order by t.started_at desc limit 1) as st,
                           (select t.error_code from turns t where t.session_id = s.id
                             order by t.started_at desc limit 1) as ec
                    from sessions s
                    left join projects p on p.id = s.project_id
                    where s.deleted_at is null
                    order by s.updated_at desc
                    limit 60
                    """
                ).fetchall()
            finally:
                con.close()
            # 已归档的会话不进列表（「已删除」由上面的 deleted_at 过滤掉）
            arch, arch_note = pi_archive.archived_ids()
            self._pi_arch = (arch, arch_note)
            self._pi_arch_hidden = 0
            for sid, title, path, upd, st, ec in rows:
                if sid in arch:
                    self._pi_arch_hidden += 1
                    continue
                proj = project_name(path)
                conv = " ".join((title or "").split())[:40]
                out.append({
                    "source": SRC_PI,
                    "key": sid,
                    "leaf": sid,
                    "project": proj,
                    "name": f"{proj} / {conv}" if proj and conv else (proj or conv or sid[:8]),
                    "upd_ms": upd,
                    "recency": upd or 0,
                    "status": st or "",
                    "error": ec,
                })
        except Exception:
            pass
        return out

    def _archive_note(self):
        """列表尾部的补充说明：Pi 侧「已归档」会话的屏蔽情况。

        归档状态只能从 PI-Desktop 的 Local Storage 里读（见 pi_archive.py）；
        读不到时明说「未屏蔽」，避免让人以为已经屏蔽了。
        """
        arch, note = getattr(self, "_pi_arch", (None, ""))
        if arch is None:
            return ""          # 没查过 Pi 列表（Pi 来源关着），不加说明
        if not arch:
            return f"｜{note}"
        return f"｜已屏蔽归档 {getattr(self, '_pi_arch_hidden', 0)} 个"
    def _refresh_sessions(self):
        self._sessions = []
        if self.var_use_codex.get():
            self._sessions += self._list_codex()
        if self.var_use_pi.get():
            self._sessions += self._list_pi()

        self.tree.delete(*self.tree.get_children())
        # 按「来源 + 项目」分组，组内按最后活动倒序
        groups = {}
        for s in self._sessions:
            groups.setdefault((s["source"], s["project"]), []).append(s)
        ordered = sorted(groups.items(),
                         key=lambda kv: max((x["upd_ms"] or 0) for x in kv[1]),
                         reverse=True)
        self._monitored = [m for m in self._monitored
                           if any(s["source"] == m["source"] and s["key"] == m["key"]
                                  for s in self._sessions)]
        mon_ids = {(m["source"], m["key"]) for m in self._monitored}
        for gi, ((src, proj), items) in enumerate(ordered):
            items.sort(key=lambda x: x["upd_ms"] or 0, reverse=True)
            # 默认展开最近活跃的前 3 个项目，其余收起
            pid = self.tree.insert("", "end",
                                   text=f"[{SRC_LABEL[src]}] {proj}（{len(items)}）",
                                   values=("", ""), open=gi < 3)
            for s in items:
                mark = "✓ " if (s["source"], s["key"]) in mon_ids else ""
                st = STATUS_CN.get(s["status"], s["status"] or "未知")
                # 名字、时间、状态各占一列：长名字只影响名字列，不会把整行挤掉
                self.tree.insert(pid, "end", iid=f"{s['source']}:{s['key']}",
                                 text=f"{mark}{s['name']}",
                                 values=(self._fmt_ts(s["upd_ms"]), st))
        n_codex = sum(1 for s in self._sessions if s["source"] == SRC_CODEX)
        n_pi = sum(1 for s in self._sessions if s["source"] == SRC_PI)
        self.lbl_sess.config(
            text=f"共 {len(ordered)} 个项目｜Codex {n_codex} 个 / Pi {n_pi} 个会话"
                 + self._archive_note())
        self._refresh_monitored()
        if not self._sessions:
            self.lbl_sess.config(text="未找到会话：请确认对应应用已运行，点“刷新列表”重试")
        self._check_channels()
        self._fit_tree_height()   # 行数变了，重算一次能放几整行

    def _expand_all(self, on):
        for iid in self.tree.get_children():
            self.tree.item(iid, open=on)

    def _on_tree_click(self, event):
        iid = self.tree.identify("item", event.x, event.y)
        if not iid or ":" not in iid or iid.startswith("I"):
            return
        src, _, key = iid.partition(":")
        if src not in (SRC_CODEX, SRC_PI):
            return
        self._toggle_monitored(src, key)

    def _toggle_monitored(self, src, key):
        idx = next((i for i, m in enumerate(self._monitored)
                    if m["source"] == src and m["key"] == key), None)
        if idx is not None:
            del self._monitored[idx]
        else:
            for s in self._sessions:
                if s["source"] == src and s["key"] == key:
                    self._monitored.append(dict(s))
                    break
        self._sync_marks()
        self._refresh_monitored()

    def _sync_marks(self):
        want = {(m["source"], m["key"]) for m in self._monitored}
        for iid in self.tree.get_children(""):
            for cid in self.tree.get_children(iid):
                src, _, key = cid.partition(":")
                if src not in (SRC_CODEX, SRC_PI):
                    continue
                text = self.tree.item(cid, "text")
                marked = text.startswith("✓ ")
                should = (src, key) in want
                if should and not marked:
                    self.tree.item(cid, text="✓ " + text)
                elif not should and marked:
                    self.tree.item(cid, text=text[2:])

    def _refresh_monitored(self):
        self.lst_mon.delete(0, "end")
        for m in self._monitored:
            st = STATUS_CN.get(m["status"], m["status"] or "未知")
            self.lst_mon.insert("end", f"[{SRC_LABEL[m['source']]}] {m['name']}｜{st}")
        if not self._monitored:
            self.lst_mon.insert("end", "（未选择——两个来源各按“监控会话数”自动选）")
        self.lbl_mon.config(text=f"已选 {len(self._monitored)} 个")

    def _remove_selected(self):
        sel = [i for i in self.lst_mon.curselection() if i < len(self._monitored)]
        if not sel:
            return
        self._monitored = [m for i, m in enumerate(self._monitored) if i not in sel]
        self._sync_marks()
        self._refresh_monitored()

    def _clear_monitored(self):
        self._monitored = []
        self._sync_marks()
        self._refresh_monitored()

    def _select_running(self):
        """把列表里所有「进行中」的会话一次性加入监控，省去逐个点击。"""
        have = {(m["source"], m["key"]) for m in self._monitored}
        added = 0
        for s in self._sessions:
            if s["status"] not in RUNNING_STATUSES:
                continue
            if (s["source"], s["key"]) in have:
                continue
            self._monitored.append(dict(s))
            have.add((s["source"], s["key"]))
            added += 1
        self._sync_marks()
        self._refresh_monitored()
        if added:
            self._log(f">>> 已选中 {added} 个进行中的会话"
                      f"（待监控共 {len(self._monitored)} 个）")
            # Pi 勾了「监控所有活跃会话」时按时间窗口纳入，这份逐个选择对 Pi 不生效；
            # 不说一声的话，用户会以为按钮没反应。
            if self.var_active_all.get() and self.var_use_pi.get():
                self._log(">>> 注意：Pi 勾了「监控所有活跃会话」，Pi 侧按时间窗口自动纳入，"
                          "不看这份逐个选择（Codex 仍按选择）")
        elif not self._sessions:
            self._log(">>> 会话列表是空的：先点「刷新列表」再选")
        else:
            self._log(">>> 列表里没有「进行中」的会话（其余都已结束或失败）")

    # ---------- 日志 ----------
    def _toggle_wrap(self):
        self.txt.configure(wrap="word" if self.var_wrap.get() else "none")

    def _log(self, line):
        self.txt.insert("end", line + "\n")
        self._lines += 1
        if self._lines > MAX_LINES:
            self.txt.delete("1.0", f"{self._lines - MAX_LINES + 1}.0")
            self._lines = MAX_LINES
        self.txt.see("end")

    # ---------- 参数 ----------
    def _num(self, var, name, lo=None):
        try:
            v = float(var.get().strip())
        except ValueError:
            raise ValueError(f"{name} 必须是数字")
        if lo is not None and v < lo:
            raise ValueError(f"{name} 不能小于 {lo}")
        return v

    def _common_args(self):
        """两个来源共用的参数。"""
        args = [
            "--poll", str(self._num(self.var_poll, "轮询间隔", 1)),
            "--message", self.var_msg.get().strip() or "继续",
            "--backoff-minutes", self.var_backoff.get().strip() or "1,1,3,5,5",
            "--jitter", str(self._num(self.var_jitter, "抖动", 0)),
            "--max-per-hour", str(int(self._num(self.var_maxph, "每小时上限", 0))),
            "--retry-minutes", str(self._num(self.var_retry, "重试", 1)),
            "--shutdown-threshold", str(int(self._num(self.var_503n, "错误码次数", 1))),
            "--all-stopped-minutes",
            str(self._num(self.var_allmin, "全部停止分钟", 0)) if self.var_use_all.get() else "0",
            "--shutdown-cmd", self.var_shut.get().strip() or DEFAULT_SHUTDOWN_CMD,
        ]
        if self.var_dry.get():
            args.append("--dry-run")
        return args

    def _args_for(self, src, items):
        """按来源拼出子进程命令行；items 为空表示自动模式。"""
        monitor = MONITOR_CODEX if src == SRC_CODEX else MONITOR_PI
        args = [monitor] if FROZEN else [sys.executable, monitor]
        # 监控范围：勾了「所有活跃会话」就不传 --thread/--auto，改用时间窗口
        use_active_all = (src == SRC_PI and self.var_active_all.get())
        if use_active_all:
            args += ["--active-within",
                     str(int(self._num(self.var_active_min, "活跃窗口分钟", 1)))]
        elif items:
            for m in items:
                args += ["--thread", m["key"]]
        else:
            args += ["--auto", str(int(self._num(self.var_auto, "监控会话数", 1)))]
        args += self._common_args()
        if src == SRC_CODEX:
            # Codex 的关机错误码是 HTTP 码；并带上 400 续接规则
            args += ["--shutdown-codes", "405,503" if self.var_use503.get() else ""]
            args += ["--bad400-threshold",
                     str(int(self._num(self.var_400n, "400 次数", 1))) if self.var_use400.get() else "0"]
        else:
            # Pi 的错误码是 turns.error_code（如 STREAM_FAILED / PROVIDER_ERROR）
            args += ["--statuses", "aborted,error"]
            args += ["--shutdown-codes",
                     "STREAM_FAILED,PROVIDER_ERROR" if self.var_use503.get() else ""]
            # Pi 独有：读 notifications 表，完成/失败/等待确认时弹系统通知
            if self.var_notify.get():
                args.append("--notify")
            # Pi 独有：上下文超限续接（Codex 对应的是 --bad400-threshold）
            if self.var_ctx_on.get():
                args += ["--context-continue", self.var_ctx_mode.get() or "compact"]
                args += ["--context-threshold",
                         str(int(self._num(self.var_ctx_n, "上下文超限次数", 1)))]
            else:
                args += ["--context-continue", "off"]
            # Pi 独有：真报 400 的会话直接新建会话（不等次数、不压缩）
            args += ["--open-new-on-400",
                     "on" if self.var_400_new.get() else "off"]
        # —— 全部正常完成后关机：两个来源共用（以前只在 Pi 分支，
        #    导致只勾 Codex 时这个复选框静默无效）——
        if self.var_all_done.get():
            args.append("--shutdown-when-all-done")
            args += ["--all-done-confirm-seconds",
                     str(int(self._num(self.var_done_secs, "全部完成确认秒数", 10)))]
        else:
            args += ["--shutdown-when-all-done", "off"]
        # 跨来源协调：告诉子进程本次一共监控了哪些来源。
        # 不传的话，每个进程都以为只有自己，会各自完成就关机。
        args += ["--all-sources", ",".join(self._enabled_sources())]
        if self.var_all_done.get() and self.var_all_sources.get():
            args += ["--shutdown-requires-all-sources", "on"]
        else:
            args += ["--shutdown-requires-all-sources", "off"]
        # 近期活跃会话全部 503 → 关机：两个来源都支持
        args += ["--shutdown-when-all-503", "on" if self.var_all503.get() else "off"]
        args += ["--all-503-window-minutes",
                 str(int(self._num(self.var_all503_win, "活跃窗口分钟", 1)))]
        # 日志文件显式指定：不传的话 exe 版会落到 PyInstaller 的临时目录，
        # 进程退出就被删掉 —— 事后完全查不到监控脚本干过什么。
        args += ["--log", os.path.join(HERE, f"{src}_monitor.log")]
        return args

    def _enabled_sources(self):
        """本次勾选了哪些来源（用于跨来源关机协调）。"""
        out = []
        if self.var_use_codex.get():
            out.append(SRC_CODEX)
        if self.var_use_pi.get():
            out.append(SRC_PI)
        return out or [SRC_PI]

    # ---------- 启停 ----------
    def start(self):
        try:
            self._common_args()   # 先校验数字参数
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return

        by_src = {SRC_CODEX: [], SRC_PI: []}
        for m in self._monitored:
            by_src[m["source"]].append(m)

        enabled = []
        if self.var_use_codex.get():
            enabled.append(SRC_CODEX)
        if self.var_use_pi.get():
            enabled.append(SRC_PI)
        if not enabled:
            messagebox.showerror("未选来源", "请至少勾选一个监控来源（Codex / Pi）。")
            return

        # Pi 需要插件执行器；没装就明确提示，而不是启动了却什么都不发
        if SRC_PI in enabled and not self.var_dry.get():
            ok, detail = pi_plugin_status()
            if not ok:
                messagebox.showerror(
                    "Pi 发送通道不可用",
                    f"{detail}\n\n"
                    "请先安装并启用 Pi 插件 local.session-keeper（见 使用说明.txt 的【Pi 插件】一节），\n"
                    "或取消勾选 Pi 来源、或勾选“模拟运行”只做检测。")
                return

        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        started = []
        for src in enabled:
            args = self._args_for(src, by_src[src])
            try:
                proc = subprocess.Popen(
                    args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW, env=env, bufsize=1)
            except Exception as e:
                self._log(f"[{SRC_LABEL[src]}] >>> 启动失败：{e}")
                continue
            self.procs[src] = proc
            started.append(src)
            scope = (f"指定 {len(by_src[src])} 个会话" if by_src[src] else "自动模式")
            self._log(f"[{SRC_LABEL[src]}] >>> 监控已启动（{scope}）：" + " ".join(args))
            threading.Thread(target=self._reader, args=(src, proc), daemon=True).start()

        if not started:
            messagebox.showerror("启动失败", "没有任何监控进程启动成功，请查看日志。")
            return
        self._set_running(True)

    def stop(self):
        if not self.procs:
            return
        self._log(">>> 正在停止监控…")
        for src, p in list(self.procs.items()):
            try:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
            except Exception:
                pass
            self._log(f"[{SRC_LABEL[src]}] >>> 已停止")
        self.procs = {}
        self._set_running(False)
        self._log(">>> 监控已全部停止")

    def _set_running(self, running):
        self.btn_start.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        self.lbl_state.config(text="运行中" if running else "已停止",
                              foreground="#0a0" if running else "#999")
        for w in self.inputs:
            try:
                if isinstance(w, ttk.Entry):
                    w.configure(state="disabled" if running else "normal")
                else:
                    w.state(["disabled"] if running else ["!disabled"])
            except Exception:
                pass
        for w in self.sel_ctrls:
            try:
                w.config(state="disabled" if running else "normal")
            except Exception:
                pass

    def _reader(self, src, proc):
        try:
            for line in proc.stdout:
                self.q.put((src, line.rstrip("\r\n")))
        except Exception:
            pass

    def _drain(self):
        try:
            while True:
                src, line = self.q.get_nowait()
                self._log(f"[{SRC_LABEL[src]}] {line}")
        except queue.Empty:
            pass
        # 子进程退出检测（两个来源独立）
        for src, p in list(self.procs.items()):
            if p.poll() is not None:
                rc = p.returncode
                del self.procs[src]
                self._log(f"[{SRC_LABEL[src]}] >>> 监控已退出（返回码 {rc}）")
        if not self.procs and self.btn_stop["state"] == "normal":
            self._set_running(False)
        self.root.after(100, self._drain)

    def _on_close(self):
        if self.procs:
            if not messagebox.askyesno("退出", "监控仍在运行，退出前先停止监控？"):
                return
            self.stop()
        self.root.destroy()


def single_instance_guard():
    """同一时间只允许开一个控制台。

    为什么需要：两个控制台会各自启动一套监控脚本，同一个会话会被发两次「继续」，
    关机判定也会互相干扰。这里用命名互斥体判断（进程退出自动释放，不留死锁文件）。
    返回 (句柄, 是否是第一个)。拿不到互斥体时不拦人（宁可重复，也不要把人挡在外面）。
    """
    if os.name != "nt":
        return None, True
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(
            None, False, "Local\\pi-session-keeper-console")
        if not handle:
            return None, True
        already = ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS
        return handle, not already
    except Exception:
        return None, True


def focus_existing_console():
    """把已经在跑的那个控制台窗口恢复到前台（重复启动时用）。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.FindWindowW(None, CONSOLE_TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def main():
    enable_dpi_awareness()   # 必须在 Tk() 之前：高 DPI 下按真实像素渲染
    handle, first = single_instance_guard()
    if not first:
        # 已经在跑了：把那个窗口拉到前台，不再开第二个（否则会出现两套监控）
        focus_existing_console()
        return
    root = tk.Tk()
    App(root)
    root.mainloop()
    # handle 不显式关闭：进程退出时系统自动释放互斥体


if __name__ == "__main__":
    main()
