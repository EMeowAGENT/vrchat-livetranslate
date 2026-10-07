"""共享音频 DSP 小工具（只用 numpy，不依赖任何平台专有库）。

目前只有一项：降采样前的**抗混叠低通**。

## 为什么单独成模块

`engine.to_16k_mono`（ASR 采集降采样）与 `output.micproxy.resample_to_48k_stereo`
（麦克风代理直通重采样）都需要「窗口化 sinc 低通」这一步 —— 属于典型的「踩过坑的
逻辑只留一份」：两份实现迟早会漂移。它不 import 平台/引擎，任何一侧都能安全引用。
"""
from __future__ import annotations

import numpy as np


def lowpass(a: np.ndarray, rate: int, cutoff_hz: float = 7000.0,
            taps: int = 65) -> np.ndarray:
    """窗口化 sinc 低通（返回 float64），供降采样前抗混叠用。

    `cutoff_hz` 取目标奈奎斯特频率略减（留过渡带）。源采样率已低到滤不掉时原样返回
    （转 float64），绝不报错。

    默认 7000Hz 对齐 `engine.to_16k_mono`（目标 16kHz、奈奎斯特 8kHz）；
    麦克风代理直通到 48k 时传 ~20000Hz（奈奎斯特 24kHz）。
    """
    if rate <= cutoff_hz * 2:
        return a.astype(np.float64)
    n = np.arange(taps) - (taps - 1) / 2
    fc = cutoff_hz / rate                      # 归一化截止频率（周期/样点）
    h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(taps)
    s = h.sum()
    if not s:
        return a.astype(np.float64)
    return np.convolve(a.astype(np.float64), h / s, mode="same")
