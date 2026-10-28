#!/usr/bin/env python
"""世界加载闸门 / 换世界清表 / 玩家名表变更计数 的离线验收。

## 这些用例能证明什么

1. **日志标记**：只认 VRChat 真正会打的 5 句话，玩家名、堆栈行、超长行都不算；
2. **尾部倒查**：标记离文件尾很远（超过一个扫描块）也能找到，不用把几十 MB 读一遍；
3. **跟读**：追加 / 换文件 / 截断都能跟上；`unknown ≠ loading`（读不到日志不拦人）；
4. **事件 226**：从出向报文里解出 `wrld_` ID，并在换世界时清掉 actor/名字/分片；
5. 名字表的 `version` 只在真的变化时递增（分玩家腿靠它决定要不要推给界面）。

跑法：.venv/bin/python tests/test_players_world.py
"""
from __future__ import annotations

import hashlib
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import capture_linux, gcm, names, photon, world  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


def line(message: str) -> str:
    return f"2026.10.25 03:00:00 Debug      -  {message}\n"


# ---------------------------------------------------------------- 日志标记


def test_markers() -> None:
    print("[world] 日志行与标记")
    check("取出 ` - ` 之后的内容",
          world.log_message(line("[Behaviour] OnLeftRoom").rstrip("\n"))
          == "[Behaviour] OnLeftRoom")
    check("玩家名里带标记不算",
          world.marker_of("[Behaviour] OnPlayerJoined Finished entering world") is None)
    check("堆栈行不算", world.log_message(" at OnLeftRoom()") is None)
    check("空行不算", world.log_message("") is None)
    check("ready 标记",
          world.marker_of("Memory Usage: after world loaded [test]") is True)
    check("ready 标记（另一种写法）",
          world.marker_of("[Behaviour] Finished entering world") is True)
    check("loading 标记",
          world.marker_of("[Behaviour] Entering Room: test") is False)
    check("离开后不再 ready", world.marker_of("[Behaviour] OnLeftRoom") is False)
    check("无标记返回 None", world.marker_of("Unrelated log message") is None)
    check("只认最后一条标记",
          world.last_marker(line("Memory Usage: after world loaded")
                           + line("[Behaviour] OnLeftRoom")) is False)
    check("没有完整行时不算",
          world.last_marker("2026.10.25 03:00:00 Debug      -  [Behaviour] OnLeftRoom")
          is None)


# 用「现在」做基准：往日志里追加会把 mtime 刷成当前时间，用固定过去时间会判错
BASE_MTIME = time.time()


def _write(path: Path, text: str, *, mtime: float) -> None:
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_gate(tmp_path: Path) -> None:
    print("[world] WorldGate 跟读")
    gate = world.WorldGate(tmp_path)
    check("没有日志时是 unknown（不拦人）", gate.poll() == world.STATE_UNKNOWN)
    check("unknown 不算 loading", not gate.loading)

    log = tmp_path / "output_log_1.txt"
    _write(log, line("[Behaviour] Entering Room: test"), mtime=BASE_MTIME + 10)
    check("正在进世界", gate.poll() == world.STATE_LOADING)
    check("loading 会被闸门拦住", gate.loading)

    with log.open("a", encoding="utf-8") as fh:
        fh.write(line("Unrelated log message"))
    check("无关行不改变状态", gate.poll() == world.STATE_LOADING)

    with log.open("a", encoding="utf-8") as fh:
        fh.write(line("Memory Usage: after world loaded [test]"))
    check("加载完成 → ready", gate.poll() == world.STATE_READY)
    check("ready 时不再拦人", not gate.loading)

    with log.open("a", encoding="utf-8") as fh:
        fh.write(line("[Behaviour] OnLeftRoom"))
    check("离开房间 → loading", gate.poll() == world.STATE_LOADING)

    # 换一份更新的日志（VRChat 每次启动新建一个）：从头倒查
    newer = tmp_path / "output_log_2.txt"
    _write(newer, line("Memory Usage: after world loaded [next]"), mtime=BASE_MTIME + 20)
    check("跟上新日志", gate.poll() == world.STATE_READY)
    check("记录的是新日志", gate.path == newer, str(gate.path))

    # 标记离尾部很远（超过一个倒查块）
    far = tmp_path / "output_log_3.txt"
    padding = "".join(line(f"noise {i}") for i in range(12000))
    _write(far, line("[Behaviour] Finished entering world") + padding, mtime=BASE_MTIME + 30)
    check("倒查能跨块找到标记", gate.poll() == world.STATE_READY)
    check("大文件标记在块外也不误判",
          far.stat().st_size > world.SCAN_CHUNK, str(far.stat().st_size))

    # 截断（同一路径被重写）
    _write(far, line("[Behaviour] Entering Room: again"), mtime=BASE_MTIME + 31)
    check("截断后重新倒查", gate.poll() == world.STATE_LOADING)


# ---------------------------------------------------------------- 事件 226


def _join_message(world_id: bytes) -> bytes:
    return b"\xf3\x02\xe2\x00" + world_id


