"""会话重连竞态：**「连上了但 session.update 还没下发」的窗口里绝不能上送音频**。

## 真机事故（用户日志，175 分钟）

    重连 → 采集泵每 ~100ms 一次的 input_audio_buffer.append 抢在 session.update 之前
    → 服务端按**默认会话**把这一轮 start 掉
    → 随后到达的 session.update 被拒：`Session update error: session already started
      or finished or failed.` → 1007 断开 → 再重连 → 采集泵又撞进同一个窗口
    → 会话风暴：107 个会话 / 63 次断线，会话寿命中位 7.5s

## 三件事一起验

(a) `start()` 期间（已连上、`session.update` 还没发出）`send_audio` 抛 `SessionNotReady`，
    并且连线上**一个 append 都没有**（这一块还留在麦克风侧，不是「发送失败」）；
(b) `start()` 返回后 append 排在 `session.update` **之后** —— 顺序是硬要求，
    反了就是上面那条 1007；
(c) `is_ready` 从 False → True；`_SessionProxy` 见到 not-ready 只计数丢弃，
    **不**记进 `send_fails`、也不打「发送音频失败」那行（那是「发了但失败」的口径）。

离线可跑：假 ws（`recv()` 卡在 Event 上）+ 假 websockets 模块，不连网络、不需要 key。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ================================================================ 假连接

class _FakeWS:
    """假 ws：`recv()` 阻塞到测试放行，`send()` 按序把事件记下来。

    `__aiter__` 永不产出（只等被 cancel）—— 真接收循环读的是服务端推来的事件，
    本用例只关心**发**出去的东西。
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.recv_entered = asyncio.Event()   # start() 已经进到「等 session.created」
        self.release = asyncio.Event()        # 放行 recv() → start() 继续发 session.update
        self.closed = False

    async def recv(self) -> str:
        self.recv_entered.set()
        await self.release.wait()
        return json.dumps({"type": "session.created", "session": {}})

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):                      # noqa: ANN204
        return self

    async def __anext__(self):                # noqa: ANN204
        await asyncio.sleep(3600)             # 永不产出：等 _recv_task 被 cancel
        raise StopAsyncIteration

    def types(self) -> list[str]:
        return [str(e.get("type", "")) for e in self.sent]


class _FakeWSModule:
    """冒充 `websockets` 模块：只要一个 `connect()`。"""

    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    async def connect(self, *_a, **_kw):      # noqa: ANN002, ANN003, ANN201
        return self._ws


# ================================================================ 场景

async def _scenario() -> dict:
    """跑一遍「start 期间送音频 → 放行 → start 完成 → 再送音频」，返回观测结果。"""
    from vlt.session import qwen38
    from vlt.session.base import SessionConfig

    ws = _FakeWS()
    original = qwen38.websockets
    qwen38.websockets = _FakeWSModule(ws)                  # type: ignore[assignment]
    buf = io.StringIO()
    obs: dict = {}
    try:
        with contextlib.redirect_stdout(buf):
            s = qwen38.QwenLiveTranslateSession(SessionConfig())
            task = asyncio.create_task(s.start(lambda d: None, None))
            # 等 start() 真正进到危险窗口（已连上、正等 session.created）
            await asyncio.wait_for(ws.recv_entered.wait(), timeout=3)
            obs["ready_during"] = s.is_ready
            obs["ws_published_during"] = s._ws is not None          # noqa: SLF001
            obs["starting_during"] = s._starting_ws is not None      # noqa: SLF001
            # (a) 窗口里送音频：必须抛 SessionNotReady，且线上没有 append
            raised: BaseException | None = None
            try:
                await s.send_audio(b"\x01\x02" * 1600)
            except BaseException as exc:                            # noqa: BLE001
                raised = exc
            obs["raised"] = raised
            obs["types_during"] = ws.types()

            ws.release.set()
            await asyncio.wait_for(task, timeout=5)                 # start() 正常返回
            obs["ready_after"] = s.is_ready
            # (b) start 之后送音频：append 必须排在 session.update 之后
            await s.send_audio(b"\x03\x04" * 1600)
            obs["types_after"] = ws.types()
            obs["ws_published_after"] = s._ws is not None           # noqa: SLF001
            # 收尾：接收循环在跑（假 ws 永不产出）→ cancel 掉，别留悬挂任务
            assert s._recv_task is not None                         # noqa: SLF001
            s._recv_task.cancel()                                   # noqa: SLF001
            with contextlib.suppress(asyncio.CancelledError):
                await s._recv_task                                  # noqa: SLF001
    finally:
        qwen38.websockets = original                            # type: ignore[assignment]
    obs["stdout"] = buf.getvalue()
    return obs


