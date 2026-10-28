#!/usr/bin/env python
"""分玩家 P0-b 的离线验收：GCM 布局、抓包、Photon 重组、USpeak 帧、内存扫描。

## 这些用例能证明什么、不能证明什么

能证明（全部离线，不需要 VRChat、不需要提权、不需要网络）：

1. **报文布局**的切片是对的（AAD=前 19 字节、nonce=第 7..19 字节、tag 在尾部）——
   用一把"假 AEAD"跑，它会把收到的 aad/nonce 拿来算 tag，切错就解不开；
2. **候选验钥的分组语义**：同一 flow 的全部抽样都要通过，跨 flow 不许错配；
3. **BPF 过滤器**的指令编码与偏移；以太网/IPv4/UDP 解析与方向判定；
4. **Photon 命令帧 → 分片乱序重组 → 事件 1 → 参数 245 → USpeak 帧** 的完整链路；
5. **Opus 帧自校验**：非法字节被拒、合法帧解出 20ms（16k 下 320 样本）；
6. **`process_vm_readv` 扫描器**能在**本进程**里找到提前埋好的 `<u64 32> + key`，
   而低熵候选（全 0）会被预筛掉。

不能证明：真实 VRChat 的类型表/字节序一定如我们所假设。那部分只能靠 P0-b 的
真机报告（`report.json` 里的事件直方图 + 首个音频体十六进制）来修。

跑法：.venv/bin/python tests/test_players_protocol.py
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import socket
import struct
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import capture_linux, gcm, key_linux, opus, photon, procfs  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


# ---------------------------------------------------------------- 假 AEAD


class _FakeAead:
    """按真实 AEAD 的**形状**工作：nonce/aad 参与 tag，data 参与密文。

    它不提供密码学安全性，只用来把「我们自己切错片」这种错误暴露出来 ——
    真 AES-GCM 的往返另有一条用例（需要 cryptography）。
    """

    def __init__(self, key: bytes) -> None:
        self.key = key

    def _xor(self, data: bytes) -> bytes:
        return bytes(b ^ self.key[i % len(self.key)] for i, b in enumerate(data))

    def encrypt(self, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        ct = self._xor(data)
        tag = hashlib.sha256(self.key + nonce + aad + ct).digest()[:16]
        return ct + tag

    def decrypt(self, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        ct, tag = data[:-16], data[-16:]
        want = hashlib.sha256(self.key + nonce + aad + ct).digest()[:16]
        if tag != want:
            raise ValueError("tag 不匹配")
        return self._xor(ct)


def _packet(aead, plaintext: bytes, *, peer: int = 7,
            challenge: bytes = b"\x01\x02\x03\x04", nonce: bytes | None = None) -> bytes:
    nonce = nonce or bytes(range(12))
    aad = struct.pack(">H", peer) + b"\x01" + challenge + nonce
    return aad + aead.encrypt(nonce, plaintext, aad)


def test_gcm_layout() -> None:
    print("[gcm] 报文布局与候选验钥")
    key = bytes(range(32))
    aead = _FakeAead(key)
    real = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)      # noqa: SLF001 - 测的就是这里
    try:
        pkt = _packet(aead, b"hello photon")
        parts = gcm.split_packet(pkt)
        check("split 成功", parts is not None)
        if parts:
            aad, nonce, body = parts
            check("AAD = 前 19 字节", aad == pkt[:19], aad.hex())
            check("nonce = 第 7..19 字节", nonce == pkt[7:19], nonce.hex())
            check("body = 19 之后全部", body == pkt[19:])
        check("正确密钥能解开", gcm.try_open(key, pkt) == b"hello photon")
        check("错误密钥解不开", gcm.try_open(bytes(32), pkt) is None)
        check("明文包（第 3 字节 != 1）被跳过",
              gcm.split_packet(b"\x00\x07\x00" + b"\x00" * 40) is None)
        check("短包被跳过", gcm.split_packet(b"\x00\x07\x01") is None)

        # 分组语义：flowA 两包（keyA）、flowB 一包（keyB）→ 只能用 keyA
        fa = gcm.Flow.of(("1.1.1.1", 27002), ("2.2.2.2", 5000))
        fb = gcm.Flow.of(("1.1.1.1", 27002), ("3.3.3.3", 6000))
        samples = [(fa, _packet(aead, b"a1")), (fa, _packet(aead, b"a2")),
                   (fb, _packet(_FakeAead(bytes([9]) * 32), b"b1"))]
        hit = gcm.validate_candidates([bytes(32), key], samples)
        check("命中正确密钥", hit is not None and hit.key == key)
        check("命中记的是它解开的那条 flow", hit is not None and hit.flow == fa,
              str(hit.flow if hit else None))
        only_one = gcm.validate_candidates([key], samples[:1])
        check("只有 1 个样本的 flow 不足以判钥", only_one is None)
        check("指纹是可打印短串", len(gcm.key_fingerprint(key)) == 8)

        # 布局退路：如果真实 AAD 只有 7 字节头（笔记记错/版本变了），
        # 同一串候选必须还能被认出来，并且报告是哪种布局。
        nonce = bytes(range(12))
        hdr7 = struct.pack(">H", 7) + b"\x01" + b"\x01\x02\x03\x04"
        body7 = _FakeAead(key).encrypt(nonce, b"alt", hdr7)
        pkt_alt = hdr7 + nonce + body7
        alt = gcm.validate_candidates([key], [(fa, pkt_alt), (fa, pkt_alt)])
        check("AAD=7 字节头的布局也能识别",
              alt is not None and alt.layout == "hdr",
              str(alt.layout if alt else None))
    finally:
        gcm._aesgcm = real


def test_gcm_real_crypto() -> None:
    print("[gcm] 真实 AES-256-GCM 往返（需要 cryptography）")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError:
        print("  SKIP 未安装 cryptography（.venv/bin/pip install -r requirements-players.txt）")
        return
    key = os.urandom(32)
    nonce = os.urandom(12)
    aad = b"\x00\x07\x01" + b"\xaa\xbb\xcc\xdd" + nonce
    ct = AESGCM(key).encrypt(nonce, b"real plaintext", aad)
    pkt = aad + ct
    check("真 AEAD 往返", gcm.try_open(key, pkt) == b"real plaintext")
    check("换密钥失败", gcm.try_open(os.urandom(32), pkt) is None)


# ---------------------------------------------------------------- 抓包


def _eth_frame(payload: bytes, *, sport: int = 5000, dport: int = 27002,
               src_ip: bytes = b"\x01\x02\x03\x04", dst_ip: bytes = b"\x05\x06\x07\x08",
               proto: int = capture_linux.IPPROTO_UDP) -> bytes:
    udp_len = 8 + len(payload)
    udp = struct.pack("!HHHH", sport, dport, udp_len, 0) + payload
    total = 20 + len(udp)
    ip = (b"\x45\x00" + struct.pack("!H", total) + b"\x00\x00\x00\x00"
          + b"\x40" + bytes([proto]) + b"\x00\x00" + src_ip + dst_ip)
    return b"\x00" * 12 + b"\x08\x00" + ip + udp


def test_capture() -> None:
    print("[capture] BPF 编码、帧解析、pcap 往返")
    raw, count = capture_linux.bpf_bytes()
    check("严格过滤 20 条指令", count == 20, str(count))
    # struct sock_filter = u16 code + u8 jt + u8 jf + u32 k = 8 字节（不是 16）
    check("BPF 长度 = 8 × 条数", len(raw) == 8 * count, str(len(raw)))
    first = struct.unpack_from("<HBBI", raw, 0)
    check("第 0 条是 ldh [12]", first == (0x28, 0, 0, 12), str(first))
    tail = [struct.unpack_from("<HBBI", raw, 8 * i) for i in (18, 19)]
    check("尾部是「接受 / 丢弃」",
          tail[0] == (0x06, 0, 0, capture_linux.SNAPLEN) and tail[1] == (0x06, 0, 0, 0),
          str(tail))
    loose, loose_count = capture_linux.bpf_bytes(strict_port=False)
    check("宽松过滤 8 条", loose_count == 8 and len(loose) == 64, str(loose_count))

    payload = b"\x00\x09\x01" + b"\x00" * 40
    dg = capture_linux.parse_frame(_eth_frame(payload))
    check("出向包（dport=27002 = 我们发给服务器）",
          dg is not None and dg.direction == "out" and dg.payload == payload,
          str(dg))
    dg2 = capture_linux.parse_frame(_eth_frame(payload, sport=27002, dport=5000))
    check("入向包（sport=27002 = 服务器发来，别人的语音）",
          dg2 is not None and dg2.direction == "in")
    check("非 27002 端口被忽略",
          capture_linux.parse_frame(_eth_frame(payload, sport=1234, dport=4321)) is None)
    check("非 UDP 被忽略",
          capture_linux.parse_frame(_eth_frame(payload, proto=6)) is None)
    check("加密标记（第 3 字节=1）", dg is not None and dg.is_encrypted)

    # 真的挂一次内核过滤器（普通 UDP socket 也能挂，不需要 CAP_NET_RAW）：
    # 这是唯一能验证 ctypes `sock_fprog` 结构没写错的办法。
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe_sock.bind(("127.0.0.1", 0))
        capture_linux.attach_filter(probe_sock)
        attached = True
        why = ""
    except OSError as exc:
        attached = False
        why = f"{type(exc).__name__}: {exc}"
    finally:
        probe_sock.close()
    check("SO_ATTACH_FILTER 在普通 socket 上挂得上（验证结构布局）",
          attached, why)

    try:
        raw_sock = capture_linux.open_packet_socket()
    except PermissionError as exc:
        check("没有 CAP_NET_RAW 时给人话而不是 traceback",
              "CAP_NET_RAW" in str(exc), str(exc))
    except OSError as exc:
        check("开包 socket 的失败要带说明", False, f"{type(exc).__name__}: {exc}")
    else:
        raw_sock.close()
        print("  ok   本机直接开成功（有 CAP_NET_RAW），权限分支未覆盖")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cap.pcap"
        frames = [(1.0, _eth_frame(payload)), (2.5, _eth_frame(payload[:10]))]
        with capture_linux.PcapWriter(path) as writer:
            for ts, frame in frames:
                writer.write(ts, frame)
        back = capture_linux.read_pcap(path)
        check("pcap 往返帧数", len(back) == 2, str(len(back)))
        check("pcap 往返内容", back[0][1] == frames[0][1])
        check("pcap 时间戳保留", abs(back[1][0] - 2.5) < 1e-6, str(back[1][0]))


# ---------------------------------------------------------------- Photon


def _commands(payloads: list[bytes], *, kind: int = 2) -> bytes:
    """构造明文数据报。⚠️ length 字段 = 12（命令头）+ payload（P0-b 实测语义）。"""
    out = bytearray()
    out.append(len(payloads))
    out += b"\x00\x00\x00\x00"
    for seq, payload in enumerate(payloads):
        out += bytes([kind, 0, 0, 0])
        out += struct.pack("!I", 12 + len(payload))
        out += struct.pack("!I", seq)
        out += payload
    return bytes(out)


def _fragment_payload(data: bytes, index: int, count: int, size: int) -> bytes:
    offset = index * (len(data) // count)
    end = len(data) if index == count - 1 else offset + len(data) // count
    return struct.pack("!IIIII", 1, count, index, size, offset) + data[offset:end]


class _FakeSocket:
    """记录调用顺序的假 socket：用来验证 open_packet_socket 的**参数选择**。

    真实 AF_PACKET 需要 root，测试机不一定有；但"空接口名不许 bind"这条
    是从真机 ENODEV（errno 19）踩出来的回归，必须能在离线环境里钉住。
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def setsockopt(self, *args) -> None:            # noqa: ANN002 - 透传即可
        self.calls.append(("setsockopt", args[:2]))

    def bind(self, addr) -> None:                   # noqa: ANN001
        self.calls.append(("bind", addr))

    def setblocking(self, flag: bool) -> None:
        self.calls.append(("setblocking", flag))

    def close(self) -> None:
        self.calls.append(("close",))


