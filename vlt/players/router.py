"""每玩家一条翻译会话（分玩家模式的核心）。

## 为什么要 N 条会话

云端实时同传是**单路音频**模型：把两个玩家的声音混着喂进去，它只会吐出一段
混杂的译文，谁也认不出是谁。要「按人出字幕」，只能一人一条会话。

## 这条路由解决的四个现实问题

1. **RPM 是账号级的**（实测 ~10 次/分钟），N 条腿必须**共享**一个
   `ConnectionBudget`，否则第三个人一开口就会撞限流；
2. **说话是稀疏的**：某人在整场里可能只讲 5 秒。所以会话**按需创建**，并限制
   同时活跃的数量（默认 3），超出时踢掉最久没说话的（LRU）；
3. **静音块不该上送**：opus 解码出来会有整段 0（VAD 停顿），按峰值门限挡掉，
   省带宽也省计费；
4. **增量要节流**：每条会话自己一个 `Merger`（首条立即、之后 2s 快照、句末必发），
   与主线的节流口径一致。

## 线程/事件循环

本类**只在事件循环里**用（`async` 方法）。采集线程拿到 PCM 后要
`run_coroutine_threadsafe(router.feed(...), loop)`。
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from ..audio_gate import LevelGate
from ..output.merger import Merger
from ..session.base import SessionConfig, TextDelta, create_session
from ..session.qwen38 import ConnectionBudget

# 原始解码流的静音门限（**RMS**，与主线 `capture.gate_db` 同口径）。
# -55 的含义：实测真实语音的 RMS 25% 分位约 -49 dBFS、5% 分位 -78；-55 只挡掉
# 底噪/极轻声（约 15~20% 的块），不会切掉正常说话。
# （以前用的是**峰值** -60：只能挡掉 8%，大量底噪照送 → 服务端判"没人说话"，
#   实测出现过 `Response stream timeout (60s)`。）
DEFAULT_GATE_DB = -55.0
DEFAULT_HOLD_MS = 500.0          # 越阈值后回落仍继续送（句中的停顿不切碎）
DEFAULT_PREROLL_MS = 250         # 开闸补发阈值之前的一小段（句首辅音不丢）
IDLE_CLOSE_S = 180.0             # 这么久没说话就回收（省连接数）
TICK_S = 0.3
CLOSE_TIMEOUT_S = 5.0            # 关一条会话的上限（关不掉就放弃，别拖住 tick）
#: 账号级建连预算：实测 ~10 次/分钟会被服务端撞掉，9 留一点余量；
#: 再加「相邻 ≥6.1s」（VRCS 实测口径），防同一秒里连开三条。
PLAYER_SESSIONS_PER_MINUTE = 9
PLAYER_MIN_INTERVAL_S = 6.1
#: 建会话失败的退避阶梯（同 VRCS：5/10/15，最后封顶 30s）
RETRY_BACKOFF_S = (5.0, 10.0, 15.0, 30.0)


@dataclass
class PlayerSession:
    actor: int
    session: object
    merger: Merger
    last_audio: float = field(default_factory=time.monotonic)
    last_text: float = 0.0
    last_source: str = ""
    started: bool = False
    failures: int = 0


class PlayerRouter:
    """actor → 会话。对外只暴露 `feed()` 与两个回调。"""

    def __init__(self, cfg: SessionConfig, *, max_sessions: int = 3,
                 gate_db: float = DEFAULT_GATE_DB,
                 hold_ms: float = DEFAULT_HOLD_MS,
                 preroll_ms: int = DEFAULT_PREROLL_MS,
                 on_text=None, on_status=None, label_of=None,
                 budget: ConnectionBudget | None = None,
                 merger_interval_s: float = 2.0,
                 distance_of=None, distance_mode: str = "off",
                 max_distance_m: float = 0.0) -> None:
        self.cfg = cfg
        self.max_sessions = max(1, int(max_sessions))
        self.gate_db = float(gate_db)
        self.hold_ms = float(hold_ms)
        self.preroll_ms = int(preroll_ms)
        # 每位玩家一个门限实例（hold/preroll 有自己的时间轴）
        self._gates: dict[int, LevelGate] = {}
        self.on_text = on_text or (lambda *_a: None)
        self.on_status = on_status or (lambda *_a: None)
        self.label_of = label_of or (lambda actor: f"actor{actor}")
        self.budget = budget or ConnectionBudget(PLAYER_SESSIONS_PER_MINUTE,
                                                 min_interval_s=PLAYER_MIN_INTERVAL_S)
        self.merger_interval_s = merger_interval_s
        # 距离（可选）：off | order | gate | both
        #   order = 会话满了优先踢「更远」的人（同距离再按最久没说话）
        #   gate  = 超过 max_distance_m 的人不建会话
        self.distance_of = distance_of or (lambda _a: None)
        self.distance_mode = str(distance_mode or "off").lower()
        self.max_distance_m = max(0.0, float(max_distance_m or 0.0))
        if self.distance_mode not in ("off", "order", "gate", "both"):
            self.on_status("warn", f"未知距离模式 {self.distance_mode!r} → 按 off 处理")
            self.distance_mode = "off"
        if "gate" in self.distance_mode and self.max_distance_m <= 0:
            self.on_status("warn", "距离门限模式但没有配 max_distance_m → 门限不生效")
            self.distance_mode = self.distance_mode.replace("gate", "").strip() or "off"
        self.sessions: "OrderedDict[int, PlayerSession]" = OrderedDict()
        # 正在建连的 actor → 那个建连任务。**同一个 actor 同时只许有一个在飞** ——
        # 这是修「每 20ms 一块、每块都建一条会话」的根本：
        # feed() 是每块一个独立协程，而 sessions[actor] 要等 start() 握手完才登记，
        # 中间那 0.3~3 秒里每一块都会各自建一条，把 RPM 预算吃光并留下泄漏连接。
        self._starting: dict[int, "asyncio.Task[PlayerSession | None]"] = {}
        # 失败退避：actor → 最早可以再试的时刻 / 连续失败次数
        self._retry_at: dict[int, float] = {}
        self._attempts: dict[int, int] = {}
        self._ticker: asyncio.Task | None = None
        self._closed = False
        self.dropped_chunks = 0
        self.sent_chunks = 0
        self.text_events = 0
        self.distance_dropped = 0
        # 诊断：建连次数 / 失败次数 / 预算排队次数（状态栏与 stats 用）
        self.sessions_created = 0
        self.create_failures = 0

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick_loop(), name="vlt-players-tick")

    async def close(self) -> None:
        self._closed = True
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except (asyncio.CancelledError, Exception):   # noqa: BLE001 - 收尾不抛
                pass
            self._ticker = None
        # 取消在飞的建连：否则「停止翻译」后还会有一条连接在后台悄悄建起来
        pending = list(self._starting.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._starting.clear()
        for st in list(self.sessions.values()):
            try:
                await asyncio.wait_for(st.session.close(), timeout=CLOSE_TIMEOUT_S)
            except Exception:                            # noqa: BLE001
                pass
        self.sessions.clear()

    # ------------------------------------------------------------ 主入口

    async def feed(self, actor: int, pcm: bytes) -> None:
        """喂一个 20ms 的 16k 单声道块。门限以下直接丢（不建会话）。"""
        if self._closed or not pcm:
            return
        gate = self._gate_for(actor)
        blocks = gate.feed(pcm, now=time.monotonic())
        if "gate" in self.distance_mode and self.max_distance_m > 0:
            distance = self.distance_of(actor)
            if distance is not None and distance > self.max_distance_m:
                self.distance_dropped += 1
                return
        if not blocks:
            return
        st = self.sessions.get(actor)
        if st is not None:
            self.sessions.move_to_end(actor)
        else:
            # 建连中 / 退避中：这一块**直接丢**，不排队等网络。
            # （排队等会让「说第一句」卡在预算窗口里几十秒，还会堆出成千上万个等待协程。）
            if actor in self._starting or time.monotonic() < self._retry_at.get(actor, 0.0):
                self.dropped_chunks += len(blocks)
                return
            st = await self._ensure(actor)
            if st is None:
                return
        st.last_audio = time.monotonic()
        try:
            for block in blocks:
                await st.session.send_audio(block)
                self.sent_chunks += 1
        except Exception as exc:                         # noqa: BLE001
            st.failures += 1
            self.on_status("warn", f"玩家 {self.label_of(actor)} 送音频失败：{exc}")
            if st.failures >= 3:
                await self._drop(actor, reason="连续发送失败")
                self._schedule_retry(actor, "连续发送失败")

    def _gate_for(self, actor: int) -> LevelGate:
        gate = self._gates.get(actor)
        if gate is None:
            gate = LevelGate(True, self.gate_db, hold_ms=self.hold_ms,
                             preroll_ms=self.preroll_ms, verbose=False)
            self._gates[actor] = gate
        return gate

    # ------------------------------------------------------------ 会话管理

    async def _ensure(self, actor: int) -> PlayerSession | None:
        """确保这个 actor 有会话；**并发调用只会建一条**。

        调用方（第一个）等建连完成；其余调用方在 `feed` 里就按「建连中」丢块了，
        不会走到这儿。返回 None = 这次没建成（退避中 / 被距离门限拒绝 / 建连失败）。
        """
        st = self.sessions.get(actor)
        if st is not None:
            self.sessions.move_to_end(actor)
            return st
        if actor in self._starting:
            return None
        if time.monotonic() < self._retry_at.get(actor, 0.0):
            return None
        task = asyncio.create_task(self._create(actor))
        self._starting[actor] = task

        def _done(t: asyncio.Task, a: int = actor) -> None:
            if self._starting.get(a) is t:
                self._starting.pop(a, None)

        task.add_done_callback(_done)
        return await asyncio.shield(task)

    async def _create(self, actor: int) -> PlayerSession | None:
        """真正建一条会话（同一次只会有这一个在飞）。"""
        if not await self._make_room(actor):
            return None
        merger = Merger(lambda text, final, a=actor: self._emit(a, text, final),
                        interval_s=self.merger_interval_s)
        session = None
        try:
            session = create_session(self.cfg, budget=self.budget)
            await session.start(on_text=lambda d, a=actor: self._on_delta(a, d),
                                on_audio=None, on_usage=None)
        except Exception as exc:                         # noqa: BLE001
            # 半成品必须自己关掉：不关就是「没人持引用、也没人关」的泄漏连接，
            # 它还会一直占着账号的并发名额。
            if session is not None:
                try:
                    await asyncio.wait_for(session.close(), timeout=CLOSE_TIMEOUT_S)
                except Exception:                        # noqa: BLE001
                    pass
            self._schedule_retry(actor, exc)
            return None
        st = PlayerSession(actor=actor, session=session, merger=merger, started=True)
        self.sessions[actor] = st
        self.sessions_created += 1
        self._attempts.pop(actor, None)
        self._retry_at.pop(actor, None)
        self.on_status("info", f"已为 {self.label_of(actor)} 建立翻译会话"
                               f"（{len(self.sessions)}/{self.max_sessions}）")
        return st

    async def _make_room(self, actor: int) -> bool:
        """腾一个名额出来；腾不了（或不该腾）返回 False。"""
        # 在飞的建连也占名额 —— 否则三个人同时开口会各自看到「还没满」，一起冲爆上限。
        # 减 1 是扣掉 `_create` 自己这一条。
        in_flight = max(0, len(self._starting) - 1)
        if len(self.sessions) + in_flight < self.max_sessions:
            return True
        if not self.sessions:
            return False                 # 名额全被正在建的占着：这次先不建
        if "order" in self.distance_mode:
            # 先按"谁更远"淘汰；距离相同再按"谁更久没说话"。
            # 新人如果比所有人都远，就不值得踢人 —— 直接不给他建会话。
            new_d = self.distance_of(actor)
            far_actor = max(
                self.sessions,
                key=lambda a: ((self.distance_of(a) or 0.0),
                               -self.sessions[a].last_audio))
            far_d = self.distance_of(far_actor) or 0.0
            if new_d is not None and new_d > far_d:
                return False
            await self._drop(far_actor,
                             reason=f"距离排序：{self.label_of(far_actor)} 更远"
                                    f"（{max_distance_label(far_d)}）")
        else:
            victim = min(self.sessions, key=lambda a: self.sessions[a].last_audio)
            await self._drop(victim, reason=f"超过 {self.max_sessions} 条活跃会话")
        return True

    def _schedule_retry(self, actor: int, reason: object) -> None:
        """记一次失败并按 5/10/15/30s 退避；期间 feed 直接丢块，不再试。"""
        attempts = self._attempts.get(actor, 0) + 1
        self._attempts[actor] = attempts
        delay = RETRY_BACKOFF_S[min(attempts - 1, len(RETRY_BACKOFF_S) - 1)]
        self._retry_at[actor] = time.monotonic() + delay
        self.create_failures += 1
        self.on_status("warn", f"玩家 {self.label_of(actor)} 会话不可用"
                               f"（{delay:.0f}s 后重试，第 {attempts} 次）：{reason}")

    async def _drop(self, actor: int, *, reason: str = "") -> None:
        st = self.sessions.pop(actor, None)
        if st is None:
            return
        try:
            await asyncio.wait_for(st.session.close(), timeout=CLOSE_TIMEOUT_S)
        except Exception:                                # noqa: BLE001
            pass
        if reason:
            self.on_status("info", f"关闭 {self.label_of(actor)} 的会话：{reason}")

    # ------------------------------------------------------------ 文本

    def _on_delta(self, actor: int, delta: TextDelta) -> None:
        st = self.sessions.get(actor)
        if st is None:
            return
        st.last_text = time.monotonic()
        st.last_source = delta.source or st.last_source
        st.merger.push(delta)

    def _emit(self, actor: int, text: str, is_final: bool) -> None:
        st = self.sessions.get(actor)
        source = st.last_source if st is not None else ""
        self.text_events += 1
        self.on_text(actor, self.label_of(actor), source, text, is_final)

    # ------------------------------------------------------------ 定时任务

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            await self._tick_once()

    async def _tick_once(self) -> None:
        """一次例行检查（拆出来是为了能离线测，不用等 0.3s）。"""
        now = time.monotonic()
        for actor, st in list(self.sessions.items()):
            try:
                st.session.tick()
            except Exception:                            # noqa: BLE001
                pass
            # 会话被服务端掐掉（限流/1011/网络）时**立刻**重建，别继续往死连接送音频。
            # 之前没人查 is_alive，于是要等连发失败 3 次才丢 —— 期间每次 feed 都在
            # 报「送音频失败」，正是"一直在刷错"的另一半。
            if not bool(getattr(st.session, "is_alive", True)):
                await self._drop(actor, reason="连接已断开")
                self._schedule_retry(actor, "连接已断开")
                continue
            # 长时间没说话：回收，给下一位腾连接预算
            if now - max(st.last_audio, st.last_text) > IDLE_CLOSE_S:
                await self._drop(actor, reason=f"静默 {IDLE_CLOSE_S:.0f}s")
        # 退避表只留还在起作用的：玩家走了别把 dict 撑大
        if len(self._retry_at) > 64:
            for actor, at in list(self._retry_at.items()):
                if at < now and actor not in self.sessions:
                    self._retry_at.pop(actor, None)
                    self._attempts.pop(actor, None)

    def stats(self) -> dict:
        return {
            "sessions": len(self.sessions),
            "starting": len(self._starting),
            "actors": list(self.sessions),
            "sent_chunks": self.sent_chunks,
            "dropped_chunks": self.dropped_chunks,
            "gated_blocks": sum(g.dropped_chunks for g in self._gates.values()),
            "text_events": self.text_events,
            "distance_dropped": self.distance_dropped,
            "distance_mode": self.distance_mode,
            "max_distance_m": self.max_distance_m,
            "sessions_created": self.sessions_created,
            "create_failures": self.create_failures,
            "budget_waits": self.budget.waits,
        }


def max_distance_label(meters: float) -> str:
    """路由器日志里的距离文案（与 positions.format_distance 保持同一口径）。"""
    from .positions import format_distance
    return format_distance(meters) or "未知"
