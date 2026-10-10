#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PI-Desktop「已归档会话」读取器。

为什么不能查数据库：归档状态不在 pi.sqlite 里 —— sessions 表只有
deleted_at / pinned 两列（deleted_at 是「删除」），而「归档」存在
PI-Desktop（Electron 渲染进程）的 Local Storage 里，键名
pi.desktop.sidebarPreferences，值形如：

    {"sessionMeta": {"<会话 id>": {"archived": true}, ...},
     "sessionView": {"archived": false}, ...}

所以要在监控里屏蔽「已归档」会话，只能去读这份 leveldb。

怎么读（不引入依赖）：
  Local Storage 目录里两类文件，各读一遍：
    *.log  写入缓冲：记录是明文，值按 UTF-16LE 存 → 直接在字节里扫 JSON。
           它装的是还没落盘的最新写入，所以优先用它。
    *.ldb  落盘表：数据块是 snappy 压缩的 → 按 leveldb 表格式解析
           （footer → index 块 → data 块 → snappy 解压 → 取键值）。
  Chromium 的 Local Storage 键带自增版本号：
      _file://\x00\x01pi.desktop.sidebarPreferences\x01<8 字节小端版本>
  一次写入 = 写新版本 + 删旧版本，而旧版本的删除记录与旧值会一起留在
  历史 .ldb 里，所以 .ldb 里可能同时存在多份值 —— 取版本号最大的那份。

  取不到时返回空集合 + 说明文字：宁可少屏蔽（漏掉几个已归档的），也不猜成
  「全都已归档」（把没归档的会话一起屏蔽掉）—— 调用方必须把状态显示出来。
"""
import json
import os
import sys
from pathlib import Path

# 单份快照最大长度：正常 prefs JSON 只有几十 KB，给足余量即可
MAX_JSON_BYTES = 4 << 20
# Local Storage 里 prefs 的键名（键 = 前缀 + 键名 + \x01 + 8 字节版本号）
KEY_MARK = b"pi.desktop.sidebarPreferences"
MARKER = '{"sessionMeta"'
# leveldb 表 footer 的魔数（0xdb4775248b80fb57 的小端字节序）
TABLE_MAGIC = b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"

_CACHE = {"sig": None, "ids": frozenset(), "note": ""}


def default_leveldb_dir():
    """PI-Desktop 的 Local Storage leveldb 目录；找不到返回 None。"""
    cands = []
    if os.name == "nt":
        base = os.environ.get("APPDATA") or ""
        if base:
            for name in ("PI-Desktop", "pi-desktop"):
                cands.append(Path(base) / name / "Local Storage" / "leveldb")
    else:
        # macOS / Linux 同布局：Application Support 下按应用名
        home = Path.home()
        for root in (home / "Library" / "Application Support", home / ".config"):
            for name in ("PI-Desktop", "pi-desktop"):
                cands.append(root / name / "Local Storage" / "leveldb")
    for p in cands:
        if p.is_dir():
            return p
    return None


# ---------------------------------------------------------------- snappy

def _varint(buf, pos):
    """读 leveldb/snappy 的 varint，返回 (值, 新位置)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint 越界")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def snappy_decompress(data):
    """解压 snappy 原始格式；失败返回 None。

    格式：varint 解压后长度 + 若干元素，元素首字节低 2 位是类型：
      0 字面量 / 1 单字节偏移拷贝 / 2 双字节偏移拷贝 / 3 四字节偏移拷贝。
    """
    try:
        expect, pos = _varint(data, 0)
    except ValueError:
        return None
    out = bytearray()
    n = len(data)
    while pos < n:
        tag = data[pos]
        pos += 1
        kind = tag & 0x03
        if kind == 0:                       # 字面量
            length = tag >> 2
            if length >= 60:
                extra = length - 59
                if pos + extra > n:
                    return None
                length = int.from_bytes(data[pos:pos + extra], "little") + 1
                pos += extra
            else:
                length += 1
            if pos + length > n:
                return None
            out += data[pos:pos + length]
            pos += length
            continue
        if kind == 1:
            length = ((tag >> 2) & 0x07) + 4
            if pos >= n:
                return None
            offset = ((tag & 0xE0) << 3) | data[pos]
            pos += 1
        elif kind == 2:
            length = (tag >> 2) + 1
            if pos + 2 > n:
                return None
            offset = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
        else:
            length = (tag >> 2) + 1
            if pos + 4 > n:
                return None
            offset = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        if offset <= 0 or offset > len(out):
            return None
        start = len(out) - offset
        for i in range(length):             # 逐字节复制：允许自重叠
            out.append(out[start + i])
    if len(out) != expect:
        return None
    return bytes(out)


# ---------------------------------------------------------------- leveldb 表

def _block_entries(raw):
    """解析一个块（data / index 同格式）→ [(key, value)]。"""
    n = len(raw)
    if n < 8:
        return []
    num_restarts = int.from_bytes(raw[n - 4:n], "little")
    if num_restarts <= 0 or num_restarts > n // 4:
        return []
    limit = n - 4 - num_restarts * 4
    if limit < 0:
        return []
    out = []
    pos = 0
    key = b""
    while pos < limit:
        try:
            shared, pos = _varint(raw, pos)
            non_shared, pos = _varint(raw, pos)
            vlen, pos = _varint(raw, pos)
        except ValueError:
            break
        if shared > len(key) or pos + non_shared + vlen > limit:
            break
        key = key[:shared] + raw[pos:pos + non_shared]
        pos += non_shared
        out.append((key, raw[pos:pos + vlen]))
        pos += vlen
    return out


