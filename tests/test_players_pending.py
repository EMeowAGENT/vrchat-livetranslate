#!/usr/bin/env python
"""取钥期缓冲（`vlt/players/pending.py`）的离线验收。

## 这些用例能证明什么

1. 回放按**时间序**，并且同一份密文只吐一次（多网卡/加速器会重复交包）；
2. 超出上限时丢**最旧**的，且丢掉的会转存进「启动前缀」——加入世界那波大快照
   正是最先被挤出去的一批，不能顺手扔掉；
3. 世界就绪后按 TTL 修剪；字节上限也生效。

跑法：.venv/bin/python tests/test_players_pending.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import capture_linux  # noqa: E402
from vlt.players.pending import PreKeyBuffer  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


LOCAL = ("192.168.1.10", 53000)
SERVER = ("34.216.184.93", 27002)


def dg(seq: int, ts: float, *, payload: bytes | None = None,
       direction: str = "in") -> capture_linux.Datagram:
    return capture_linux.Datagram(
        ts=ts,
        src=SERVER if direction == "in" else LOCAL,
        dst=LOCAL if direction == "in" else SERVER,
        payload=payload if payload is not None else bytes([seq]) * 40,
        direction=direction,
    )


def test_order_and_dedup() -> None:
    print("[pending] 顺序与去重")
    buf = PreKeyBuffer()
    # 故意乱序 + 完全重复的一个（同一密文从两块网卡交上来）
    dup = bytes([9]) * 40
    buf.add(dg(0, 3.0))
    buf.add(dg(1, 1.0))
    buf.add(dg(2, 2.0))
    buf.add(dg(3, 2.5, payload=dup))
    buf.add(dg(3, 2.5, payload=dup))
    out = buf.release()
    check("时间序回放", [p.ts for p in out] == [1.0, 2.0, 2.5, 3.0],
          str([p.ts for p in out]))
    check("重复密文只回放一次", len(out) == 4, str(len(out)))
    check("release 之后缓冲清空", buf.packets == 0 and buf.nbytes == 0)
    check("再次 release 为空", buf.release() == [])


def test_overflow_keeps_prefix() -> None:
    print("[pending] 上限与启动前缀")
    buf = PreKeyBuffer(max_packets=4, prefix_packets=8)
    for i in range(6):
        buf.add(dg(i, float(i)))
    check("主缓冲不超过上限", buf.packets == 4, str(buf.packets))
    check("记账被挤掉的包", buf.dropped == 2, str(buf.dropped))
    check("被挤掉的转存前缀", buf.kept == 2, str(buf.kept))
    out = buf.release()
    check("前缀 + 主缓冲都回放（不丢开头那批快照）", len(out) == 6, str(len(out)))
    check("回放仍然有序", [p.ts for p in out] == [float(i) for i in range(6)])

    small = PreKeyBuffer(max_packets=2, prefix_packets=1)
    for i in range(5):
        small.add(dg(i, float(i)))
    check("前缀也按上限截断", len(small.release()) == 3)


def test_byte_cap_and_ttl() -> None:
    print("[pending] 字节上限与 TTL")
    buf = PreKeyBuffer(max_packets=100, max_bytes=100)
    for i in range(5):
        buf.add(dg(i, float(i), payload=bytes(40)))
    check("字节上限生效", buf.packets <= 2, str(buf.packets))
    check("摘要里有字节数", buf.summary()["bytes"] == buf.nbytes)

    # 世界就绪后才按 TTL 修剪（加载中的大快照可能比 10 秒更久才等到密钥）
    warm = PreKeyBuffer(max_packets=100, ttl_s=1.0, world_ready=True)
    warm.add(dg(0, 1.0))
    warm.add(dg(1, 10.0))
    check("就绪后修剪过期包", warm.packets == 1, str(warm.packets))
    not_ready = PreKeyBuffer(max_packets=100, ttl_s=1.0, world_ready=False)
    not_ready.add(dg(0, 1.0))
    not_ready.add(dg(1, 10.0))
    check("未就绪时不按 TTL 修剪", not_ready.packets == 2, str(not_ready.packets))


def main() -> int:
    test_order_and_dedup()
    test_overflow_keeps_prefix()
    test_byte_cap_and_ttl()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {', '.join(FAILS)}")
        return 1
    print("ALL PASSED（取钥期缓冲离线验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
