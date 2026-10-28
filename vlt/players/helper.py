"""与提权助手的会话（握手 / 令牌 / fd 传递 / 扫描请求）。

从 `scripts/probe_player_audio.py` 抽出来的原因：GUI 的分玩家腿要跑**同一条**
链路，不能复制一份（口径分裂）。
"""
from __future__ import annotations

import array
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import payload as payload_mod, procfs

ROOT = Path(__file__).resolve().parent.parent.parent
HELPER = ROOT / "scripts" / "vlt_priv_helper.py"
# PyInstaller/AppImage：`sys.executable` 是**打包后的主程序**，不是 Python 解释器。
# 所以冻结后改用 `--priv-helper` 让主程序自己以助手身份重启（run_gui.py 里拦这个开关）。
FROZEN = bool(getattr(sys, "frozen", False))


def helper_payload() -> bytes | None:
    """冻结产物要经 stdin 送过去的自包含脚本；源码运行返回 None（直接跑文件）。"""
    if not FROZEN:
        return None
    return payload_mod.build_payload()


def helper_command(session: "HelperSession") -> list[str]:
    """拉起提权助手的命令行。单独抽出来是为了**能被测试替换**成假助手
    （`tests/test_players_helper.py` 用它验证 socket/令牌/fd 传递，不需要 root）。

    两种形态：
      * 源码：`pkexec <python> scripts/vlt_priv_helper.py …`
      * 冻结（AppImage）：`pkexec <主程序> --priv-helper …` —— 不要求宿主机装 Python
    """
    if FROZEN:
        # AppImage：**不能**执行挂载点里的东西（FUSE 没开 allow_root，root 读不到，
        # pkexec 会在弹密码前就 EACCES）。改成把助手脚本经 stdin 喂给系统 python：
        # root 只执行 /usr/bin/python3，脚本内容来自本进程，不落盘、无需安装。
        python = payload_mod.system_python()
        if python is None:
            raise RuntimeError(
                "AppImage 里的分玩家模式需要宿主有 python3（助手是 Python 脚本）。\n"
                "  Arch: sudo pacman -S python；其它发行版装 python3 即可。")
        head = [python, "-"]
    else:
        head = [sys.executable, str(HELPER)]
    return [
        "pkexec", *head,
        "--socket", str(session.sock_path),
        "--token", session.token,
        "--pid", str(session.host.pid),
        "--starttime", str(session.host.starttime),
        "--port", str(session.port),
        # 助手要活到"取样 + 两种 needle 扫描 + 解码阶段"结束；给足再退出，
        # 免得解码跑到一半助手已经走了（虽然那时也不再需要它）。
        "--max-lifetime", str(session.lifetime_s),
    ]


class HelperSession:
    """与提权助手的会话：拿 packet socket 的 fd，按需请求扫内存。"""

    def __init__(self, host: procfs.ProcInfo, *, port: int = 27002,
                 lifetime_s: float = 300.0) -> None:
        self.host = host
        self.port = port
        self.dir: Path | None = None
        self.sock_path: Path | None = None
        self.token = secrets.token_hex(16)
        self.proc: subprocess.Popen | None = None
        self.conn: socket.socket | None = None
        self.packet_socket: socket.socket | None = None
        self.scans = 0
        # 助手存活上限：按「预采样 + 两种 needle 扫描 + 解码」估出来再传给 pkexec，
        # 免得解码跑到一半助手已经超时退出（虽然那时也不再需要它）。
        self.lifetime_s = max(180.0, float(lifetime_s))

    # ---------------------------------------------------------- 生命周期

    def __enter__(self) -> "HelperSession":
        base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        self.dir = Path(tempfile.mkdtemp(prefix="vlt-players-", dir=base))
        self.sock_path = self.dir / "helper.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.sock_path))
        server.listen(1)

        argv = helper_command(self)
        stdin_data = helper_payload()
        print("[p0b] 拉起提权助手（会弹一次密码）…", flush=True)
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE if stdin_data else None)
            if stdin_data and self.proc.stdin is not None:
                # 脚本约 60KB，可能超过管道缓冲：放到后台线程写，别卡住等 accept。
                import threading
                threading.Thread(target=_feed_stdin,
                                 args=(self.proc.stdin, stdin_data),
                                 daemon=True, name="vlt-helper-stdin").start()
        except FileNotFoundError as exc:
            server.close()
            self.__exit__()
            raise RuntimeError("找不到 pkexec（polkit 未安装？）") from exc

        try:
            # 不要用「一个 120s 的 accept 超时」：用户点取消时 pkexec 会立刻退出，
            # 那样会白等两分钟。这里每 0.5s 看一眼子进程还在不在。
            deadline = time.monotonic() + 120.0
            conn = None
            while time.monotonic() < deadline:
                server.settimeout(0.5)
                try:
                    conn, _addr = server.accept()
                    break
                except socket.timeout:
                    if self.proc.poll() is not None:
                        raise RuntimeError(
                            f"提权被取消或失败（pkexec 退出码 {self.proc.returncode}）")
            if conn is None:
                raise RuntimeError("等助手连接超时（密码没输入？）")
            self.conn = conn
            hello, _fds = _recv_json(conn, want_fd=True, timeout=30.0)
            if hello.get("token") != self.token:
                raise RuntimeError("助手握手令牌不符（可能有别的进程在抢这个 socket）")
            return self
        except Exception:
            # 失败路径必须**自己收尸**：否则会留下一个还在跑的 root 助手
            # （它要空转到 --max-lifetime 才退）和一个临时目录。
            self.__exit__()
            raise
        finally:
            server.close()

    def __exit__(self, *exc_info) -> None:
        try:
            if self.conn is not None:
                self.send({"cmd": "bye"})
                # 等一句 ack 再关：直接关会让助手在写回复时撞 EPIPE（实测日志很难看），
                # 也让"助手是否正常退出"这件事变得无法观察。
                _recv_json(self.conn, timeout=2.0)
        except Exception:                            # noqa: BLE001 - 收尾不抛
            pass
        for closable in (self.packet_socket, self.conn):
            try:
                if closable is not None:
                    closable.close()
            except Exception:                        # noqa: BLE001
                pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.sock_path is not None:
            try:
                self.sock_path.unlink()
            except OSError:
                pass
        if self.dir is not None:
            try:
                self.dir.rmdir()
            except OSError:
                pass

    # ---------------------------------------------------------- 协议

    def send(self, payload: dict) -> None:
        assert self.conn is not None
        self.conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))

    def start_capture(self, *, filter_mode: str = "strict") -> None:
        """开（或重开）一个抓包 socket。`filter_mode` = strict / loose / none。

        重开会先关掉旧的那份 —— 诊断路径要换一个"不过滤"的 socket，
        而内核过滤器挂上去就摘不掉。
        """
        if self.packet_socket is not None:
            try:
                self.packet_socket.close()
            except OSError:
                pass
            self.packet_socket = None
        self.send({"cmd": "start", "filter": filter_mode})
        reply, fds = _recv_json(self.conn, want_fd=True, timeout=30.0)
        if not reply.get("ok"):
            raise RuntimeError(f"助手开抓包失败：{reply.get('error')}")
        if not fds:
            raise RuntimeError("助手没有交回抓包 fd")
        self.packet_socket = socket.socket(fileno=fds[0])
        self.packet_socket.setblocking(False)

    def scan(self, *, deadline_s: float = 30.0, needle: str = "q") -> dict:
        self.scans += 1
        self.send({"cmd": "scan", "deadline_s": deadline_s, "needle": needle})
        reply, _fds = _recv_json(self.conn, timeout=deadline_s + 30.0)
        return reply

    def refresh(self) -> None:
        self.send({"cmd": "refresh"})
        _recv_json(self.conn, timeout=15.0)