def _run_bpf(insns: list[tuple[int, int, int, int]], frame: bytes) -> int:
    """最小的经典 BPF 解释器（只支持我们用到的 LD/JEQ/AND/RET）。

    为什么值得写：过滤器是**内核**跑的，写错时现象是"一个包都收不到"，
    而真机排查要先开游戏、再弹一次提权密码。用这 30 行就能在离线用例里把
    "以太网 / 裸 IP 两种布局、端口命中、跳转偏移"全部跑一遍。
    """
    a = 0
    pc = 0
    for _ in range(200):
        if pc >= len(insns):
            return 0
        code, jt, jf, k = insns[pc]
        cls = code & 0x07
        if cls == 0x00:                       # LD
            width = {0x00: 4, 0x08: 2, 0x10: 1}.get(code & 0x18)
            if width is None or (code & 0xE0) != 0x20:
                return -1
            if k + width > len(frame):
                return 0                      # 越界 → 内核按丢弃处理
            a = int.from_bytes(frame[k:k + width], "big")
            pc += 1
        elif cls == 0x04:                     # ALU
            if (code & 0xF0) != 0x50:
                return -1
            a &= k
            pc += 1
        elif cls == 0x05:                     # JMP
            if (code & 0xF0) != 0x10:
                return -1
            pc = pc + 1 + (jt if a == k else jf)
        elif cls == 0x06:                     # RET
            return k
        else:
            return -1
    return -1


