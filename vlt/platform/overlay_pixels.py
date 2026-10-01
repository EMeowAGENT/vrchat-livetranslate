"""原生桌面叠加窗共用的像素工具（Wayland layer-shell 与 X11 ARGB 两个后端共享）。

## 为什么抽出来

两个后端的出图路径不同（`wl_shm` 的 memfd vs `XPutImage` 的 XImage 缓冲），但
**像素字节完全同构**：小端 B,G,R,A 四通道、逐像素预乘 alpha、行序自上而下 ——
正是 :func:`premultiplied_bgra` 吐出来的那套。抄成两份迟早会漂移，而预乘漏一处
的表现是「半透明处偏亮/发白」（或反向：被当成预乘再乘一次 → 暗），这类错误在
真机上非常难定位，所以只留这一份实现。

⚠️ **不做行翻转**：GL 纹理原点在左下、X11 的 ZPixmap 原点在左上 —— 翻转是
出图端各自的约定（Wayland 后端在上传时翻，X11 后端不翻），在这里翻会让另一个
后端再翻一次。谁用谁翻，本模块只管颜色。
"""
from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image


def clamp01(value: Any, default: float = 1.0) -> float:
    """把任意值夹到 [0, 1]；解析不出（None / 垃圾 / NaN）时返回 `default`。

    配置里的 alpha 可能来自 YAML（数字、数字字符串、或用户手写的怪值），
    这里统一收口：怪值不抛、也不静默变成 0（0 会让整块面板直接消失），
    而是回落到调用方给的默认值（一般是「不透明」）。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    if v != v:                                  # NaN
        return float(default)
    return max(0.0, min(1.0, v))


def premultiplied_bgra(image: Image.Image, alpha: float = 1.0) -> bytes:
    """RGBA 面板 → **预乘 alpha** 的 BGRA 字节（小端序，行序与输入一致）。

    语义（与两个后端的合成约定一致）：

    * 输出通道按内存序排：**B, G, R, A**（小端 32 位 0xAARRGGBB 的字节序）；
    * RGB 每个通道乘 `A/255 × alpha`，A 通道乘 `alpha` —— 输出即**预乘**结果。
      `wl_shm` 的 ARGB8888 与 X11 合成器（cairo 惯例）都按预乘消费；
    * `alpha` 是整层乘子（用户要的「整窗透明度」），先经 :func:`clamp01`；
    * 输入是 PIL 的**直通** alpha（未预乘），本函数负责转成预乘。
    """
    k = clamp01(alpha)
    rgba = np.asarray(image.convert("RGBA"), dtype=np.float32)
    a = rgba[..., 3] / 255.0
    scale = a * k

    def _u8(x: np.ndarray) -> np.ndarray:
        return np.clip(x + 0.5, 0, 255).astype(np.uint8)

    out = np.empty(rgba.shape, dtype=np.uint8)
    out[..., 0] = _u8(rgba[..., 2] * scale)     # B
    out[..., 1] = _u8(rgba[..., 1] * scale)     # G
    out[..., 2] = _u8(rgba[..., 0] * scale)     # R
    out[..., 3] = _u8(scale * 255.0)            # A
    return out.tobytes()