def _block_data(data, offset, size):
    """按 BlockHandle 取块内容；压缩类型 0=原样、1=snappy。

    注意：leveldb 的 BlockHandle.size 不含块尾那 5 字节（1 字节压缩类型 +
    4 字节 crc），所以尾部要从 offset+size 处另读。
    """
    end = offset + size
    if size <= 0 or offset < 0 or end + 5 > len(data):
        return None
    payload = data[offset:end]
    ctype = data[end]
    if ctype == 0:
        return payload
    if ctype == 1:
        return snappy_decompress(payload)
    return None


def table_entries(data):
    """遍历 leveldb 表文件（*.ldb）里的所有 (key, value)。"""
    if len(data) < 48 or data[-8:] != TABLE_MAGIC:
        return
    try:
        _meta_off, pos = _varint(data, len(data) - 48)     # metaindex handle
        _meta_size, pos = _varint(data, pos)
        index_off, pos = _varint(data, pos)                # index handle
        index_size, pos = _varint(data, pos)
    except ValueError:
        return
    index = _block_data(data, index_off, index_size)
    if index is None:
        return
    for _sep, handle in _block_entries(index):
        try:
            off, q = _varint(handle, 0)
            size, q = _varint(handle, q)
        except ValueError:
            continue
        block = _block_data(data, off, size)
        if block is None:
            continue
        for item in _block_entries(block):
            yield item


def _version_of(key):
    """从 Local Storage 的键里取版本号；不是我们要的键就返回 None。"""
    i = key.find(KEY_MARK)
    if i < 0:
        return None
    rest = key[i + len(KEY_MARK):]
    if len(rest) >= 9 and rest[0] == 1:
        return int.from_bytes(rest[1:9], "little")
    return 0


def _ldb_candidates(path):
    """一个 *.ldb 里所有 prefs 值 → [(版本号, 原始值)]。"""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    out = []
    for key, value in table_entries(data):
        ver = _version_of(key)
        if ver is not None:
            out.append((ver, value))
    return out


# ---------------------------------------------------------------- 值解码

def decode_local_storage_value(raw):
    """解 Chromium Local Storage 的值：首字节 0=UTF-16LE、1=Latin-1。"""
    if not raw:
        return None
    enc, payload = raw[0], raw[1:]
    if enc == 0:
        return payload.decode("utf-16-le", "replace")
    if enc == 1:
        return payload.decode("latin-1", "replace")
    return None


def _session_meta_from_text(text):
    """从 prefs JSON 文本里取 sessionMeta 字典；取不到返回 None。"""
    if not text:
        return None
    start = text.find(MARKER)
    if start < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text[start:])
    except Exception:
        return None
    meta = obj.get("sessionMeta")
    return meta if isinstance(meta, dict) else None


def _log_snapshot(path):
    """从写入缓冲（*.log，明文）里取最后一份能解析的 sessionMeta。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    best = None
    for enc in ("utf-16-le", "utf-8"):
        pat = MARKER.encode(enc)
        pos = 0
        while True:
            i = data.find(pat, pos)
            if i < 0:
                break
            pos = i + len(pat)
            text = data[i:i + MAX_JSON_BYTES].decode(enc, "replace")
            try:
                obj, _end = json.JSONDecoder().raw_decode(text)
            except Exception:
                continue
            meta = obj.get("sessionMeta")
            if isinstance(meta, dict):
                best = meta
    return best


# ---------------------------------------------------------------- 对外接口

def _read_meta(files):
    """返回 (sessionMeta 字典, 来源文件名)；读不到返回 (None, None)。"""
    logs = sorted((f for f in files if f[1].endswith(".log")), reverse=True)
    ldbs = sorted((f for f in files if f[1].endswith(".ldb")), reverse=True)
    for _mt, name, path, _size in logs:          # 未落盘的最新写入
        meta = _log_snapshot(path)
        if meta is not None:
            return meta, name
    best = None
    for _mt, name, path, _size in ldbs:          # 落盘表：取版本号最大的那份
        for ver, value in _ldb_candidates(path):
            if best is None or ver > best[0]:
                best = (ver, value, name)
    if best is None:
        return None, None
    return _session_meta_from_text(decode_local_storage_value(best[1])), best[2]


def archived_ids(leveldb_dir=None, force=False):
    """返回 (已归档会话 id 的 frozenset, 状态说明)。

    状态说明用于界面/日志，形如「已读到 180 个已归档会话（002061.ldb）」
    或「读不到：…（未屏蔽归档）」。
    """
    d = Path(leveldb_dir) if leveldb_dir else default_leveldb_dir()
    if d is None or not Path(d).is_dir():
        return frozenset(), "读不到 PI-Desktop 的 Local Storage 目录（未屏蔽归档）"

    files = []
    try:
        for p in Path(d).iterdir():
            if p.is_file() and p.suffix in (".log", ".ldb"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append((st.st_mtime_ns, p.name, p, st.st_size))
    except OSError as e:
        return frozenset(), f"读不到 Local Storage 目录：{e!r}（未屏蔽归档）"

    sig = tuple(sorted((name, size, mt) for mt, name, _p, size in files))
    if not force and _CACHE["sig"] == sig:
        return _CACHE["ids"], _CACHE["note"]

    meta, src = _read_meta(files)
    if meta is None:
        ids = frozenset()
        note = "读不到归档表（leveldb 里没有可解析的快照，未屏蔽归档）"
    else:
        ids = frozenset(k for k, v in meta.items()
                        if isinstance(v, dict) and v.get("archived"))
        note = f"已读到 {len(ids)} 个已归档会话（{src}）"

    _CACHE.update(sig=sig, ids=ids, note=note)
    return ids, note


def main():
    force = "--force" in sys.argv
    ids, note = archived_ids(force=force)
    print(note)
    for sid in sorted(ids):
        print(sid)


if __name__ == "__main__":
    main()
