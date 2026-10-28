"""每玩家一条翻译会话（分玩家模式的核心）。

## 为什么要 N 条会话

云端实时同传是**单路音频**模型：把两个玩家的声音混着喂进去，它只会吐出一段
混杂的译文，谁也认不出是谁。要「按人出字幕」，只能一人一条会话。

## 这条路由解决的五个现实问题

1. **RPM 是账号级的**（实测 ~10 次/分钟），N 条腿必须**共享**一个
   `ConnectionBudget`；新建连接还要摊到 ≥6.1s 一次才稳；
2. **说话是稀疏的**：某人在整场里可能只讲 5 秒。所以会话**按需创建**，并限制
   同时活跃的数量（默认 4），超出时只回收**真的空闲**的人；
3. **建连必须串行**：一次只说一句话的房间里，6 个人同时开口会想建 6 条连接，
   而账号一分钟只允许 ~10 条。串行 + 愿望清单把「谁先建」变成时间问题，
   而不是「谁的协程先跑到」（否则就是实测过的建连风暴 + 互相踢）；
4. **静音块不该上送**：opus 解码出来会有整段 0（VAD 停顿），按 RMS 门限挡掉，
   省带宽也省计费；
5. **增量要节流**：每条会话自己一个 `Merger`（首条立即、之后 2s 快照、句末必发），
   与主线的节流口径一致。

## 名额策略（2026-10 真机日志逼出来的）

实测事故：6 个人同时说话、`max_sessions=3` 时，每个新开口的人都要建会话，而建连
要排队 6 秒；排队期间「正在建」也被算成占名额，于是**唯一一条真实会话**被反复踢掉
（日志：`会话 0（建连 21 / 预算排队 22）`，90% 语音被丢，10 分钟只出 8 条字幕）。

现在：

* 名额**只算真实会话**，排队中的建连不占名额；
* 只有「有空位」或「存在 >15s 没音频的空闲会话」才建；全都还在说话就**不踢人**，
  新人的音频丢弃并按 30s 频率警告（`overflow_dropped`）——宁可不翻译一个人，
  也不要把正在说话的三个人一起打断；
* 愿望清单按最近说话时间排序，3s 没再说话自动作废；拿到预算后**二次确认**
  他还在说，否则放弃这次建连（不白烧一次连接）；
* 排队期间保留最近 **2 秒**音频，会话一就绪立刻补发（句首不丢）。

## 线程/事件循环

本类**只在事件循环里**用（`async` 方法）。采集线程拿到 PCM 后要
`run_coroutine_threadsafe(router.feed(...), loop)`。
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
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
#: 这么多秒没音频才算「空闲、可以回收」。低于它一律算「正在说话」，绝不踢。
HOT_SILENCE_S = 15.0
#: 这么久没说话就回收会话。**必须小于服务端的 60s 响应流超时** ——
#: 实测保留 180s 时，玩家静下来一分钟就会收到
#: `Response stream timeout (timeout_seconds=60)`，等于白报一次错 + 白重建一次。
IDLE_CLOSE_S = 45.0
TICK_S = 0.3
CLOSE_TIMEOUT_S = 5.0            # 关一条会话的上限（关不掉就放弃，别拖住 tick）
#: 排队等建连时保留多少块音频（20ms/块 → 100 块 = 2s，句首不丢）
PREROLL_BLOCKS = 100
#: 愿望有效期：这么久没再说话就不再为他排队
WANT_TTL_S = 3.0
#: 同一玩家「名额满、暂不翻译」的警告间隔（不然每块 20ms 都要报一次）
OVERFLOW_WARN_S = 30.0
#: 距离排序时的滞回：新来的必须比被踢的近这么多米才值得换人（防来回抖动）
DISTANCE_HYSTERESIS_M = 1.0
#: 账号级建连预算：实测 ~10 次/分钟会被服务端撞掉，9 留一点余量；
#: 再加「相邻 ≥6.1s」（VRCS 实测口径），防同一秒里连开三条。
PLAYER_SESSIONS_PER_MINUTE = 9
PLAYER_MIN_INTERVAL_S = 6.1
#: 建会话失败的退避阶梯（同 VRCS：5/10/15，最后封顶 30s）
RETRY_BACKOFF_S = (5.0, 10.0, 15.0, 30.0)
#: 同时最多几条活跃会话（config `players.max_sessions` 覆盖）
DEFAULT_MAX_SESSIONS = 4


class _GrantedBudget:
    """已经付过预算的占位对象。

    `QwenLiveTranslateSession.start()` 里会 `await budget.acquire()`。本路由器
    自己先 acquire 过一次（为了在等预算的 6 秒里还能二次确认「他还在说话」），
    所以传给会话的必须是一个**不再计费**的对象。
    """

    waits = 0
    acquired = 0

    async def acquire(self) -> None:
        return


_GRANTED = _GrantedBudget()


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

    def __init__(self, cfg: SessionConfig, *,
                 max_sessions: int = DEFAULT_MAX_SESSIONS,
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
        # 愿望清单：actor → 最近一次「有音频但还没有会话」的时刻（monotonic）。
        # 建连是串行的，所以「谁先建」由这个时间排序决定，而不是谁先抢到协程。
        self._wanted: "OrderedDict[int, float]" = OrderedDict()
        # 排队期间的音频（每人最近 2s）：会话一就绪立刻补发，句首不丢。
        self._pre: dict[int, deque[bytes]] = {}
        # 失败退避：actor → 最早可以再试的时刻 / 连续失败次数
        self._retry_at: dict[int, float] = {}
        self._attempts: dict[int, int] = {}
        # 「名额满、暂不翻译」的限频警告时刻
        self._overflow_warned: dict[int, float] = {}
        self._wake: asyncio.Event | None = None
        self._creator: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None
        self._busy = False
        self._closed = False
        self.dropped_chunks = 0
        self.sent_chunks = 0
        self.text_events = 0
        self.distance_dropped = 0
        # 诊断：建连次数 / 失败次数 / 溢出丢弃块数（状态栏与 stats 用）
        self.sessions_created = 0
        self.create_failures = 0
        self.overflow_dropped = 0

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick_loop(), name="vlt-players-tick")
        if self._creator is None:
            self._creator = asyncio.create_task(self._creator_loop(),
                                                name="vlt-players-create")

    async def close(self) -> None:
        self._closed = True
        for task in (self._ticker, self._creator):
            if task is not None:
                task.cancel()
        for task in (self._ticker, self._creator):
            if task is None:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001 - 收尾不抛
                pass
        self._ticker = self._creator = None
        for st in list(self.sessions.values()):
            try:
                await asyncio.wait_for(st.session.close(), timeout=CLOSE_TIMEOUT_S)
            except Exception:                            # noqa: BLE001
                pass
        self.sessions.clear()
        self._wanted.clear()
        self._pre.clear()

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
            await self._send(actor, st, blocks)
            return
        now = time.monotonic()
        if now < self._retry_at.get(actor, 0.0):
            # 退避中：直接丢，别排队（否则又是 20ms 一个等待协程）
            self.dropped_chunks += len(blocks)
            return
        # 没有会话：登记愿望 + 缓冲最近 2s（句首不丢），交给建连循环
        self._wanted[actor] = now
        self._wanted.move_to_end(actor)
        self._buffer(actor, blocks)
        self._kick()

    def _gate_for(self, actor: int) -> LevelGate:
        gate = self._gates.get(actor)
        if gate is None:
            gate = LevelGate(True, self.gate_db, hold_ms=self.hold_ms,
                             preroll_ms=self.preroll_ms, verbose=False)
            self._gates[actor] = gate
        return gate

    def _buffer(self, actor: int, blocks: list[bytes]) -> None:
        pre = self._pre.get(actor)
        if pre is None:
            pre = deque(maxlen=PREROLL_BLOCKS)
            self._pre[actor] = pre
        pre.extend(blocks)

    async def _send(self, actor: int, st: PlayerSession, blocks) -> None:
        st.last_audio = time.monotonic()
        try:
            for block in blocks:
                await st.session.send_audio(block)
                self.sent_chunks += 1
            st.failures = 0
        except Exception as exc:                         # noqa: BLE001
            st.failures += 1
            self.on_status("warn", f"玩家 {self.label_of(actor)} 送音频失败：{exc}")
            if st.failures >= 3:
                await self._drop(actor, reason="连续发送失败")
                self._schedule_retry(actor, "连续发送失败")

    # ------------------------------------------------------------ 建连循环

    def _kick(self) -> None:
        if self._wake is not None and not self._closed:
            self._wake.set()

    async def _creator_loop(self) -> None:
        """唯一的建连者：串行、可合并、可取消。"""
        assert self._wake is not None
        while True:
            await self._wake.wait()
            self._wake.clear()
            # 合并同一波音频的连续唤醒（每 20ms 一块，别一块跑一次）
            await asyncio.sleep(0.2)
            self._wake.clear()
            if self._closed:
                return
            try:
                await self._create_next()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                     # noqa: BLE001 - 循环不能死
                self.on_status("warn", f"建会话循环异常：{type(exc).__name__}: {exc}")

    async def _create_next(self) -> None:
        """为「最近的说话者」建一条会话（如果名额/预算允许）。"""
        now = time.monotonic()
        self._prune_wanted(now)
        if not self._wanted or self._busy:
            return
        actor = next(reversed(self._wanted))
        if now < self._retry_at.get(actor, 0.0):
            self._drop_wish(actor)
            return
        if not await self._ensure_room(actor, now):
            self._warn_overflow(actor, now)
            return

        self._busy = True
        try:
            # 先自己拿预算：等 6.1s 的这段时间里他可能已经说完了，
            # 那时就**不要**建（白烧一次连接，还会占一个名额）。
            await self.budget.acquire()
            spoken = self._wanted.get(actor)
            if spoken is None or time.monotonic() - spoken > WANT_TTL_S:
                self._drop_wish(actor)
                return
            self._wanted.pop(actor, None)
            await self._start_session(actor)
        finally:
            self._busy = False
            # 还有人在等就再跑一轮（下一轮会重新排队预算）
            if self._wanted and not self._closed:
                self._kick()

    async def _start_session(self, actor: int) -> None:
        merger = Merger(lambda text, final, a=actor: self._emit(a, text, final),
                        interval_s=self.merger_interval_s)
        session = None
        try:
            session = create_session(self.cfg, budget=_GRANTED)
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
            self._drop_wish(actor, counted=False)
            self._schedule_retry(actor, exc)
            return
        st = PlayerSession(actor=actor, session=session, merger=merger, started=True)
        self.sessions[actor] = st
        self.sessions_created += 1
        self._attempts.pop(actor, None)
        self._retry_at.pop(actor, None)
        self._overflow_warned.pop(actor, None)
        self.on_status("info", f"已为 {self.label_of(actor)} 建立翻译会话"
                               f"（{len(self.sessions)}/{self.max_sessions}）")
        # 补发排队期间的音频（含句首 2s），这一段播放完就进入实时
        pre = self._pre.pop(actor, None)
        if pre:
            await self._send(actor, st, list(pre))

    async def _ensure_room(self, actor: int, now: float) -> bool:
        """判断/腾出一个名额。腾不了返回 False（调用方按"溢出"处理）。"""
        if len(self.sessions) < self.max_sessions:
            return True
        idle = [a for a, st in self.sessions.items()
                if now - max(st.last_audio, st.last_text or 0.0) > HOT_SILENCE_S]
        if not idle:
            return False                     # 全都在说话：不踢人，宁可暂时不翻
        if "order" in self.distance_mode:
            victim = max(idle, key=lambda a: (self.distance_of(a) or 0.0,
                                              -self.sessions[a].last_audio))
            new_d = self.distance_of(actor)
            far_d = self.distance_of(victim) or 0.0
            if new_d is not None and new_d + DISTANCE_HYSTERESIS_M >= far_d:
                return False                 # 不比被踢的近，不值得换人
        else:
            victim = min(idle, key=lambda a: self.sessions[a].last_audio)
        await self._drop(victim, reason=f"名额已满，回收空闲的 {self.label_of(victim)}")
        return True

    def _prune_wanted(self, now: float) -> None:
        for a in [a for a, t in self._wanted.items() if now - t > WANT_TTL_S]:
            self._drop_wish(a)
        for a in [a for a in self._wanted if a in self.sessions]:
            self._wanted.pop(a, None)

    def _drop_wish(self, actor: int, *, counted: bool = True) -> None:
        """放弃为这个玩家排队；排队期缓冲的音频算「溢出丢弃」。"""
        self._wanted.pop(actor, None)
        pre = self._pre.pop(actor, None)
        if counted and pre:
            self.overflow_dropped += len(pre)

    def _warn_overflow(self, actor: int, now: float) -> None:
        last = self._overflow_warned.get(actor, 0.0)
        if now - last < OVERFLOW_WARN_S:
            return
        self._overflow_warned[actor] = now
        self.on_status(
            "warn",
            f"{self.label_of(actor)} 的语音暂不翻译：{self.max_sessions} 条会话都在被人"
            f"使用（可调大 config 的 players.max_sessions；未翻的块会累计在「溢出丢弃」）")

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
        self._pre.pop(actor, None)
        self._wanted.pop(actor, None)
        try:
            await asyncio.wait_for(st.session.close(), timeout=CLOSE_TIMEOUT_S)
        except Exception:                                # noqa: BLE001
            pass
        if reason:
            self.on_status("info", f"关闭 {self.label_of(actor)} 的会话：{reason}")
        self._kick()          # 空出来的名额立刻给排队的人

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
            if not bool(getattr(st.session, "is_alive", True)):
                await self._drop(actor, reason="连接已断开")
                self._schedule_retry(actor, "连接已断开")
                continue
            # 长时间没说话：回收，给下一位腾名额（也是**赶在服务端 60s 超时之前**）
            if now - max(st.last_audio, st.last_text or 0.0) > IDLE_CLOSE_S:
                await self._drop(actor, reason=f"静默 {IDLE_CLOSE_S:.0f}s")
        self._prune_wanted(now)
        if self._wanted and not self._busy:
            self._kick()
        # 诊断用的 dict 别无限长
        if len(self._retry_at) > 64:
            for actor, at in list(self._retry_at.items()):
                if at < now and actor not in self.sessions:
                    self._retry_at.pop(actor, None)
                    self._attempts.pop(actor, None)
        if len(self._overflow_warned) > 64:
            for actor, at in list(self._overflow_warned.items()):
                if now - at > 300 and actor not in self.sessions:
                    self._overflow_warned.pop(actor, None)

    def stats(self) -> dict:
        return {
            "sessions": len(self.sessions),
            "max_sessions": self.max_sessions,
            "creating": int(self._busy),
            "queued": len(self._wanted),
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
            "overflow_dropped": self.overflow_dropped,
            "budget_waits": self.budget.waits,
        }
