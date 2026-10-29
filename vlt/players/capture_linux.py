"""Linux 抓 Photon UDP：`AF_PACKET` 原始套接字 + 内核 BPF 过滤 + pcap 落盘。

## 为什么是 `AF_PACKET` 而不是别的东西

- **不需要 Wintun/驱动**：那是 Windows 那套的答案（见 VRCS 的实现）。Linux 上
  `AF_PACKET` 本来就是内核提供的旁路接口，只要 `CAP_NET_RAW`。
- **被动**：只读自己网卡上收发的帧，不改变任何流量；VRChat 那边完全感知不到。
- **必须在内核里过滤**：这台机器同时跑 WiVRn（UDP 视频流，包量可以到几千/秒）。
  收上来再在 Python 里筛端口会把一个核吃满，所以把 `udp port 27002` 编成经典
  BPF 挂到 socket 上（`SO_ATTACH_FILTER`），让内核先丢。

## BPF 程序（IPv4 / 无 VLAN）

```
0  ldh  [12]                 ; ethertype
1  jeq  #0x0800 → 2 / → 8    ; 非 IPv4 丢
2  ldb  [23]                 ; ip.protocol
3  jeq  #0x11   → 4 / → 8    ; 非 UDP 丢
4  ldh  [36]                 ; udp.dport
5  jeq  #27002  → 9 / → 6    ; 命中收
6  ldh  [34]                 ; udp.sport
7  jeq  #27002  → 9 / → 8    ; 命中收
8  ret  #0                   ; 丢
9  ret  #262144              ; 收（快照上限 256KB）
```

偏移按「以太网 14 + IPv4 头 20」算。带 IP 选项的包（`ihl != 5`）会因此被错判 ——
实测 VRChat 不发这种包，但 Python 侧仍按 `ihl` 正确解析，并把 `ihl!=5` 的计数
打到报告里；真出现大比例就说明要改 BPF。

## 为什么抓**双向**

- `dst_port == 27002` ⇒ 本机发出（自己的语音，用于排除）；
- `src_port == 27002` ⇒ 服务器发来（**别人的语音**，这才是我们要的）。

把自己那条方向留着不是为了用，而是为了让「密钥验证」有更多样本、也方便核对
时间线（对方说话时自己的包应该同时存在）。
"""
from __future__ import annotations

import ctypes
import select
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path

ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800
ETH_P_IPV6 = 0x86DD
ETH_P_VLAN = 0x8100
IPPROTO_UDP = 17
SO_ATTACH_FILTER = 26
SNAPLEN = 262144

# ---- 经典 BPF 指令编码（uapi/linux/bpf_common.h）----
_BPF_LD = 0x00
_BPF_LDX = 0x01
_BPF_JMP = 0x05
_BPF_RET = 0x06
_BPF_W = 0x00
_BPF_H = 0x08
_BPF_B = 0x10
_BPF_ABS = 0x20
_BPF_ALU = 0x04
_BPF_AND = 0x50
_BPF_JEQ = 0x10
_BPF_K = 0x00

_LD_H_ABS = _BPF_LD | _BPF_H | _BPF_ABS       # 0x28
_LD_B_ABS = _BPF_LD | _BPF_B | _BPF_ABS       # 0x30
_JEQ_K = _BPF_JMP | _BPF_JEQ | _BPF_K         # 0x15
_RET_K = _BPF_RET | _BPF_K                    # 0x06
_ALU_AND_K = _BPF_ALU | _BPF_AND | _BPF_K     # 0x54


@dataclass(frozen=True)
class Datagram:
    """一个已解析的 UDP 数据报。`payload` 是 Photon 的字节（可能已加密）。"""

    ts: float
    src: tuple[str, int]
    dst: tuple[str, int]
    payload: bytes
    direction: str          # "in"（服务器发来，别人说） / "out"（本机发出，自己说）

    @property
    def is_encrypted(self) -> bool:
        """Photon 加密包的第 3 字节恒为 0x01（建连那几十个明文包不是）。"""
        return len(self.payload) >= 3 and self.payload[2] == 0x01


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort),
                ("filter", ctypes.POINTER(_SockFilter))]


