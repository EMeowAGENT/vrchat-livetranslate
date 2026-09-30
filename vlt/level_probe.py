"""设置窗「输入门限」的**独立电平探针**：没在翻译时也能看到实时电平。

## 治什么

电平条以前只有一个数据源 —— **运行中引擎**的 `input_gate.level_db`。于是想调门限
就得先点「开始翻译」：可门限调的正是「多小的声音该被滤掉」，这时候音频已经在往
模型上送了（白花钱，且调的过程本身就在污染会话）。用户口径（原话）：

    「只要上面的 select 勾选了启用就会显示当前电平，
      但是也需要注意，只有设置这个窗口被打开的时候才会有」

所以这里补一路**只为看电平**的采集。开关判定不在本模块，在
`vlt/gui.py:_sync_gate_level_probe()`：设置窗可见 + 勾了「启用」+ 没在翻译 → 开；
窗口一关 / 一取消勾选 / 一开始翻译 → 立刻停并释放设备。

## 为什么必须与引擎「同一路配方」

用户是照着这条电平条调门限的 —— 探针抓的端点、重采样、电平口径只要与引擎差一点，
调出来的门限就是错的。所以这里**不另写一套**，逐环复用 `engine.run_loopback` 用的
那些函数：端点由 `pick_loopback_target` / `pick_vrchat_targets` 挑（含界面上手选的
那台设备），打开走 `platform.capture_backend().open_loopback(blocksize=CHUNK_BYTES)`，
重采样与电平用 `to_16k_mono` / `chunk_level_db`。

同一时刻**只允许一路**电平来源：有引擎就用引擎的（见 gui 侧的优先级），
再开一路 loopback 会与引擎抢同一个采集端点。

## 线程模型

与 `Engine` 一致：daemon 线程里跑一个自己的 asyncio 事件循环。这不是可选项 ——
平台侧的 `open_loopback()` 要 `asyncio.get_running_loop()`，`AudioSource.read()`
也是协程。

⚠️ 读取一律**带超时**：端点没在出声时 WASAPI loopback 压根不产数据（见
`vlt/platform/win.py:PyaudioLoopbackSource` 的实测记录），死等会让「停止」按不下去。
超时 = 「还活着但暂时没数据」，按静音处理，**不是失败**。

⚠️ 收尾顺序照抄 `vlt/platform/audio.py:QueueAudioSource.close()` 的硬约束：
**置停止位 → join 线程 → 才碰底层资源**。反过来在 Windows 上会撞访问违规
（那边有实测记录：点「停止翻译」闪退、退出码 139）。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from . import platform
from .engine import (
    CHUNK_BYTES,
    LEVEL_FLOOR_DB,
    chunk_level_db,
    pick_loopback_target,
    pick_vrchat_targets,
    to_16k_mono,
)
from .platform.audio import MixedAudioSource

LOG = "[level]"

# 单次 read 的超时。它同时决定两件事：①「静音」多久把电平归到地板值；
# ② `stop()` 的最大延迟（线程要等这一次 read 返回才看得到停止位）。
# 取 0.2s：远小于引擎的 1.0s，关设置窗时不会有可感的卡顿。
READ_TIMEOUT_S = 0.2
JOIN_TIMEOUT_S = 3.0


def open_level_source(device_name: str | None = None) -> Any:
    """按引擎 loopback 腿的同一配方，开一路「只听电平」的采集源。

    `device_name`：界面上手选的系统声设备（`capture.loopback_device`），只在 Windows
    有意义 —— Linux 的 loopback 腿不看这个配置，目标固定是「VRChat 的播放流」
    （见 `engine._run_loopback_linux`），探针必须抓同一个东西，否则用户按这条电平
    调出来的门限对不上真正被上送的音频。

    打不开一律**抛异常**（由 `LevelProbe` 统一留痕）：绝不返回 None 让调用方猜原因。
    """
    backend = platform.capture_backend()
    if platform.IS_LINUX:
        targets = pick_vrchat_targets()
        if not targets:
            raise RuntimeError("没找到 VRChat 的音频输出流（VRChat 在跑并且出声了吗？）")
        opened = [backend.open_loopback(t, blocksize=CHUNK_BYTES) for t in targets]
        print(f"{LOG} 探针采集 {len(opened)} 路 VRChat 输出："
              f"{'、'.join(t.name for t in targets)}", flush=True)
        # 多路要混成一路再判电平 —— 与 engine._pump_vrchat_capture 同一口径
        return opened[0] if len(opened) == 1 else MixedAudioSource(opened)

    target = pick_loopback_target(None, device_name)
    if target is None:
        raise RuntimeError("没找到任何可采集的系统输出（音频服务正常吗？）")
    print(f"{LOG} 探针采集端点「{target.name}」{target.sample_rate}Hz "
          f"×{target.channels}ch → 16kHz 单声道", flush=True)
    return backend.open_loopback(target, blocksize=CHUNK_BYTES)


class LevelProbe:
    """一路「只为显示电平」的采集：后台线程读块 → 算 dBFS → 写 `level_db`。

    `level_db` 的语义与 `engine._LevelGate.level_db` **完全一致**：最近一块的
    瞬时电平，不做峰保。峰保只在界面那一处做（`gui._refresh_gate_level` 里
    每 100ms 掉 1.5dB），这样「有引擎」与「用探针」两条路的条子观感一模一样；
    两边各做一层峰保会双重衰减、且与引擎那条路对不上。

    `opener` 依赖注入：默认打开真设备（`open_level_source`），测试塞假源 ——
    离线测试**绝不许**碰真声卡（CI 机器上根本没有）。

    跨线程只共享标量（float / int / str），CPython 里读写原子、不会读到半截值 ——
    与 `gui._apply_gate_live` 同一取舍，不加锁。
    """

    def __init__(self, opener: Callable[[], Any] | None = None, *,
                 device_name: str | None = None,
                 read_timeout: float = READ_TIMEOUT_S,
                 join_timeout: float = JOIN_TIMEOUT_S) -> None:
        self._opener = opener or (lambda: open_level_source(device_name))
        self._read_timeout = float(read_timeout)
        self._join_timeout = float(join_timeout)
        self.level_db = LEVEL_FLOOR_DB     # 最近一块的电平（dBFS）
        self.last_error: str | None = None  # 失败原因（已留痕的那一行），没失败为 None
        self.chunks = 0                    # 本次采到的块数（停止时写进日志，好核对）
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._source: Any = None
        self._running = False

    # ---------------------------------------------------------------- 对外

    @property
    def running(self) -> bool:
        """采集线程是否还活着（开设备失败 / 读取异常 / stop() 之后都是 False）。"""
        return self._running

    def start(self) -> None:
        """起 daemon 线程开始采集。重复调用无效 —— 一个探针只用一次，失败不自我重试
        （要不要再来一次由界面决定：见 `_sync_gate_level_probe` 的说明）。"""
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_run, daemon=True,
                                        name="vlt-level-probe")
        self._thread.start()

    def stop(self) -> None:
        """停止采集并释放设备。幂等，可以在任何线程调。"""
        self._stop_evt.set()
        self._running = False
        th = self._thread
        if th is not None and th is not threading.current_thread() and th.is_alive():
            th.join(timeout=self._join_timeout)
            if th.is_alive():
                print(f"{LOG} ⚠️ 采集线程未在 {self._join_timeout:g}s 内退出"
                      f"（仍继续尝试关闭设备，可能有竞争）", flush=True)
        # 正常路径下线程自己的 finally 已经关过源（这里 _source 已是 None）；
        # 只有线程卡住没退出时才轮到这一手 —— 与 QueueAudioSource.close() 同一取舍。
        self._close_source()

    # ---------------------------------------------------------------- 线程内

    def _thread_run(self) -> None:
        try:
            asyncio.run(self._pump())
        except Exception as exc:  # noqa: BLE001 — 采集线程绝不能把异常抛出去
            self._fail(f"电平采集线程异常退出：{type(exc).__name__}: {exc}")
        finally:
            self._running = False

    async def _pump(self) -> None:
        try:
            source = self._opener()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"打不开系统声采集，电平条不可用：{type(exc).__name__}: {exc}")
            return
        self._source = source
        try:
            while not self._stop_evt.is_set():
                try:
                    chunk = await source.read(timeout=self._read_timeout)
                except Exception as exc:  # noqa: BLE001
                    self._fail(f"读取系统声失败，电平条停止更新："
                               f"{type(exc).__name__}: {exc}")
                    return
                if chunk is None:
                    # 超时 = 「还活着但暂时没数据」：端点静音时就是这么表现的。
                    # 必须显式写地板值 —— 否则读数会**冻在**最后一块上，
                    # 用户看着一条不动的电平以为还在出声。
                    self.level_db = LEVEL_FLOOR_DB
                    continue
                pcm16 = to_16k_mono(chunk, source.rate, source.channels)
                self.level_db = chunk_level_db(pcm16)
                self.chunks += 1
        finally:
            self._close_source()

    # ---------------------------------------------------------------- 内部

    def _close_source(self) -> None:
        src = self._source
        if src is None:
            return
        self._source = None
        try:
            src.close()
        except Exception as exc:  # noqa: BLE001 — 关不上也不该把停止流程带崩
            print(f"{LOG} ⚠️ 关闭采集源时出错（忽略）：{type(exc).__name__}: {exc}",
                  flush=True)

    def _fail(self, msg: str) -> None:
        """失败留痕（仓库硬约定：降级路径不许静默）。

        只打这一行、**不重试**：设备开不了就是开不了，每 100ms 刷一行日志只会把
        真问题淹掉。界面看到 `running=False` 就把读数显示成「—」。
        """
        self.last_error = msg
        self.level_db = LEVEL_FLOOR_DB
        print(f"{LOG} ❌ {msg}", flush=True)
