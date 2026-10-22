"""建会话互斥：**同一时刻只允许一条「关旧会话 → 建新会话」流程**。

## 治什么

重建路径（`_reconnect_session` / `_rebuild_session`）是两步组合：关旧会话 + 建新会话。
采集泵、看门狗、语言/词库热更新都在旁边跑，两条重建交错就会：

* 各 close 一次旧会话、各建一条新连接 —— **后建的把先建的覆盖**，前一条既没人持有
  也没人关闭（泄漏连接，服务端一直等到超时）；
* 白烧两次 RPM 预算（RPM 10，实测每次 WS 连接算一次请求），第二次重连就可能被推进
  「连接预算已满，等待 Ns」；
* 真机表现是「重连成功但立刻又断」——重连风暴的另一半。

## 三件事

1. 并发两次 `_create_session()` → `start()` 的最大并发数 == 1，且两条都建成了；
2. 并发两次 `_reconnect_session()`（真机走的就是这条）→ 同样串行，且**旧会话只关一次**；
3. `_session_lock` 被持有期间 `_watchdog()` **不许**再起第二条重连（新会话 start 期间
   `is_alive` 还是 False，不挡就会把「正在建的会话」判成死会话）。

离线可跑：`vlt.engine.create_session` 换成假会话工厂，不连网络、不需要 key。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import vlt.engine as E  # noqa: E402

#: 并发观测：cur = 此刻在 start() 里的会话数，max = 历史峰值
STATE = {"cur": 0, "max": 0, "starts": 0, "closes": 0}


class _FakeSession:
    """假会话：`start()` 睡一会儿（给「并发」留出插进来的机会）并记并发峰值。"""

    def __init__(self) -> None:
        self.started = False
        self.closed = False

    async def start(self, on_text=None, on_audio=None, on_usage=None):   # noqa: ANN001, ANN201
        STATE["cur"] += 1
        STATE["max"] = max(STATE["max"], STATE["cur"])
        try:
            await asyncio.sleep(0.05)
        finally:
            STATE["cur"] -= 1
        self.started = True
        STATE["starts"] += 1

    async def close(self) -> None:
        self.closed = True
        STATE["closes"] += 1

    async def send_audio(self, pcm: bytes) -> None:
        pass

    def tick(self) -> None:
        pass

    @property
    def is_alive(self) -> bool:
        return True


def _cfg():
    from vlt.config import AppConfig, Direction

    return AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [0.05],
                      "max_new_sessions_per_minute": 100, "final_silence_s": 0.1},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=False)},
        chatbox={}, merger={}, overlay={}, output={},
    )


def _engine():
    return E.Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                    events=E.EngineEvents())


def _reset() -> None:
    STATE.update(cur=0, max=0, starts=0, closes=0)


def test_concurrent_create_session_is_serialized() -> None:
    """并发两次 `_create_session()`：start() 最大并发 1，两条都建成。"""
    _reset()
    original = E.create_session
    E.create_session = lambda scfg: _FakeSession()        # type: ignore[assignment]
    try:
        eng = _engine()
        scfg = eng._cfg.directions["mine"].to_session_config(eng._cfg.session_base)  # noqa: SLF001

        async def run() -> None:
            with contextlib.redirect_stdout(io.StringIO()):
                await asyncio.gather(eng._create_session(scfg),      # noqa: SLF001
                                     eng._create_session(scfg))      # noqa: SLF001

        asyncio.run(run())
    finally:
        E.create_session = original                      # type: ignore[assignment]

    assert STATE["starts"] == 2, f"两条都该建会话，实际 {STATE['starts']}"
    assert STATE["max"] == 1, \
        f"start() 出现并发（峰值 {STATE['max']}）→ 锁没起作用（会各建一条连接互相覆盖）"
    print(f"  并发 _create_session 串行 OK（start 并发峰值 {STATE['max']}，建成 {STATE['starts']} 条）")


def test_concurrent_reconnect_session_is_serialized() -> None:
    """并发两次 `_reconnect_session()`（重连走这条）：串行，且**没有会话被漏关**。

    说明：`_reconnect_session` 的语义是「换一条新连接」，所以两次串行执行会各自
    接手前一条（第一条新会话被第二次关掉）—— 这不是泄漏，是「后一次重连接手」。
    真正要守住的两条：① 建会话不并发；② 每条建出来的会话要么是当前会话、要么已被关。
    （生产路径上 `_reconnect_task` 是单飞的，本来就不会同时起两条。）
    """
    _reset()
    original = E.create_session
    created: list[_FakeSession] = []

    def _factory(scfg):                                          # noqa: ANN001, ANN202
        s = _FakeSession()
        created.append(s)
        return s

    E.create_session = _factory                             # type: ignore[assignment]
    try:
        eng = _engine()
        old = _FakeSession()
        old.started = True
        eng._session = old                                          # noqa: SLF001

        async def run() -> None:
            with contextlib.redirect_stdout(io.StringIO()):
                await asyncio.gather(eng._reconnect_session(),       # noqa: SLF001
                                     eng._reconnect_session())       # noqa: SLF001

        asyncio.run(run())
    finally:
        E.create_session = original                     # type: ignore[assignment]

    assert STATE["max"] == 1, f"重连路径出现了并发建会话（峰值 {STATE['max']}）"
    assert len(created) == 2, f"两次重连各建一条，实际 {len(created)}"
    assert old.closed, "旧会话没被关掉"
    assert created[0].closed, "先建的那条被后来者接手时必须关掉（否则泄漏连接）"
    assert not created[-1].closed, "当前会话不该被关"
    assert eng._session is created[-1], "当前会话应是最新建成的那条"   # noqa: SLF001
    print(f"  并发 _reconnect_session 串行 OK（start 并发峰值 {STATE['max']}，"
          f"建成 {len(created)} 条、无会话漏关）")


def test_watchdog_skips_while_creating() -> None:
    """锁被持有（正在建会话）时 `_watchdog()` 立刻返回，不起第二条重连。"""
    _reset()
    eng = _engine()

    class _Dead:
        @property
        def is_alive(self) -> bool:
            return False

        @property
        def fail_reason(self) -> str:
            return "测试：死会话"

        async def close(self) -> None:
            pass

    eng._session = _Dead()                                          # noqa: SLF001

    async def run() -> None:
        async with eng._session_lock:                               # noqa: SLF001
            await eng._watchdog()                                   # noqa: SLF001
            assert eng._reconnect_task is None, \
                "正在建会话时不许再起第二条重连（新会话 start 期间 is_alive 也是 False）"
        # 锁放开后，看门狗照常接手
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await eng._watchdog()                                   # noqa: SLF001
        assert eng._reconnect_task is not None, "锁放开后看门狗应安排重连"
        eng._reconnect_task.cancel()                                # noqa: SLF001
        with contextlib.suppress(asyncio.CancelledError):
            await eng._reconnect_task                               # noqa: SLF001

    asyncio.run(run())
    print("  建会话期间看门狗不插队、放开后照常重连 OK")


if __name__ == "__main__":
    print("test_session_create_lock:")
    test_concurrent_create_session_is_serialized()
    test_concurrent_reconnect_session_is_serialized()
    test_watchdog_skips_while_creating()
    print("ALL PASSED")