def test_bpf_semantics() -> None:
    print("[capture] 用 BPF 解释器验证过滤器语义（以太网 + 裸 IP）")
    strict = capture_linux.bpf_instructions()
    loose = capture_linux.bpf_instructions(strict_port=False)
    payload = b"\x00" * 16

    eth_hit = _eth_frame(payload, sport=5000, dport=27002)
    eth_hit_in = _eth_frame(payload, sport=27002, dport=5000)
    eth_other = _eth_frame(payload, sport=1234, dport=4321)
    eth_tcp = _eth_frame(payload, sport=5000, dport=443, proto=6)
    raw_hit = _raw_ipv4_udp(payload, sport=5000, dport=27002)
    raw_other = _raw_ipv4_udp(payload, sport=5000, dport=5555)
    arp = b"\xff" * 12 + b"\x08\x06" + b"\x00" * 28

    check("以太网 27002 出向 → 收", _run_bpf(strict, eth_hit) == capture_linux.SNAPLEN)
    check("以太网 27002 入向 → 收", _run_bpf(strict, eth_hit_in) == capture_linux.SNAPLEN)
    check("以太网其它端口 → 丢", _run_bpf(strict, eth_other) == 0)
    check("以太网 TCP → 丢", _run_bpf(strict, eth_tcp) == 0)
    check("裸 IP 27002 → 收（tun 场景）", _run_bpf(strict, raw_hit) == capture_linux.SNAPLEN)
    check("裸 IP 其它端口 → 丢", _run_bpf(strict, raw_other) == 0)
    check("ARP → 丢", _run_bpf(strict, arp) == 0)

    check("宽松版收下任意 UDP（以太网）",
          _run_bpf(loose, eth_other) == capture_linux.SNAPLEN)
    check("宽松版收下任意 UDP（裸 IP）",
          _run_bpf(loose, raw_other) == capture_linux.SNAPLEN)
    check("宽松版仍丢 TCP", _run_bpf(loose, eth_tcp) == 0)


