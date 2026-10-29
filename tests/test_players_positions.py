#!/usr/bin/env python
"""位置/距离解析的离线验收（不需要 VRChat、不需要网络）。

## 钉住的事

1. Event 12 的 body 布局：`body[4] & 0x0F == 0x07` 才带位置，坐标在 `body[6:18]`（float32 LE）；
2. 没有标志 / 全 0 / 越界 → 当作"没有位置"，**不许**喂出错误距离；
3. 距离 = 3D 欧氏距离（本机位置来自**出向** Event 12）；
4. 位置会过期（默认 30s）——静坐玩家的位置是按需下发的，必须允许过期而不是乱用旧值。

跑法：xvfb-run -a .venv/bin/python tests/test_players_positions.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import positions  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


def _payload(x: float, y: float, z: float, *, flag: int = 0x07,
             body_len: int = 40) -> bytes:
    """造一条 Event 12 的 payload：F5 43 <LEB len> body。"""
    body = bytearray(body_len)
    struct.pack_into("<I", body, 0, 123456)          # 服务器时间
    body[4] = flag
    struct.pack_into("<fff", body, 6, x, y, z)
    head = b"\xf3\x04\x0c\x02\xf5\x43"
    assert body_len < 0x80
    return head + bytes([body_len]) + bytes(body)


def test_parse() -> None:
    print("[positions] 解析与判据")
    pos = positions.parse_position(_payload(1.5, 0.76, -47.25), ts=100.0)
    check("带标志 → 解析成功", pos is not None)
    check("坐标值正确",
          pos is not None and abs(pos.x - 1.5) < 1e-6 and abs(pos.y - 0.76) < 1e-6
          and abs(pos.z + 47.25) < 1e-6, str(pos))
    check("时间戳透传", pos is not None and pos.ts == 100.0)

    check("没有位置标志 → None",
          positions.parse_position(_payload(1, 2, 3, flag=0x00)) is None)
    check("标志高半字节不影响判据",
          positions.parse_position(_payload(1, 2, 3, flag=0xF7)) is not None)
    check("全 0 坐标 → None（还没初始化）",
          positions.parse_position(_payload(0.0, 0.0, 0.0)) is None)
    check("越界值 → None",
          positions.parse_position(_payload(1e9, 0, 0)) is None)
    check("body 太短 → None", positions.parse_position(b"\xf3\x04\x0c\x02\xf5\x43\x04") is None)
    check("不是 Event 12 的载荷（没有 F5 43）→ None",
          positions.parse_position(b"\xf3\x04\x0d\x02\xf5\x44") is None)


def _body_only(x: float, y: float, z: float, *, flag: int = 0x07) -> bytes:
    body = bytearray(40)
    body[4] = flag
    struct.pack_into("<fff", body, 6, x, y, z)
    return bytes(body)


def test_body_and_self() -> None:
    print("[positions] 直接给 body（出向 RaiseEvent 路径）")
    body = bytearray(40)
    struct.pack_into("<fff", body, 6, 1.0, 0.8, -2.0)
    body[4] = 0x07
    check("parse_body 正常", positions.parse_body(bytes(body)) == (1.0, 0.8, -2.0)
          or all(abs(a - b) < 1e-6 for a, b in zip(positions.parse_body(bytes(body)), (1.0, 0.8, -2.0))))
    body[4] = 0x02
    check("没有位置标志 → None", positions.parse_body(bytes(body)) is None)

    table = positions.PositionTable()
    table.observe_body(0, _body_only(1.0, 0.8, -2.0), ts=10.0, is_self=True)
    check("observe_body 能写本机位置", table.self_position is not None)
    check("本机位置数值正确",
          table.self_position is not None and abs(table.self_position.z + 2.0) < 1e-6,
          str(table.self_position))


def test_table() -> None:
    print("[positions] 距离与过期")
    table = positions.PositionTable(max_age_s=30.0)
    table.observe(0, _payload(0.0, 0.0, 0.0), ts=1000.0, is_self=True)   # 全 0 不收
    check("本机位置未建立时距离为 None", table.distance(7, now=1000.0) is None)

    table.observe(0, _payload(0.0, 0.0, 0.0 + 0.01), ts=1000.0, is_self=True)
    table.observe(7, _payload(3.0, 4.0, 0.01), ts=1000.0)
    d = table.distance(7, now=1001.0)
    check("3-4-5 距离 = 5m", d is not None and abs(d - 5.0) < 1e-3, str(d))
    check("本机位置单独存", table.self_position is not None)

    check("过期位置 → None", table.distance(7, now=1000.0 + 31) is None)
    check("表里仍有记录（只是过期）", 7 in table.snapshot())

    table.forget(7)
    check("forget 之后查不到", table.get(7) is None)

    # 未知 actor
    check("没见过的 actor → None", table.distance(999, now=1001.0) is None)

    check("距离文案", positions.format_distance(8.34) == "8.3m",
          positions.format_distance(8.34))
    check("近距离文案", positions.format_distance(0.4) == "<1m")
    check("未知文案为空", positions.format_distance(None) == "")


def main() -> int:
    test_parse()
    test_body_and_self()
    test_table()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（位置/距离）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
