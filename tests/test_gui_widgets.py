# -*- coding: utf-8 -*-
"""验证 GUI 新控件的真实联动行为。

前面只检查了「源码里有这些字符串」，那证明不了控件真能联动。
这里真的实例化窗口（隐藏），点开关、改下拉框，看提示文字与
参数拼装是否随之变化 —— 这才是用户实际会遇到的路径。
"""
import os
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


try:
    import codex_monitor_gui as gui
except Exception as e:
    print(f"导入 GUI 失败：{e!r}")
    sys.exit(2)

print("=" * 70)
print("实例化窗口（隐藏，不弹到前台）")
root = tk.Tk()
root.withdraw()
try:
    app = gui.App(root)
except Exception as e:
    print(f"创建界面失败：{e!r}")
    sys.exit(2)
print("  ✓ 界面创建成功")

print()
print("=" * 70)
print("1. 默认状态：开关开启、模式 compact、提示说明 compact")
check("开关默认开启", app.var_ctx_on.get() is True)
check("模式默认 compact", app.var_ctx_mode.get() == "compact")
hint0 = app.lbl_ctx_hint.cget("text")
check("提示说明了 compact 行为", "compact" in hint0, hint0[:60])
check("下拉框可用", str(app.cb_ctx_mode.cget("state")) == "readonly")

print()
print("=" * 70)
print("2. 切到 new：提示应跟着变")
app.var_ctx_mode.set("new")
app._toggle_ctx()
hint1 = app.lbl_ctx_hint.cget("text")
check("提示变为 new 的说明", "new" in hint1, hint1[:60])
check("提示不再是 compact 文案", hint1 != hint0)

print()
print("=" * 70)
print("3. 关掉开关：下拉框禁用，提示说明已关闭")
app.var_ctx_on.set(False)
app._toggle_ctx()
check("下拉框已禁用", str(app.cb_ctx_mode.cget("state")) == "disabled")
hint2 = app.lbl_ctx_hint.cget("text")
check("提示说明已关闭", "关闭" in hint2, hint2[:60])

print()
print("=" * 70)
print("4. 重新打开：恢复可用")
app.var_ctx_on.set(True)
app.var_ctx_mode.set("compact")
app._toggle_ctx()
check("下拉框恢复可用", str(app.cb_ctx_mode.cget("state")) == "readonly")
check("提示恢复 compact 说明", "compact" in app.lbl_ctx_hint.cget("text"))

print()
print("=" * 70)
print("5. 参数拼装：Pi 分支应带上续接参数")
args_pi = app._args_for(gui.SRC_PI, [])
check("含 --context-continue", "--context-continue" in args_pi)
check("值为 compact", args_pi[args_pi.index("--context-continue") + 1] == "compact")
check("含 --context-threshold", "--context-threshold" in args_pi)
check("阈值为 3", args_pi[args_pi.index("--context-threshold") + 1] == "3")

print()
print("=" * 70)
print("6. 切到 new 后，拼装出的参数应变成 new")
app.var_ctx_mode.set("new")
app._toggle_ctx()
args_new = app._args_for(gui.SRC_PI, [])
check("值变为 new", args_new[args_new.index("--context-continue") + 1] == "new")

print()
print("=" * 70)
print("7. 关掉开关后，应显式传 off（不能靠省略）")
app.var_ctx_on.set(False)
app._toggle_ctx()
args_off = app._args_for(gui.SRC_PI, [])
check("传了 --context-continue", "--context-continue" in args_off)
check("值为 off", args_off[args_off.index("--context-continue") + 1] == "off")
check("关闭时不再传 --context-threshold",
      "--context-threshold" not in args_off)

print()
print("=" * 70)
print("7b. 「真报 400 直接新建会话」：默认跟随插件，且显式传参")
check("默认勾选（插件没这一项时按开）", app.var_400_new.get() is True,
      str(app.var_400_new.get()))
args_400 = app._args_for(gui.SRC_PI, [])
check("含 --open-new-on-400", "--open-new-on-400" in args_400)
check("值为 on", args_400[args_400.index("--open-new-on-400") + 1] == "on")
app.var_400_new.set(False)
args_400off = app._args_for(gui.SRC_PI, [])
check("关掉后显式传 off",
      args_400off[args_400off.index("--open-new-on-400") + 1] == "off")
app.var_400_new.set(True)