def test_open_packet_socket_arguments() -> None:
    print("[capture] 开包 socket 的参数选择（空接口名不 bind）")
    real_socket = capture_linux.socket.socket
    made: list[_FakeSocket] = []

    def _factory(*_args, **_kwargs):                 # noqa: ANN002, ANN003
        fake = _FakeSocket()
        made.append(fake)
        return fake

    capture_linux.socket.socket = _factory
    try:
        capture_linux.open_packet_socket()
        ok_no_bind = bool(made) and not any(c[0] == "bind" for c in made[-1].calls)
        check("不指定网卡时不调用 bind（否则空名字 → ENODEV）",
              ok_no_bind, str(made[-1].calls if made else None))

        capture_linux.open_packet_socket(ifname="wlan0")
        binds = [c for c in made[-1].calls if c[0] == "bind"]
        check("指定网卡时才 bind，且参数是 (网卡名, 0)",
              binds == [("bind", ("wlan0", 0))], str(binds))

        check("两种情况下都挂了 BPF 过滤",
              all(any(c[0] == "setsockopt" for c in f.calls) for f in made))
        check("都设成非阻塞",
              all(("setblocking", False) in f.calls for f in made))
    finally:
        capture_linux.socket.socket = real_socket


def _raw_ipv4_udp(payload: bytes, *, sport: int, dport: int) -> bytes:
    """没有链路层头的裸 IPv4/UDP 包（tun / 某些加速器就是这样喂给 AF_PACKET）。"""
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = (b"\x45\x00" + struct.pack("!H", 20 + len(udp)) + b"\x00\x00\x00\x00"
          + b"\x40" + bytes([capture_linux.IPPROTO_UDP]) + b"\x00\x00"
          + b"\x01\x02\x03\x04" + b"\x05\x06\x07\x08")
    return ip + udp


