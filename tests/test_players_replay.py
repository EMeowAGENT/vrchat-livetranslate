#!/usr/bin/env python
"""端到端离线验收：`pcap + 密钥` → 解密 → 分片 → 音频 → wav。

## 为什么这条用例最重要

真机每迭代一次要「开游戏 + 弹密码 + 说 90 秒」。这条用例把**同一条代码路径**
（`VoiceCollector` / `cmd_replay`）在离线环境跑一遍，于是协议假设改错了，
几秒钟就能知道，而不是再约一次真机。

它刻意把几个"真实流量里踩过的坑"都塞进同一个样本：

* 事件码 **不是 1**（用 13）——证明我们不再依赖 Event 1；
* 音频参数的长度前缀用 **u16 LE**（不是 LEB128）——证明多编码回退有效；
* 消息走 **分片**，而且两条消息**交错**——证明并发重组；
* 参数表里放一个**未知类型**制造错位——证明保底路径仍能找到音频。

跑法：.venv/bin/python tests/test_players_replay.py
"""
from __future__ import annotations

import struct
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from vlt.players import capture_linux, opus  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


def _eth_udp(payload: bytes, *, sport: int, dport: int) -> bytes:
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = (b"\x45\x00" + struct.pack("!H", 20 + len(udp)) + b"\x00\x00\x00\x00"
          + b"\x40" + bytes([capture_linux.IPPROTO_UDP]) + b"\x00\x00"
          + b"\x01\x02\x03\x04" + b"\x05\x06\x07\x08")
    return b"\x00" * 12 + b"\x08\x00" + ip + udp


def _command(kind: int, payload: bytes, *, seq: int = 0) -> bytes:
    # length 含 12 字节命令头（P0-b 实测语义）
    return (bytes([kind, 0, 0, 0]) + struct.pack("!I", 12 + len(payload))
            + struct.pack("!I", seq) + payload)


def _datagram(commands: list[bytes]) -> bytes:
    return bytes([len(commands)]) + b"\x00\x00\x00\x00" + b"".join(commands)


def _message(code: int, *, actor: int, body: bytes) -> bytes:
    params = (bytes([254, 0x0B, actor])
              # 先塞一个未知类型，模拟参数表错位（正向解析会在这里断掉）
              + bytes([200, 0x99])
              + bytes([245, 0x43]) + struct.pack("<H", len(body)) + body)
    return b"\xf3\x04" + bytes([code]) + bytes([3]) + params


def _fragments(payload: bytes, count: int, *, start_seq: int) -> list[bytes]:
    size = len(payload)
    step = (size + count - 1) // count
    out = []
    for index in range(count):
        offset = index * step
        part = payload[offset:offset + step]
        out.append(struct.pack("!IIIII", start_seq, count, index, size, offset) + part)
    return out


def main() -> int:
    if not opus.available():
        print("SKIP 找不到 libopus")
        return 0
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        print("SKIP 未安装 cryptography（uv pip install --python .venv/bin/python "
              "-r requirements-players.txt）")
        return 0

    import probe_player_audio as probe

    print("[replay] 构造样本：非 Event 1 + u16 长度前缀 + 交错分片 + 未知类型")
    silence = b"\x00\x00" * 960
    frame = opus.encode_frame(silence, rate=48000, channels=1)
    body = b"\x00\x00\x00\x00" + struct.pack("<HH", 0, len(frame)) + frame

    msg_a = _message(13, actor=3, body=body)
    msg_b = _message(13, actor=7, body=body)
    frag_a = _fragments(msg_a, 2, start_seq=10)
    frag_b = _fragments(msg_b, 2, start_seq=20)

    key = bytes(range(32))
    nonce = b"\xaa" * 12
    header = struct.pack(">H", 7) + b"\x01" + b"\x01\x02\x03\x04" + nonce
    aead = AESGCM(key)

    def sealed(command_payload: bytes, direction_port: int) -> bytes:
        ct = aead.encrypt(nonce, command_payload, header)
        return header + ct

    # 两条消息的分片交错到达（同一 channel、不同 startSeq）
    # 第一条走"不可靠通道"（type 11，payload 前多 4 字节）；第二条走可靠通道。
    packets = [
        _datagram([_command(8, _fragments(msg_a, 2, start_seq=10)[0])]),
        _datagram([_command(8, _fragments(msg_b, 2, start_seq=20)[0])]),
        _datagram([_command(8, _fragments(msg_a, 2, start_seq=10)[1])]),
        _datagram([_command(8, _fragments(msg_b, 2, start_seq=20)[1])]),
        _datagram([_command(11, b"\x00\x03\x66\x43" + msg_b)]),
    ]
    del frag_a, frag_b

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pcap = root / "cap.pcap"
        with capture_linux.PcapWriter(pcap) as writer:
            for index, plain in enumerate(packets):
                writer.write(1.0 + index, _eth_udp(sealed(plain, 27002),
                                                  sport=27002, dport=5000))
        key_file = root / "key.bin"
        key_file.write_bytes(key)

        args = type("A", (), {
            "pcap": str(pcap), "key_file": str(key_file), "out": str(root / "out"),
            "port": 27002, "layout": "hdr+nonce", "fill_gaps": False,
            "log_dir": "",
        })()
        rc = probe.cmd_replay(args)
        check("replay 退出码 0", rc == 0, str(rc))

        wavs = sorted((root / "out").glob("player_actor*.wav"))
        check("解出两个玩家的 wav", len(wavs) == 2, str([w.name for w in wavs]))
        if wavs:
            with wave.open(str(wavs[0]), "rb") as wf:
                check("wav 参数（1ch/16bit/16k）",
                      (wf.getnchannels(), wf.getsampwidth(), wf.getframerate())
                      == (1, 2, 16000), "wav 头不对")
                check("wav 里有采样", wf.getnframes() > 0, str(wf.getnframes()))

        # GUI 翻译腿唯一的入口：collector 的 pcm_sink。
        # 这里曾经漏过一次（只在内存里累加、不回调）→ 现象是"密钥命中但一个字都不出"。
        from vlt.players.collector import VoiceCollector

        got: list[tuple[int, int]] = []
        col = VoiceCollector({}, pcm_sink=lambda a, pcm: got.append((a, len(pcm))))
        for _ts, raw in capture_linux.read_pcap(pcap):
            dg = capture_linux.parse_frame(raw, port=27002)
            if dg is not None:
                col.handle(dg, key, "hdr+nonce")
        check("pcm_sink 收到音频（漏了它 GUI 就一个字都不出）",
              len(got) >= 2 and {a for a, _ in got} == {3, 7}, str(got[:4]))

        import json
        report = json.loads((root / "out" / "report.json").read_text())
        check("识别出音频消息（非 Event 1）",
              report["audio_param_hits"].get("4:13") == 3,
              str(report["audio_param_hits"]))
        check("不可靠通道（4 字节前缀）也被对齐",
              report["message_prefix_offsets"].get("4") == 1,
              str(report["message_prefix_offsets"]))
        check("分片两条都拼完", report["fragments"]["completed"] == 2,
              str(report["fragments"]))
        check("actor 3 / 7 都被识别",
              set(report["actors"]) == {"3", "7"}, str(report["actors"]))
        check("没有把出向包算进来", report["packets"]["decrypted"] == 5,
              str(report["packets"]))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（离线重放端到端）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