print()
print("=" * 70)
print("8. Codex 分支不得带上 Pi 专有的续接参数")
app.var_ctx_on.set(True)
app._toggle_ctx()
args_cx = app._args_for(gui.SRC_CODEX, [])
check("Codex 不含 --context-continue", "--context-continue" not in args_cx)
check("Codex 不含 --context-threshold", "--context-threshold" not in args_cx)
check("Codex 仍含 --bad400-threshold", "--bad400-threshold" in args_cx)
check("Codex 不含 --open-new-on-400", "--open-new-on-400" not in args_cx)

print()
print("=" * 70)
print("9. 阈值输入框的校验（填非法值应报错而不是静默用 0）")
app.var_ctx_n.set("abc")
try:
    app._args_for(gui.SRC_PI, [])
    check("非法阈值应报错", False, "没有报错")
except ValueError as e:
    check("非法阈值报错", True, str(e))
app.var_ctx_n.set("3")

print()
print("=" * 70)
print("10. 关机：Codex 也必须有「全部完成」参数（修复勾了没反应）")
app.var_all_done.set(True)
app.var_done_secs.set("90")
app.var_use_codex.set(True)
app.var_use_pi.set(True)
app.var_all_sources.set(True)
cx2 = app._args_for(gui.SRC_CODEX, [])
pi2 = app._args_for(gui.SRC_PI, [])
check("Codex 含 --shutdown-when-all-done",
      "--shutdown-when-all-done" in cx2)
check("Pi 含 --shutdown-when-all-done",
      "--shutdown-when-all-done" in pi2)
check("Codex 带上了观察秒数",
      cx2[cx2.index("--all-done-confirm-seconds") + 1] == "90",
      str(cx2[cx2.index("--all-done-confirm-seconds") + 1]))
check("Pi 也带上了观察秒数",
      pi2[pi2.index("--all-done-confirm-seconds") + 1] == "90")

print()
print("=" * 70)
print("11. 跨来源协调参数：两个来源都要被告知本次监控了谁")
check("Codex 传 --all-sources", "--all-sources" in cx2)
check("Pi 传 --all-sources", "--all-sources" in pi2)
srcs_cx = cx2[cx2.index("--all-sources") + 1]
check("来源列表含 codex 和 pi",
      set(srcs_cx.split(",")) == {"codex", "pi"}, srcs_cx)
check("Codex 传 --shutdown-requires-all-sources",
      "--shutdown-requires-all-sources" in cx2)
check("跨来源要求为 on",
      cx2[cx2.index("--shutdown-requires-all-sources") + 1] == "on")

print()
print("=" * 70)
print("12. 关掉「全部完成」时应显式传 off（不能靠省略）")
app.var_all_done.set(False)
cx3 = app._args_for(gui.SRC_CODEX, [])
pi3 = app._args_for(gui.SRC_PI, [])
check("Codex 传 off", cx3[cx3.index("--shutdown-when-all-done") + 1] == "off")
check("Pi 传 off", pi3[pi3.index("--shutdown-when-all-done") + 1] == "off")
check("关闭时不传观察秒数", "--all-done-confirm-seconds" not in cx3)

print()
print("=" * 70)
print("13. 只勾一个来源时，来源列表要如实反映（不能骗子进程）")
app.var_all_done.set(True)
app.var_use_codex.set(False)
app.var_use_pi.set(True)
pi4 = app._args_for(gui.SRC_PI, [])
check("来源列表只有 pi",
      pi4[pi4.index("--all-sources") + 1] == "pi",
      pi4[pi4.index("--all-sources") + 1])
app.var_use_codex.set(True)
app.var_use_pi.set(False)
cx4 = app._args_for(gui.SRC_CODEX, [])
check("来源列表只有 codex",
      cx4[cx4.index("--all-sources") + 1] == "codex",
      cx4[cx4.index("--all-sources") + 1])

print()
print("=" * 70)
print("14. 不勾跨来源时，应传 off（各自独立判定）")
app.var_use_codex.set(True)
app.var_use_pi.set(True)
app.var_all_sources.set(False)
cx5 = app._args_for(gui.SRC_CODEX, [])
check("跨来源要求为 off",
      cx5[cx5.index("--shutdown-requires-all-sources") + 1] == "off")
app.var_all_sources.set(True)

print()
print("=" * 70)
print("15. GUI 初始值应跟随插件设置（避免两个入口静默打架）")
import json as _json
import tempfile as _tempfile
from pathlib import Path as _Path

