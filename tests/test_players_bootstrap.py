#!/usr/bin/env python
"""一次授权（`helper.bootstrap`）的离线验收。

## 为什么

预热原来分两次起助手：开抓包一个、扫内存一个 → 用户被连着要两次 pkexec 密码。
`bootstrap` 把它们合进**同一个短命助手**：开 socket → 采样 → 两种 needle 扫描 →
交回 (socket, 候选) → 退出。

## 这里钉住

1. 整次 bootstrap 只创建 **1 个** `HelperSession`（= 只弹一次授权）；
2. 会话退出后 socket 仍然可用（fd 已交回主进程，不能被子会话 close 掉）；
3. 两种 needle 都在**同一个会话**里扫；命中即返回；
4. 读内存被拒 → `blocked=True`（调用方按「不可用」处理，别再重试）；
5. 没抓到样本 → 只交回 socket，不扫内存（等有流量再取钥）。

跑法：.venv/bin/python tests/test_players_bootstrap.py
"""
from __future__ import annotations

import hashlib
import os
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import gcm, helper, procfs  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


KEY = bytes(range(32))
LOCAL = ("192.168.1.10", 53000)
SERVER = ("34.216.184.93", 27002)


class _FakeAead:
    def __init__(self, key: bytes) -> None:
        self.key = key

    def _xor(self, data: bytes) -> bytes:
        return bytes(b ^ self.key[i % len(self.key)] for i, b in enumerate(data))

    def encrypt(self, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        ct = self._xor(data)
        return ct + hashlib.sha256(self.key + nonce + aad + ct).digest()[:16]

    def decrypt(self, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        ct, tag = data[:-16], data[-16:]
        if tag != hashlib.sha256(self.key + nonce + aad + ct).digest()[:16]:
            raise ValueError("tag 不匹配")
        return self._xor(ct)


def _packet(key: bytes, plaintext: bytes, nonce: bytes) -> bytes:
    aad = b"\x00\x07\x01\x01\x02\x03\x04" + nonce
    return aad + _FakeAead(key).encrypt(nonce, plaintext, aad)


def _command(message: bytes) -> bytes:
    return (b"\x01\x00\x00\x00\x00"
            + b"\x02\x00\x00\x00" + struct.pack("!I", 12 + len(message)) + b"\x00\x00\x00\x01"
            + message)


def _frame(payload: bytes) -> bytes:
    udp = struct.pack("!HHHH", SERVER[1], LOCAL[1], 8 + len(payload), 0) + payload
    ip = bytearray(20)
    ip[0] = 0x45
    ip[2:4] = struct.pack("!H", 20 + len(udp))
    ip[8] = 64
    ip[9] = 17
    ip[12:16] = bytes(int(x) for x in SERVER[0].split("."))
    ip[16:20] = bytes(int(x) for x in LOCAL[0].split("."))
    return bytes(ip) + udp


def _encrypted_frames(count: int = 4) -> list[tuple[float, bytes]]:
    frames = []
    for i in range(count):
        plain = _command(b"\xf3\x04\x2a\x01" + b"\xfe\x0d" + (7).to_bytes(2, "little"))
        frames.append((time.time(), _frame(_packet(KEY, plain, bytes([i] * 12)))))
    return frames


class _FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    instances: list["_FakeSession"] = []
    reply = "ok"          # ok | blocked | fail_q

    def __init__(self, host, *, port=27002, lifetime_s=60.0) -> None:  # noqa: ANN001
        self.host = host
        self.port = port
        self.lifetime_s = lifetime_s
        self.socket = _FakeSocket()
        self.packet_socket = None
        self.scanned: list[str] = []
        self.closed = False
        self.mode = ""
        _FakeSession.instances.append(self)

    def __enter__(self):
        self.packet_socket = self.socket
        return self

    def __exit__(self, *exc) -> bool:  # noqa: ANN002
        self.closed = True
        return False

    def start_capture(self, *, filter_mode: str = "strict") -> None:
        self.mode = filter_mode

    def scan(self, *, deadline_s: float = 40.0, needle: str = "q") -> dict:
        self.scanned.append(needle)
        if _FakeSession.reply == "blocked":
            return {"ok": False, "blocked": True, "error": "读内存被拒绝"}
        if _FakeSession.reply == "fail_q" and needle == "q":
            return {"ok": False, "error": "needle q 没命中"}
        return {"ok": True, "scanned_bytes": 1 << 30, "candidates": [KEY.hex()]}


def _host():
    return procfs.collect(os.getpid(), with_maps=False)


def _install(frames: list | None):
    _FakeSession.instances = []
    _FakeSession.reply = "ok"
    original_session = helper.HelperSession
    original_drain = helper.capture_linux.drain
    queue = list(frames or [])

    def _drain(_sock, *, timeout=0.2, max_frames=128):  # noqa: ANN001
        batch, queue[:] = queue[:max_frames], queue[max_frames:]
        return batch

    helper.HelperSession = _FakeSession
    helper.capture_linux.drain = _drain
    return original_session, original_drain


def test_one_session() -> None:
    print("[bootstrap] 一次授权做完抓包 + 取钥")
    frames = _encrypted_frames()
    orig_s, orig_d = _install(frames)
    real_aead = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)         # noqa: SLF001
    try:
        result = helper.bootstrap(_host(), sample_s=0.3, deadline_s=1.0)
        check("只起了 1 个助手会话", len(_FakeSession.instances) == 1,
              str(len(_FakeSession.instances)))
        session = _FakeSession.instances[0]
        check("会话已退出（短命）", session.closed)
        check("开的是 strict 抓包", session.mode == "strict", session.mode)
        check("拿到了密钥", result.key is not None and result.key.key == KEY,
              str(result.key))
        check("socket 交回且没被会话关掉",
              result.socket is session.socket and not session.socket.closed)
        check("采样里留下了包", len(result.packets) >= 2, str(len(result.packets)))
        check("只在同一个会话里扫了 q", session.scanned == ["q"], str(session.scanned))
    finally:
        helper.HelperSession, helper.capture_linux.drain = orig_s, orig_d
        gcm._aesgcm = real_aead


def test_fallback_needle() -> None:
    print("[bootstrap] q 验不过 → 同一个会话里试 i")
    orig_s, orig_d = _install(_encrypted_frames())
    real_aead = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)         # noqa: SLF001
    try:
        _FakeSession.reply = "fail_q"
        result = helper.bootstrap(_host(), sample_s=0.3, deadline_s=1.0)
        session = _FakeSession.instances[0]
        check("两种 needle 都在同一会话里", session.scanned == ["q", "i"],
              str(session.scanned))
        check("仍然起了 1 个助手", len(_FakeSession.instances) == 1)
        check("i 命中", result.key is not None)
    finally:
        helper.HelperSession, helper.capture_linux.drain = orig_s, orig_d
        gcm._aesgcm = real_aead


