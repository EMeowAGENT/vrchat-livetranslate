#!/usr/bin/env python
"""分玩家路由器的离线验收（不需要 VRChat、不需要网络、不需要 root）。

## 钉住的行为

1. 门限以下的块**不建会话**（省钱）；
2. 第一次有人说话才建会话，之后复用；
3. 同时活跃的会话数受限，超出按 LRU 踢掉最久没说话的那个；
4. 文本增量经 `Merger` 变成 `(actor, 名字, 原文, 译文, is_final)`；
5. 所有会话共享**同一个** RPM 预算（N 条腿不能各自算一份）。

跑法：xvfb-run -a .venv/bin/python tests/test_players_router.py（不需要显示，加 xvfb 只是沿用仓库口径）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import router as router_mod  # noqa: E402
from vlt.session.base import SessionConfig, TextDelta  # noqa: E402
from vlt.session.qwen38 import ConnectionBudget  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


LOUD = (10000).to_bytes(2, "little", signed=True) * 320      # 20ms @16k
QUIET = b"\x00\x00" * 320


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[int] = []
        self.closed = False
        self.on_text = None
        self.budget = None

    async def start(self, on_text, on_audio=None, on_usage=None) -> None:
        self.on_text = on_text

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(len(pcm))

    def tick(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


async def _case() -> None:
    created: list[FakeSession] = []
    original = router_mod.create_session

    def fake_create(cfg, *, budget=None):        # noqa: ANN001
        session = FakeSession()
        session.budget = budget
        created.append(session)
        return session

    router_mod.create_session = fake_create
    try:
        texts: list[tuple] = []
        budget = ConnectionBudget(5)
        router = router_mod.PlayerRouter(
            SessionConfig(api_key="x"), max_sessions=1, gate_db=-60.0,
            on_text=lambda *a: texts.append(a), label_of=lambda a: f"玩家{a}",
            budget=budget)
        await router.start()

        print("[router] 门限 / 建会话 / 复用")
        await router.feed(1, QUIET)
        check("静音不建会话", created == [], str(len(created)))
        await router.feed(1, LOUD)
        check("有人说话才建会话", len(created) == 1, str(len(created)))
        # 开闸时会把 preroll（阈值之前的 250ms）一并补发 —— 所以可能是 2 块，
        # 这是**要的**（句首辅音不丢），不是 bug。
        check("音频被送进会话（含 preroll 补发）",
              bool(created[0].sent) and created[0].sent[-1] == len(LOUD),
              str(created[0].sent))
        await router.feed(1, LOUD)
        check("同一个 actor 复用会话", len(created) == 1, str(len(created)))
        check("共用注入的预算", created[0].budget is budget)

        print("[router] 文本增量")
        created[0].on_text(TextDelta(confirmed="你好", source="hello", is_final=False))
        check("首条增量立即上抛", len(texts) == 1, str(texts))
        check("带上 actor / 名字", texts and texts[0][0] == 1 and texts[0][1] == "玩家1",
              str(texts[:1]))
        check("带上原文", texts and texts[0][2] == "hello", str(texts[:1]))
        check("带上译文", texts and texts[0][3] == "你好", str(texts[:1]))

        print("[router] LRU 淘汰")
        await router.feed(2, LOUD)
        check("第二个 actor 建了新会话", len(created) == 2, str(len(created)))
        check("超出上限时踢掉最久没说话的", created[0].closed, "旧会话没关")
        check("router 只留一条", len(router.sessions) == 1, str(list(router.sessions)))

        await router.close()
        check("收尾关掉全部会话", created[-1].closed)
    finally:
        router_mod.create_session = original


async def _case_distance() -> None:
    created: list[FakeSession] = []
    original = router_mod.create_session

    def fake_create(cfg, *, budget=None):        # noqa: ANN001
        session = FakeSession()
        created.append(session)
        return session

    router_mod.create_session = fake_create
    try:
        dists = {1: 5.0, 2: 50.0, 3: 2.0}
        print("[router] 距离门限")
        gate = router_mod.PlayerRouter(
            SessionConfig(api_key="x"), max_sessions=2, gate_db=-60.0,
            on_text=lambda *a: None, label_of=lambda a: f"P{a}",
            distance_mode="gate", max_distance_m=10.0,
            distance_of=lambda a: dists.get(a))
        await gate.start()
        await gate.feed(2, LOUD)
        check("超过门限的人不建会话", created == [], str(len(created)))
        check("门限丢弃计数", gate.distance_dropped == 1, str(gate.distance_dropped))
        await gate.feed(1, LOUD)
        check("门限内的人正常建会话", len(created) == 1, str(len(created)))

        print("[router] 距离排序（满了先踢更远的）")
        near_far = router_mod.PlayerRouter(
            SessionConfig(api_key="x"), max_sessions=1, gate_db=-60.0,
            on_text=lambda *a: None, label_of=lambda a: f"P{a}",
            distance_mode="order", distance_of=lambda a: dists.get(a))
        await near_far.start()
        await near_far.feed(1, LOUD)      # 5m，占住唯一名额
        check("第一个人建了会话", len(created) == 2, str(len(created)))
        await near_far.feed(2, LOUD)      # 50m：比现有的人更远 → 不该踢人
        check("更远的人不值得踢掉现有的", len(near_far.sessions) == 1
              and 1 in near_far.sessions, str(list(near_far.sessions)))
        check("更远的人不建会话", len(created) == 2, str(len(created)))
        await near_far.feed(3, LOUD)      # 2m：比现有的近 → 踢掉 5m 的
        check("更近的人会替换更远的", list(near_far.sessions) == [3],
              str(list(near_far.sessions)))
        check("被替换的会话已关闭", created[1].closed, "旧会话没关")

        await gate.close()
        await near_far.close()
    finally:
        router_mod.create_session = original


def main() -> int:
    asyncio.run(_case())
    asyncio.run(_case_distance())
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（分玩家路由器）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