_hb = _Path(_tempfile.mkdtemp()) / "plugin.json"
_hb.write_text(_json.dumps({
    "ok": True, "version": "1.5.0", "at": int(time.time() * 1000),
    "enabled": True,
    "shutdownWhenAllDone": True,
    "allDoneConfirmSeconds": 77,
    "shutdownRequiresAllSources": False,
    "contextContinue": False,
    "contextContinueThreshold": 9,
    "contextContinueMode": "new",
    "openNewOn400": False,
}), encoding="utf-8")
_orig = gui.PI_HEARTBEAT
gui.PI_HEARTBEAT = _hb
_ps = gui.plugin_shutdown_settings()
check("读到插件里的关机开关", _ps.get("shutdownWhenAllDone") is True)
check("读到观察秒数", _ps.get("allDoneConfirmSeconds") == 77)
check("读到跨来源开关", _ps.get("shutdownRequiresAllSources") is False)
check("读到续接开关", _ps.get("contextContinue") is False)
check("读到续接次数", _ps.get("contextContinueThreshold") == 9)
check("读到续接模式", _ps.get("contextContinueMode") == "new")
check("读到 400 直开开关", _ps.get("openNewOn400") is False)

# 新开一个窗口：续接控件的初始值必须跟随插件面板，而不是写死的默认值
try:
    app2 = gui.App(root)
except Exception as e:
    app2 = None
    check("新窗口能实例化", False, repr(e))
if app2 is not None:
    check("新窗口续接开关跟随插件（关）", app2.var_ctx_on.get() is False)
    check("新窗口续接次数跟随插件", app2.var_ctx_n.get() == "9",
          app2.var_ctx_n.get())
    check("新窗口续接模式跟随插件", app2.var_ctx_mode.get() == "new",
          app2.var_ctx_mode.get())
    check("新窗口 400 直开跟随插件（关）", app2.var_400_new.get() is False)

# 心跳过期时应视为不可信
_hb.write_text(_json.dumps({"at": 0, "shutdownWhenAllDone": True}), encoding="utf-8")
check("心跳过期时返回空（不采信）", gui.plugin_shutdown_settings() == {})
gui.PI_HEARTBEAT = _orig

print()
print("=" * 70)
print("16. 监控范围：勾「所有活跃会话」时应传 --active-within")
app.var_active_all.set(True)
app.var_active_min.set("60")
args_all = app._args_for(gui.SRC_PI, [{"key": "x", "source": gui.SRC_PI}])
check("传了 --active-within", "--active-within" in args_all)
check("窗口值为 60",
      args_all[args_all.index("--active-within") + 1] == "60",
      str(args_all[args_all.index("--active-within") + 1]))
check("勾选后不再传 --thread（避免与窗口模式冲突）",
      "--thread" not in args_all)
check("勾选后不再传 --auto", "--auto" not in args_all)

print()
print("=" * 70)
print("17. 不勾时应退回原有的逐个挑选模式")
app.var_active_all.set(False)
args_sel = app._args_for(gui.SRC_PI, [{"key": "sess-1", "source": gui.SRC_PI}])
check("不传 --active-within", "--active-within" not in args_sel)
check("改为传 --thread", "--thread" in args_sel)
check("--thread 值是选中会话",
      args_sel[args_sel.index("--thread") + 1] == "sess-1")

print()
print("=" * 70)
print("18. Codex 不支持活跃窗口模式（不该传给它）")
app.var_active_all.set(True)
args_cx_all = app._args_for(gui.SRC_CODEX, [])
check("Codex 不传 --active-within", "--active-within" not in args_cx_all)
app.var_active_all.set(False)

print()
print("=" * 70)
print("19. 必须显式传 --log（否则 exe 版日志落到临时目录，退出即删）")
for _src, _name in [(gui.SRC_CODEX, "codex_monitor.log"),
                    (gui.SRC_PI, "pi_monitor.log")]:
    _a = app._args_for(_src, [])
    check(f"{_src} 传了 --log", "--log" in _a)
    if "--log" in _a:
        _p = _a[_a.index("--log") + 1]
        check(f"{_src} 日志文件名正确", os.path.basename(_p) == _name, _p)
        check(f"{_src} 日志落在程序目录",
              os.path.dirname(os.path.abspath(_p)) == os.path.abspath(gui.HERE), _p)

