"""响度门限（**所有**采集腿共用一份实现）。

## 为什么单独一个模块

主线两条腿（麦克风 / VRChat 混音）与分玩家腿都需要同一件事：**按 RMS 判响度，
低于门限的块不上送**，同时不能把句子切坏。这份逻辑以前只存在于 `engine._LevelGate`，
分玩家腿如果要另写一份，就会出现"两套门限口径"（同名配置 `gate_db` 行为不一致）——
正是本仓库反复踩过的坑。

## 三类保护（缺一个都会出真问题）

- **hold**（默认 500ms）：越过阈值后回落，仍继续送 —— 句中的停顿、句尾渐弱不被切碎；
- **preroll**（默认 250ms）：开闸时补发阈值**之前**的一小段 —— 句首辅音不丢；
- **tail**（默认 600ms 数字静音）：关闸时先补一段等长静音再停 —— 服务端靠"停顿"
  给这一轮收尾；只挖洞不补静音会让 turn_detection 永远等不到句末（实测拖到 196s），
  也会把不相邻的两段语音当成同一轮连着听。
"""
from __future__ import annotations

import time
from collections import deque

from .output.level_match import LEVEL_FLOOR_DB, block_db

DEFAULT_HOLD_MS = 500.0
DEFAULT_PREROLL_MS = 250
DEFAULT_TAIL_MS = 600.0


def chunk_level_db(chunk: bytes) -> float:
    """一个音频块的电平（dBFS，满量程 = 0）。纯函数，离线可测。

    用 **RMS** 而不是峰值：峰值只反映块里最响的几个采样 —— 一个尖刺就能顶上来，
    但它不代表「这句话整体有多响」；底噪/远处小声的块峰值偶发偏高、RMS 一直很低。
    实现只有一份：委托给 `level_match.block_db`。
    """
    return block_db(chunk)


class LevelGate:
    """输入响度门限：音量达不到门限的块**不上送**。

    `verbose=True` 时打日志（主线沿用旧行为）；分玩家腿每位玩家一个实例，
    传 False 只累计计数，不刷屏。
    """

    def __init__(self, enabled: bool, threshold_db: float,
                 hold_ms: float = DEFAULT_HOLD_MS,
                 preroll_ms: int = DEFAULT_PREROLL_MS,
                 tail_ms: float = DEFAULT_TAIL_MS,
                 *, verbose: bool = True) -> None:
        self.enabled = enabled
        self.threshold_db = threshold_db
        self.hold_ms = hold_ms
        self.preroll_ms = preroll_ms
        self.tail_ms = max(0.0, float(tail_ms))
        self.verbose = verbose
        self.is_open = False
        self._last_loud_ts: float | None = None
        self._preroll: deque[tuple[bytes, float]] = deque()
        self._preroll_ms = 0.0
        self.level_db = LEVEL_FLOOR_DB
        self.dropped_chunks = 0
        self.opened = 0
        self.replay_count = 0
        self.tail_chunks = 0

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    def feed(self, chunk: bytes, *, now: float | None = None,
             dur_s: float | None = None) -> list[bytes]:
        """返回本次真正要上送的块序列（0..N 块；N>1 = 开闸补 preroll / 关闸补静音）。"""
        now = time.monotonic() if now is None else now
        dur_s = (len(chunk) / 2 / 16000.0) if dur_s is None else dur_s
        db = chunk_level_db(chunk)
        self.level_db = db
        if not self.enabled:
            return [chunk]
        if db >= self.threshold_db:
            self._last_loud_ts = now
            if self.is_open:
                return [chunk]
            self.is_open = True
            self.opened += 1
            replay = [c for c, _ in self._preroll]
            self._preroll.clear()
            self._preroll_ms = 0.0
            if replay:
                self.replay_count += 1
                self._log(f"[gate] 门限：{db:.1f} dBFS ≥ {self.threshold_db:g} → 开始上送，"
                          f"补发 {len(replay)} 块（防丢句首）")
            return replay + [chunk]

        # —— 低于门限 ——
        if self.is_open:
            held = (self._last_loud_ts is not None
                    and (now - self._last_loud_ts) * 1000.0 <= self.hold_ms)
            if held:
                return [chunk]              # hold 期内继续送：句中的停顿不切碎
            self.is_open = False
            tail = self._silence_tail(len(chunk))
            self.tail_chunks += len(tail)
            self.dropped_chunks += 1
            self._remember(chunk, dur_s)
            self._log(f"[gate] 门限：已回落 {self.hold_ms:g}ms 低于 {self.threshold_db:g} dBFS"
                      f"（当前 {db:.1f}）→ 暂停上送，并补 {len(tail)} 块静音"
                      f"（累计拦截 {self.dropped_chunks} 块）")
            return tail
        self.dropped_chunks += 1
        self._remember(chunk, dur_s)
        return []

    def _silence_tail(self, nbytes: int) -> list[bytes]:
        """关闸那一下补的等长数字静音（块数 = tail_ms / 单块时长，至少 1 块）。"""
        if self.tail_ms <= 0 or nbytes <= 0:
            return []
        block_ms = nbytes / 2 / 16000.0 * 1000.0
        if block_ms <= 0:
            return []
        return [b"\x00" * nbytes] * max(1, int(round(self.tail_ms / block_ms)))

    def _remember(self, chunk: bytes, dur_s: float) -> None:
        if self.preroll_ms <= 0:
            return
        self._preroll.append((chunk, dur_s))
        self._preroll_ms += dur_s * 1000.0
        while self._preroll and self._preroll_ms > self.preroll_ms + 1e-3:
            _, d = self._preroll.popleft()
            self._preroll_ms -= d * 1000.0
