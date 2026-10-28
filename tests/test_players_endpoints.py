#!/usr/bin/env python
"""游戏 UDP 端点发现 + 方向判定 的离线验收。

## 这些用例能证明什么

1. `/proc/net/udp` 的**小端十六进制**地址/端口解析是对的（包括「未连接」的空远端）；
2. 端点表按 **inode** 与 `/proc/<pid>/fd` 对上，别的进程的 UDP 端口不会被算进来；
3. 方向判定：有端点表时要求「本机这一端确实是游戏」，没有时退回只比端口；
   两端都是 27002 这种歧义情形**不猜**（猜错会把自己的声音当别人的）。

跑法：.venv/bin/python tests/test_players_endpoints.py
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import capture_linux, procfs  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


#: 真实 `/proc/net/udp` 的两种行：未连接（rem 全 0）与已连接。
LISTEN_LINE = ("  456: 0100007F:B26C 00000000:0000 07 00000000:00000000 "
               "00:00000000 00000000  1000        0 123456 2 "
               "0000000000000000 0")
CONNECTED_LINE = ("  457: 0100007F:9C40 5DB8D822:697A 01 00000000:00000000 "
                  "00:00000000 00000000  1000        0 123457 2 "
                  "0000000000000000 0")


def test_parse_line() -> None:
    print("[endpoints] /proc/net/udp 行解析")
    listen = procfs.parse_udp_line(LISTEN_LINE)
    check("未连接行解析成功", listen is not None)
    if listen:
        check("本地地址按小端还原", listen[0] == "127.0.0.1", listen[0])
        check("本地端口按十六进制还原", listen[1] == 0xB26C, hex(listen[1]))
        check("未连接时远端为空", listen[2] is None and listen[3] is None, str(listen))
        check("inode 取第 10 列", listen[4] == 123456, str(listen[4]))

    conn = procfs.parse_udp_line(CONNECTED_LINE)
    check("已连接行解析成功", conn is not None)
    if conn:
        check("远端地址正确", conn[2] == "34.216.184.93", str(conn[2]))
        check("远端端口 = 27002", conn[3] == 27002, str(conn[3]))
        check("端口不是 27002 的包不会被当成语音",
              conn[3] == 27002 and conn[1] == 0x9C40)

    check("表头行被跳过", procfs.parse_udp_line(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode") is None)
    check("垃圾行被跳过", procfs.parse_udp_line("hello world") is None)
    check("地址段坏了返回 None",
          procfs.parse_udp_line("  1: zzzz:0001 00000000:0000 07 0 0 0 0 0 1") is None)


def test_fake_proc(tmp: Path) -> None:
    print("[endpoints] 假 /proc 树：inode ↔ 进程")
    pid = 4242
    other = 4343
    for owner in (pid, other):
        (tmp / str(owner) / "fd").mkdir(parents=True)
    (tmp / "net").mkdir()
    (tmp / "net" / "udp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode\n"
        + LISTEN_LINE + "\n" + CONNECTED_LINE + "\n"
        + "  458: 0100007F:1F90 00000000:0000 07 0 0 0 0 0 999999 2 0 0\n",
        encoding="utf-8")
    os.symlink("socket:[123456]", tmp / str(pid) / "fd" / "4")
    os.symlink("socket:[123457]", tmp / str(pid) / "fd" / "5")
    os.symlink("socket:[999999]", tmp / str(other) / "fd" / "4")   # 别的进程的
    os.symlink("/dev/null", tmp / str(pid) / "fd" / "7")
    os.symlink("pipe:[1234]", tmp / str(pid) / "fd" / "8")

    inodes = procfs.socket_inodes(pid, root=tmp)
    check("只认 socket:[N] 链接", inodes == {123456, 123457}, str(inodes))

    endpoints = procfs.udp_endpoints(pid, root=tmp)
    check("端点按 inode 过滤（别的进程不混进来）",
          {e.inode for e in endpoints} == inodes, str([e.inode for e in endpoints]))
    check("拿到本地端口集合",
          procfs.udp_ports(pid, root=tmp) == {0xB26C, 0x9C40},
          str(procfs.udp_ports(pid, root=tmp)))
    check("另一个进程的端口互不干扰",
          procfs.udp_ports(other, root=tmp) == {0x1F90},
          str(procfs.udp_ports(other, root=tmp)))
    connected = [e for e in endpoints if e.connected]
    check("已连接端点带远端端口",
          len(connected) == 1 and connected[0].remote_port == 27002,
          str([(e.local_port, e.remote_port) for e in endpoints]))

    # inode 匹配不上时不能瞎猜「这些都是我的」
    (tmp / "net" / "udp").write_text(
        "  sl  local_address rem_address   st ...\n"
        + LISTEN_LINE.replace("123456", "555555") + "\n", encoding="utf-8")
    check("没有交集时返回空", procfs.udp_endpoints(pid, root=tmp) == [])
    check("读不到 fd 目录时返回空", procfs.udp_endpoints(777, root=tmp) == [])


def ip_frame(src: tuple[str, int], dst: tuple[str, int], payload: bytes = b"x") -> bytes:
    udp = struct.pack("!HHHH", src[1], dst[1], 8 + len(payload), 0) + payload
    ip = bytearray(20)
    ip[0] = 0x45
    ip[2:4] = struct.pack("!H", 20 + len(udp))
    ip[8] = 64
    ip[9] = 17
    ip[12:16] = bytes(int(x) for x in src[0].split("."))
    ip[16:20] = bytes(int(x) for x in dst[0].split("."))
    return bytes(ip) + udp


LOCAL = ("192.168.1.10", 53000)
SERVER = ("34.216.184.93", 27002)


def test_direction() -> None:
    print("[endpoints] 方向判定")
    check("服务器 → 本机 = in",
          capture_linux.direction_of(SERVER, LOCAL) == "in")
    check("本机 → 服务器 = out",
          capture_linux.direction_of(LOCAL, SERVER) == "out")
    check("无关流量被忽略",
          capture_linux.direction_of(("1.2.3.4", 1234), ("5.6.7.8", 4321)) is None)
    check("两端都是 27002 时不猜",
          capture_linux.direction_of(("192.168.1.10", 27002),
                                     ("34.216.184.93", 27002)) is None)

    ports = {53000}
    check("端点表里的本地端口 = in",
          capture_linux.direction_of(SERVER, LOCAL, local_ports=ports) == "in")
    check("端点表里的本地端口 = out",
          capture_linux.direction_of(LOCAL, SERVER, local_ports=ports) == "out")
    check("不是游戏的本地端口 → 丢弃（别的进程）",
          capture_linux.direction_of(SERVER, ("192.168.1.10", 60001),
                                     local_ports=ports) is None)
    check("上行也不是游戏的 → 丢弃",
          capture_linux.direction_of(("192.168.1.10", 60001), SERVER,
                                     local_ports=ports) is None)


def test_parse_frame() -> None:
    print("[endpoints] 帧解析带端点表")
    frame = ip_frame(SERVER, LOCAL)
    dg = capture_linux.parse_frame(frame, local_ports={53000})
    check("正常入向包", dg is not None and dg.direction == "in",
          str(dg.direction if dg else None))
    check("没有端点表时也能解", capture_linux.parse_frame(frame) is not None)
    check("端点表不匹配时丢包",
          capture_linux.parse_frame(frame, local_ports={53001}) is None)
    check("上行包方向正确",
          capture_linux.parse_frame(ip_frame(LOCAL, SERVER),
                                    local_ports={53000}).direction == "out")
    # 以太网帧（12 字节目的 + 源 + ethertype）
    eth = b"\x00" * 12 + b"\x08\x00" + frame
    eth_dg = capture_linux.parse_frame(eth, local_ports={53000})
    check("以太网帧同样支持", eth_dg is not None and eth_dg.direction == "in")


def main() -> int:
    test_parse_line()
    test_direction()
    test_parse_frame()
    with tempfile.TemporaryDirectory() as tmp:
        test_fake_proc(Path(tmp))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {', '.join(FAILS)}")
        return 1
    print("ALL PASSED（UDP 端点与方向判定离线验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