print()
print("=" * 70)
print("20. 「全 503 关机」开关必须真的传下去")
app.var_all503.set(True)
app.var_all503_win.set("45")
_a503 = app._args_for(gui.SRC_PI, [])
check("传了 --shutdown-when-all-503", "--shutdown-when-all-503" in _a503)
check("值为 on",
      _a503[_a503.index("--shutdown-when-all-503") + 1] == "on")
check("传了活跃窗口",
      _a503[_a503.index("--all-503-window-minutes") + 1] == "45",
      str(_a503[_a503.index("--all-503-window-minutes") + 1]))
app.var_all503.set(False)
_a503cx = app._args_for(gui.SRC_CODEX, [])
check("关闭时传 off",
      _a503cx[_a503cx.index("--shutdown-when-all-503") + 1] == "off")
check("Codex 也支持这条规则", "--all-503-window-minutes" in _a503cx)


print()
print("=" * 70)
print("20b. 「选中所有进行中」按钮：只选进行中、可重复点、标记同步")
check("状态口径含两个来源的进行中",
      gui.RUNNING_STATUSES == {"running", "inProgress"},
      str(sorted(gui.RUNNING_STATUSES)))
check("按钮在运行时会被禁用（与列表控件一致）",
      app.btn_pick_running in app.sel_ctrls)

_sess = [
    {"source": gui.SRC_PI, "key": "p-run", "name": "alpha / 跑着的",
     "project": "alpha", "status": "running", "upd_ms": 1},
    {"source": gui.SRC_PI, "key": "p-err", "name": "alpha / 失败的",
     "project": "alpha", "status": "error", "upd_ms": 1},
    {"source": gui.SRC_CODEX, "key": "c-run", "name": "beta / 跑着的",
     "project": "beta", "status": "inProgress", "upd_ms": 1},
    {"source": gui.SRC_CODEX, "key": "c-done", "name": "beta / 完成的",
     "project": "beta", "status": "completed", "upd_ms": 1},
]
app._sessions = [dict(s) for s in _sess]
app._monitored = []
# 造出与 _sessions 对应的树行：勾选标记同步要靠真实行来验证，而不是只看内部列表
app.tree.delete(*app.tree.get_children())
_pid = app.tree.insert("", "end", text="[Pi] alpha（2）", values=("", ""))
for _s in app._sessions[:2]:
    app.tree.insert(_pid, "end", iid=f"{_s['source']}:{_s['key']}",
                    text=_s["name"], values=("", ""))
_cid = app.tree.insert("", "end", text="[Codex] beta（2）", values=("", ""))
for _s in app._sessions[2:]:
    app.tree.insert(_cid, "end", iid=f"{_s['source']}:{_s['key']}",
                    text=_s["name"], values=("", ""))

app._select_running()
check("只选中进行中的两个",
      {m["key"] for m in app._monitored} == {"p-run", "c-run"},
      str([m["key"] for m in app._monitored]))
check("两个来源都被覆盖",
      {m["source"] for m in app._monitored} == {gui.SRC_PI, gui.SRC_CODEX})
check("已失败的没被选进来", all(m["key"] != "p-err" for m in app._monitored))
check("会话列表里的勾选标记同步",
      app.tree.item("pi:p-run", "text").startswith("✓ ")
      and app.tree.item("codex:c-run", "text").startswith("✓ "),
      app.tree.item("pi:p-run", "text"))
check("没选中的行没有标记",
      not app.tree.item("pi:p-err", "text").startswith("✓ "))
check("待监控列表显示了这 2 个", app.lst_mon.size() == 2, str(app.lst_mon.size()))
check("计数标签正确", "已选 2 个" in app.lbl_mon.cget("text"),
      app.lbl_mon.cget("text"))

app._select_running()
check("再点一次不会重复添加", len(app._monitored) == 2,
      str([m["key"] for m in app._monitored]))

# Pi 勾了「监控所有活跃会话」时，逐个选择对 Pi 不生效：必须给个提示，否则按钮像没反应
app._monitored = []
app.var_use_pi.set(True)
app.var_active_all.set(True)
app._select_running()
check("勾了「所有活跃会话」时提示 Pi 不看逐个选择",
      "不看这份逐个选择" in app.txt.get("1.0", "end"))
app.var_active_all.set(False)

app._monitored = []
app._sessions = [dict(_sess[1])]
app._select_running()
check("没有进行中的会话时不误选", app._monitored == [], str(app._monitored))

app._sessions = []
app._select_running()
check("列表为空时不崩且不选", app._monitored == [], str(app._monitored))
print()
print("=" * 70)
print("21. 布局：不允许文字被吞（控件被压窄）或被遮挡（内容超出可视区）")
root.update_idletasks()
root.update()