def test_raw_ip_and_sniff() -> None:
    print("[capture] 裸 IP 帧 + 诊断直方图")
    dg = capture_linux.parse_frame(_raw_ipv4_udp(b"\x00\x09\x01payload", sport=27002, dport=51000))
    check("裸 IP 帧也能解析（tun 场景）",
          dg is not None and dg.direction == "in" and dg.payload.endswith(b"payload"), str(dg))

    sys.path.insert(0, str(ROOT / "scripts"))
    import probe_player_audio as probe  # noqa: PLC0415 - 只为这一个纯函数

    tcp_eth = _eth_frame(b"", sport=51000, dport=443, proto=6)
    frames = [
        _eth_frame(b"\x00\x09\x01aaaa", sport=5000, dport=27002),
        _eth_frame(b"\x00\x09\x01bbbb", sport=27002, dport=5000),
        tcp_eth,
        _raw_ipv4_udp(b"\x00\x09\x01cccc", sport=5058, dport=5058),
    ]
    summary = probe._sniff_summary(frames, 27002)  # noqa: SLF001 - 纯函数
    check("总帧数", summary["frames"] == 4, str(summary["frames"]))
    check("目标端口命中 2 个", summary["port_hits"] == 2, str(summary["port_hits"]))
    check("协议直方图", summary["proto"].get("udp") == 3 and summary["proto"].get("tcp") == 1,
          str(summary["proto"]))
    check("裸 IP 被单独计数", summary["link"].get("raw-ip") == 1, str(summary["link"]))
    check("端口表里能看到 27002",
          any("27002" in k for k, _v in summary["top_ports"]), str(summary["top_ports"]))