def bpf_instructions(port: int = 27002, *, strict_port: bool = True) -> list[tuple[int, int, int, int]]:
    """生成 (code, jt, jf, k) 列表。

    **两套布局都要认**（这是实测逼出来的）：
      * 以太网帧：ethertype 在 12，IP 头在 14，UDP 端口在 34/36；
      * 裸 IPv4 包：tun / 某些加速器（甚至部分 WireGuard 配置）交给 AF_PACKET 的
        帧**没有链路层头**，IP 头就在 0，端口在 20/22。

    只写以太网版的话，经过 tun 的流量会被内核过滤器**全部丢掉**——现象就是
    "抓包 socket 一个帧都收不到"（实测 `cap.pcap` 只有 24 字节全局头）。
    """
    if strict_port:
        # 0..7 以太网路径；8..16 裸 IP 路径；18 接受 / 19 丢弃
        return [
            (_LD_H_ABS, 0, 0, 12),          # 0
            (_JEQ_K, 0, 6, ETH_P_IP),       # 1  → 2（以太网）/ → 8（试裸 IP）
            (_LD_B_ABS, 0, 0, 23),          # 2
            (_JEQ_K, 0, 15, IPPROTO_UDP),   # 3  → 4 / → 19
            (_LD_H_ABS, 0, 0, 36),          # 4
            (_JEQ_K, 12, 0, port),          # 5  → 18
            (_LD_H_ABS, 0, 0, 34),          # 6
            (_JEQ_K, 10, 0, port),          # 7  → 18 / → 8
            (_LD_B_ABS, 0, 0, 0),           # 8  裸 IP：版本/IHL
            (_ALU_AND_K, 0, 0, 0xF0),       # 9
            (_JEQ_K, 0, 8, 0x40),           # 10 → 11（IPv4）/ → 19
            (_LD_B_ABS, 0, 0, 9),           # 11
            (_JEQ_K, 0, 6, IPPROTO_UDP),    # 12 → 13 / → 19
            (_LD_H_ABS, 0, 0, 22),          # 13
            (_JEQ_K, 3, 0, port),           # 14 → 18
            (_LD_H_ABS, 0, 0, 20),          # 15
            (_JEQ_K, 1, 2, port),           # 16 → 18 / → 19
            (_RET_K, 0, 0, 0),              # 17（占位，不会被跳到）
            (_RET_K, 0, 0, SNAPLEN),        # 18 接受
            (_RET_K, 0, 0, 0),              # 19 丢弃
        ]
    # 宽松版：只认 "IPv4 + UDP"，两种布局都放行（排查"是不是端口不对"）
    return [
        (_LD_H_ABS, 0, 0, 12),              # 0
        (_JEQ_K, 0, 2, ETH_P_IP),           # 1  → 2（以太网）/ → 4（试裸 IP）
        (_LD_B_ABS, 0, 0, 23),              # 2
        (_JEQ_K, 3, 0, IPPROTO_UDP),        # 3  → 7 接受
        (_LD_B_ABS, 0, 0, 0),               # 4
        (_JEQ_K, 1, 0, 0x45),               # 5  → 7 接受
        (_RET_K, 0, 0, 0),                  # 6 丢弃
        (_RET_K, 0, 0, SNAPLEN),            # 7 接受
    ]


def bpf_bytes(port: int = 27002, *, strict_port: bool = True) -> tuple[bytes, int]:
    """把指令表打包成内核要的 `struct sock_filter[]`（16 字节/条）。"""
    insns = bpf_instructions(port, strict_port=strict_port)
    return b"".join(struct.pack("<HBBI", *insn) for insn in insns), len(insns)


def attach_filter(sock: socket.socket, port: int = 27002, *, strict_port: bool = True) -> None:
    """把 BPF 程序挂到 socket 上（`SO_ATTACH_FILTER`）。"""
    raw, count = bpf_bytes(port, strict_port=strict_port)
    filters = (_SockFilter * count).from_buffer_copy(raw)
    fprog = _SockFprog(len=count, filter=ctypes.cast(filters, ctypes.POINTER(_SockFilter)))
    sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, fprog)


def open_packet_socket(port: int = 27002, *, ifname: str = "",
                       filter_mode: str = "strict",
                       rcvbuf: int = 8 << 20) -> socket.socket:
    """开一个只收「IPv4 + UDP + 指定端口」的原始套接字。需要 `CAP_NET_RAW`。

    失败时把 errno 变成人话（`EPERM` 就是没权限，不要把用户丢进 traceback）。
    """
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                             socket.htons(ETH_P_ALL))
    except PermissionError as exc:
        raise PermissionError(
            "开 AF_PACKET 被拒（需要 CAP_NET_RAW）：分玩家模式必须由提权助手建这个 socket"
        ) from exc
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        # 诊断用 `filter_mode="none"`：抓全部帧，交给用户态统计。
        if filter_mode != "none":
            attach_filter(sock, port, strict_port=(filter_mode == "strict"))
        # ⚠️ **不能** bind(("", 0))：CPython 会把接口名拿去做 SIOCGIFINDEX，
        # 空名字必然返回 ENODEV（实测 errno 19）。而 AF_PACKET 套接字**不 bind**
        # 本来就意味着"收所有网卡上的包"，正是我们要的语义。
        if ifname:
            sock.bind((ifname, 0))
        sock.setblocking(False)
    except Exception:
        sock.close()
        raise
    return sock


# ---------------------------------------------------------------- 解析


def direction_of(src: tuple[str, int], dst: tuple[str, int], *, port: int = 27002,
                 local_ports: set[int] | None = None) -> str | None:
    """判断这条数据报的方向；不是我们关心的流量返回 None。

    * `in`  = 服务器 → 本机（**别人的声音**，我们要的就是这条）；
    * `out` = 本机 → 服务器（自己的语音，只用来验钥/核对时间线）。

    `local_ports` 是**游戏进程自己绑定的 UDP 端口**（`procfs.udp_ports`）。
    给了它，就要求「本机这一端确实是游戏」—— 同机别的进程连 27002 的流量会被
    排除；没给（读不到 `/proc/net/udp`）时退回只比端口。

    两端都是 `port`（本机恰好绑到 27002、或自环）时**不猜**：猜错方向的后果是
    把自己的声音当成别人说的，比丢几个包严重得多。
    """
    if src[1] == port and dst[1] != port:
        if not local_ports or dst[1] in local_ports:
            return "in"
        return None
    if dst[1] == port and src[1] != port:
        if not local_ports or src[1] in local_ports:
            return "out"
        return None
    return None


