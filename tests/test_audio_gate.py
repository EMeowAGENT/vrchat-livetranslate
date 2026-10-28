#!/usr/bin/env python
"""响度门限（`vlt/audio_gate.py`）的离线验收。

## 这里钉住的事

1. **滞回 = 0 时行为与以前逐块一致**（主线还在用这一档，不能悄悄变）；
2. **滞回 > 0 时**：开闸后只要还在关闸线以上就继续上送 —— 轻声/断续的人不会被
   「开一下 → 补 600ms 静音 → 再开」切成好几轮；
3. 真正安静下来（低于关闸线）时，hold 到期才关，且**只补一次** tail 静音；
4. `peak_of` 与 `VOICE_PEAK` 的口径（`note_voice` 判据）。

跑法：.venv/bin/python tests/test_audio_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.audio_gate import VOICE_PEAK, LevelGate, peak_of  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


BLOCK = 320          # 20ms @ 16k


def _block(amplitude: int, n: int = BLOCK) -> bytes:
    """恒定幅度的块：RMS = amplitude（→ dBFS = 20·log10(amp/32768)）。"""
    return amplitude.to_bytes(2, "little", signed=True) * n


LOUD = _block(104)      # ≈ -50 dBFS
SOFT = _block(46)       # ≈ -57 dBFS（低于 -55，但高于 -61）
SILENT = _block(3)      # ≈ -80 dBFS


def test_peak() -> None:
    print("[gate] 峰值口径")
    check("空块峰值 0", peak_of(b"") == 0)
    check("全零块峰值 0", peak_of(b"\x00\x00" * BLOCK) == 0)
    check("负峰值按绝对值", peak_of((-500).to_bytes(2, "little", signed=True) * 4) == 500)
    check("VOICE_PEAK 是 220（与主线同口径）", VOICE_PEAK == 220, str(VOICE_PEAK))


def test_no_hysteresis() -> None:
    """回归：滞回 0 = 与升级前逐块一致。"""
    print("[gate] 滞回 0（主线行为不变）")
    gate = LevelGate(True, -55.0, hold_ms=500.0, tail_ms=600.0, verbose=False)
    check("门限以下不上送", gate.feed(SILENT, now=0.0) == [])
    out = gate.feed(LOUD, now=0.1)
    # 前面的静音块在 preroll 里，开闸时一并补发（这是要的：句首不丢）
    check("过门限开闸并上送", len(out) == 2 and gate.is_open, str(len(out)))
    check("hold 内回落仍上送", len(gate.feed(SOFT, now=0.2)) == 1)
    check("hold 到期后关闸", gate.feed(SILENT, now=1.0) != [] and not gate.is_open)
    check("只开过一次", gate.opened == 1, str(gate.opened))
    check("关闸补了 tail", gate.tail_chunks == 30, str(gate.tail_chunks))


def test_hysteresis() -> None:
    """滞回 6dB：开闸 -55 / 关闸 -61。"""
    print("[gate] 滞回 6dB（分玩家腿）")
    gate = LevelGate(True, -55.0, hold_ms=500.0, tail_ms=600.0,
                     verbose=False, hysteresis_db=6.0)
    check("关闸线 = 门限 - 滞回", gate.close_db == -61.0, str(gate.close_db))
    out = gate.feed(LOUD, now=0.1)
    check("过门限开闸", len(out) == 1 and gate.is_open)
    # 已经是「开闸 + 低于门限但在关闸线以上」→ 继续送，且不关闸
    kept = [gate.feed(SOFT, now=0.5) for _ in range(5)]
    check("轻声块全部继续上送（不被切成多轮）",
          all(len(item) == 1 for item in kept) and gate.is_open, str(kept))
    check("滞留期间不算 dropped", gate.dropped_chunks == 0, str(gate.dropped_chunks))
    # 真安静下来：低于关闸线 + hold 到期 → 关闸，只补一次 tail
    tail = gate.feed(SILENT, now=3.0)
    check("低于关闸线后关闸", not gate.is_open)
    check("只补一次 tail", len(tail) == 30, str(len(tail)))
    check("再次安静不重复补", gate.feed(SILENT, now=3.5) == [])
    check("只开过一次", gate.opened == 1, str(gate.opened))


def test_preroll() -> None:
    print("[gate] preroll 补发（句首不丢）")
    gate = LevelGate(True, -55.0, hold_ms=500.0, tail_ms=0.0,
                     verbose=False, preroll_ms=250)
    gate.feed(SILENT, now=0.0)
    gate.feed(SILENT, now=0.02)
    out = gate.feed(LOUD, now=0.04)
    check("开闸时补发之前的块", len(out) == 3, str(len(out)))
    check("补发计数", gate.replay_count == 1, str(gate.replay_count))


def main() -> int:
    test_peak()
    test_no_hysteresis()
    test_hysteresis()
    test_preroll()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {', '.join(FAILS)}")
        return 1
    print("ALL PASSED（响度门限）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
