#!/usr/bin/env python
"""提权助手的**协议联调**（不需要 root）：socket 握手 + 令牌 + fd 传递。

## 为什么这条用例必须存在

`run` 里最容易悄悄坏掉的不是密码学，而是**跨进程交接**：

- `SCM_RIGHTS` 传 fd（抓包 socket）—— 传丢了主程序会一直收不到包；
- 行式 JSON 粘包/半包 —— 助手回复被切成两段时，朴素 `recv()` 会解析失败；
- 令牌校验 —— 防止把 root 助手接到别人的 socket 上。

这些全都可以用**假助手**测：把 `helper_command()` 换成「仓库自己的 python 跑一段
脚本」，脚本只做三件事：连 socket、回 hello、把 socketpair 的一端用 `SCM_RIGHTS`
发回去。真实助手被替换掉，但主程序侧的代码路径（`HelperSession`）**一行不改**。

跑法：.venv/bin/python tests/test_players_helper.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))

from vlt.players import helper as helper_mod, procfs  # noqa: E402
import probe_player_audio as probe  # noqa: E402  (仅为兼容：测试替换的是 helper 模块)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


FAKE_HELPER = r'''
import argparse, array, json, os, socket, sys

p = argparse.ArgumentParser()
p.add_argument("--socket"); p.add_argument("--token"); p.add_argument("--pid", type=int)
p.add_argument("--starttime", type=int, default=0); p.add_argument("--port", type=int)
a = p.parse_args()

conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
conn.connect(a.socket)
conn.sendall((json.dumps({"v": 1, "token": a.token, "pid": a.pid,
                          "starttime": a.starttime}) + "\n").encode())

def recv_line():
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            return ""
        buf += chunk
    return buf.decode()

while True:
    line = recv_line()
    if line == "":
        break
    cmd = json.loads(line)
    if cmd.get("cmd") == "bye":
        try:
            conn.sendall(b'{"ok": true}\n')
        except OSError:
            pass              # 主程序可能已经关了；这不是错误
        break
    if cmd.get("cmd") == "start":
        ours, theirs = socket.socketpair()
        reply = json.dumps({"ok": True, "pid": a.pid, "starttime": a.starttime,
                            "regions": 3, "port": a.port}).encode() + b"\n"
        conn.sendmsg([reply], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                array.array("i", [theirs.fileno()]))])
        theirs.close()
        ours.sendall(b"X")    # 证明交出去的 fd 是连通的双向 socket
        ours.close()
        continue
    if cmd.get("cmd") == "scan":
        # 故意分两段发，验证主程序能处理半包
        payload = json.dumps({"ok": True, "candidates": ["00" * 32, "11" * 32],
                              "scanned_bytes": 4096, "elapsed_ms": 1.5,
                              "needle_hits": 2, "regions": 3, "stopped": "done",
                              "fingerprints": ["deadbeef"]}).encode() + b"\n"
        conn.sendall(payload[:20])
        conn.sendall(payload[20:])
        continue
sys.exit(0)
'''


def test_helper_session_roundtrip() -> None:
    print("[helper] 握手 / fd 传递 / 分扫描请求")
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "fake_helper.py"
        fake.write_text(FAKE_HELPER, encoding="utf-8")

        original = helper_mod.helper_command

        def _fake_command(session):      # noqa: ANN001 - 测试替身
            return [sys.executable, str(fake),
                    "--socket", str(session.sock_path),
                    "--token", session.token,
                    "--pid", str(session.host.pid),
                    "--port", str(session.port)]

        helper_mod.helper_command = _fake_command
        try:
            host = procfs.ProcInfo(pid=4242, comm="VRChat.exe", cmdline=("VRChat.exe",),
                                   starttime=1, uid=1000,
                                   summary=procfs.MapSummary(anon_rw_bytes=1 << 20,
                                                             has_gameassembly=True))
            with probe.HelperSession(host, port=27002) as sess:
                check("握手成功（令牌一致）", sess.conn is not None)
                sess.start_capture()
                check("拿到抓包 fd", sess.packet_socket is not None)
                if sess.packet_socket is not None:
                    # 假助手给的是 socketpair 的一端，并先在对面写了一个字节
                    sess.packet_socket.settimeout(3.0)
                    got = sess.packet_socket.recv(1)
                    check("fd 是连通的 socket（收到助手发来的 1 字节）",
                          got == b"X", repr(got))
                reply = sess.scan(deadline_s=2.0)
                check("扫描回复 ok", bool(reply.get("ok")), str(reply))
                check("候选被正确解码为 32 字节",
                      len(reply.get("candidates", [])) == 2
                      and len(bytes.fromhex(reply["candidates"][0])) == 32,
                      str(reply.get("candidates")))
                check("半包 JSON 也能读全（前 20 字节先到）",
                      reply.get("scanned_bytes") == 4096, str(reply.get("scanned_bytes")))
        finally:
            helper_mod.helper_command = original


def test_token_mismatch_rejected() -> None:
    """令牌不符必须拒绝 —— 否则随便谁都能把 root 助手接到自己的 socket 上。"""
    print("[helper] 令牌不符要拒绝")
    with tempfile.TemporaryDirectory() as tmp:
        evil = Path(tmp) / "evil.py"
        evil.write_text(
            "import argparse, json, socket\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--socket'); p.add_argument('--token'); p.add_argument('--pid', type=int)\n"
            "p.add_argument('--starttime', type=int, default=0); p.add_argument('--port', type=int)\n"
            "a = p.parse_args()\n"
            "c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(a.socket)\n"
            "c.sendall((json.dumps({'v': 1, 'token': 'wrong', 'pid': a.pid})+'\\n').encode())\n"
            "c.close()\n", encoding="utf-8")
        original = helper_mod.helper_command
        helper_mod.helper_command = lambda s: [sys.executable, str(evil),
                                          "--socket", str(s.sock_path),
                                          "--token", s.token,
                                          "--pid", str(s.host.pid)]
        try:
            host = procfs.ProcInfo(pid=4242, comm="VRChat.exe", cmdline=("VRChat.exe",),
                                   starttime=1, uid=1000, summary=procfs.MapSummary())
            raised = ""
            try:
                with probe.HelperSession(host):
                    pass
            except RuntimeError as exc:
                raised = str(exc)
            check("令牌错误时抛 RuntimeError", "令牌" in raised, raised)
        finally:
            helper_mod.helper_command = original


def test_frozen_command_shape() -> None:
    """AppImage（冻结）里 `sys.executable` 不是解释器 → 必须改走 `--priv-helper`。"""
    print("[helper] 冻结产物的命令行形态")
    original = helper_mod.FROZEN
    helper_mod.FROZEN = True
    try:
        host = procfs.ProcInfo(pid=4242, comm="VRChat.exe", cmdline=("VRChat.exe",),
                               starttime=1, uid=1000, summary=procfs.MapSummary())
        sess = probe.HelperSession(host)
        sess.sock_path = Path("/run/user/1000/vlt/x.sock")
        cmd = helper_mod.helper_command(sess)
        check("第一个参数是 pkexec", cmd[0] == "pkexec", str(cmd[:3]))
        check("用宿主 python3 + stdin（不执行挂载点里的东西）",
              "python" in cmd[1] and cmd[2] == "-", str(cmd[:3]))
        check("不再传脚本路径", all("vlt_priv_helper.py" not in c for c in cmd), str(cmd[:3]))
        payload = helper_mod.helper_payload()
        check("冻结模式下有 stdin 脚本", payload is not None and len(payload) > 10000,
              str(len(payload or b"")))
    finally:
        helper_mod.FROZEN = original


def test_payload_runs() -> None:
    """自包含脚本必须真的能跑（模块顺序/相对导入都对），而不是只有长度。"""
    print("[helper] 自包含脚本可执行性")
    import subprocess
    import tempfile

    blob = helper_mod.payload_mod.build_payload()
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "helper.py"
        script.write_bytes(blob)
        proc = subprocess.run(
            [sys.executable, str(script), "--socket", "/tmp/x", "--token", "t", "--pid", "1"],
            capture_output=True, text=True, timeout=30)
    check("非 root 时退出码 3", proc.returncode == 3, str(proc.returncode))
    check("提示要求 root 且说明来源", "root" in proc.stderr, proc.stderr[-200:])


def main() -> int:
    test_helper_session_roundtrip()
    test_token_mismatch_rejected()
    test_frozen_command_shape()
    test_payload_runs()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（分玩家助手协议联调）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
