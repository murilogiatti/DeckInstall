"""Names of Steam store apps from Steam's own appcache/appinfo.vdf (also uninstalled ones).

Tolerant reader: format versions 27, 28 and 29 (29 keeps the key names in a string table at
the end of the file). An unknown version, a truncated file or a broken entry never raises; the
reader returns what it could read. Only the apps asked for are decoded; the rest are skipped.
"""

from __future__ import annotations

import logging
import mmap
import struct
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_MAGIC_V27 = 0x07564427
_MAGIC_V28 = 0x07564428
_MAGIC_V29 = 0x07564429
_ENTRY_HEAD_V27 = 4 + 4 + 8 + 20 + 4  # info state, last updated, PICS token, SHA-1, change number
_ENTRY_HEAD_V28 = _ENTRY_HEAD_V27 + 20  # + SHA-1 of the binary data
_MAX_DEPTH = 32

# Binary KeyValues value types.
_T_MAP, _T_STR, _T_INT32, _T_FLOAT, _T_PTR, _T_WSTR, _T_COLOR, _T_UINT64, _T_END, _T_INT64 = (
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x0A,
)
_FIXED = {_T_INT32: "<i", _T_FLOAT: "<f", _T_PTR: "<i", _T_COLOR: "<i", _T_UINT64: "<Q", _T_INT64: "<q"}


_CACHE_LOCK = threading.Lock()
_APPINFO_CACHE: tuple[tuple[str, int, int], dict[int, str]] | None = None


class _Reader:
    def __init__(self, data: bytes | memoryview | mmap.mmap, strings: list[str] | None) -> None:
        self.data = data
        self.pos = 0
        self.strings = strings

    def cstring(self) -> str:
        if isinstance(self.data, (bytes, mmap.mmap)):
            end = self.data.find(b"\0", self.pos)
        else:
            end = bytes(self.data).index(b"\0", self.pos)
        if end < 0:
            raise ValueError("Unterminated cstring")
        text = bytes(self.data[self.pos:end]).decode("utf-8", "replace")
        self.pos = end + 1
        return text

    def unpack(self, fmt: str) -> Any:
        value = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += struct.calcsize(fmt)
        return value

    def key(self) -> str:
        if self.strings is None:
            return self.cstring()
        return self.strings[self.unpack("<I")]

    def kv_map(self, depth: int = 0) -> dict[str, Any]:
        if depth > _MAX_DEPTH:
            raise ValueError("KeyValues nested too deep")
        out: dict[str, Any] = {}
        while True:
            kind = self.data[self.pos]
            self.pos += 1
            if kind == _T_END:
                return out
            name = self.key()
            if kind == _T_MAP:
                out[name] = self.kv_map(depth + 1)
            elif kind == _T_STR:
                out[name] = self.cstring()
            elif kind == _T_WSTR:
                end = self.pos
                while self.data[end:end + 2] != b"\0\0":
                    end += 2
                out[name] = bytes(self.data[self.pos:end]).decode("utf-16-le", "replace")
                self.pos = end + 2
            elif kind in _FIXED:
                out[name] = self.unpack(_FIXED[kind])
            else:
                raise ValueError(f"Unknown KeyValues type {kind:#x}")


def _string_table(data: bytes | mmap.mmap, offset: int) -> list[str]:
    reader = _Reader(data, None)
    reader.pos = offset
    count = reader.unpack("<I")
    return [reader.cstring() for _ in range(count)]


def _parse_all_app_names(data: bytes | mmap.mmap) -> dict[int, str]:
    names: dict[int, str] = {}
    try:
        magic, _universe = struct.unpack_from("<II", data, 0)
        pos = 8
        strings: list[str] | None = None
        if magic == _MAGIC_V29:
            (table_offset,) = struct.unpack_from("<q", data, pos)
            pos += 8
            strings = _string_table(data, table_offset)
            end_of_apps = table_offset
            head = _ENTRY_HEAD_V28
        elif magic in (_MAGIC_V27, _MAGIC_V28):
            end_of_apps = len(data)
            head = _ENTRY_HEAD_V28 if magic == _MAGIC_V28 else _ENTRY_HEAD_V27
        else:
            logging.info("appinfo.vdf: unknown format %#x; app names not read", magic)
            return {}
        while pos + 8 <= end_of_apps:
            appid, size = struct.unpack_from("<II", data, pos)
            if appid == 0:
                break
            body_start, next_pos = pos + 8, pos + 8 + size
            pos = next_pos
            if next_pos > end_of_apps or size < head:
                break
            reader = _Reader(data[body_start + head:next_pos], strings)
            try:
                kv = reader.kv_map()
            except (ValueError, IndexError, struct.error, UnicodeDecodeError):
                continue  # one broken entry: keep the others
            info = kv.get("appinfo", kv)
            common = info.get("common") if isinstance(info, dict) else None
            name = common.get("name") if isinstance(common, dict) else None
            if isinstance(name, str) and name.strip():
                names[appid] = name.strip()
    except (ValueError, IndexError, struct.error) as exc:
        logging.info("appinfo.vdf partly unreadable (%s); using %d names", exc, len(names))
    return names


def read_app_names(path: Path, appids: Iterable[int] | None = None) -> dict[int, str]:
    """{appid: name} for the apps in appinfo.vdf (only `appids` when given), cached by file stat."""
    global _APPINFO_CACHE
    wanted = set(appids) if appids is not None else None
    if wanted is not None and not wanted:
        return {}
    try:
        st = path.stat()
    except OSError:
        return {}
    if st.st_size < 8:
        return {}
    cache_key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        cached = _APPINFO_CACHE
    if cached is not None and cached[0] == cache_key:
        all_names = cached[1]
    else:
        try:
            with path.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                all_names = _parse_all_app_names(mm)
        except (OSError, ValueError):
            try:
                all_names = _parse_all_app_names(path.read_bytes())
            except OSError:
                return {}
        with _CACHE_LOCK:
            _APPINFO_CACHE = (cache_key, all_names)
    if wanted is None:
        return dict(all_names)
    return {aid: all_names[aid] for aid in wanted if aid in all_names}


def steam_app_names(steam_root: Path, appids: Iterable[int]) -> dict[int, str]:
    """Names Steam's app cache knows for appids (empty when the cache is missing or unreadable)."""
    return read_app_names(steam_root / "appcache" / "appinfo.vdf", appids)