def parse_frame(frame: bytes, *, port: int = 27002,
                local_ports: set[int] | None = None) -> Datagram | None:
    """链路层帧 → `Datagram`。不是我们关心的 UDP 包就返回 None。

    两种输入都支持：
      * **以太网帧**（物理网卡）—— 含 VLAN（0x8100）时后移 4 字节；
      * **裸 IPv4 包**（tun / 某些加速器）—— 没有链路层头，首字节高 4 位就是版本。
    第二种是实测需要的：不分流就永远抓不到经过 tun 的流量。
    """
    if not frame:
        return None
    # 先按**以太网**判（以太网帧偏移 12 就是 ethertype，判据最强）；
    # 只有判不出以太网 IPv4 时，才退到"裸 IP"（tun / 某些加速器在 AF_PACKET
    # 上给的是没有链路层头的 IP 包）。
    if len(frame) >= 14:
        ethertype = struct.unpack_from("!H", frame, 12)[0]
        off = 14
        if ethertype == ETH_P_VLAN and len(frame) >= 18:
            ethertype = struct.unpack_from("!H", frame, 16)[0]
            off = 18
        if ethertype == ETH_P_IP:
            return _parse_ipv4(frame, off, port, local_ports)
        if ethertype == ETH_P_IPV6:
            return None
    if frame[0] >> 4 == 4 and len(frame) >= 20 and frame[9] == IPPROTO_UDP:
        return _parse_ipv4(frame, 0, port, local_ports)
    return None


def _parse_ipv4(frame: bytes, off: int, port: int,
                local_ports: set[int] | None = None) -> Datagram | None:
    if len(frame) < off + 20:
        return None
    vihl = frame[off]
    if vihl >> 4 != 4:
        return None
    ihl = (vihl & 0x0F) * 4
    if ihl < 20 or len(frame) < off + ihl + 8:
        return None
    if frame[off + 9] != IPPROTO_UDP:
        return None
    src_ip = socket.inet_ntoa(frame[off + 12:off + 16])
    dst_ip = socket.inet_ntoa(frame[off + 16:off + 20])
    udp = off + ihl
    sport, dport, ulen = struct.unpack_from("!HHH", frame, udp)
    payload = frame[udp + 8: udp + max(8, ulen)]
    if not payload:
        return None
    direction = direction_of((src_ip, sport), (dst_ip, dport),
                             port=port, local_ports=local_ports)
    if direction is None:
        return None
    return Datagram(ts=time.time(), src=(src_ip, sport), dst=(dst_ip, dport),
                    payload=payload, direction=direction)


def drain(sock: socket.socket, *, timeout: float = 0.2,
          max_frames: int = 128) -> list[tuple[float, bytes]]:
    """从原始 socket 取一批帧（非阻塞 + select）。返回 [(时间戳, 原始帧)]。"""
    out: list[tuple[float, bytes]] = []
    try:
        ready, _, _ = select.select([sock], [], [], timeout)
    except (OSError, ValueError):
        return out
    if not ready:
        return out
    for _ in range(max_frames):
        try:
            frame = sock.recv(SNAPLEN)
        except (BlockingIOError, InterruptedError):
            break
        except OSError:
            break
        if not frame:
            break
        out.append((time.time(), frame))
    return out


# ---------------------------------------------------------------- pcap


class PcapWriter:
    """最小 pcap 写出器（`LINKTYPE_ETHERNET`），只存密文，便于离线复现。

    不引入 `scapy`/`pcapy`：它们要么太重，要么需要额外系统库；pcap 格式本身
    只有 24 字节全局头 + 每包 16 字节头。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("wb")
        self._fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, SNAPLEN, 1))
        self.frames = 0

    def write(self, ts: float, frame: bytes) -> None:
        sec = int(ts)
        usec = int((ts - sec) * 1_000_000)
        self._fh.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)))
        self._fh.write(frame)
        self.frames += 1

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "PcapWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def read_pcap(path: str | Path) -> list[tuple[float, bytes]]:
    """离线回放用：读回 `PcapWriter` 写的文件（只支持这个子集）。"""
    data = Path(path).read_bytes()
    if len(data) < 24:
        return []
    magic, _, _, _, _, _, _ = struct.unpack_from("<IHHiIII", data, 0)
    if magic != 0xA1B2C3D4:
        raise ValueError(f"不是本模块写出的 pcap（magic={magic:#x}）")
    out: list[tuple[float, bytes]] = []
    off = 24
    while off + 16 <= len(data):
        sec, usec, incl, _orig = struct.unpack_from("<IIII", data, off)
        off += 16
        frame = data[off:off + incl]
        off += incl
        out.append((sec + usec / 1_000_000, frame))
    return out