def _feed_stdin(pipe, data: bytes) -> None:
    try:
        pipe.write(data)
        pipe.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _recv_json(conn: socket.socket | None, *, want_fd: bool = False,
               timeout: float = 30.0) -> tuple[dict, list[int]]:
    """读一条 JSON 行；`want_fd=True` 时顺带收 `SCM_RIGHTS` 传来的 fd。"""
    if conn is None:
        raise RuntimeError("与助手的连接已断开")
    conn.settimeout(timeout)
    data = b""
    fds: list[int] = []
    while not data.endswith(b"\n"):
        try:
            if want_fd:
                chunk, ancdata, _flags, _addr = conn.recvmsg(65536, socket.CMSG_SPACE(4 * 4))
            else:
                chunk = conn.recv(65536)
                ancdata = []
        except socket.timeout as exc:
            raise RuntimeError("等助手回复超时") from exc
        if not chunk:
            raise RuntimeError("助手已退出（连接关闭）")
        data += chunk
        for level, ctype, cdata in ancdata:
            if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
                count = len(cdata) // 4
                fds.extend(array.array("i", cdata[:count * 4]))
        if len(data) > 4 << 20:
            raise RuntimeError("助手回复异常（超长）")
    try:
        return json.loads(data.decode("utf-8", "replace")), fds
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"助手回复不是 JSON：{data[:200]!r}") from exc



# ---------------------------------------------------------------- 一次性助手
#
# 预热要在**没有翻译**的时候也抓包：socket 需要 root 来开，但助手没必要常驻 ——
# 下面两个函数各起一个**短命**助手，干完就退（与现有承诺一致）：
#   * `open_capture_once`：只开 socket，把 fd 交回主进程后助手退出；
#   * `scan_once`：只扫内存，把候选交回后助手退出。
# socket 的 fd 已经在本进程里，助手退出不影响继续收包；游戏重启/换密钥时再起一次即可。


def open_capture_once(host: procfs.ProcInfo, *, port: int = 27002,
                      filter_mode: str = "strict") -> socket.socket:
    """起一个短命助手，只做「开抓包 socket」；返回的 socket 归调用方所有。"""
    with HelperSession(host, port=port, lifetime_s=60.0) as sess:
        sess.start_capture(filter_mode=filter_mode)
        sock = sess.packet_socket
        if sock is None:
            raise RuntimeError("助手没有交回抓包 fd")
        # 交给调用方：`__exit__` 不许把它关掉（fd 是同一个内核对象）
        sess.packet_socket = None
        return sock


def scan_once(host: procfs.ProcInfo, *, needle: str = "q",
              deadline_s: float = 40.0, port: int = 27002) -> dict:
    """起一个短命助手，只做一次「扫内存找候选密钥」；返回助手原始回包。"""
    with HelperSession(host, port=port, lifetime_s=max(60.0, deadline_s + 30.0)) as sess:
        return sess.scan(deadline_s=deadline_s, needle=needle)