def test_no_audio_before_session_update() -> None:
    """(a) start 窗口里送音频 → SessionNotReady，线上零 append。"""
    from vlt.session.base import SessionNotReady

    obs = asyncio.run(_scenario())
    assert obs["starting_during"], "前提不成立：start() 期间应把 ws 挂在 _starting_ws 上"
    assert not obs["ws_published_during"], \
        "start() 期间绝不能把 ws 发布成 _ws（采集泵会立刻往上发音频）"
    raised = obs["raised"]
    assert isinstance(raised, SessionNotReady), \
        f"窗口里 send_audio 应抛 SessionNotReady，实际 {type(raised).__name__}: {raised}"
    assert "session.update" in str(raised), f"异常文案要说清原因：{raised}"
    assert obs["types_during"] == [], \
        f"窗口里一个事件都不该上送（尤其不能有 append），实际 {obs['types_during']}"
    print(f"  start 窗口内 send_audio 被拒且线上零事件 OK（{type(raised).__name__}）")


def test_append_sent_after_session_update() -> None:
    """(b)(c) start 返回后：append 排在 session.update 之后，is_ready False→True。"""
    obs = asyncio.run(_scenario())
    assert obs["ready_during"] is False, "start 窗口里 is_ready 必须是 False"
    assert obs["ready_after"] is True, "start 返回后 is_ready 必须变 True"
    assert obs["ws_published_after"], "start 返回后应把 ws 发布成 _ws"
    types = obs["types_after"]
    assert types == ["session.update", "input_audio_buffer.append"], \
        f"线上事件顺序不对（update 必须最先）：{types}"
    print(f"  is_ready False→True 且 append 排在 session.update 之后 OK（{types}）")


def test_proxy_drops_not_ready_chunks() -> None:
    """(c) `_SessionProxy` 对 not-ready 会话：静默丢块、只计数，不算「发送失败」。"""
    from vlt.engine import Engine, EngineEvents

    class _Session:
        is_ready = False

        def __init__(self) -> None:
            self.got = 0

        async def send_audio(self, pcm: bytes) -> None:
            self.got += 1

        def note_voice(self) -> None:
            pass

    class _Ready(_Session):
        is_ready = True

    eng = Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                 events=EngineEvents())
    not_ready = _Session()
    eng._session = not_ready                                       # noqa: SLF001

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(eng._proxy.send_audio(b"\x00\x20" * 1600))      # noqa: SLF001
    out = buf.getvalue()
    assert not_ready.got == 0, "not-ready 会话不该收到音频"
    assert eng._proxy.not_ready_chunks == 1, \
        f"应记 1 块「重连窗口丢弃」，实际 {eng._proxy.not_ready_chunks}"          # noqa: SLF001
    assert eng._proxy.send_fails == 0, \
        f"不是发送失败，send_fails 必须为 0，实际 {eng._proxy.send_fails}"        # noqa: SLF001
    assert "发送音频失败" not in out, f"不该打「发送音频失败」那行（那是发送故障的口径）：{out!r}"

    # 诊断里必须能看见这个计数（否则「重连窗口丢了音频」永远查不到）
    eng._proxy.not_ready_chunks = 3                                # noqa: SLF001
    dbuf = io.StringIO()
    with contextlib.redirect_stdout(dbuf):
        eng._dump_diagnostics("测试")                              # noqa: SLF001
    diag = dbuf.getvalue()
    assert "重连窗口丢弃 3 块" in diag, f"断线诊断缺「重连窗口丢弃」那行：\n{diag}"

    # ready 的会话照常上送（别把守卫做成「一律不发」）
    ready = _Ready()
    eng._session = ready                                           # noqa: SLF001
    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(eng._proxy.send_audio(b"\x00\x20" * 1600))      # noqa: SLF001
    assert ready.got == 1, "ready 会话应收到 1 块"
    assert eng._proxy.send_fails == 0
    print("  代理对 not-ready 会话只计数丢弃（send_fails=0、未刷「发送失败」）+ 诊断留痕 OK")


def _cfg():
    from vlt.config import AppConfig, Direction

    return AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [0.05],
                      "max_new_sessions_per_minute": 100, "final_silence_s": 0.1},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=False)},
        chatbox={}, merger={}, overlay={}, output={},
    )


if __name__ == "__main__":
    print("test_session_start_race:")
    test_no_audio_before_session_update()
    test_append_sent_after_session_update()
    test_proxy_drops_not_ready_chunks()
    print("ALL PASSED")
