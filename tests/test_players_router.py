#!/usr/bin/env python
"""分玩家路由器的离线验收（不需要 VRChat、不需要网络、不需要 root）。

## 钉住的行为

1. 门限以下的块**不建会话**（省钱）；
2. 第一次有人说话才建会话，之后复用；
3. 同时活跃的会话数受限，超出按 LRU 踢掉最久没说话的那个；
4. 文本增量经 `Merger` 变成 `(actor, 名字, 原文, 译文, is_final)`；
5. 所有会话共享**同一个** RPM 预算（N 条腿不能各自算一份）；
6. ★ 并发块只建**一条**会话（每个 20ms 块是一个独立协程）；
7. 建会话失败按 5/10/15/30s 退避，半成品必须关掉；
8. 会话被服务端掐掉后 tick 立刻发现，退避后重建；
9. 预算同时管「条数」和「相邻建连最小间隔」。

跑法：xvfb-run -a .venv/bin/python tests/test_players_router.py（不需要显示，加 xvfb 只是沿用仓库口径）
"""
from __future__ import annotations

import asyncio
import sys
import time
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
        self.alive = True

    @property
    def is_alive(self) -> bool:
        return self.alive

    async def start(self, on_text, on_audio=None, on_usage=None) -> None:
        self.on_text = on_text

    async def send_audio(self, pcm: bytes) -> None:
        if not self.alive:
            raise RuntimeError("连接已断")
        self.sent.append(len(pcm))

    def tick(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class FailingSession(FakeSession):
    """start() 就失败：用来钉「失败退避 + 半成品必须关掉」。"""

    async def start(self, on_text, on_audio=None, on_usage=None) -> None:
        raise RuntimeError("服务端拒绝：Throttling.RateQuota")


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


async def _case_concurrent_start() -> None:
    """★ 每个 20ms 块一个协程：并发进来只能建**一条**会话。

    真机现象（修复前）：一个人刚开口，一次 start() 握手期间几十块各自建一条，
    4/min 的预算瞬间吃光 → 日志每 20ms 刷一行「连接预算已满」，
    多出来的连接没人引用也没人关。
    """
    created: list[FakeSession] = []
    original = router_mod.create_session

    def fake_create(cfg, *, budget=None):        # noqa: ANN001
        session = FakeSession()
        created.append(session)
        return session

    router_mod.create_session = fake_create
    try:
        print("[router] 并发建会话（一人说话的 1 秒 = 50 块）")
        router = router_mod.PlayerRouter(
            SessionConfig(api_key="x"), max_sessions=3, gate_db=-60.0,
            on_text=lambda *a: None, label_of=lambda a: f"玩家{a}",
            budget=ConnectionBudget(100))
        await router.start()
        await asyncio.gather(*(router.feed(1, LOUD) for _ in range(50)))
        check("50 个并发块只建 1 条会话", len(created) == 1, str(len(created)))
        check("其余块直接丢掉（不排队等网络）", router.dropped_chunks > 0,
              str(router.dropped_chunks))
        check("会话表只有一条", len(router.sessions) == 1, str(list(router.sessions)))
        check("stats 里有建连计数", router.stats()["sessions_created"] == 1,
              str(router.stats()))
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_retry_backoff() -> None:
    """建会话失败：按 5/10/15/30s 退避，且半成品必须关掉（不留泄漏连接）。"""
    created: list[FakeSession] = []
    status: list[str] = []
    original = router_mod.create_session

    def fake_create(cfg, *, budget=None):        # noqa: ANN001
        session = FailingSession()
        created.append(session)
        return session

    router_mod.create_session = fake_create
    try:
        print("[router] 建会话失败 → 退避 + 关半成品")
        router = router_mod.PlayerRouter(
            SessionConfig(api_key="x"), max_sessions=3, gate_db=-60.0,
            on_text=lambda *a: None, label_of=lambda a: f"玩家{a}",
            on_status=lambda lvl, msg: status.append(msg),
            budget=ConnectionBudget(100))
        await router.start()
        await router.feed(1, LOUD)
        check("失败只试一次", len(created) == 1, str(len(created)))
        check("半成品被关掉（不留泄漏连接）", created[0].closed)
        first = router._retry_at.get(1, 0.0) - time.monotonic()   # noqa: SLF001
        check("第一次退避 ≈5s", 4.0 < first <= 5.0, f"{first:.2f}s")
        await router.feed(1, LOUD)
        check("退避期内不再试", len(created) == 1, str(len(created)))
        router._retry_at[1] = 0.0                                # noqa: SLF001
        await router.feed(1, LOUD)
        check("退避到期后重试", len(created) == 2, str(len(created)))
        second = router._retry_at.get(1, 0.0) - time.monotonic()  # noqa: SLF001
        check("失败阶梯递增到 ~10s", 9.0 < second <= 10.0, f"{second:.2f}s")
        check("状态栏给了原因与等待秒数",
              any("5s 后重试" in m for m in status), str(status[:2]))
        check("失败计数", router.stats()["create_failures"] == 2,
              str(router.stats()))
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_dead_session_rebuild() -> None:
    """会话被服务端掐掉 → tick 立刻发现并重建（受退避约束），不继续往死连接送音频。"""
    created: list[FakeSession] = []
    original = router_mod.create_session

    def fake_create(cfg, *, budget=None):        # noqa: ANN001
        session = FakeSession()
        created.append(session)
        return session

    router_mod.create_session = fake_create
    try:
        print("[router] 断线检测 → 退避后重建")
        router = router_mod.PlayerRouter(
            SessionConfig(api_key="x"), max_sessions=3, gate_db=-60.0,
            on_text=lambda *a: None, label_of=lambda a: f"玩家{a}",
            budget=ConnectionBudget(100))
        await router.start()
        await router.feed(1, LOUD)
        check("先有一条会话", len(created) == 1 and 1 in router.sessions)
        created[0].alive = False
        await router._tick_once()                                # noqa: SLF001
        check("断线会话被移除", 1 not in router.sessions, str(list(router.sessions)))
        check("断线会话被关闭", created[0].closed)
        await router.feed(1, LOUD)
        check("退避期内不重建", len(created) == 1, str(len(created)))
        router._retry_at[1] = 0.0                                # noqa: SLF001
        await router.feed(1, LOUD)
        check("退避到期后重建", len(created) == 2 and 1 in router.sessions,
              str(len(created)))
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_budget() -> None:
    """预算的两道闸：条数窗口 + 相邻建连最小间隔。"""
    print("[router] 建连预算：最大间隔与排队计数")
    budget = ConnectionBudget(9, min_interval_s=0.12)
    t0 = time.monotonic()
    await budget.acquire()
    first = time.monotonic() - t0
    check("第一条不必等", first < 0.05, f"{first:.3f}s")
    await budget.acquire()
    gap = time.monotonic() - t0
    check("第二条按最小间隔排队", gap >= 0.10, f"{gap:.3f}s")
    check("排队/成功计数", budget.waits == 1 and budget.acquired == 2,
          f"waits={budget.waits} acquired={budget.acquired}")

    tight = ConnectionBudget(1)
    tight._stamps.append(time.monotonic() - 61.0)     # noqa: SLF001 - 伪造一分钟前的记录
    t = time.monotonic()
    await tight.acquire()
    check("超过 60s 的旧记录会被清掉", time.monotonic() - t < 0.05,
          f"{time.monotonic() - t:.3f}s")


def main() -> int:
    asyncio.run(_case())
    asyncio.run(_case_distance())
    asyncio.run(_case_concurrent_start())
    asyncio.run(_case_retry_backoff())
    asyncio.run(_case_dead_session_rebuild())
    asyncio.run(_case_budget())
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（分玩家路由器）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