def test_photon() -> None:
    print("[photon] 命令帧、分片重组、事件参数")
    plain = _commands([b"\xf3\x04\x01\x00", b"\x00" * 10])
    cmds, err = photon.parse_commands(plain)
    check("命令条数", len(cmds) == 2, str(len(cmds)))
    check("命令 payload 正确", cmds[0].payload == b"\xf3\x04\x01\x00")
    check("正常解析无错误", err == "", err)
    bad, bad_err = photon.parse_commands(plain[:-4])
    check("长度越界要报错而不是静默", bad_err != "", bad_err)
    check("多命令包必须**精确消费完**（length 含 12 字节头）",
          len(cmds) == 2 and err == "", f"{len(cmds)} / {err!r}")
    # 长度字段按"只算 payload"解释时，第二个命令会被截断 —— 这是 P0-b 的真 bug，
    # 用"含头"语义后必须能完整解出两条。
    legacy = bytearray(plain)
    struct.pack_into("!I", legacy, 5 + 4, len(cmds[0].payload))
    got_legacy, err_legacy = photon.parse_commands(bytes(legacy))
    check("旧语义（只算 payload）会丢掉后面的命令",
          len(got_legacy) < 2 or err_legacy != "", f"{len(got_legacy)} / {err_legacy!r}")

    # 并发交错：真实流量里同一 channel 上有多条消息同时在拼（P0-b 实测 96% 的
    # 载荷是分片）。旧实现每个 channel 只留一个槽 → 必丢语音。
    msg_a = b"A" * 300
    msg_b = b"B" * 200
    fa = [_fragment_payload(msg_a, i, 2, len(msg_a)) for i in range(2)]
    fb = [_fragment_payload(msg_b, i, 2, len(msg_b)) for i in range(2)]
    inter = photon.Fragmenter()
    out_a = inter.feed(photon.Command(kind=8, channel=0, flags=0, seq=0, payload=fa[0]))
    out_b = inter.feed(photon.Command(kind=8, channel=0, flags=0, seq=0, payload=fb[0]))
    # 注意：两条消息的 startSeq 都是 1（同一 channel）——真实流量里靠 startSeq 区分，
    # 这里刻意为两条消息用不同的 startSeq 才能分开。用相同值时应表现为"丢一组"而不是崩。
    check("交错分片不会互相顶掉（不同 startSeq）", out_a == [] and out_b == [],
          f"{out_a} {out_b}")
    inter2 = photon.Fragmenter()
    fa2 = [_fragment_payload(msg_a, i, 2, len(msg_a)) for i in range(2)]
    fb2 = [_fragment_payload(msg_b, i, 2, len(msg_b)) for i in range(2)]
    # 手工把两条消息的 startSeq 改成不同值
    import struct as _s
    def _with_seq(payload: bytes, seq: int) -> bytes:
        return _s.pack("!I", seq) + payload[4:]
    fa2 = [_with_seq(p, 10) for p in fa2]
    fb2 = [_with_seq(p, 20) for p in fb2]
    got: list[bytes] = []
    for part in (fa2[0], fb2[0], fa2[1], fb2[1]):
        got.extend(inter2.feed(photon.Command(kind=8, channel=0, flags=0, seq=0,
                                              payload=part)))
    check("同一 channel 两条消息交错也能各自拼回", sorted(got) == sorted([msg_a, msg_b]),
          f"{[len(x) for x in got]}")

    message = bytes(range(200)) * 2
    frag = photon.Fragmenter()
    parts = [_fragment_payload(message, i, 3, len(message)) for i in range(3)]
    got: list[bytes] = []
    for part in (parts[1], parts[2], parts[0]):          # 乱序到达
        got.extend(frag.feed(photon.Command(kind=8, channel=0, flags=0, seq=0,
                                            payload=part)))
    check("分片乱序仍能拼回", got == [message], f"{len(got)} 段")

    # 事件 1：参数 254（actor=3）+ 245（音频体）
    body = b"\x00\x00\x00\x00" + struct.pack("<HH", 0, 4) + b"\xf8\xff\xfe\x00"
    payload = (b"\xf3\x04\x01" + bytes([2])
               + bytes([254, 0x0B, 3])
               + bytes([245, 0x43, len(body)]) + body)
    msg = photon.parse_message(payload)
    check("事件解析成功", msg is not None and msg.kind == 4 and msg.code == 1)
    check("actor 取到 3",
          msg is not None and msg.param(254) is not None and msg.param(254).value == 3)
    check("音频体一致",
          msg is not None and msg.param(245) is not None
          and msg.param(245).value == body)
    check("保底扫描也能找到音频体", photon.scan_voice_body(payload) == body)

    # 真实流量（P0-b）：不可靠通道的 payload 前面多 4 字节（`00 03 66 43`），
    # 只看 payload[0] 会把整类语音丢掉。
    prefixed = b"\x00\x03\x66\x43" + payload
    msg0, skip0 = photon.parse_message_at(prefixed)
    check("带 4 字节前缀的消息能对齐", msg0 is not None and skip0 == 4, f"{skip0}")
    check("对齐后事件码/actor 仍正确",
          msg0 is not None and msg0.code == 1
          and msg0.param(254) is not None and msg0.param(254).value == 3)
    check("原位消息 skip=0",
          photon.parse_message_at(payload)[1] == 0)
    check("垃圾载荷不会误对齐",
          photon.parse_message_at(b"\x00" * 32) == (None, -1))
    check("保底扫描能拿到 actor", photon.scan_actor(payload) == 3)

    # 未知类型挡在前面时，正向解析会中断（但保底路径仍可用）
    weird = payload[:3] + bytes([2]) + bytes([200, 0x99]) + payload[3:]
    broken = photon.parse_message(weird)
    check("未知类型被记录成错误而不是崩溃",
          broken is not None and broken.error != "", str(broken.error if broken else None))
    check("未知类型下保底路径仍能取到音频",
          photon.scan_voice_body(weird) == body)