def test_joined_world() -> None:
    print("[world] 出向事件 226 → wrld_ ID")
    world_id = b"wrld_00000000-0000-4000-8000-000000000000:12345~region(jp)"
    payload = b"\x00\x00\x00\x00" + _join_message(world_id)
    check("带不可靠通道前缀也能解",
          photon.joined_world(payload) == world_id.decode())
    check("没有 wrld_ 返回 None",
          photon.joined_world(b"\xf3\x02\xe2\x00") is None)
    check("太短的世界 ID 不认",
          photon.joined_world(b"\xf3\x02\xe2\x00wrld_short") is None)
    check("不是 226 不认",
          photon.joined_world(b"\xf3\x02\xe3\x00" + world_id) is None)
    check("世界 ID 在非 ascii 处截断",
          photon.joined_world(b"\xf3\x02\xe2\x00" + world_id + b"\x00\xff")
          == world_id.decode())


# ---------------------------------------------------------------- 采集器清表


class _FakeAead:
    """按真实 AEAD 的形状工作（nonce/aad 参与 tag），只用来验证切片。"""

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


def _datagram(payload: bytes, *, direction: str) -> capture_linux.Datagram:
    local, server = ("192.168.1.10", 53000), ("34.216.184.93", 27002)
    src, dst = (server, local) if direction == "in" else (local, server)
    return capture_linux.Datagram(ts=1.0, src=src, dst=dst,
                                  payload=payload, direction=direction)


def _text(value: str) -> bytes:
    raw = value.encode()
    return bytes([0x07, len(raw)]) + raw


def _name_message(actor: int, display: str) -> bytes:
    usr = "usr_00000000-0000-4000-8000-000000000007"
    props = (bytes([1]) + _text("user") + bytes([0x15, 2])
             + _text("id") + _text(usr)
             + _text("displayName") + _text(display))
    return (b"\xf3\x04\x2a\x02" + b"\xf5\x15" + props
            + b"\xfe\x0d" + actor.to_bytes(2, "little"))


def test_collector_world_reset() -> None:
    print("[world] 采集器：换世界清 actor/名字/分片")
    try:
        from vlt.players.collector import VoiceCollector
        from vlt.players import opus
        if not opus.available():
            print("  SKIP 找不到 libopus")
            return
    except Exception as exc:                        # noqa: BLE001
        print(f"  SKIP 导入失败：{exc}")
        return

    key = bytes(range(32))
    real = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)           # noqa: SLF001 - 测的就是这里
    try:
        collector = VoiceCollector({})
        check("名字表初始为空", len(collector.names) == 0)
        version = collector.names.version
        collector.handle(_datagram(_packet(key, _command(_name_message(7, "玩家A")),
                                           bytes(12)), direction="in"),
                         key, "hdr+nonce")
        check("解出玩家名", len(collector.names) == 1,
              str(collector.names.snapshot()))
        check("名字表 version 递增", collector.names.version > version)

        first = b"wrld_11111111-1111-4111-8111-111111111111:1~region(jp)"
        collector.handle(_datagram(_packet(key, _command(_join_message(first)),
                                           bytes([1]) * 12), direction="out"),
                         key, "hdr+nonce")
        check("第一次见到世界只记录、不清表",
              collector.world == first.decode() and len(collector.names) == 1,
              f"world={collector.world!r} names={len(collector.names)}")

        second = b"wrld_22222222-2222-4222-8222-222222222222:2~region(us)"
        collector.handle(_datagram(_packet(key, _command(_join_message(second)),
                                           bytes([2]) * 12), direction="out"),
                         key, "hdr+nonce")
        check("换世界清空名字表", len(collector.names) == 0, str(collector.names.snapshot()))
        check("换世界计数", collector.world_changes == 1, str(collector.world_changes))
        check("清表也递增 version", collector.names.version > 0)
        check("同一世界重复到达不再清表",
              collector.handle(_datagram(_packet(key, _command(_join_message(second)),
                                                 bytes([3]) * 12), direction="out"),
                                key, "hdr+nonce") is None
              and collector.world_changes == 1)
        collector.finish(Path(tempfile.mkdtemp()))
        report = collector.report
        check("报告里有世界信息",
              report.get("world") == second.decode() and report.get("world_changes") == 1,
              str({k: report.get(k) for k in ("world", "world_changes")}))
    finally:
        gcm._aesgcm = real


def test_default_log_dir() -> None:
    print("[world] 日志目录兜底")
    path = names.default_log_dir()
    check("返回 Path", isinstance(path, Path), str(path))
    check("路径以 VRChat 结尾", path.name == "VRChat", str(path))


def main() -> int:
    test_markers()
    test_joined_world()
    test_collector_world_reset()
    test_default_log_dir()
    with tempfile.TemporaryDirectory() as tmp:
        test_gate(Path(tmp))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {', '.join(FAILS)}")
        return 1
    print("ALL PASSED（世界闸门与换世界清表离线验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