def _squeezed(widget, out):
    """实际分配宽度 < 自己请求的宽度 → 文字会被截断（吞字）。"""
    try:
        if widget.winfo_width() > 1 and widget.winfo_reqwidth() > widget.winfo_width() + 2:
            text = ""
            try:
                text = str(widget.cget("text"))[:40]
            except Exception:
                pass
            out.append((widget.winfo_class(), widget.winfo_reqwidth(),
                        widget.winfo_width(), text))
    except Exception:
        pass
    for child in widget.winfo_children():
        _squeezed(child, out)


_bad = []
_squeezed(root, _bad)
check("没有被压窄到显示不下的控件（吞字）", not _bad, str(_bad[:3]))
check("窗口不超出屏幕",
      root.winfo_width() <= root.winfo_screenwidth()
      and root.winfo_height() <= root.winfo_screenheight(),
      f"{root.winfo_width()}x{root.winfo_height()}")
check("会话列表是多列（名字/时间/状态各占一列）",
      set(app.tree["columns"]) == {"updated", "status"}
      and "headings" in str(app.tree["show"]),
      f"{app.tree['columns']} show={app.tree['show']}")

# 布局切换：测试窗口是隐藏的，像素不可靠，直接按宽度驱动再查 grid 结构
_real_width = root.winfo_width
import tkinter.font as _tkfont
from tkinter import ttk as _ttk

_line = _tkfont.nametofont("TkDefaultFont").metrics("linespace")
_rowh = int(_ttk.Style(root).lookup("Treeview", "rowheight") or 0)
check("会话列表行高显式给足（中文不被压扁/叠行）",
      _rowh >= _line + 4, f"rowheight={_rowh} linespace={_line}")

# 布局切换：测试窗口是隐藏的，像素不可靠，直接按宽度驱动再查 grid 结构
try:
    root.winfo_width = lambda: 1700          # 模拟宽屏
    app._apply_layout()
    check("宽屏切到两列", app._wide is True)
    check("两列是并排（同一行、不同列）",
          app.col_left.grid_info()["row"] == app.col_right.grid_info()["row"]
          and app.col_left.grid_info()["column"] != app.col_right.grid_info()["column"],
          f"{app.col_left.grid_info()} / {app.col_right.grid_info()}")
    root.winfo_width = lambda: 720           # 模拟窄屏
    app._apply_layout()
    check("窄屏切回单列", app._wide is False)
    check("单列是上下堆叠（同一列、不同行）",
          app.col_left.grid_info()["column"] == app.col_right.grid_info()["column"]
          and app.col_right.grid_info()["row"] > app.col_left.grid_info()["row"],
          f"{app.col_left.grid_info()} / {app.col_right.grid_info()}")
finally:
    root.winfo_width = _real_width

# 缩到最小尺寸：内容必须能滚到，而不是被裁掉
root.geometry("720x520")
root.update_idletasks()
root.update()
# 视口与内容的配合：窗口比内容高时要铺满（会话列表才有空间自动变长），
# 内容比视口宽时保持自然宽度（宁可出滚动条，也不压窄文字）。
# 隐藏窗口的像素不可靠，这里直接把视口尺寸喂给回调来验证逻辑。
class _Ev:
    width = 1200
    height = 600


app.canvas.winfo_width = lambda: 1200
app.canvas.winfo_height = lambda: 600
app._on_canvas_configure(_Ev())
check("视口比内容高时内容铺满视口（会话列表才有空间变长）",
      int(app.canvas.itemcget(app._body_id, "height")) >= 600 - 2,
      f"item 高={app.canvas.itemcget(app._body_id, 'height')}")
check("视口比内容宽时内容保持自然宽度（不压窄文字）",
      int(app.canvas.itemcget(app._body_id, "width"))
      >= app.body.winfo_reqwidth() - 2,
      f"item 宽={app.canvas.itemcget(app._body_id, 'width')} "
      f"需要={app.body.winfo_reqwidth()}")
_bad2 = []
_squeezed(root, _bad2)
check("缩小后依然没有控件被压窄（宁可出滚动条）", not _bad2, str(_bad2[:3]))
root.destroy()

print()
print("=" * 70)
if FAIL:
    print(f"失败 {len(FAIL)} 项：")
    for x in FAIL:
        print("   -", x)
    sys.exit(1)
print("GUI 联动验证全部通过")