def test_blocked() -> None:
    print("[bootstrap] 读内存被拒 → blocked")
    orig_s, orig_d = _install(_encrypted_frames())
    real_aead = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)         # noqa: SLF001
    try:
        _FakeSession.reply = "blocked"
        result = helper.bootstrap(_host(), sample_s=0.3, deadline_s=1.0)
        check("标记 blocked", result.blocked and result.key is None, str(result))
        check("socket 仍然交回", result.socket is not None)
        check("只扫了一次就收手", _FakeSession.instances[0].scanned == ["q"],
              str(_FakeSession.instances[0].scanned))
    finally:
        helper.HelperSession, helper.capture_linux.drain = orig_s, orig_d
        gcm._aesgcm = real_aead


def test_no_traffic() -> None:
    print("[bootstrap] 没有流量 → 只交回 socket，不扫内存")
    orig_s, orig_d = _install([])
    real_aead = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)         # noqa: SLF001
    try:
        started = time.monotonic()
        result = helper.bootstrap(_host(), sample_s=0.3, deadline_s=1.0)
        check("没扫内存", _FakeSession.instances[0].scanned == [],
              str(_FakeSession.instances[0].scanned))
        check("socket 可用", result.socket is not None)
        check("说明了原因", "流量" in (result.detail or ""), result.detail)
        check("没有等太久", time.monotonic() - started < 1.5,
              f"{time.monotonic() - started:.2f}s")
    finally:
        helper.HelperSession, helper.capture_linux.drain = orig_s, orig_d
        gcm._aesgcm = real_aead


def main() -> int:
    if _host() is None:
        print("SKIP 读不到 /proc/self")
        return 0
    test_one_session()
    test_fallback_needle()
    test_blocked()
    test_no_traffic()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {', '.join(FAILS)}")
        return 1
    print("ALL PASSED（一次授权）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
