"""**提权助手**：一次性替主程序做两件事，然后退出。

## 为什么必须有这个进程

主程序是普通用户，做不了两件需要特权的事：

1. 开 `AF_PACKET` 原始套接字（`CAP_NET_RAW`）抓 Photon 的 UDP；
2. 读游戏进程内存找密钥（`CAP_SYS_PTRACE`；本机 Yama `ptrace_scope=1`，
   而主程序不是 VRChat 的祖先进程）。

**合并成一个短命助手**的理由：特权代码越少越好审。助手只做「开 socket」和
「扫内存」，连 GCM 都不碰（那在主程序侧做）—— 所以它**不会**把游戏内存交出去，
只交回几十个 32 字节候选。整个进程活几秒到两分钟（取决于主程序什么时候说 bye），
没有常驻 root 服务、没有 setcap、没有任何持久化提权。

## 怎么被拉起（源码 / AppImage 两种）

- **源码**：`pkexec <venv python> scripts/vlt_priv_helper.py --socket …`
- **AppImage（冻结）**：`sys.executable` 不是解释器，所以改由**主程序自己**
  以 `--priv-helper` 重启（`run_gui.py` 在导入 GUI 之前就拦下这个开关）。这样不
  要求宿主机装 Python，也不用把脚本从 FUSE 挂载点里拷出来执行。

## 边界（硬性）

- 只接受**确认是 VRChat 的 pid**：`/proc/<pid>/maps` 里必须有 `GameAssembly.dll`，
  且 `starttime` 与请求一致（防 PID 复用）。否则直接退。
- 只读：不存在写目标内存的代码路径（`tests/test_players_safety.py` 用 AST 钉住）。
- 不把内存写盘、不写日志、不打印候选内容；只能通过**已被校验过属主**的
  unix socket（0700 目录）与主程序通信。
- 存活上限 `--max-lifetime`（默认 180s），到点就退；主程序异常退出（EOF）也会退。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from array import array
from pathlib import Path

from . import capture_linux, key_linux, procfs

EXIT_NOT_ROOT = 3
EXIT_BAD_TARGET = 4
EXIT_BAD_SOCKET = 5


def fail(code: int, message: str) -> "NoReturn":  # noqa: F821 - 仅作标注
    print(f"[helper] ❌ {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def _check_socket_owner(path: Path) -> None:
    """只接受「属主是本用户、且别的用户进不去」的目录里的 socket。

    pkexec 会把原用户 uid 放进 `PKEXEC_UID`。这条检查防的是「让 root 助手去连
    别人的 socket」——虽然能跑 pkexec 的人本来就是特权用户，但特权代码不该有
    可被复用的形状。
    """
    parent = path.parent
    try:
        st = parent.stat()
    except OSError as exc:
        fail(EXIT_BAD_SOCKET, f"拿不到 socket 目录状态：{exc}")
    want = os.environ.get("PKEXEC_UID", "")
    if want.isdigit() and st.st_uid != int(want):
        fail(EXIT_BAD_SOCKET, f"socket 目录属主 {st.st_uid} 与 PKEXEC_UID {want} 不符")
    if st.st_mode & 0o077:
        fail(EXIT_BAD_SOCKET, f"socket 目录权限过宽（{oct(st.st_mode & 0o777)}），"
                             "应为 0700")


def _verify_target(pid: int, starttime: int) -> list[procfs.Region]:
    """确认 pid 真的是 VRChat 宿主，且没有发生 PID 复用。"""
    stat_text = None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        fail(EXIT_BAD_TARGET, f"pid {pid} 不存在")
    _comm, actual = procfs.parse_stat(stat_text or "")
    if starttime and actual != starttime:
        fail(EXIT_BAD_TARGET, f"pid {pid} 的 starttime 已变（{actual} != {starttime}）"
                              "：进程已重启，请重跑探针")
    regions, err = procfs.read_maps(pid)
    if err:
        fail(EXIT_BAD_TARGET, f"读不到 maps：{err}")
    if not any("gameassembly.dll" in r.pathname.lower() for r in regions):
        fail(EXIT_BAD_TARGET, f"pid {pid} 的映射里没有 GameAssembly.dll —— "
                              "不是 VRChat 宿主，拒绝读它的内存")
    return regions


def _recv_line(conn: socket.socket, timeout: float = 1.0) -> str | None:
    conn.settimeout(timeout)
    buf = b""
    while not buf.endswith(b"\n"):
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            return None
        except OSError:
            return None
        if not chunk:
            return ""            # EOF：主程序退出/关连接，要立刻收摊，别空转到寿命上限
        buf += chunk
        if len(buf) > 1 << 20:
            return None
    return buf.decode("utf-8", "replace").strip()


def _send_json(conn: socket.socket, payload: dict, fds: list[int] | None = None) -> None:
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if not fds:
        conn.sendall(data)
        return
    conn.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", fds))])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分玩家语音的提权助手（内部使用）")
    parser.add_argument("--socket", required=True, help="主程序的 unix socket 路径")
    parser.add_argument("--token", required=True, help="握手令牌（主程序生成）")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--starttime", type=int, default=0)
    parser.add_argument("--port", type=int, default=27002)
    parser.add_argument("--max-lifetime", type=float, default=180.0)
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        fail(EXIT_NOT_ROOT, "必须以 root 运行（由主程序用 pkexec 拉起）")

    path = Path(args.socket)
    _check_socket_owner(path)
    regions = _verify_target(args.pid, args.starttime)

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(str(path))
    except OSError as exc:
        fail(EXIT_BAD_SOCKET, f"连不上主程序：{exc}")

    _send_json(conn, {"v": 1, "token": args.token, "pid": args.pid,
                      "starttime": args.starttime})

    packet_sock = None
    deadline = time.monotonic() + max(10.0, args.max_lifetime)
    try:
        # 单层循环：start / scan / refresh / bye 都可能反复出现。
        # （原来套了两层，导致"再开一个 socket 做诊断"这种请求在第二层里被判未知命令。）
        while time.monotonic() < deadline:
            line = _recv_line(conn, timeout=1.0)
            if line is None:
                continue
            if line == "":
                break
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                _send_json(conn, {"ok": False, "error": "请求不是 JSON"})
                continue

            kind = str(cmd.get("cmd") or "")
            if kind == "bye":
                _send_json(conn, {"ok": True})
                break
            if kind == "refresh":
                regions = _verify_target(args.pid, args.starttime)
                _send_json(conn, {"ok": True, "regions": len(regions)})
                continue
            if kind == "start":
                mode = str(cmd.get("filter") or "strict")
                if mode not in ("strict", "loose", "none"):
                    _send_json(conn, {"ok": False, "error": f"未知过滤模式 {mode!r}"})
                    continue
                try:
                    fresh = capture_linux.open_packet_socket(args.port, filter_mode=mode)
                except Exception as exc:            # noqa: BLE001 - 原样回给主程序
                    _send_json(conn, {"ok": False, "error": f"开抓包 socket 失败：{exc}"})
                    continue
                if packet_sock is not None:
                    packet_sock.close()
                _send_json(conn, {"ok": True, "pid": args.pid,
                                  "starttime": args.starttime, "regions": len(regions),
                                  "port": args.port, "filter": mode},
                           fds=[fresh.fileno()])
                # hand over 之后主程序自己读；助手这边关掉副本（内核 socket 对象
                # 因为主程序还持有引用而继续存在）。
                fresh.close()
                packet_sock = None
                continue
            if kind != "scan":
                _send_json(conn, {"ok": False, "error": f"未知命令 {kind!r}"})
                continue
            budget = float(cmd.get("deadline_s", 30.0))
            needle = str(cmd.get("needle") or "q")
            print(f"[helper] 开始扫描匿名可读映射（预算 {budget:.0f}s）",
                  file=sys.stderr, flush=True)
            try:
                res = key_linux.scan_key(args.pid, deadline_s=budget,
                                         needle=needle, regions=regions)
            except key_linux.MemoryBlocked as exc:
                _send_json(conn, {"ok": False, "blocked": True,
                                  "error": f"读内存被拒绝：{exc}"})
                continue
            print(f"[helper] 扫完 {res.scanned_bytes / 2**30:.2f} / "
                  f"{res.region_bytes / 2**30:.2f} GiB / {res.elapsed_ms:.0f}ms，"
                  f"候选 {len(res.candidates)}，needle[{res.needle}] 命中 "
                  f"{res.needle_hits}（{res.stopped}）",
                  file=sys.stderr, flush=True)
            _send_json(conn, {
                "ok": True,
                "candidates": [c.hex() for c in res.candidates],
                "scanned_bytes": res.scanned_bytes,
                "elapsed_ms": round(res.elapsed_ms, 1),
                "needle_hits": res.needle_hits,
                "regions": res.regions,
                "region_bytes": res.region_bytes,
                "stopped": res.stopped,
                "needle": res.needle,
                "fingerprints": res.fingerprints,
            })
    finally:
        if packet_sock is not None:
            packet_sock.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
