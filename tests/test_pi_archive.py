# -*- coding: utf-8 -*-
"""验证「已归档 / 已删除」会话都被屏蔽。

背景：PI-Desktop 的「归档」状态不在 pi.sqlite 里（sessions 表只有 deleted_at），
而是存在 PI-Desktop 的 Local Storage leveldb 里。所以这里分三层验证：

  1. 读 leveldb 的能力：snappy 解压、leveldb 表解析、明文写入缓冲扫描
     —— 用临时目录造数据，不依赖本机 PI-Desktop；
  2. GUI 会话列表：归档的会话不进列表，且底部说明写明屏蔽了几个；
  3. 监控脚本：归档的会话不纳入监控（自动模式与 --thread 显式指定都一样），
     已在监控里的归档后会被移出。

第 2、3 层用真实的 pi.sqlite（有就用真实会话 id，没有就跳过）。
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import pi_archive as pa  # noqa: E402

FAIL = []


def check(label, cond, extra=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


# ---------------------------------------------------------------- 造数据工具

def _varint_bytes(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _snappy_literal(payload):
    """把整段数据编成一个 snappy 字面量元素（测试用，不求压缩率）。"""
    n = len(payload)
    out = bytearray(_varint_bytes(n))
    if n < 60:
        out.append((n - 1) << 2)
    else:
        extra = ((n - 1).bit_length() + 7) // 8
        out.append((59 + extra) << 2)
        out += (n - 1).to_bytes(extra, "little")
    return bytes(out) + payload


def _block(entries):
    """手工拼一个 leveldb 块（entries 必须按 key 升序）。"""
    out = bytearray()
    for k, v in entries:
        out += _varint_bytes(0) + _varint_bytes(len(k)) + _varint_bytes(len(v))
        out += k + v
    out += (0).to_bytes(4, "little")      # restarts[0]
    out += (1).to_bytes(4, "little")      # num_restarts
    return bytes(out)


def _table_file(path, key, value, compress=False):
    """写一个最小可用的 leveldb 表文件（一个数据块 + 索引块 + footer）。

    BlockHandle 里的 size 是「存盘后的字节数」（压缩后），不是原始长度。
    """
    data_stored = _snappy_literal(_block([(key, value)])) if compress \
        else _block([(key, value)])
    data_part = data_stored + (b"\x01" if compress else b"\x00") + b"\x00" * 4
    handle = _varint_bytes(0) + _varint_bytes(len(data_stored))
    index_payload = _block([(b"~sep", handle)])
    index_stored = _snappy_literal(index_payload) if compress else index_payload
    index_part = index_stored + (b"\x01" if compress else b"\x00") + b"\x00" * 4
    index_off = len(data_part)
    footer = (_varint_bytes(0) + _varint_bytes(0)                  # metaindex
              + _varint_bytes(index_off) + _varint_bytes(len(index_stored)))
    footer += b"\x00" * (40 - len(footer)) + pa.TABLE_MAGIC
    path.write_bytes(data_part + index_part + footer)


def _ls_key(version):
    return b"_file://\x00\x01" + pa.KEY_MARK + b"\x01" + version.to_bytes(8, "little")


def _ls_value(meta):
    text = json.dumps({"sessionMeta": meta, "sessionView": {"archived": False}})
    return b"\x00" + text.encode("utf-16-le")


print("=" * 70)
print("1. snappy 解压（leveldb 数据块用它）")
check("字面量", pa.snappy_decompress(b"\x05\x10hello") == b"hello")
check("重叠拷贝", pa.snappy_decompress(b"\x09\x08abc\x16\x03\x00") == b"abcabcabc")
check("长字面量（长度 ≥ 60 的扩展编码）",
      pa.snappy_decompress(_snappy_literal(bytes(range(200)))) == bytes(range(200)))
check("坏数据返回 None", pa.snappy_decompress(b"\x99\xff") is None)

print()
print("=" * 70)
print("2. 读 leveldb：压缩落盘表 / 未压缩落盘表 / 明文写入缓冲")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)

    # 2a. 只有未压缩的 .ldb
    _table_file(d / "000001.ldb", _ls_key(5),
                _ls_value({"a1": {"archived": True}, "b2": {"pinned": True}}))
    ids, note = pa.archived_ids(d, force=True)
    check("未压缩表里能读到归档会话", ids == frozenset({"a1"}), str(sorted(ids)))
    check("pinned 不算归档", "b2" not in ids)
    check("说明里带来源文件名", "000001.ldb" in note, note)

    # 2b. 压缩的 .ldb + 更高的版本号 → 必须赢
    _table_file(d / "000002.ldb", _ls_key(9),
                _ls_value({"a1": {"archived": True}, "c3": {"archived": True}}),
                compress=True)
    ids, note = pa.archived_ids(d, force=True)
    check("压缩表（snappy）能读到", ids == frozenset({"a1", "c3"}), str(sorted(ids)))
    check("取版本号最大的那份", "000002.ldb" in note, note)

    # 2c. 明文写入缓冲（*.log）优先于落盘表
    record = json.dumps({"sessionMeta": {"z9": {"archived": True}}}).encode("utf-16-le")
    (d / "000003.log").write_bytes(b"\x00" * 20 + record)
    ids, note = pa.archived_ids(d, force=True)
    check("写入缓冲优先（它是还没落盘的最新值）", ids == frozenset({"z9"}), str(sorted(ids)))

    # 2d. 目录里什么都没有
with tempfile.TemporaryDirectory() as td2:
    ids, note = pa.archived_ids(Path(td2), force=True)
    check("空目录 → 空集合 + 明确说「未屏蔽」", ids == frozenset() and "未屏蔽" in note, note)
ids, note = pa.archived_ids(Path("Z:/不存在/leveldb"), force=True)
check("目录不存在 → 空集合 + 明确说「未屏蔽」", ids == frozenset() and "未屏蔽" in note, note)

print()
print("=" * 70)
print("3. GUI 会话列表：归档的不进列表")
try:
    import codex_monitor_gui as gui
    root = tk.Tk()
    root.withdraw()
    app = gui.App(root)
    pi_rows = app._list_pi()
    if not pi_rows:
        print("  （本机 pi.sqlite 里没有可列会话，跳过这一节）")
    else:
        real = pa.archived_ids
        victim = pi_rows[0]["key"]
        try:
            pa.archived_ids = lambda *a, **k: (frozenset(), "测试：不屏蔽")
            all_rows = app._list_pi()
            pa.archived_ids = lambda *a, **k: (frozenset({victim}), "测试：1 个已归档")
            app._refresh_sessions()
            left = [s["key"] for s in app._sessions if s["source"] == "pi"]
            check("归档的会话没进列表", victim not in left, victim[:8])
            check("其它会话仍在列表", len(left) == len(all_rows) - 1,
                  f"{len(left)}/{len(all_rows)}")
            check("底部写明屏蔽了几个", "已屏蔽归档 1 个" in app.lbl_sess.cget("text"),
                  app.lbl_sess.cget("text"))
        finally:
            pa.archived_ids = real
        # 读不到归档表时必须明说「未屏蔽」，不能默默当没事
        pa.archived_ids = lambda *a, **k: (frozenset(), "读不到归档表（测试，未屏蔽归档）")
        try:
            app._refresh_sessions()
            check("读不到归档表时界面明说「未屏蔽」",
                  "未屏蔽" in app.lbl_sess.cget("text"), app.lbl_sess.cget("text"))
        finally:
            pa.archived_ids = real
    root.destroy()
except Exception as e:
    check(f"GUI 检查未能运行：{e!r}", False)

print()
print("=" * 70)
print("4. 监控脚本：归档的不纳入监控、已在监控里的会被移出")
import pi_monitor as pm  # noqa: E402

con = sqlite3.connect(":memory:")
con.executescript(
    """
    create table sessions (id text, title text, project_id int, deleted_at int,
                           pinned int, updated_at int, permission_mode text);
    create table projects (id int, path text);
    create table turns (session_id text, status text, error_code text, started_at int);
    """
)
con.execute("insert into projects values (1, 'D:/proj/demo')")
for i, sid in enumerate(("a1-archived", "b2-live", "c3-live")):
    con.execute("insert into sessions values (?, ?, 1, null, 0, ?, 'auto')",
                (sid, f"会话{sid}", 1000 + i))
con.commit()

real = pa.archived_ids
pa.archived_ids = lambda *a, **k: (frozenset({"a1-archived"}), "测试：1 个已归档")
try:
    picked = [s["id"] for s in pm.pick_sessions(con, [], 5)]
    check("自动模式：归档的没被选进来", "a1-archived" not in picked, str(picked))
    check("自动模式：其余照常选入", picked == ["c3-live", "b2-live"], str(picked))

    picked = [s["id"] for s in pm.pick_sessions(con, ["a1-archived"], 5)]
    check("--thread 显式指定归档会话也被跳过", picked == [], str(picked))

    sessions = [{"id": "a1-archived", "name": "已归档会话", "status": "", "error": None,
                 "path": None, "title": "", "updated_at": None, "permission_mode": ""}]
    state = {"a1-archived": pm.new_session_state()}
    log_buf = io.StringIO()
    pm.sync_sessions(con, sessions, state, log_buf, True, 3)
    check("已在监控里的归档后被移出", sessions == [], str(sessions))
    check("移出时写了日志（含「已归档」）", "已归档" in log_buf.getvalue(),
          log_buf.getvalue().strip()[:80])
finally:
    pa.archived_ids = real
con.close()

print()
print("=" * 70)
if FAIL:
    print(f"有 {len(FAIL)} 项未通过：")
    for name in FAIL:
        print("  ✗ " + name)
    sys.exit(1)
print("归档/删除屏蔽检查全部通过")
