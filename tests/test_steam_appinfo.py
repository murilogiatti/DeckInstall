"""appinfo.vdf names: format 27, 28 and 29, and tolerance to broken files."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from deckinstall import steam_appinfo as ai


def _kv(node: dict, key_of) -> bytes:
    out = b""
    for k, v in node.items():
        if isinstance(v, dict):
            out += b"\x00" + key_of(k) + _kv(v, key_of)
        elif isinstance(v, int):
            out += b"\x02" + key_of(k) + struct.pack("<i", v)
        else:
            out += b"\x01" + key_of(k) + v.encode() + b"\0"
    return out + b"\x08"


def _file(version: int, apps: dict[int, str], broken: int | None = None) -> bytes:
    magic = {27: ai._MAGIC_V27, 28: ai._MAGIC_V28, 29: ai._MAGIC_V29}[version]
    head = ai._ENTRY_HEAD_V27 if version == 27 else ai._ENTRY_HEAD_V28
    table: list[str] = []

    def key_of(k: str) -> bytes:
        if version < 29:
            return k.encode() + b"\0"
        if k not in table:
            table.append(k)
        return struct.pack("<I", table.index(k))

    body = b""
    for appid, name in apps.items():
        kv = _kv({"appinfo": {"appid": appid, "common": {"name": name, "type": "Game"}}}, key_of)
        if appid == broken:
            kv = b"\x09garbage"
        payload = b"\0" * head + kv
        body += struct.pack("<II", appid, len(payload)) + payload
    body += struct.pack("<I", 0)
    if version < 29:
        return struct.pack("<II", magic, 1) + body
    offset = 8 + 8 + len(body)
    strings = struct.pack("<I", len(table)) + b"".join(s.encode() + b"\0" for s in table)
    return struct.pack("<IIq", magic, 1, offset) + body + strings


@pytest.mark.parametrize("version", [27, 28, 29])
def test_reads_names_of_each_version(tmp_path: Path, version: int) -> None:
    f = tmp_path / "appinfo.vdf"
    f.write_bytes(_file(version, {10: "Counter-Strike", 570: "Dota 2", 1091500: "Cyberpunk 2077"}))
    assert ai.read_app_names(f) == {10: "Counter-Strike", 570: "Dota 2", 1091500: "Cyberpunk 2077"}
    assert ai.read_app_names(f, [570]) == {570: "Dota 2"}


def test_broken_entry_unknown_version_and_truncation(tmp_path: Path) -> None:
    f = tmp_path / "appinfo.vdf"
    f.write_bytes(_file(29, {10: "A", 20: "B", 30: "C"}, broken=20))
    assert ai.read_app_names(f) == {10: "A", 30: "C"}
    data = _file(28, {10: "A", 20: "B"})
    f.write_bytes(data[: len(data) - 20])
    assert ai.read_app_names(f) == {10: "A"}
    f.write_bytes(struct.pack("<II", 0x07564499, 1) + b"\0" * 64)
    assert ai.read_app_names(f) == {}
    assert ai.read_app_names(tmp_path / "missing.vdf") == {}
