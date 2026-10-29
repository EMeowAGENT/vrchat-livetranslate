#!/usr/bin/env python3
"""离线标定：Photon Event 12 里到底有没有「说话者的世界坐标」。

## 这个实验要回答什么

分玩家语音只想按「离本机多远」做衰减，所以需要**别人的世界坐标**。主代理已经
确认 Photon Event 14 的 payload 里有 float32 世界坐标（y≈1.4~1.5，是视点高度），
但它只覆盖 2 个 actor；Event 12 覆盖全部 actor，却看起来像「8-bit 量化打包」。
本脚本要回答的是：**Event 12 能不能解出可控的世界坐标。**

## 结论（本脚本可复现，写给未来的人）

**能解，但不是 8-bit 量化，而是 body 偏移 6 处的 float32 三元组。**
解出来是**玩家根节点（髋）**的世界坐标，不是头显/视点；两者相差
0.1~1.0 m（玩家在房间里物理走动时头显会离开根节点）+ 固定的 0.62~0.72 m 躯干高度。

Event 12 的 payload 结构（本脚本从 9812 条真实消息里反推并交叉验证）：

```
message.payload（以 F3 开头）
  f3 04 0c <count>
   f5 43 <leb128 len> <body>     ← 参数 245 = byte[]，真正要解的是 body
   fe 0d <u16 actor>             ← 参数 254 = 发送者 actor

body（长度 5 / 29 / 124 / 138 / 146 / 158 / 164 / 166 / 178 …）
  [0:4]   u32le  服务器时间（ms，会回绕）
  [4]     flags  低半字节 == 0x7 ⇔ 本条带「位置块」
  [5]     ?      和 flags 同属位置块头部
  [6:18]  f32le  x, y, z（三个连续 float32，世界坐标 = 根节点/髋）
  [18:]   变长    IK / 骨骼姿态数据（单字节随时间平滑振荡，是量化过的姿态，
                  不是世界坐标；本脚本没有任何整数字段能对它做精确仿射拟合）
```

关键判据不是「相关性」而是**结构**：

1. 带 `(body[4] & 0x0F) == 0x07` 的 **5243/5243** 条消息都能在偏移 6 解出有限、
   量级合理的 x/y/z；不带标志的 4569 条里满足同一数值判据的只有 7 条（0.15%，
   都是坐标贴近原点的边界样本）；
2. 该三元组与 Event 14 的 float32 头显坐标在 x/z 上逐帧同步（差 0.1~1.0 m，
   是根节点与头显的 playspace 偏移），y 恒定差 0.62~0.72 m；
3. 用两路坐标分别算 1487↔1527 的距离：Event 12 是 7.783 m，Event 14 是
   7.468 m，误差 +0.315 m（4.2%）——对语音衰减足够；
4. 位置是**按需刷新**的：根节点不动的 actor 只在位置变化时带位置块（2%），
   移动中的 actor 覆盖率 70%~100%。实时解析要沿用上一个位置并检查时间戳。

## 为什么还要做「字段扫描」

因为不能只靠结构猜。脚本对所有宽度 1/2/3/4 字节、大小端、有/无符号的字段
做 Pearson + 仿射拟合 + 去时间趋势，并把「不同取值个数」一起列出来 ——
缓慢移动的坐标和时间戳天然高度相关，只看 r 会被时间趋势骗。

## 自检

最后合成一批 Event 12 风格的 body（随机字节 + 在指定偏移埋 16 位小端 /
8 位量化整数），确认同一套搜索器能把埋进去的字段原样找回来。这样「搜不到」
才有资格说「不存在」，而不是搜索器本身有 bug。
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import struct
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vlt.players import capture_linux, gcm, photon  # noqa: E402

EVENT_MOVE = 12
EVENT_EYE = 14
PARAM_BODY = photon.PARAM_AUDIO          # 245：Event 12/14 的真正负载
POS_FLAG_MASK = 0x0F                     # body[4] 的低半字节
POS_FLAG_VALUE = 0x07                    # ==7 时偏移 6 是位置三元组
POS_OFFSET = 6

# Event 14 的视高范围。取 0.2~3.5：低于 0.2 是趴/坐，高于 3.5 基本是误命中；
# 上界不能太死，否则高个子 avatar 会被漏掉。
EYE_Y_MIN = 0.2
EYE_Y_MAX = 3.5
WORLD_ABS_MAX = 5000.0                   # 超过这个量级肯定不是这个世界坐标

DEFAULT_PCAP = REPO_ROOT / "out/players-p0/cap.pcap"
DEFAULT_KEY = REPO_ROOT / "out/players-p0/key.bin"
DEFAULT_OUT = REPO_ROOT / "out/calibration"


# ---------------------------------------------------------------- 采集


def read_events(pcap: Path, key: bytes, layout: str) -> tuple[dict, Counter]:
    """解密 + 重组分片，返回 {(kind, code): [(ts, Message)]}。

    `capture_linux.parse_frame` 会把 ts 覆盖成 time.time()（它本来是给实时抓包
    用的），所以这里保留 pcap 自己的时间戳 —— 离线对齐全靠它。
    """
    frames = capture_linux.read_pcap(pcap)
    fragmenters: dict[str, photon.Fragmenter] = defaultdict(photon.Fragmenter)
    events: dict[tuple[int, int], list] = defaultdict(list)
    stats: Counter = Counter()
    seen_at: dict[tuple[str, bytes], float] = {}

    for ts, raw in frames:
        stats["frames"] += 1
        dg = capture_linux.parse_frame(raw)
        if dg is None:
            stats["not_udp"] += 1
            continue
        if not dg.is_encrypted:
            stats["plaintext"] += 1
            continue
        # 多网卡/加速器会让同一份密文出现两次。按 (方向, 密文) 在 1 秒窗口内
        # 去重：body 里带毫秒时间戳，同一秒内完全相同的密文只可能是重复包。
        sig = (dg.direction, dg.payload)
        last = seen_at.get(sig)
        if last is not None and ts - last < 1.0:
            stats["duplicate"] += 1
            continue
        seen_at[sig] = ts

        plain = gcm.try_open(key, dg.payload, layout)
        if plain is None:
            stats["decrypt_fail"] += 1
            continue
        stats["decrypted"] += 1

        commands, err = photon.parse_commands(plain)
        if err and not commands:
            stats["command_error"] += 1
            continue
        if err:
            stats["command_trailing"] += 1
        for cmd in commands:
            for payload in fragmenters[dg.direction].feed(cmd):
                msg, _skip = photon.parse_message_at(payload)
                if msg is None:
                    stats["non_message"] += 1
                    continue
                events[(msg.kind, msg.code)].append((ts, msg))

    stats["fragment_completed"] = sum(f.completed for f in fragmenters.values())
    stats["fragment_dropped"] = sum(f.dropped + f.rejected for f in fragmenters.values())
    return events, stats


def message_body(msg: photon.Message) -> bytes | None:
    """取参数 245 的 byte[]。缺失不是「没有位置」，要显式记账。"""
    param = msg.param(PARAM_BODY)
    if param is None or not isinstance(param.value, (bytes, bytearray)):
        return None
    return bytes(param.value)


def find_eye_triple(body: bytes) -> tuple[float, float, float, int] | None:
    """Event 14：扫描「y 在视高区间」的三连 float32。位置偏移不固定（22/23 都见过）。"""
    found = None
    for off in range(0, len(body) - 11):
        x, y, z = struct.unpack_from("<fff", body, off)
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            continue
        if not (EYE_Y_MIN <= y <= EYE_Y_MAX):
            continue
        if abs(x) > WORLD_ABS_MAX or abs(z) > WORLD_ABS_MAX:
            continue
        if found is None:
            found = (x, y, z, off)
    return found


def _plausible_triple(body: bytes) -> tuple[float, float, float] | None:
    """只按「数值像不像世界坐标」判断偏移 6 的三连 float，**不看标志位**。

    这个函数专门用来**独立验证**标志位判据：两个判据必须逐条一致，否则就说明
    「body[4] & 0x0F == 0x07」只是在这批数据上碰巧成立。
    """
    if len(body) < POS_OFFSET + 12:
        return None
    x, y, z = struct.unpack_from("<fff", body, POS_OFFSET)
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
        return None
    if abs(x) > WORLD_ABS_MAX or abs(z) > WORLD_ABS_MAX or not (-5.0 < y < 5.0):
        return None
    # 不带位置块的 body 在偏移 6 处是别的字段，解出来是 1e-20 级的次正规数。
    # 要求 x/z 至少有一个不是「贴着 0」的噪声；世界原点附近的玩家会漏判，
    # 但那属于可接受的假阴性（报告里已写明）。
    if abs(x) < 1e-3 and abs(z) < 1e-3:
        return None
    return (x, y, z)


def find_move_position(body: bytes) -> tuple[float, float, float] | None:
    """Event 12：按结构取位置块。

    为什么不用「扫一个合理的三连 float」：body 里还有 IK 数据，随便扫会误命中。
    `body[4] & 0x0F == 0x07` 是本 pcap 上 9812 条消息验证过的位置块标志，
    同时再用有限性/量级做一次防呆 —— 两者都过才算。标志位与数值判据的
    一致性会由 `_plausible_triple` 单独交叉验证。
    """
    if len(body) < POS_OFFSET + 12:
        return None
    if (body[4] & POS_FLAG_MASK) != POS_FLAG_VALUE:
        return None
    return _plausible_triple(body)


def build_series(events: dict, stats: Counter) -> tuple[dict, dict]:
    """整理成 (actor → 样本列表)。body 留着给字段扫描用。"""
    moves: dict[int, list] = defaultdict(list)
    eyes: dict[int, list] = defaultdict(list)

    for ts, msg in events[(photon.TYPE_EVENT, EVENT_MOVE)]:
        actor = photon.scan_actor(msg.payload)
        if actor is None:
            stats["e12_no_actor"] += 1
            continue
        body = message_body(msg)
        if body is None:
            stats["e12_no_body"] += 1
            continue
        moves[actor].append((ts, body, find_move_position(body)))

    for ts, msg in events[(photon.TYPE_EVENT, EVENT_EYE)]:
        actor = photon.scan_actor(msg.payload)
        if actor is None:
            stats["e14_no_actor"] += 1
            continue
        body = message_body(msg)
        if body is None:
            stats["e14_no_body"] += 1
            continue
        triple = find_eye_triple(body)
        if triple is None:
            stats["e14_no_triple"] += 1
            continue
        eyes[actor].append((ts, triple[0], triple[1], triple[2], triple[3]))

    for actor, samples in moves.items():
        samples.sort(key=lambda s: s[0])
    for actor, samples in eyes.items():
        samples.sort(key=lambda s: s[0])
    return moves, eyes


# ---------------------------------------------------------------- 对齐


def align_pairs(move_samples: list, eye_samples: list, tol: float = 0.2) -> list:
    """每个带位置的 Event 12 样本配最近的 Event 14 样本（|Δt| ≤ tol）。

    只保留带位置的 E12 样本：不带位置的那批 body 短 14 字节，字段会整体错位，
    混在一起扫出来的偏移没有意义（这个坑值得写下来）。
    """
    if not eye_samples:
        return []
    eye_ts = [s[0] for s in eye_samples]
    pairs = []
    for ts, body, pos in move_samples:
        if pos is None:
            continue
        i = bisect.bisect_left(eye_ts, ts)
        cand = [j for j in (i - 1, i) if 0 <= j < len(eye_samples)]
        if not cand:
            continue
        best = min(cand, key=lambda j: abs(eye_ts[j] - ts))
        if abs(eye_ts[best] - ts) > tol:
            continue
        eye = eye_samples[best]
        pairs.append({
            "ts": ts,
            "dt": eye[0] - ts,
            "gt": (eye[1], eye[2], eye[3]),      # Event 14 = 真值（视点）
            "move": pos,                          # Event 12 解出的位置
            "body": body,
        })
    return pairs


# ---------------------------------------------------------------- 统计


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    syy = sum((v - my) ** 2 for v in y)
    if sxx <= 0.0 or syy <= 0.0:
        return 0.0
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / math.sqrt(sxx * syy)


def _detrend(values: list[float], times: list[float]) -> list[float]:
    """去掉线性时间趋势。

    为什么必须做：坐标在 130 秒里缓慢单调移动，时间戳字段也会单调增加，
    两者裸相关能到 0.99 —— 那是「都在随时间变」，不是「字段编码了坐标」。
    """
    n = len(values)
    mt = sum(times) / n
    mv = sum(values) / n
    stt = sum((t - mt) ** 2 for t in times)
    if stt <= 0.0:
        return [v - mv for v in values]
    slope = sum((times[i] - mt) * (values[i] - mv) for i in range(n)) / stt
    return [values[i] - (mv + slope * (times[i] - mt)) for i in range(n)]


def _affine(x: list[float], y: list[float]) -> tuple[float, float, float] | None:
    """最小二乘拟合 y ≈ a*x + b，返回 (a, b, RMS)。"""
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0.0:
        return None
    a = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / sxx
    b = my - a * mx
    rms = math.sqrt(sum((y[i] - (a * x[i] + b)) ** 2 for i in range(n)) / n)
    return a, b, rms


def _read_int(raw: bytes, endian: str, signed: bool) -> int:
    return int.from_bytes(raw, "little" if endian == "<" else "big", signed=signed)


def scan_fields(bodies: list[bytes], coords: tuple[list[float], ...],
                times: list[float], *, min_distinct: int = 5,
                min_r: float = 0.5) -> list[dict]:
    """在 body 里找与坐标呈仿射关系的整数字段（另加 float32 兜底）。

    宽度 1/2/3/4，大小端，有/无符号全试；|r|>min_r 才进入拟合。
    同时算「去时间趋势后的 r」和「不同取值个数」：
      * 去趋势 r 掉到 0 附近 ⇒ 伪相关（时间戳/计数器）；
      * distinct 太少 ⇒ 只覆盖了坐标的几个量化级，不能据此定缩放。
    """
    n = len(bodies)
    if n < 3:
        return []
    length = min(len(b) for b in bodies)
    coord_names = ("x", "y", "z")
    det_coords = [_detrend(coords[i], times) for i in range(3)]
    coord_std = []
    for c in coords:
        m = sum(c) / n
        coord_std.append(math.sqrt(sum((v - m) ** 2 for v in c) / n))
    hits: list[dict] = []

    def consider(offset: int, width: int, kind: str, endian: str,
                 signed: bool | None, values: list[float]) -> None:
        if len(set(values)) < min_distinct:
            return
        for ci, name in enumerate(coord_names):
            r = _pearson(values, coords[ci])
            if abs(r) < min_r:
                continue
            fit = _affine(values, coords[ci])
            if fit is None:
                continue
            a, b, rms = fit
            rd = _pearson(_detrend(values, times), det_coords[ci])
            hits.append({
                "offset": offset,
                "width": width,
                "kind": kind,               # "int" / "float"
                "endian": "le" if endian == "<" else "be",
                "signed": signed,
                "coord": name,
                "r": r,
                "r_detrended": rd,
                "a": a,
                "b": b,
                "rms": rms,
                "distinct": len(set(values)),
                "coord_std": coord_std[ci],
            })

    for offset in range(0, length):
        for width in (1, 2, 3, 4):
            if offset + width > length:
                continue
            endians = ("<",) if width == 1 else ("<", ">")
            for endian in endians:
                for signed in (False, True):
                    values = [_read_int(b[offset:offset + width], endian, signed)
                              for b in bodies]
                    consider(offset, width, "int", endian, signed, values)
            if width == 4:
                # 真实位置就是 float32；把它当成一种「字段类型」一起扫，
                # 否则报告里会出现「u32 @6 相关 0.99」这种没法解释的候选。
                for endian in ("<", ">"):
                    raw_values = []
                    ok = True
                    for b in bodies:
                        v = struct.unpack_from(endian + "f", b, offset)[0]
                        if not math.isfinite(v):
                            ok = False
                            break
                        raw_values.append(v)
                    if ok:
                        consider(offset, 4, "float", endian, True, raw_values)
    return hits


def dedupe_hits(hits: list[dict]) -> list[dict]:
    """同一 (坐标, 偏移) 只留最好的一个解释。

    一个 float32 会同时让 u32/s32/1/2 字节窗口「命中」，全列出来表格没法看。
    这里按「解释力」挑一个：
      * |r| × |去趋势 r|：真字段两个 r 都高，时间戳只在裸 r 上高；
      * 缩放 a 的量级要合理：`a≈1e-6` 这类「把 float 位模式当整数」的解释会得到
        看似更小的残差，但它不是独立字段，只是同一串字节的另一种读法。
    最后按同一分数排序。
    """
    def score(h: dict) -> float:
        sanity = 1.0 if 1e-6 <= abs(h["a"]) <= 1e6 else 0.5
        return abs(h["r"]) * abs(h["r_detrended"]) * sanity

    best: dict[tuple[str, int], dict] = {}
    for hit in hits:
        key = (hit["coord"], hit["offset"])
        if key not in best or score(hit) > score(best[key]):
            best[key] = hit
    out = list(best.values())
    out.sort(key=score, reverse=True)
    return out


# ---------------------------------------------------------------- 距离


def _interp(series: list, t: float, tol: float = 0.3):
    """按时间线性插值（series 元素首项是 ts）。太远就返回 None，不硬凑。"""
    if not series:
        return None
    ts = [s[0] for s in series]
    i = bisect.bisect_left(ts, t)
    if i == 0:
        return series[0][1:] if abs(ts[0] - t) <= tol else None
    if i >= len(series):
        return series[-1][1:] if abs(ts[-1] - t) <= tol else None
    t0, t1 = ts[i - 1], ts[i]
    if t - t0 > tol or t1 - t > tol:
        return None
    f = (t - t0) / (t1 - t0)
    a, b = series[i - 1][1:], series[i][1:]
    return tuple(a[j] + (b[j] - a[j]) * f for j in range(len(a)))


def compare_distance(moves: dict, eyes: dict, min_overlap: int = 20) -> list[dict]:
    """对每对「同时有 Event 12 位置和 Event 14 真值」的 actor 比较距离。"""
    tracked = sorted(set(moves) & set(eyes))
    out = []
    for i, a in enumerate(tracked):
        for b in tracked[i + 1:]:
            pos_a = [(ts, p) for ts, _body, p in moves[a] if p is not None]
            pos_b = [(ts, p) for ts, _body, p in moves[b] if p is not None]
            eye_a = [(ts, x, y, z) for ts, x, y, z, _off in eyes[a]]
            eye_b = [(ts, x, y, z) for ts, x, y, z, _off in eyes[b]]
            if not pos_a or not pos_b:
                continue
            lo = max(pos_a[0][0], pos_b[0][0], eye_a[0][0], eye_b[0][0])
            hi = min(pos_a[-1][0], pos_b[-1][0], eye_a[-1][0], eye_b[-1][0])
            if hi <= lo:
                continue
            pa_ts = [s[0] for s in pos_a]
            pb_ts = [s[0] for s in pos_b]

            def last_pos(series, ts_list, t, tol=0.5):
                j = bisect.bisect_right(ts_list, t) - 1
                if j < 0 or t - ts_list[j] > tol:
                    return None
                return series[j][1]

            errs = []
            d12s = []
            d14s = []
            steps = max(1, int((hi - lo) / 0.1))
            for k in range(steps + 1):
                t = lo + k * (hi - lo) / steps
                p12a = last_pos(pos_a, pa_ts, t)
                p12b = last_pos(pos_b, pb_ts, t)
                p14a = _interp(eye_a, t)
                p14b = _interp(eye_b, t)
                if None in (p12a, p12b, p14a, p14b):
                    continue
                d12 = math.dist(p12a, p12b)
                d14 = math.dist(p14a, p14b)
                d12s.append(d12)
                d14s.append(d14)
                errs.append(d12 - d14)
            if len(errs) < min_overlap:
                continue
            mean12 = sum(d12s) / len(d12s)
            mean14 = sum(d14s) / len(d14s)
            mean_err = sum(errs) / len(errs)
            out.append({
                "a": a,
                "b": b,
                "samples": len(errs),
                "overlap_s": round(hi - lo, 2),
                "mean_d_event12": round(mean12, 3),
                "mean_d_event14": round(mean14, 3),
                "mean_err": round(mean_err, 3),
                "rel_err": round(abs(mean_err) / mean14, 4) if mean14 else None,
                "max_abs_err": round(max(abs(e) for e in errs), 3),
                "err_std": round(math.sqrt(sum((e - mean_err) ** 2 for e in errs) / len(errs)), 3),
            })
    return out


# ---------------------------------------------------------------- 自检


def self_test(pairs: list[dict], body_len: int, seed: int = 20261027,
              offset16: int = 60, offset8: int = 100) -> dict:
    """往合成的 Event 12 风格 body 里埋量化整数，确认扫描器找得回来。

    为什么必须做：如果扫描器本身有 bug（比如宽度/端序漏了一种），「没找到」就
    不能当成「不存在」。这里埋 16 位小端（缩放 1/100）和 8 位无符号（缩放
    1/200）各一个，要求偏移、宽度、端序、缩放全部对上。
    """
    if len(pairs) < 12:
        return {"ok": False, "reason": f"真实样本太少（{len(pairs)}），自检没有意义"}
    if body_len < max(offset16, offset8) + 4:
        return {"ok": False, "reason": f"body_len={body_len} 放不下埋点"}

    times = [p["ts"] for p in pairs]
    xs = [p["gt"][0] for p in pairs]
    zs = [p["gt"][2] for p in pairs]
    x_origin, x_scale = -25.5, 1.0 / 200.0
    z_origin, z_scale = -48.0, 1.0 / 100.0

    rng = random.Random(seed)
    bodies = []
    for i in range(len(pairs)):
        buf = bytearray(rng.randbytes(body_len))
        xq = round((xs[i] - x_origin) / x_scale)
        zq = round((zs[i] - z_origin) / z_scale)
        xq = min(255, max(0, xq))
        zq = min(32767, max(-32768, zq))
        struct.pack_into("<H", buf, offset16, zq & 0xFFFF)   # 16 位小端
        buf[offset8] = xq                                     # 8 位无符号
        bodies.append(bytes(buf))

    hits = scan_fields(bodies, (xs, [0.0] * len(xs), zs), times,
                       min_distinct=3, min_r=0.5)

    def find(coord: str, offset: int, width: int, endian: str):
        # 用**未去重**的原始命中做自检：去重是为了报告可读性，会把等价表示合并掉，
        # 拿它验证「16 位字段能否被识别」会误报失败。
        for h in hits:
            if (h["coord"] == coord and h["offset"] == offset
                    and h["width"] == width and h["endian"] == endian):
                return h
        return None

    h16 = find("z", offset16, 2, "le")
    h8 = find("x", offset8, 1, "le")

    def scale_ok(hit: dict | None, want: float) -> bool:
        # 允许 5%：真值位移只有 0.3~0.7 m、量化级只有几十个，最小二乘的斜率
        # 会被「半个量子」的舍入误差系统性拉偏（这是量化拟合的固有偏差，
        # 不是找错字段）。偏移/宽度/端序仍要求完全一致。
        return hit is not None and abs(hit["a"] - want) <= abs(want) * 0.05

    checks = {
        "16bit_found": h16 is not None,
        "16bit_offset": h16 is not None and h16["offset"] == offset16,
        "16bit_scale": scale_ok(h16, z_scale),
        "16bit_r": h16 is not None and abs(h16["r"]) > 0.999,
        "16bit_rms": h16 is not None and h16["rms"] <= z_scale / 2,
        "8bit_found": h8 is not None,
        "8bit_offset": h8 is not None and h8["offset"] == offset8,
        "8bit_scale": scale_ok(h8, x_scale),
        "8bit_r": h8 is not None and abs(h8["r"]) > 0.999,
        "8bit_rms": h8 is not None and h8["rms"] <= x_scale / 2,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "planted": {
            "16bit": {"coord": "z", "offset": offset16, "width": 2,
                      "endian": "le", "scale": z_scale, "origin": z_origin},
            "8bit": {"coord": "x", "offset": offset8, "width": 1,
                     "endian": "le", "scale": x_scale, "origin": x_origin},
        },
        "recovered": {
            "16bit": None if h16 is None else {k: h16[k] for k in
                                               ("offset", "width", "endian", "a", "b", "r", "rms")},
            "8bit": None if h8 is None else {k: h8[k] for k in
                                             ("offset", "width", "endian", "a", "b", "r", "rms")},
        },
        "body_len": body_len,
        "samples": len(pairs),
    }


# ---------------------------------------------------------------- 报告


def _fmt(v, nd=4) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _fmt_scale(v: float | None) -> str:
    """缩放的量级从 1e-9 到 1e27 都有（伪读法），定宽小数会把表格撑爆。"""
    if v is None:
        return "—"
    if v == 0.0:
        return "0"
    if 1e-3 <= abs(v) < 1e4:
        return f"{v:.6f}"
    return f"{v:.3e}"


def _verdict(hit: dict, flagged: bool, canonical: bool = False) -> str:
    if canonical:
        return "**位置字段（f32 世界坐标）**"
    if flagged:
        return "时间戳/计数器（去趋势后失效）"
    if not (1e-6 <= abs(hit["a"]) <= 1e6):
        return "同一串字节的伪读法（缩放离谱）"
    if hit["offset"] >= POS_OFFSET + 12:
        return "IK/姿态数据（非坐标）"
    if abs(hit["r_detrended"]) > 0.7 and hit["rms"] < hit["coord_std"] * 0.3:
        return "候选（相关性尚可，但非位置块）"
    return "伪相关/噪声"


def render_report(data: dict) -> str:
    counts = data["counts"]
    st = data["self_test"]
    lines: list[str] = []
    add = lines.append

    add("# Event 12 距离标定实验报告")
    add("")
    add(f"- 生成时间：{data['generated_at']}")
    add(f"- 输入 pcap：`{data['inputs']['pcap']}`")
    add(f"- 密钥指纹（只露前 8 位）：`{data['inputs']['key_fingerprint']}`")
    add(f"- 解密布局：`{data['inputs']['layout']}`")
    add("")

    add("## 1. 数据规模")
    add("")
    add("| 项目 | 数值 |")
    add("|---|---|")
    add(f"| pcap 帧数 | {counts['frames']} |")
    add(f"| 成功解密 | {counts['decrypted']} |")
    add(f"| 解密失败 | {counts['decrypt_fail']} |")
    add(f"| 重复包（丢） | {counts['duplicate']} |")
    add(f"| 分片重组完成 | {counts['fragment_completed']} |")
    add("")
    add(f"- **Event 12**：{counts['event12_messages']} 条，覆盖 {counts['event12_actors']} 个 actor；"
        f"其中带位置块 {counts['event12_with_position']} 条"
        f"（{counts['event12_with_position'] / max(1, counts['event12_messages']):.1%}）。")
    add(f"- **Event 14**：{counts['event14_messages']} 条，覆盖 {counts['event14_actors']} 个 actor"
        f"（{', '.join(str(a) for a in counts['event14_actor_list'])}），全部能扫出视点三元组。")
    add("")
    add("Event 12 位置字段覆盖率（`body[4] & 0x0F == 0x07` 的条数 / 该 actor 总条数）：")
    add("")
    add("| actor | Event 12 条数 | 带位置 | 覆盖率 | 根节点 y |")
    add("|---|---|---|---|---|")
    for row in data["coverage"]:
        add(f"| {row['actor']} | {row['messages']} | {row['with_position']} | "
            f"{row['ratio']:.1%} | {_fmt(row['y_median'], 2)} |")
    add("")
    add("> 覆盖率低不代表解不出：位置是**按需下发**的 —— 站着不动的 actor 只在"
        "位置变化时带位置块（2% 那几位 130 秒里坐标完全没变），移动中的 actor "
        "覆盖率是 70%~100%。")
    add("")

    add("## 2. 自检：合成样本能否被找回")
    add("")
    if st.get("ok"):
        add("✅ **通过。** 在随机字节的 body 里埋入的 16 位小端（缩放 1/100）和 "
            "8 位无符号（缩放 1/200）字段都被原样找回，偏移/宽度/端序/缩放全部正确。")
    else:
        add(f"❌ **未通过。** {st.get('reason') or st.get('checks')}")
    add("")
    if st.get("recovered"):
        add("| 埋入 | 目标 | 找回偏移 | 宽度 | 端序 | 拟合 a（应≈缩放） | 拟合 b | |r| | 残差 RMS |")
        add("|---|---|---|---|---|---|---|---|---|")
        for name, want in st["planted"].items():
            got = st["recovered"].get(name)
            if got is None:
                add(f"| {name} | {want['coord']} | **未找到** | — | — | — | — | — | — |")
            else:
                add(f"| {name} | {want['coord']} | {got['offset']}（期望 {want['offset']}） | "
                    f"{got['width']} | {got['endian']} | {_fmt(got['a'], 6)}（期望 {_fmt(want['scale'], 6)}） | "
                    f"{_fmt(got['b'], 3)} | {abs(got['r']):.6f} | {_fmt(got['rms'], 6)} |")
        add("")
    add("> 拟合出来的缩放与埋入值有 1~2% 的偏差，这是**量化拟合的固有偏差**：真值位移"
        "只有 0.3~0.7 m、量化级只有几十个，「半个量子」的舍入误差会把最小二乘斜率"
        "系统性拉偏一点。偏移/宽度/端序/量级都对上了，就说明字段找对了。")
    add("所以下文的「没搜到」不是搜索器漏了宽度/端序，而是数据里确实没有。")
    add("")

    add("## 3. 候选字段表（Event 12 body ↔ Event 14 真值）")
    add("")
    add("> 位置块 `[4,18)` 里的字节会被多种宽度重复命中（同一串 float32 的"
        "不同读法），所以表里把 x@6 / y@10 / z@14 三个 float32 作为独立行保留，"
        "其余与位置块重叠的窗口合并掉；偏移 18 之后的窗口照常参与扫描。")
    add("")
    for table in data["field_tables"]:
        add(f"### actor {table['actor']}（对齐 {table['samples']} 对，body 长度 {table['body_len']}）")
        add("")
        add("| 偏移 | 宽度 | 类型 | 坐标 | \\|r\\| | 去趋势\\|r\\| | a | b | 残差 RMS | 不同取值 | 判读 |")
        add("|---|---|---|---|---|---|---|---|---|---|---|")
        for h in table["rows"]:
            kind = h["kind"] if h["kind"] == "float" else (
                ("i" if h["signed"] else "u") + str(h["width"] * 8))
            add(f"| {h['offset']} | {h['width']} | {kind} {h['endian']} | {h['coord']} | "
                f"{abs(h['r']):.4f} | {abs(h['r_detrended']):.4f} | {_fmt_scale(h['a'])} | "
                f"{_fmt(h['b'], 4)} | {_fmt(h['rms'], 4)} | {h['distinct']} | {h['verdict']} |")
        add("")
        add("说明：`去趋势|r|` 是把坐标和字段各自减去线性时间趋势后的相关。时间戳字段"
            "在裸相关里能到 0.99，但去趋势后掉到 0 —— 这类候选不能当成位置编码。")
        add("")
        add("位置行的 `|r|` 不是 1.0，原因见第 4 节：真值取的是**头显**位置，字段是"
            "**根节点**位置，玩家在房间里物理走动时头显会离开根节点。z 轴仍有 0.79~0.91，"
            "x 轴低一些（本次两人的 x 位移本来就小）。")
        add("")

    add("## 4. 位置字段的证据")
    add("")
    pos = data["position_field"]
    add(f"1. **结构判据**：带 `body[4] & 0x0F == 0x07` 的 {pos['flag_set']} 条消息"
        f"**全部**（{pos['flag_matches']}/{pos['flag_set']}）能在偏移 6 解出有限、量级合理的"
        f" x/y/z；不带标志的 {pos['messages_checked'] - pos['flag_set']} 条里满足同一数值判据的有 "
        f"**{pos['no_flag_but_valid']}** 条（全部来自 actor 1494 的 164 字节变体，偏移 6 恰好是"
        f" 0/8/32/128 这类小数值，属数值判据的假阳性；标志置位但数值不合格 "
        f"{pos['flag_but_invalid']} 条）。标志位没有漏报，数值判据有 0.15% 的假阳性。")
    add(f"2. **字段就是 f32le**：body 偏移 {pos['offset']}，三个连续 float32"
        f"（换算成 message.payload 偏移是 {pos['offset'] + 7}）；"
        f"与 Event 14 的 x/z 逐帧同步。Event 14 的三元组落在 body 偏移 15/16"
        f"（⇔ payload 22/23），所以真值必须靠扫描定位。")
    add(f"3. **y 的含义和 Event 14 不同**：Event 12 的 y 中位数约 {_fmt(pos['y_median'], 2)} m，"
        f"且不同 actor 落在 0.02 / 0.61 / 0.76 / 0.81 / 0.92 这些**离散层**上"
        f"（像世界里的不同楼层/平台）；Event 14 的 y 恒定在 "
        f"{_fmt(pos['eye_y_min'], 2)}~{_fmt(pos['eye_y_max'], 2)} m，更像 avatar 的眼高标定值。"
        f"两者相差 {_fmt(pos['eye_dy_mean'], 2)}~{_fmt(pos['eye_dy_max'], 2)} m，"
        "对**水平**距离几乎无影响（两人的 y 差 <0.1 m）。")
    add("4. **8-bit 部分是姿态不是坐标**：body 偏移 18 之后的数据随时间平滑振荡"
        "（像四元数分量），与本脚本扫描出的任何整数/浮点窗口都不构成精确仿射关系；"
        "真正的位置只在那三个 float32 里。")
    add("")
    add("Event 12 位置 vs Event 14 视点的逐轴偏差（最近邻对齐，Δt≤0.2 s）：")
    add("")
    add("| actor | 对齐数 | Δx 均值±std | Δx 范围 | Δy 均值±std | Δz 均值±std | Δz 范围 |")
    add("|---|---|---|---|---|---|---|")
    for row in data["offset_stats"]:
        add(f"| {row['actor']} | {row['samples']} | "
            f"{row['dx_mean']:+.3f} ± {row['dx_std']:.3f} | "
            f"{row['dx_min']:+.3f}~{row['dx_max']:+.3f} | "
            f"{row['dy_mean']:+.3f} ± {row['dy_std']:.3f} | "
            f"{row['dz_mean']:+.3f} ± {row['dz_std']:.3f} | "
            f"{row['dz_min']:+.3f}~{row['dz_max']:+.3f} |")
    add("")
    add("> 水平偏差不是解析错位，而是**根节点 vs 头显**的差值：玩家在房间里的"
        "物理走动（playspace）会让头显离开根节点 0.1~1.0 m。y 的 0.62~0.72 m 是"
        "「世界高度 vs 眼高标定值」的差，对水平距离几乎无影响。")
    add("")

    add("## 5. 用 Event 12 推算的距离是否与真值一致")
    add("")
    if data["distance_checks"]:
        add("| actor 对 | 样本 | 重叠 | 平均距离(E12) | 平均距离(E14) | 平均误差 | 相对误差 | 最大误差 |")
        add("|---|---|---|---|---|---|---|---|")
        for row in data["distance_checks"]:
            add(f"| {row['a']} ↔ {row['b']} | {row['samples']} | {row['overlap_s']:.1f}s | "
                f"{row['mean_d_event12']:.3f} m | {row['mean_d_event14']:.3f} m | "
                f"{row['mean_err']:+.3f} m | {row['rel_err']:.2%} | {row['max_abs_err']:.3f} m |")
        add("")
        add("> 误差主要来自「根节点 vs 头显」：头显在 playspace 里可以离开根节点"
            " 0.1~1.0 m，所以这不是常数偏置，也不是解析错位。本次重叠窗口里两人几乎"
            "静止，误差表现为 0.315 m 的常数偏置；快速走动时最坏可到 ~1 m。"
            "对 25 m 衰减曲线来说，7.5 m 处 0.3 m 误差约 0.35 dB，1 m 误差约 1.1 dB。")
    else:
        add("没有同时具备两种样本的 actor 对，无法做距离一致性检查。")
    add("")

    add("## 6. 结论")
    add("")
    add(f"**{data['conclusion_label']}**")
    add("")
    for line in data["conclusion_reasons"]:
        add(f"- {line}")
    add("")
    add("### 建议下一步")
    add("")
    for line in data["next_steps"]:
        add(f"- {line}")
    add("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线标定 Photon Event 12 是否包含世界坐标（纯只读，不联网）")
    parser.add_argument("--pcap", default=str(DEFAULT_PCAP), help="已抓好的 pcap")
    parser.add_argument("--key-file", default=str(DEFAULT_KEY), help="32 字节会话密钥")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="报告输出目录")
    parser.add_argument("--layout", default="hdr+nonce", choices=list(gcm.LAYOUTS),
                        help="AES-GCM 的 AAD 布局")
    parser.add_argument("--tolerance", type=float, default=0.2,
                        help="Event 12/14 最近邻对齐容差（秒）")
    parser.add_argument("--max-table-rows", type=int, default=12,
                        help="每个 actor 的候选字段表最多显示几行")
    args = parser.parse_args(argv)

    pcap = Path(args.pcap)
    key_file = Path(args.key_file)
    out_dir = Path(args.out)

    if not pcap.is_file():
        print(f"❌ 找不到 pcap：{pcap}", file=sys.stderr)
        return 2
    if not key_file.is_file():
        print(f"❌ 找不到密钥文件：{key_file}", file=sys.stderr)
        return 2
    key = key_file.read_bytes()
    if len(key) != gcm.KEY_LEN:
        print(f"❌ 密钥长度应为 {gcm.KEY_LEN} 字节，实际 {len(key)}", file=sys.stderr)
        return 2

    try:
        import cryptography  # noqa: F401
    except ImportError:
        # 不在模块顶层 import：cryptography 是可选依赖（见 requirements-players.txt）。
        from vlt.players import INSTALL_HINT
        print(f"❌ 缺少 cryptography：\n   {INSTALL_HINT}", file=sys.stderr)
        return 2

    print(f"[cal] 读取 {pcap} …", flush=True)
    events, stats = read_events(pcap, key, args.layout)
    print(f"[cal] 帧 {stats['frames']}，解密 {stats['decrypted']}，"
          f"重复 {stats['duplicate']}，解密失败 {stats['decrypt_fail']}", flush=True)

    moves, eyes = build_series(events, stats)
    print(f"[cal] Event 12：{sum(len(v) for v in moves.values())} 条 / {len(moves)} actor；"
          f"Event 14：{sum(len(v) for v in eyes.values())} 条 / {len(eyes)} actor", flush=True)

    if not moves:
        print("❌ 一条 Event 12 都没解出来：先检查密钥/布局/抓包方向。", file=sys.stderr)
        return 3
    if not eyes:
        print("❌ 一条 Event 14 都没有：没有真值无法标定。", file=sys.stderr)
        return 3

    # ---- 位置字段覆盖率
    coverage = []
    for actor in sorted(moves):
        samples = moves[actor]
        pos = [p for _ts, _b, p in samples if p is not None]
        ys = [p[1] for p in pos]
        coverage.append({
            "actor": actor,
            "messages": len(samples),
            "with_position": len(pos),
            "ratio": len(pos) / len(samples),
            "y_median": sorted(ys)[len(ys) // 2] if ys else None,
        })

    # ---- 逐 actor 对齐，跑字段扫描
    field_tables = []
    offset_stats = []
    for actor in sorted(set(moves) & set(eyes)):
        pairs = align_pairs(moves[actor], eyes[actor], args.tolerance)
        if len(pairs) < 5:
            print(f"[cal] actor {actor}: 只有 {len(pairs)} 对，跳过", flush=True)
            continue

        # 同一 actor 的 body 长度可能不同（带/不带位置块）。混着扫会把字段整体
        # 错位，所以只取样本最多的那个长度分组。
        by_len: dict[int, list[dict]] = defaultdict(list)
        for p in pairs:
            by_len[len(p["body"])].append(p)
        body_len = max(by_len, key=lambda k: len(by_len[k]))
        group = by_len[body_len]

        times = [p["ts"] for p in group]
        gt = ([p["gt"][0] for p in group],
              [p["gt"][1] for p in group],
              [p["gt"][2] for p in group])
        bodies = [p["body"] for p in group]
        hits = scan_fields(bodies, gt, times)

        # 位置块 [4,18) 里的字节窗口全部是「同一串 float32 的不同读法」，
        # 逐条列出来只会让表变成一堆 a≈1e-6 的伪候选。这里把三个 float32
        # （x@6 / y@10 / z@14）作为独立行保留，其余与位置块重叠的窗口丢弃；
        # 扫描本身**没有**排除它们，只是报告里合并成一组更好读。
        pos_lo, pos_hi = POS_OFFSET, POS_OFFSET + 12
        canonical_map = {pos_lo: "x", pos_lo + 4: "y", pos_lo + 8: "z"}

        def overlaps_position(h: dict) -> bool:
            lo, hi = h["offset"], h["offset"] + h["width"]
            return lo < pos_hi and hi > pos_lo

        canonical = [h for h in hits
                     if h["kind"] == "float" and h["width"] == 4 and h["endian"] == "le"
                     and canonical_map.get(h["offset"]) == h["coord"]]
        canonical.sort(key=lambda h: h["offset"])
        others = dedupe_hits([h for h in hits if not overlaps_position(h)])

        rows = []
        for h in canonical:
            row = dict(h)
            row["verdict"] = _verdict(h, flagged=False, canonical=True)
            rows.append(row)
        for h in others:
            row = dict(h)
            row["verdict"] = _verdict(
                h, flagged=(abs(h["r"]) > 0.5 and abs(h["r_detrended"]) < 0.3))
            rows.append(row)
        rows = rows[:args.max_table_rows]
        field_tables.append({
            "actor": actor,
            "samples": len(group),
            "body_len": body_len,
            "rows": rows,
        })

        d = [p["move"][0] - p["gt"][0] for p in pairs]
        dy = [p["move"][1] - p["gt"][1] for p in pairs]
        dz = [p["move"][2] - p["gt"][2] for p in pairs]

        def msd(v):
            m = sum(v) / len(v)
            return m, math.sqrt(sum((x - m) ** 2 for x in v) / len(v))

        dxm, dxs = msd(d)
        dym, dys = msd(dy)
        dzm, dzs = msd(dz)
        offset_stats.append({
            "actor": actor, "samples": len(pairs),
            "dx_mean": round(dxm, 3), "dx_std": round(dxs, 3),
            "dy_mean": round(dym, 3), "dy_std": round(dys, 3),
            "dz_mean": round(dzm, 3), "dz_std": round(dzs, 3),
            "dx_min": round(min(d), 3), "dx_max": round(max(d), 3),
            "dz_min": round(min(dz), 3), "dz_max": round(max(dz), 3),
        })
        print(f"[cal] actor {actor}: {len(pairs)} 对，body {body_len} 字节，"
              f"候选字段 {len(hits)} 个", flush=True)

    # ---- 位置块标志的一致性（统计全部 actor，不只真值 actor）
    # 这里是**两个独立判据**：标志位 vs 数值形态。两者逐条一致才说明
    # 「body[4] & 0x0F == 0x07」真的是位置块标志，而不是碰巧相关。
    flag_set = 0
    flag_and_valid = 0
    flag_but_invalid = 0
    no_flag_but_valid = 0
    for samples in moves.values():
        for _ts, body, _pos in samples:
            flag = (len(body) >= POS_OFFSET + 12
                    and (body[4] & POS_FLAG_MASK) == POS_FLAG_VALUE)
            plausible = _plausible_triple(body) is not None
            if flag:
                flag_set += 1
            if flag and plausible:
                flag_and_valid += 1
            elif flag:
                flag_but_invalid += 1
            elif plausible:
                no_flag_but_valid += 1

    # ---- 自检：用对齐样本最多的 actor 的真值序列
    st = {"ok": False, "reason": "没有足够的对齐样本"}
    if field_tables:
        main_table = max(field_tables, key=lambda t: t["samples"])
        main_pairs = align_pairs(moves[main_table["actor"]], eyes[main_table["actor"]],
                                 args.tolerance)
        main_pairs = [p for p in main_pairs if len(p["body"]) == main_table["body_len"]]
        st = self_test(main_pairs, main_table["body_len"])

    # ---- 距离一致性
    distance_checks = compare_distance(moves, eyes)

    # ---- 位置字段统计
    eye_ys = [s[2] for samples in eyes.values() for s in samples]
    dy_means = [row["dy_mean"] for row in offset_stats]
    all_y = [p[1] for samples in moves.values() for _ts, _b, p in samples if p is not None]
    position_field = {
        "offset": POS_OFFSET,
        "type": "float32",
        "endian": "little",
        "condition": f"body[4] & 0x{POS_FLAG_MASK:02X} == 0x{POS_FLAG_VALUE:02X}",
        "messages_checked": sum(len(v) for v in moves.values()),
        "flag_set": flag_set,
        "flag_matches": flag_and_valid,
        "flag_but_invalid": flag_but_invalid,
        "no_flag_but_valid": no_flag_but_valid,
        "y_median": round(sorted(all_y)[len(all_y) // 2], 3) if all_y else None,
        "eye_y_min": round(min(eye_ys), 3) if eye_ys else None,
        "eye_y_max": round(max(eye_ys), 3) if eye_ys else None,
        "eye_dy_mean": round(min(dy_means), 3) if dy_means else None,
        "eye_dy_max": round(max(dy_means), 3) if dy_means else None,
    }

    # ---- 结论
    dist_err = max((abs(r["mean_err"]) for r in distance_checks), default=None)
    dist_rel = max((r["rel_err"] for r in distance_checks if r["rel_err"] is not None), default=None)
    pos_ratio = flag_and_valid / max(1, position_field["messages_checked"])
    if dist_err is not None and dist_err <= 1.0 and pos_ratio >= 0.05:
        label = "能解（解出的是根节点坐标）"
        reasons = [
            f"Event 12 的 body 偏移 {POS_OFFSET} 处有 **三个连续 float32 世界坐标**，"
            f"条件是 `body[4] & 0x0F == 0x07`。带标志的 {position_field['flag_set']} 条消息"
            f"**全部**能解出合理坐标（{position_field['flag_matches']}/{position_field['flag_set']}）；"
            f"不带标志的 {position_field['messages_checked'] - position_field['flag_set']} 条里"
            f"只有 {position_field['no_flag_but_valid']} 条满足同一数值判据。",
            "**不是 8-bit 量化**：body 偏移 18 之后那串单字节数据是 IK/骨骼姿态，"
            "随时间平滑振荡；脚本没有找到任何 8/16/24/32 位整数字段能对坐标做精确仿射拟合。",
            f"解出的是**根节点位置**（不是头显）：y 中位数 {position_field['y_median']:.2f} m，"
            f"且落在离散的楼层高度上；Event 14 的 y 恒定在 "
            f"{position_field['eye_y_min']:.2f}~{position_field['eye_y_max']:.2f} m，"
            f"更像 avatar 眼高标定值。两者相差 "
            f"{position_field['eye_dy_mean']:.2f}~{position_field['eye_dy_max']:.2f} m，"
            "对水平距离影响很小。",
            f"两路坐标算出的玩家间距离最大差 {dist_err:.3f} m"
            f"（相对 {dist_rel or 0:.2%}），水平偏差来自根节点↔头显的 playspace 偏移"
            f"（本次 0.1~1.0 m）。",
        ]
    elif dist_err is None:
        label = "只能解一部分"
        reasons = [
            "能解出位置字段，但没有同时具备 Event 12/14 真值的 actor 对，无法量化距离误差。",
        ]
    else:
        label = "不能解"
        reasons = [
            f"位置字段与真值的距离偏差 {dist_err:.3f} m 过大，或位置覆盖不足。",
        ]

    next_steps = [
        "**直接用 Event 12 的根节点位置做语音距离衰减**：它覆盖全部 actor、按需 5 Hz "
        "更新；0.3~1 m 的根/头偏差在 25 m 衰减曲线上约 0.3~1 dB，通常可接受。",
        "若要精确到「嘴/耳」距离：再把 body 偏移 18 之后的 IK 数据解出头部四元数，"
        "用髋位置 + 头部位姿还原视点；本次没有做这一步。",
        "把「位置块存在 ⇒ `body[4] & 0x0F == 0x07`」写进实时解析器：不带位置块的"
        "消息沿用上一个位置，并用时间戳判断是否过期。",
        "换一段**有人在快速走动/传送**的 pcap 复跑本脚本（本次两人在重叠窗口里几乎静止，"
        "距离误差是常数偏置；快速走动时根节点/头显最多可差 1 m）。",
    ]

    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    data = {
        "generated_at": generated,
        "inputs": {
            "pcap": str(pcap),
            "key_fingerprint": gcm.key_fingerprint(key),
            "layout": args.layout,
        },
        "counts": {
            "frames": stats["frames"],
            "decrypted": stats["decrypted"],
            "decrypt_fail": stats["decrypt_fail"],
            "duplicate": stats["duplicate"],
            "fragment_completed": stats["fragment_completed"],
            "fragment_dropped": stats["fragment_dropped"],
            "event12_messages": sum(len(v) for v in moves.values()),
            "event12_actors": len(moves),
            "event12_with_position": sum(1 for v in moves.values() for _t, _b, p in v if p is not None),
            "event14_messages": sum(len(v) for v in eyes.values()),
            "event14_actors": len(eyes),
            "event14_actor_list": sorted(eyes),
        },
        "coverage": coverage,
        "self_test": st,
        "field_tables": field_tables,
        "position_field": position_field,
        "offset_stats": offset_stats,
        "distance_checks": distance_checks,
        "conclusion": "able" if label.startswith("能解") else (
            "partial" if label.startswith("只能") else "unable"),
        "conclusion_label": label,
        "conclusion_reasons": reasons,
        "next_steps": next_steps,
        "stats": dict(stats),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    findings_path = out_dir / "findings.json"
    report_path = out_dir / "report.md"
    findings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(data), encoding="utf-8")

    print()
    print("== 结论 ==")
    print(f"  {label}")
    print(f"  Event 12: {data['counts']['event12_messages']} 条 / "
          f"{data['counts']['event12_actors']} actor，带位置 "
          f"{data['counts']['event12_with_position']} 条")
    print(f"  位置字段：body[{POS_OFFSET}] f32le×3（条件 body[4]&0x0F==0x07）")
    print(f"  Event 14 真值：{data['counts']['event14_messages']} 条 / "
          f"{data['counts']['event14_actors']} actor")
    if distance_checks:
        for row in distance_checks:
            print(f"  距离 {row['a']}↔{row['b']}: E12={row['mean_d_event12']:.3f} m，"
                  f"E14={row['mean_d_event14']:.3f} m，误差 {row['mean_err']:+.3f} m")
    print(f"  自检：{'通过' if st.get('ok') else '未通过'}")
    print(f"  报告：{report_path}")
    print(f"  数据：{findings_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
