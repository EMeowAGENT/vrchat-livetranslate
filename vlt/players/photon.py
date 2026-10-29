"""Photon v5 命令帧、分片重组、事件参数与 USpeak 语音帧。

## 为什么要写这么"防御性"的解析

P0-b 的输入是**真实流量**，而协议细节（类型表、字符串长度字节序、事件参数表
的计数宽度）只在别人的逆向笔记里出现过，没有官方文档。所以这里的策略是：

1. 按已知布局**正向解析**；
2. 任何一步对不上就**记录错误并保留已解析的部分**（`Message.error`），绝不
   静默返回"没有语音"；
3. 语音与发送者另有一条**不依赖参数表**的保底路径（`scan_voice_body` /
   `scan_actor`）——直接找 `F5 43`（参数 245 = 音频，类型 0x43 = byte[]）这类
   固定字节对，并用 opus 帧自校验确认真伪。

这样即使类型表有一处猜错，P0-b 仍能解出音频，报告里也会留下足够的十六进制证据
让我们当场修。

## 已知布局（来源：VoxTrace `docs/PROTOCOL.md`，P0-b 复核）

```
明文：[command_count:1][timestamp:4]
command：[type:1][channel:1][flags:1][reserved:1][length:4 BE][seq:4 BE][payload]
type=8 分片：payload 头 5 个 u32 BE = startSeq, fragCount, fragIndex, totalLength, fragOffset
消息：payload[0]==0xF3；type 2=op请求 3=op响应 4=事件；事件码在第 3 字节
事件参数：count:1，随后 [paramCode:1][type:1][value]
USpeak 音频（参数 245）：[可选 4 字节头][u16 index][u16 size][opus…] 重复
```
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass

MSG_MARKER = 0xF3
TYPE_OP_REQUEST = 2
TYPE_OP_RESPONSE = 3
TYPE_EVENT = 4
FRAGMENT_TYPE = 8

MAX_MESSAGE = 1 << 20          # 单条消息上限 1MB
MAX_FRAGMENTS = 1024
FRAG_TTL_S = 2.0
MAX_FRAMES_PER_MESSAGE = 64

PARAM_AUDIO = 245
PARAM_SENDER = 254
PARAM_PROPERTIES = 253

# 参数类型码（VoxTrace 实测：0x43=byte[]、0x0D=int16、0x0B=byte、0x08=null、0xFE=sender）
PT_BOOLEAN = 0x02
PT_BYTE8 = 0x03          # Protocol16 的 Byte（出向 RaiseEvent 的事件码就是它）
PT_SHORT = 0x04
PT_FLOAT = 0x05
PT_DOUBLE = 0x06
PT_NULL = 0x08
PT_COMPRESSED_INT = 0x09
PT_COMPRESSED_LONG = 0x0A
PT_BYTE = 0x0B
PT_INT1 = 0x0C
PT_INT2 = 0x0D
PT_INT2_ = 0x0E
PT_STRING = 0x07
PT_BYTE_ARRAY = 0x43
PT_SENDER = 0xFE


@dataclass(frozen=True)
class Command:
    kind: int
    channel: int
    flags: int
    seq: int
    payload: bytes


@dataclass(frozen=True)
class Param:
    code: int
    ptype: int
    value: object


@dataclass
class Message:
    kind: int
    code: int
    params: tuple[Param, ...] = ()
    error: str = ""
    payload: bytes = b""

    def param(self, code: int) -> Param | None:
        for p in self.params:
            if p.code == code:
                return p
        return None


# ---------------------------------------------------------------- 命令帧


def parse_commands(plain: bytes) -> tuple[list[Command], str]:
    """解析一条明文数据报。返回 (commands, error)。

    ⚠️ **`length` 是「含 12 字节命令头」的总长度**，不是 payload 长度。
    这条是 P0-b 用 13554 个真实包验证出来的：按"只算 payload"解释，其中
    12699 个包会越界（被静默截断，于是每个包只剩第一条命令能解析——语音量
    因此少了 3.6 倍）；按"含头"解释，**全部 13554 个包恰好用完、零残留**。
    """
    if len(plain) < 5:
        return [], "明文太短"
    count = plain[0]
    off = 5
    out: list[Command] = []
    for i in range(count):
        if off + 12 > len(plain):
            return out, f"第 {i} 条命令头越界（off={off}, len={len(plain)}）"
        kind, channel, flags, _reserved = plain[off], plain[off + 1], plain[off + 2], plain[off + 3]
        length, seq = struct.unpack_from("!II", plain, off + 4)
        if length < 12 or off + length > len(plain):
            return out, (f"第 {i} 条命令长度不合法（{length}，剩余 {len(plain) - off}）")
        out.append(Command(kind=kind, channel=channel, flags=flags, seq=seq,
                           payload=plain[off + 12:off + length]))
        off += length
    return out, ("" if off == len(plain) else f"命令区尾部残留 {len(plain) - off} 字节")


class Fragmenter:
    """按 (channel, startSeq) 的**并发**重组器（每连接每方向一个实例）。

    ⚠️ 这个类最容易被写错的地方：**分片是并发交错的**。真实流量里同一 channel
    上同时有多条消息在装配（P0-b 实测：10016 个包只认出 370 条消息，96% 的载荷
    是分片）。如果每个 channel 只留一个槽，第二条消息的第一个分片就会把第一条
    的槽顶掉 —— 现象正是"语音帧全部丢失、只偶尔剩下几个小包消息"。
    所以槽按 (channel, startSeq) 索引，并保留多个（上限 64，与 VRCS 的 16 并发
    同量级、留足余量）。
    """

    MAX_ACTIVE = 64

    def __init__(self) -> None:
        self._partial: dict[tuple[int, int], dict] = {}
        self.completed = 0
        self.dropped = 0          # 过期/被挤掉/异常的分片组
        self.rejected = 0         # 头部字段不合法的分片

    @property
    def active(self) -> int:
        """还在装配中的分片组数量（诊断用）。"""
        return len(self._partial)

    def _prune(self, now: float) -> None:
        stale = [k for k, v in self._partial.items() if now - v["at"] > FRAG_TTL_S]
        for key in stale:
            self._partial.pop(key, None)
            self.dropped += 1
        while len(self._partial) > self.MAX_ACTIVE:
            oldest = min(self._partial, key=lambda k: self._partial[k]["at"])
            self._partial.pop(oldest, None)
            self.dropped += 1

    def feed(self, cmd: Command) -> list[bytes]:
        if cmd.kind != FRAGMENT_TYPE:
            return [cmd.payload]
        if len(cmd.payload) < 20:
            self.rejected += 1
            return []
        start_seq, frag_count, frag_index, total_len, frag_offset = struct.unpack_from(
            "!IIIII", cmd.payload, 0)
        if (frag_count <= 0 or frag_count > MAX_FRAGMENTS
                or total_len <= 0 or total_len > MAX_MESSAGE
                or frag_index >= frag_count):
            self.rejected += 1
            return []
        part = cmd.payload[20:]
        if frag_offset + len(part) > total_len:
            self.rejected += 1
            return []

        now = time.monotonic()
        self._prune(now)
        slot_key = (cmd.channel, start_seq)
        slot = self._partial.get(slot_key)
        if slot is None:
            slot = {"at": now, "buf": bytearray(total_len), "got": set(),
                    "count": frag_count, "size": total_len}
            self._partial[slot_key] = slot
        slot["at"] = now
        # 按分片自带的 fragOffset 定位：实测顺序对不上（乱序/重传），按到达顺序
        # 拼会得到错位的音频。
        slot["buf"][frag_offset:frag_offset + len(part)] = part
        slot["got"].add(frag_index)
        if len(slot["got"]) < slot["count"]:
            return []
        self._partial.pop(slot_key, None)
        self.completed += 1
        return [bytes(slot["buf"])]


# ---------------------------------------------------------------- 消息与参数


class _UnknownType(Exception):
    def __init__(self, ptype: int, offset: int) -> None:
        super().__init__(f"未知参数类型 {ptype:#04x} @ {offset}")
        self.ptype = ptype
        self.offset = offset


def _read_leb128(data: bytes, off: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if off >= len(data):
            raise ValueError("LEB128 越界")
        byte = data[off]
        off += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, off
        shift += 7
        if shift > 35:
            raise ValueError("LEB128 过长")


def _read_value(data: bytes, off: int, ptype: int) -> tuple[object, int]:
    if ptype == PT_NULL:
        return None, off
    if ptype in (PT_BOOLEAN, PT_BYTE8):
        if off + 1 > len(data):
            raise ValueError("bool/byte 越界")
        return data[off], off + 1
    if ptype == PT_SHORT:
        if off + 2 > len(data):
            raise ValueError("short 越界")
        return struct.unpack_from("<h", data, off)[0], off + 2
    if ptype == PT_FLOAT:
        if off + 4 > len(data):
            raise ValueError("float 越界")
        return struct.unpack_from("<f", data, off)[0], off + 4
    if ptype == PT_DOUBLE:
        if off + 8 > len(data):
            raise ValueError("double 越界")
        return struct.unpack_from("<d", data, off)[0], off + 8
    if ptype in (PT_BYTE, PT_INT1):
        if off + 1 > len(data):
            raise ValueError("byte 越界")
        return data[off] if ptype == PT_BYTE else struct.unpack_from("<b", data, off)[0], off + 1
    if ptype in (PT_INT2, PT_INT2_):
        if off + 2 > len(data):
            raise ValueError("int16 越界")
        raw = struct.unpack_from("<H", data, off)[0]
        return (raw if ptype == PT_INT2 else struct.unpack_from("<h", data, off)[0]), off + 2
    if ptype == PT_COMPRESSED_INT:
        value, off = _read_leb128(data, off)
        return (value >> 1) ^ -(value & 1), off
    if ptype == PT_COMPRESSED_LONG:
        value, off = _read_leb128(data, off)
        return (value >> 1) ^ -(value & 1), off
    if ptype == PT_STRING:
        if off + 2 > len(data):
            raise ValueError("string 长度越界")
        size = struct.unpack_from(">H", data, off)[0]
        off += 2
        if off + size > len(data):
            raise ValueError("string 内容越界")
        return data[off:off + size].decode("utf-8", "replace"), off + size
    if ptype == PT_BYTE_ARRAY:
        size, off = _read_leb128(data, off)
        if size < 0 or off + size > len(data):
            raise ValueError(f"byte[] 越界（size={size}）")
        return data[off:off + size], off + size
    if ptype == PT_SENDER:
        # 发送者 actor：实测是 4 字节小端；给 2 字节留一条退路（值必须像 actor）。
        if off + 4 <= len(data):
            value = struct.unpack_from("<I", data, off)[0]
            if value < 1 << 16:
                return value, off + 4
        if off + 2 <= len(data):
            return struct.unpack_from("<H", data, off)[0], off + 2
        raise ValueError("sender 越界")
    raise _UnknownType(ptype, off)


def parse_message(payload: bytes) -> Message | None:
    """解析一条 Photon 消息。不是 `F3` 消息或长度不够返回 None。"""
    if len(payload) < 4 or payload[0] != MSG_MARKER:
        return None
    kind = payload[1]
    code = payload[2]
    off = 3
    params: list[Param] = []
    error = ""
    if off < len(payload):
        count = payload[off]
        off += 1
        for index in range(count):
            if off + 2 > len(payload):
                error = f"第 {index} 个参数头越界"
                break
            pcode, ptype = payload[off], payload[off + 1]
            off += 2
            try:
                value, off = _read_value(payload, off, ptype)
            except _UnknownType as exc:
                error = str(exc)
                break
            except ValueError as exc:
                error = f"参数 {pcode} 解析失败：{exc}"
                break
            params.append(Param(code=pcode, ptype=ptype, value=value))
    return Message(kind=kind, code=code, params=tuple(params), error=error, payload=payload)


def _read_len_u16le(data: bytes, off: int) -> tuple[int, int]:
    if off + 2 > len(data):
        raise ValueError("u16le 越界")
    return struct.unpack_from("<H", data, off)[0], off + 2


def _read_len_u16be(data: bytes, off: int) -> tuple[int, int]:
    if off + 2 > len(data):
        raise ValueError("u16be 越界")
    return struct.unpack_from(">H", data, off)[0], off + 2


def _read_len_u32le(data: bytes, off: int) -> tuple[int, int]:
    if off + 4 > len(data):
        raise ValueError("u32le 越界")
    return struct.unpack_from("<I", data, off)[0], off + 4


# byte[] 的长度前缀实测有两种说法（VoxTrace 说 LEB128；P0-b 的 "参数 245 解析失败"
# 说明至少还有一种）。这里全部试一遍，由 `valid`（opus 帧自校验）裁决 ——
# 这比"猜对哪一种"可靠得多。
_LEN_READERS = (_read_leb128, _read_len_u16le, _read_len_u16be, _read_len_u32le)


# 有些命令类型的 payload 在消息前多几个字节（P0-b 实测：type 11 的 payload 是
# `00 03 66 43` 再跟 `F3 04 01`，也就是不可靠通道多带了一个 4 字节序号）。
# 与其按类型硬编码，不如在**消息标志本身**（0xF3）上对齐：F3 在协议里是固定魔法值，
# 误命中率极低，后面 parse_message 还会再校验一遍结构。
PREFIX_SKIPS = (0, 4, 2, 8, 12)


def parse_message_at(payload: bytes) -> tuple[Message | None, int]:
    """在 payload 里找消息起点并解析。返回 (message, 跳过的字节数)。

    这条是 P0-b 的关键发现：**只看 payload[0] 是不是 `F3`** 会把整个不可靠通道
    （type 11/17，语音全在这里）静默丢掉 —— 现象是"解密全通、一个语音帧也没有"。
    """
    if not payload:
        return None, -1
    for skip in PREFIX_SKIPS:
        if len(payload) <= skip or payload[skip] != MSG_MARKER:
            continue
        msg = parse_message(payload[skip:])
        if msg is not None:
            return msg, skip
    return None, -1


def scan_voice_body(payload: bytes, valid=None) -> bytes | None:
    """保底路径：在消息里直接找 `F5 43`（参数 245 + byte[]）并读出音频体。

    不依赖参数表、**也不依赖事件码**（P0-b 实测：真实流量里连一个 Event 1 都没有，
    音频只出现在别的消息码里）。长度前缀会依次尝试 LEB128 / u16le / u16be / u32le；
    给了 `valid`（opus 帧校验）时，只有能切出合法帧的才算数 —— 这样即使长度编码
    猜错，也不会把垃圾当音频。
    """
    needle = bytes((PARAM_AUDIO, PT_BYTE_ARRAY))
    start = 0
    while True:
        pos = payload.find(needle, start)
        if pos < 0:
            return None
        start = pos + 1
        for reader in _LEN_READERS:
            try:
                size, off = reader(payload, pos + 2)
            except ValueError:
                continue
            # 下界取 8：USpeak 头 4 字节 + 单帧头 4 字节，再小的"音频体"不是音频。
            if not (8 <= size <= len(payload) - off):
                continue
            body = payload[off:off + size]
            if valid is None or valid(body):
                return body


EVENT_JOIN_WORLD = 226


def joined_world(payload: bytes) -> str | None:
    """出向的「加入世界」事件（`F3 02 E2`）里取 `wrld_` ID。

    VRChat 进世界时由客户端发一条 op 请求事件 226，body 里带世界 ID（含区域后缀，
    形如 `wrld_...:12345~region(jp)`）。拿到它就能在**换世界时立刻清表** ——
    actor 号只对「这个房间的这次加入」有意义，残留必然串名。

    事件码不可信（同语音那套），所以只在消息标志 `F3` 对齐之后再匹配。
    """
    for skip in PREFIX_SKIPS:
        if len(payload) < skip + 3:
            break
        if payload[skip] != MSG_MARKER:
            continue
        if payload[skip + 1] != TYPE_OP_REQUEST or payload[skip + 2] != EVENT_JOIN_WORLD:
            continue
        at = payload.find(b"wrld_", skip + 3)
        if at < 0:
            return None
        chars = bytearray()
        for byte in payload[at:at + 256]:
            if not (0x21 <= byte <= 0x7E):   # 可打印 ASCII
                break
            chars.append(byte)
        world = chars.decode("ascii", "replace")
        # 只认完整的世界 ID：`wrld_` + 36 位 UUID + `:实例号`（实测 ≥42 且带 `:`）。
        return world if len(world) >= 42 and ":" in world else None
    return None


def scan_actor(payload: bytes) -> int | None:
    """保底路径：找参数 254（发送者 actor）的取值，返回最后一个合理值。"""
    best: int | None = None
    start = 0
    while True:
        pos = payload.find(bytes((PARAM_SENDER,)), start)
        if pos < 0 or pos + 2 >= len(payload):
            return best
        start = pos + 1
        ptype = payload[pos + 1]
        try:
            value, _off = _read_value(payload, pos + 2, ptype)
        except (_UnknownType, ValueError):
            continue
        if isinstance(value, int) and 1 <= value < (1 << 16):
            best = value
    return best


# ---------------------------------------------------------------- USpeak 帧


@dataclass(frozen=True)
class VoiceFrame:
    index: int
    data: bytes


def _try_split(body: bytes, skip: int, endian: str, valid) -> list[VoiceFrame] | None:
    off = skip
    frames: list[VoiceFrame] = []
    while off + 4 <= len(body) and len(frames) < MAX_FRAMES_PER_MESSAGE:
        index, size = struct.unpack_from(f"{endian}HH", body, off)
        off += 4
        if size == 0 or off + size > len(body):
            return None
        data = body[off:off + size]
        off += size
        if valid is not None and not valid(data):
            return None
        frames.append(VoiceFrame(index=index, data=data))
    if not frames:
        return None
    # 允许尾部 0 填充（实测有这样的包），但不允许残留非零垃圾
    if any(body[off:]):
        return None
    return frames


def sequence_gap(prev: int | None, cur: int, *, max_fill: int = 50) -> tuple[int, bool]:
    """USpeak 的 u16 序号 → (要补的帧数, 是否重复帧)。

    P0-b 实测：真实流量里约 15% 的帧编号有跳跃（不可靠通道丢包），但也有大段
    跳跃是"对方停口不发了"。所以只补 ≤max_fill 的小缺口（保持时间轴连续），
    大跳跃按正常停顿处理，并用新序号重同步 —— 否则会在静音里插进几十秒的 0。

    返回的"补帧数"以 20ms 帧计；调用方乘以 320 个 16k 采样即可。
    """
    if prev is None:
        return 0, False
    delta = (cur - prev) & 0xFFFF
    if delta == 0:
        return 0, True
    if delta > max_fill:
        return 0, False
    return delta - 1, False


def split_voice_frames(body: bytes, valid=None) -> tuple[list[VoiceFrame], str]:
    """把 USpeak 音频体拆成 Opus 帧。

    `valid(bytes) -> bool` 由调用方给（opus 自校验）；给了就必须**每一帧**都通过。
    返回 (frames, layout) —— layout 形如 `skip=4/le`，写进报告便于核对。

    顺序刻意是「4 字节头 → 无头」，字节序「小端 → 大端」：VoxTrace 的笔记说 4 字节
    头 + LE，而 VRCS 的报告看起来像无头 + LE。用 opus 自校验来裁决，不靠猜。
    """
    for skip in (4, 0):
        for endian in ("<", ">"):
            frames = _try_split(body, skip, endian, valid)
            if frames:
                return frames, f"skip={skip}/{'le' if endian == '<' else 'be'}"
    return [], "unknown"