def test_voice_body_lengths() -> None:
    print("[photon] 音频体长度前缀的多种编码（LEB128 / u16 / u32）")
    body = b"\x00\x00\x00\x00" + struct.pack("<HH", 0, 4) + b"\xf8\xff\xfe\x00"
    head = b"\xf3\x04\x0d\x01" + bytes([photon.PARAM_AUDIO, photon.PT_BYTE_ARRAY])

    leb = head + bytes([len(body)]) + body
    check("LEB128（VoxTrace 的说法）", photon.scan_voice_body(leb) == body)

    u16 = head + struct.pack("<H", len(body)) + body
    check("u16 LE 长度也能识别（P0-b '参数 245 解析失败' 的原因）",
          photon.scan_voice_body(u16, valid=lambda b: b == body) == body, u16.hex())

    u32 = head + struct.pack("<I", len(body)) + body
    check("u32 LE 长度也能识别",
          photon.scan_voice_body(u32, valid=lambda b: b == body) == body)

    check("校验函数能挡掉错切（防止把移位数据当音频）",
          photon.scan_voice_body(u16, valid=lambda b: False) is None)


def test_uspeak_split() -> None:
    print("[photon] USpeak 帧切分（用 opus 自校验裁决）")
    if not opus.available():
        print("  SKIP 找不到 libopus")
        return
    silence = b"\x00\x00" * 960
    f0 = opus.encode_frame(silence, rate=48000, channels=1)
    f1 = opus.encode_frame(silence, rate=48000, channels=1)
    check("造出的帧是合法 opus", opus.is_valid_packet(f0) and opus.is_valid_packet(f1))

    body = (b"\x00\x00\x00\x00" + struct.pack("<HH", 0, len(f0)) + f0
            + struct.pack("<HH", 1, len(f1)) + f1)
    frames, layout = photon.split_voice_frames(body, valid=opus.is_valid_packet)
    check("4 字节头 + LE 切法", layout == "skip=4/le", layout)
    check("切出两帧", len(frames) == 2 and frames[1].index == 1, str(len(frames)))

    body2 = struct.pack("<HH", 0, len(f0)) + f0
    frames2, layout2 = photon.split_voice_frames(body2, valid=opus.is_valid_packet)
    check("无头切法（退路）", layout2 == "skip=0/le", layout2)
    check("无头也能切出 1 帧", len(frames2) == 1)

    garbage = b"\x00\x00\x00\x00\x10\x00" + b"\x99" * 16
    check("垃圾数据切不出来任务帧",
          photon.split_voice_frames(garbage, valid=opus.is_valid_packet) == ([], "unknown"))


def test_sequence_gap() -> None:
    print("[photon] USpeak 序号：补缺口 / 去重 / 长停顿重同步")
    check("第一帧不补", photon.sequence_gap(None, 100) == (0, False))
    check("连续帧不补", photon.sequence_gap(100, 101) == (0, False))
    check("缺 4 帧要补 4 帧", photon.sequence_gap(100, 105) == (4, False))
    check("重复帧要丢弃", photon.sequence_gap(100, 100) == (0, True))
    check("长停顿不补（否则插几十秒静音）",
          photon.sequence_gap(100, 400) == (0, False))
    check("u16 回绕不算大跳", photon.sequence_gap(65535, 0) == (0, False))


