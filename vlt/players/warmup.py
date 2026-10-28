"""预热：**没开始翻译**时也抓包、取钥、解析玩家名。

## 为什么需要

名字（Event 42/253/255 的 `actor → usr_id → displayName`）只在「加入 / 属性变更」
时广播一次。以前抓包要等用户点「开始翻译」—— 那时可能已经错过整张名单，只能靠
`output_log` 兜底（没有 actor 号）。

预热把「抓包」从翻译腿里独立出来：程序发现 VRChat 进程后就开 socket、取一次钥，
之后**持续解密并只解析名字**（不解码 opus、不建任何翻译会话，CPU 接近零）。
点开始翻译时直接复用 socket + 密钥 + 名字表，既不用重新授权，也不用再扫 11 秒内存。

## 权限与生命周期（与现有承诺一致）

* 开 socket、扫内存各起一个**短命**提权助手（`helper.open_capture_once` /
  `helper.scan_once`）：干完就退，主程序只拿到 fd / 候选，永远拿不到原始内存；
* socket 的 fd 留在主进程里，助手退出后照样继续收包；停止翻译也不关，
  所以「停止后继续记名字」不需要再授权；
* 游戏重启（pid/starttime 变了）→ 旧密钥失效 → 自动重新采样 + 重扫。

## 读者只有一个

预热线程与翻译腿都通过 `drain()` 取帧（一把锁 + `_leased` 标志）。腿借用期间
预热线程**不读** socket，避免两个读者抢包。

## 密钥失效判定

连续 `MAX_DECRYPT_FAILURES` 个加密包都解不开 → 认为游戏重启/换号，清掉密钥并重扫
（名字表保留：新会话里 actor 会重新广播，旧的按 actor 复用规则会被覆盖）。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from . import capture_linux, gcm, guard, names, pending, photon, procfs
from ..players_settings import PREWARM_AUTO, PlayersSettings

PHASE_OFF = "off"
PHASE_WAITING = "waiting_game"          # 没有 VRChat 进程
PHASE_BLOCKED = "blocked"               # EAC 闸门拒绝
PHASE_UNAUTHORIZED = "unauthorized"     # pkexec 被拒 / 助手起不来
PHASE_CAPTURING = "capturing"           # 已开 socket，还没拿到密钥
PHASE_RECORDING = "recording"           # 有密钥，正在记名字
PHASE_FAILED = "failed"                 # 取钥失败（重试也失败）

SAMPLE_TARGET = 40
MAX_DECRYPT_FAILURES = 10
VOICE_REPLAY_S = 5.0
POLL_S = 2.0
DRAIN_S = 0.25
LOCAL_PORTS_REFRESH_S = 5.0


class WarmupLease:
    """翻译腿向预热器「借用」抓包/密钥/名字表的一张小票。"""

    def __init__(self, warmup: "PlayerWarmup") -> None:
        self._warmup = warmup

    def drain(self, timeout: float = 0.2) -> list[tuple[float, bytes]]:
        return self._warmup.drain(timeout)

    def ensure_key(self, samples, *, deadline_s: float = 40.0):
        return self._warmup.ensure_key(samples, deadline_s=deadline_s)

    @property
    def key(self):
        return self._warmup.key

    @property
    def host(self):
        return self._warmup.host

    def names_copy(self) -> names.NameTable:
        return self._warmup.names_copy()

    def status(self) -> dict:
        return self._warmup.status()

    def recent_cipher(self, seconds: float = VOICE_REPLAY_S) -> list[capture_linux.Datagram]:
        return self._warmup.recent_cipher(seconds)

    def release(self) -> None:
        self._warmup.release()


class PlayerWarmup:
    """常驻预热器（App 级）。`start()` 起线程，`close()` 收摊。"""

    def __init__(self, settings: PlayersSettings, *, port: int = 27002,
                 on_status: Callable[[str, str], None] | None = None,
                 find_host: Callable[[], procfs.ProcInfo | None] | None = None,
                 open_capture: Callable[[procfs.ProcInfo], object] | None = None,
                 scan: Callable[[procfs.ProcInfo, str], dict] | None = None,
                 guard_check: Callable[[], object] | None = None,
                 drain_fn: Callable[..., list[tuple[float, bytes]]] | None = None,
                 poll_s: float = POLL_S) -> None:
        self.settings = settings
        self._port = port
        self._on_status = on_status or (lambda *_a: None)
        self._find_host = find_host or (lambda: procfs.pick_host(procfs.find_vrchat()))
        self._open_capture = open_capture or self._default_open
        self._scan = scan or self._default_scan
        self._guard = guard_check or (lambda: guard.assess(guard.collect()))
        self._drain_fn = drain_fn or capture_linux.drain
        self._poll_s = max(0.02, float(poll_s))

        self._lock = threading.RLock()          # socket 读 + 租借状态
        self._names_lock = threading.Lock()
        self._key_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._socket: object | None = None
        self._host: procfs.ProcInfo | None = None
        self._local_ports: set[int] | None = None
        self._ports_at = 0.0
        self._leased = False
        self._auth_failed = False

        self._key: gcm.KeyHit | None = None
        self._opener = None
        self._samples: list[tuple[gcm.Flow, bytes]] = []
        self._fail_streak = 0
        self._next_scan = 0.0
        # 拿到密钥之前收到的包：装好密钥后**补解一遍**（加入世界那批名单就在里面）
        self._pre_key: list[capture_linux.Datagram] = []
        self._retry_pending = False

        self._names = names.NameTable()
        self._frag: dict[str, photon.Fragmenter] = {
            "in": photon.Fragmenter(), "out": photon.Fragmenter()}
        self._world = ""
        self._buffer = pending.PreKeyBuffer(
            max_bytes=max(1, int(settings.prewarm_buffer_mb)) << 20, world_ready=True)

        self._phase = PHASE_OFF
        self._detail = ""
        # 统计（状态页/日志用；名字本身不打印）
        self.name_events = 0
        self.packets = 0
        self.decrypted = 0
        self.rescans = 0

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self.settings.prewarm != PREWARM_AUTO:
            self._set_phase(PHASE_OFF, "预热未启用")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="vlt-players-warmup")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        with self._lock:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._set_phase(PHASE_OFF, "预热已停止")

    def reconfigure(self, settings: PlayersSettings) -> None:
        """配置变了（设置页保存）：按需启停，不必重启程序。"""
        was_on = self.settings.prewarm == PREWARM_AUTO
        self.settings = settings
        if settings.prewarm != PREWARM_AUTO:
            if was_on:
                self.close()
            return
        if not was_on or self._thread is None or not self._thread.is_alive():
            self.start()

    def retry(self) -> None:
        """用户点「重新授权」：清掉失败/拒绝状态，由**预热线程**重开（不跨线程动 socket）。"""
        self._auth_failed = False
        self._retry_pending = True

    # ------------------------------------------------------------ 对外查询

    @property
    def key(self) -> gcm.KeyHit | None:
        return self._key

    @property
    def host(self) -> procfs.ProcInfo | None:
        return self._host

    def available(self) -> bool:
        with self._lock:
            return self._socket is not None

    def names_copy(self) -> names.NameTable:
        with self._names_lock:
            return self._names.copy()

    def status(self) -> dict:
        return {
            "phase": self._phase,
            "detail": self._detail,
            "players": len(self._names),
            "name_events": self.name_events,
            "packets": self.packets,
            "decrypted": self.decrypted,
            "rescans": self.rescans,
            "key": self._key.fingerprint if self._key else "",
            "host_pid": self._host.pid if self._host else 0,
        }

    # ------------------------------------------------------------ 租借（给翻译腿）

    def lease(self) -> WarmupLease | None:
        with self._lock:
            if self._socket is None or self._leased:
                return None
            self._leased = True
        return WarmupLease(self)

    def release(self) -> None:
        with self._lock:
            self._leased = False

    def drain(self, timeout: float = 0.2) -> list[tuple[float, bytes]]:
        with self._lock:
            sock = self._socket
            if sock is None:
                return []
            try:
                return self._drain_fn(sock, timeout=timeout)
            except OSError:
                return []

    def recent_cipher(self, seconds: float = VOICE_REPLAY_S) -> list[capture_linux.Datagram]:
        """最近几秒的密文（给翻译腿回放语音；名字早已解析进表）。"""
        since = time.time() - max(0.0, seconds)
        return self._buffer.snapshot_recent(since)

    # ------------------------------------------------------------ 密钥

    def ensure_key(self, samples=None, *, deadline_s: float = 40.0):
        """有缓存就用缓存；否则**起短命助手扫一次**。失败返回 None。"""
        with self._key_lock:
            if self._key is not None:
                return self._key
            host = self._host
            usable = list(samples or [])
            if len(usable) < 2:
                # 翻译腿刚接管时可能只有 1 个样本：用预热自己攒的
                usable = list(self._samples)
            if host is None or len(usable) < 2:
                return None
            self._set_phase(PHASE_CAPTURING, "正在取密钥（扫描游戏内存）…")
            for needle in ("q", "i"):
                if self._stop.is_set():
                    return None
                try:
                    reply = self._scan(host, needle)
                except Exception as exc:                 # noqa: BLE001 - 授权/助手问题
                    self._auth_failed = True
                    self._samples.clear()
                    self._set_phase(PHASE_UNAUTHORIZED, f"授权失败：{exc}")
                    return None
                if not reply.get("ok"):
                    if reply.get("blocked"):
                        self._set_phase(PHASE_FAILED, f"读内存被拒绝：{reply.get('error')}")
                        return None
                    print(f"[warmup] ⚠️ 扫描失败（needle={needle}）：{reply.get('error')}",
                          flush=True)
                    continue
                candidates = [bytes.fromhex(h) for h in reply.get("candidates", [])]
                hit = gcm.validate_candidates(candidates, usable)
                if hit is not None:
                    self._install_key(hit)
                    return hit
            self._set_phase(PHASE_FAILED, "没找到能通过 GCM 认证的密钥")
            return None

    def _install_key(self, hit: gcm.KeyHit) -> None:
        self._key = hit
        self._opener = gcm.opener(hit.key, hit.layout)
        self._fail_streak = 0
        # 把「拿到密钥之前」的包补解一遍：加入世界时的 253/255 名单就在这里，
        # 不补的话预热会永远错过整张名单（实测就是这个现象）。
        pending_frames, self._pre_key = self._pre_key, []
        for dg in pending_frames:
            self._decrypt_into(dg, count_failure=False)
        with self._names_lock:
            count = len(self._names)
        print(f"[warmup] 密钥就绪（指纹 {hit.fingerprint}），开始记录玩家名"
              f"（已识别 {count} 位）", flush=True)
        self._set_phase(PHASE_RECORDING, "")

    # ------------------------------------------------------------ 线程主体

    def _run(self) -> None:
        while not self._stop.is_set():
            host = self._find_host()
            if host is None:
                self._drop_capture()
                if self._host is not None:
                    self._host = None
                    self._set_phase(PHASE_WAITING, "等待 VRChat 进程…")
                elif self._phase == PHASE_OFF:
                    self._set_phase(PHASE_WAITING, "等待 VRChat 进程…")
                self._stop.wait(self._poll_s)
                continue
            if self._retry_pending:
                self._retry_pending = False
                self._select_new_host(host, force=True)
            elif self._host is None or (host.pid, host.starttime) != (
                    self._host.pid, self._host.starttime):
                self._select_new_host(host)
            if self._socket is None:
                # 授权失败：不反复弹密码，等用户点「重新授权」或游戏重启
                self._stop.wait(min(self._poll_s, 1.0))
                continue
            self._refresh_local_ports()
            with self._lock:
                leased = self._leased
            if leased:
                self._stop.wait(0.05)
                continue
            frames = self.drain(DRAIN_S)
            if frames:
                self._process(frames)
            if self._key is None and len(self._samples) >= 2 and time.monotonic() >= self._next_scan:
                self.ensure_key()
                # 失败别立刻重扫：等一轮（10s），避免把助手/日志刷爆
                self._next_scan = time.monotonic() + 10.0

    def _select_new_host(self, host: procfs.ProcInfo | None = None, *,
                         force: bool = False) -> None:
        host = host or self._find_host()
        if host is None:
            return
        self._drop_capture()
        self._host = host
        self._names.clear()
        self._key = None
        self._opener = None
        self._samples.clear()
        self._fail_streak = 0
        self._pre_key.clear()
        self._world = ""
        self._frag = {"in": photon.Fragmenter(), "out": photon.Fragmenter()}
        self._buffer = pending.PreKeyBuffer(
            max_bytes=max(1, int(self.settings.prewarm_buffer_mb)) << 20, world_ready=True)
        self._next_scan = 0.0
        if self._auth_failed and not force:
            self._set_phase(PHASE_UNAUTHORIZED, "未授权（可点「重新授权」）")
            return
        verdict = self._guard()
        if getattr(verdict, "level", "") == "blocked":
            reasons = "；".join(getattr(verdict, "reasons", []) or [])
            self._set_phase(PHASE_BLOCKED, f"EAC 闸门拒绝：{reasons}")
            return
        try:
            sock = self._open_capture(host)
        except Exception as exc:                          # noqa: BLE001
            self._auth_failed = True
            self._set_phase(PHASE_UNAUTHORIZED, f"开抓包被拒：{exc}")
            return
        with self._lock:
            self._socket = sock
        self._auth_failed = False
        self._set_phase(PHASE_CAPTURING, "已开始抓包，等待加密流量…")

    def _drop_capture(self) -> None:
        with self._lock:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _refresh_local_ports(self) -> None:
        host = self._host
        now = time.monotonic()
        if host is None or now - self._ports_at < LOCAL_PORTS_REFRESH_S:
            return
        self._ports_at = now
        try:
            self._local_ports = procfs.udp_ports(host.pid) or None
        except OSError:
            self._local_ports = None

    def _process(self, frames: list[tuple[float, bytes]]) -> None:
        for ts, raw in frames:
            dg = capture_linux.parse_frame(raw, port=self._port,
                                           local_ports=self._local_ports)
            if dg is None or not dg.is_encrypted:
                continue
            self.packets += 1
            if len(self._samples) < SAMPLE_TARGET:
                self._samples.append((gcm.Flow.of(dg.src, dg.dst), dg.payload))
            self._buffer.add(dg)
            if self._key is None:
                if len(self._pre_key) < 4096:
                    self._pre_key.append(dg)
                continue
            self._decrypt_into(dg)

    def _decrypt_into(self, dg: capture_linux.Datagram, *, count_failure: bool = True) -> None:
        opener = self._opener
        plain = opener(dg.payload) if opener is not None else None
        if plain is None:
            if not count_failure:
                return
            self._fail_streak += 1
            if self._fail_streak >= MAX_DECRYPT_FAILURES:
                # 连续解不开 = 游戏重启/换号：清钥重扫（名字表保留）
                self.rescans += 1
                print(f"[warmup] ⚠️ 连续 {self._fail_streak} 个包解不开 → "
                      f"密钥失效，重新取钥（第 {self.rescans} 次）", flush=True)
                self._key = None
                self._opener = None
                self._samples.clear()
                self._fail_streak = 0
                self._next_scan = 0.0
                self._set_phase(PHASE_CAPTURING, "密钥失效，重新取钥")
            return
        self._fail_streak = 0
        self.decrypted += 1
        self._on_plaintext(dg, plain)

    def _on_plaintext(self, dg: capture_linux.Datagram, plain: bytes) -> None:
        commands, _err = photon.parse_commands(plain)
        fragmenter = self._frag[dg.direction]
        for cmd in commands:
            for payload in fragmenter.feed(cmd):
                self._on_message(payload)

    def _on_message(self, payload: bytes) -> None:
        msg, _skip = photon.parse_message_at(payload)
        if msg is None:
            return
        if msg.kind == photon.TYPE_EVENT and (
                msg.code in names.NAME_EVENT_CODES or msg.code == names.LEAVE_EVENT):
            parsed, _diag = ([], {})
            if msg.code != names.LEAVE_EVENT:
                parsed, _diag = names.parse_records_ex(msg.payload)
            with self._names_lock:
                self._names.feed(msg.payload, code=msg.code, records=parsed)
            self.name_events += 1
            return
        if msg.kind == photon.TYPE_OP_REQUEST and msg.code == photon.EVENT_JOIN_WORLD:
            world = photon.joined_world(payload)
            if world and world != self._world:
                if self._world:
                    with self._names_lock:
                        self._names.clear()
                    self._frag = {"in": photon.Fragmenter(), "out": photon.Fragmenter()}
                self._world = world

    # ------------------------------------------------------------ 状态

    def _set_phase(self, phase: str, detail: str) -> None:
        if phase == self._phase and detail == self._detail:
            return
        self._phase = phase
        self._detail = detail
        try:
            self._on_status(phase, detail)
        except Exception:                                 # noqa: BLE001 - 状态回调不该影响抓包
            pass

    # ------------------------------------------------------------ 默认实现

    def _default_open(self, host: procfs.ProcInfo):
        from . import helper
        return helper.open_capture_once(host, port=self._port)

    def _default_scan(self, host: procfs.ProcInfo, needle: str) -> dict:
        from . import helper
        return helper.scan_once(host, needle=needle, port=self._port)