def test_opus_decode() -> None:
    print("[opus] 解码到 16k 单声道 + wav 落盘")
    if not opus.available():
        print("  SKIP 找不到 libopus")
        return
    frame = opus.encode_frame(b"\x00\x00" * 960, rate=48000, channels=1)
    check("48k 下 20ms = 960 样本", opus.packet_samples(frame, 48000) == 960,
          str(opus.packet_samples(frame, 48000)))
    check("单声道码流", opus.packet_channels(frame) == 1, str(opus.packet_channels(frame)))
    with opus.Decoder() as dec:
        pcm = dec.decode(frame)
    check("解出 16k×20ms = 320 样本 = 640 字节", len(pcm) == 640, str(len(pcm)))

    stereo_pcm = b"\x00\x00" * 1920
    stereo = opus.encode_frame(stereo_pcm, rate=48000, channels=2)
    check("立体声码流被识别", opus.packet_channels(stereo) == 2,
          str(opus.packet_channels(stereo)))
    with opus.Decoder() as dec:
        mono = dec.decode(stereo)
    check("立体声 → 单声道（长度仍 640）", len(mono) == 640, str(len(mono)))

    with tempfile.TemporaryDirectory() as tmp:
        path = opus.write_wav(Path(tmp) / "x.wav", pcm)
        with wave.open(str(path), "rb") as wf:
            check("wav 参数（1ch/16bit/16k）",
                  (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 16000),
                  str((wf.getnchannels(), wf.getsampwidth(), wf.getframerate())))
            check("wav 帧数", wf.getnframes() == 320, str(wf.getnframes()))


# ---------------------------------------------------------------- 内存扫描


def test_scan_self() -> None:
    """在本进程里埋一个 `<u64 32> + key`，用真的 `process_vm_readv` 扫出来。

    这条用例覆盖 ctypes 封装、分块、needle 匹配、熵预筛 —— 全程不需要 VRChat、
    也不需要任何提权（读自己总是允许的）。
    """
    print("[key] process_vm_readv 自读扫描")
    size = 64 << 10
    good_key = bytes(range(32))
    buf = ctypes.create_string_buffer(size)
    ctypes.memmove(buf, struct.pack("<Q", 32) + good_key, 40)
    base = ctypes.addressof(buf)

    regions = [procfs.Region(start=base, end=base + size, perms="rw-p", pathname="")]

    zeros = ctypes.create_string_buffer(size)
    ctypes.memmove(zeros, struct.pack("<Q", 32) + bytes(32), 40)
    zero_base = ctypes.addressof(zeros)
    regions.append(procfs.Region(start=zero_base, end=zero_base + size,
                                 perms="rw-p", pathname=""))

    res = key_linux.scan_key(os.getpid(), deadline_s=5.0, regions=regions)
    check("扫到密钥", good_key in res.candidates,
          f"候选 {len(res.candidates)} 个")
    check("低熵候选（全 0）被预筛掉", bytes(32) not in res.candidates)
    check("扫描字节数正确", res.scanned_bytes >= size, str(res.scanned_bytes))

    short = key_linux.read_mem(os.getpid(), base, 16)
    check("read_mem 短读返回实际字节", short == bytes(buf.raw[:16]), short.hex())

    # 第二种 needle：4 字节长度字段（数据紧跟其后，偏移 4）
    alt_buf = ctypes.create_string_buffer(size)
    ctypes.memmove(alt_buf, struct.pack("<I", 32) + good_key, 36)
    alt_base = ctypes.addressof(alt_buf)
    alt_regions = [procfs.Region(start=alt_base, end=alt_base + size,
                                 perms="rw-p", pathname="")]
    alt = key_linux.scan_key(os.getpid(), deadline_s=5.0, needle="i",
                             regions=alt_regions)
    check("needle=i（4 字节长度）也能扫到", good_key in alt.candidates,
          f"候选 {len(alt.candidates)} 个")
    check("ScanResult 记录 needle 变体", alt.needle == "i", alt.needle)


# ---------------------------------------------------------------- main


def main() -> int:
    test_gcm_layout()
    test_gcm_real_crypto()
    test_capture()
    test_open_packet_socket_arguments()
    test_bpf_semantics()
    test_raw_ip_and_sniff()
    test_photon()
    test_voice_body_lengths()
    test_uspeak_split()
    test_sequence_gap()
    test_opus_decode()
    test_scan_self()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（分玩家 P0-b 离线验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
