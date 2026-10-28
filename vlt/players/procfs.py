"""`/proc` 的只读视图：进程发现、映射统计、EAC 姿态线索。

## 为什么单独一层

分玩家模式需要知道三件事，全部来自 `/proc` 的**元数据**（不碰内存）：

1. **VRChat 的宿主进程是哪个** —— Proton/Wine 下游戏进程的映射里有
   `GameAssembly.dll`（IL2CPP 的托管代码都在它和匿名 rw 堆里）；
2. **EAC 是什么形态** —— `EasyAntiCheat_EOS.exe` 是前缀里的 Windows PE
   （随游戏一起跑），`easyanticheat_x64.so` 则是注入型的用户态模块；
3. **游戏进程有没有把自己设成不可读** —— `/proc/<pid>/status` 的 `Dumpable`
   与 `TracerPid`。这两个字段决定了「提权助手有没有必要」，也是 EAC 姿态的
   直接证据（`TracerPid != 0` 说明有人 attach 着，我们**绝不**制造这种状态）。

## 纪律

- **只读**：本模块只有 `open(..., "r")` 与 `os.listdir`，没有任何写操作。
- 不用 `psutil`：那是额外依赖，而且我们要的字段（`Dumpable`/`TracerPid`/
  匿名映射占比）它并不直接给。
- 解析函数与 IO 函数**分开**（`parse_maps_line` / `parse_stat` / `parse_status`），
  这样测试可以纯离线喂字符串，不需要真有 VRChat 在跑。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

PROC = Path("/proc")

# 判定「这是不是 VRChat 宿主」的标记（小写比较）。
#
# ⚠️ **不许**放宽成裸的 `"vrchat"`：本项目目录就叫 `vrchat-livetranslate`，
# 沙盒/终端进程的命令行里带着这个路径，会被误判成游戏（实测把 bwrap 和 zsh
# 选成了"宿主"）。只认可执行名与 IL2CPP 模块名这类**不可混淆**的标记。
GAME_MARKERS = ("gameassembly.dll", "vrchat.exe", "start_protected_game")
# 判定「这是不是 EAC」的标记（进程命令行 / 映射路径都用它）。
EAC_MARKERS = ("easyanticheat", "easyanticheat_eos")


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class Region:
    """一条 `/proc/<pid>/maps` 记录。"""

    start: int
    end: int
    perms: str
    pathname: str

    @property
    def size(self) -> int:
        return max(0, self.end - self.start)

    @property
    def is_scan_candidate(self) -> bool:
        """能不能拿来扫密钥：**可写**、够大、且不是设备/内核伪映射。

        为什么不再只认「匿名 rw」：托管堆可能落在 `[heap]`（brk）或匿名共享映射上，
        P0-b 实测就出现过「只扫匿名 rw 时覆盖不到一半内存」的情况。凡是可写的
        用户态映射都值得扫 —— 读的代价是内存带宽，不是正确性。
        """
        if "w" not in self.perms or self.size < 64 << 10:
            return False
        name = self.pathname
        if name.startswith(("/dev/", "/sys/", "/proc/")):
            return False
        if name.startswith(("[vvar", "[vdso", "[vsyscall")):
            return False
        return True

    @property
    def is_anon_rw(self) -> bool:
        """是否「匿名可读写」——IL2CPP/托管堆的候选面。

        只认**没有路径**的映射，以及内核在 `mmap` 时起了名字的 `[anon:...]`。
        `[heap]` / `[stack]` 单独排除：前者是 brk 堆（托管堆不走它），后者是栈。
        """
        if len(self.perms) < 2 or self.perms[0] != "r" or self.perms[1] != "w":
            return False
        name = self.pathname
        if not name:
            return True
        return name.startswith("[anon")


@dataclass(frozen=True)
class MapSummary:
    """一次 `maps` 扫描的汇总（只留判定要用的数字，不留内容）。"""

    region_count: int = 0
    total_bytes: int = 0
    anon_rw_bytes: int = 0
    has_gameassembly: bool = False
    has_vrchat_exe: bool = False
    has_eac: bool = False


@dataclass(frozen=True)
class ProcInfo:
    """一个候选进程的全部只读元数据。"""

    pid: int
    comm: str
    cmdline: tuple[str, ...]
    starttime: int
    uid: int | None
    summary: MapSummary
    dumpable: str | None = None
    tracer_pid: int | None = None
    maps_ok: bool = True
    error: str = ""


# ---------------------------------------------------------------- 纯解析


def parse_maps_line(line: str) -> Region | None:
    """解析一条 maps 记录。格式不合法返回 None（不许抛，扫描要能跳过怪行）。

    路径里可能有空格（`/mnt/game/.../My Game/x.dll`），所以只 split 5 次。
    """
    parts = line.rstrip("\n").split(None, 5)
    if len(parts) < 5:
        return None
    span, perms = parts[0], parts[1]
    if "-" not in span:
        return None
    lo, _, hi = span.partition("-")
    try:
        start, end = int(lo, 16), int(hi, 16)
    except ValueError:
        return None
    name = parts[5].strip() if len(parts) >= 6 else ""
    if name.endswith(" (deleted)"):
        name = name[: -len(" (deleted)")]
    return Region(start=start, end=end, perms=perms, pathname=name)


def summarize(regions: Iterable[Region]) -> MapSummary:
    """把映射列表压成判定用的几个数。"""
    count = total = anon_rw = 0
    ga = vr = eac = False
    for r in regions:
        count += 1
        total += r.size
        if r.is_anon_rw:
            anon_rw += r.size
        low = r.pathname.lower()
        if "gameassembly.dll" in low:
            ga = True
        if "vrchat.exe" in low:
            vr = True
        if any(m in low for m in EAC_MARKERS):
            eac = True
    return MapSummary(region_count=count, total_bytes=total, anon_rw_bytes=anon_rw,
                      has_gameassembly=ga, has_vrchat_exe=vr, has_eac=eac)


def parse_stat(text: str) -> tuple[str, int]:
    """`/proc/<pid>/stat` → (comm, starttime)。

    `comm` 里可能有空格和右括号（Wine 进程尤其常见：`VRChat.exe` 之外还有带
    括号的参数），所以按**最后一个** `)` 切开，不能 `split()`。
    `starttime` 是第 22 字段（切掉前两个后的第 20 个），用于防 PID 复用。
    """
    rp = text.rfind(")")
    if rp < 0:
        return "", 0
    comm = text[text.find("(") + 1: rp]
    rest = text[rp + 1:].split()
    if len(rest) < 20:
        return comm, 0
    try:
        return comm, int(rest[19])
    except ValueError:
        return comm, 0


def parse_status(text: str) -> dict[str, str]:
    """`/proc/<pid>/status` → {键: 值}（只取第一段空白前的值）。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        out[key.strip()] = val.strip()
    return out


def status_fields(status: dict[str, str]) -> tuple[str | None, int | None, int | None]:
    """从 status 字典里取 (Dumpable, TracerPid, uid)。缺失就是 None，不猜。"""
    dumpable = status.get("Dumpable")
    tracer: int | None = None
    raw_tracer = status.get("TracerPid")
    if raw_tracer is not None:
        try:
            tracer = int(raw_tracer)
        except ValueError:
            tracer = None
    uid: int | None = None
    raw_uid = status.get("Uid")
    if raw_uid:
        try:
            uid = int(raw_uid.split()[0])
        except (ValueError, IndexError):
            uid = None
    return dumpable, tracer, uid


# ---------------------------------------------------------------- IO（薄壳）


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def iter_pids(root: Path | None = None) -> Iterator[int]:
    base = root or PROC
    try:
        entries = os.listdir(base)
    except OSError:
        return
    for name in entries:
        if name.isdigit():
            yield int(name)


def read_cmdline(pid: int, root: Path | None = None) -> tuple[str, ...]:
    base = root or PROC
    raw = None
    try:
        raw = (base / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ()
    text = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    return tuple(text.split()) if text else ()


def read_maps(pid: int, root: Path | None = None) -> tuple[list[Region], str]:
    """读 maps。返回 (regions, 错误说明)；读不到时 regions 为空、错误非空。"""
    base = root or PROC
    path = base / str(pid) / "maps"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"{type(exc).__name__}: {exc.strerror or exc}"
    regions: list[Region] = []
    for line in text.splitlines():
        r = parse_maps_line(line)
        if r is not None:
            regions.append(r)
    return regions, ""


def collect(pid: int, *, root: Path | None = None, with_maps: bool = True) -> ProcInfo | None:
    """采集一个进程的元数据；进程已退出返回 None（扫描期间进程消失是常态）。"""
    base = root or PROC
    stat_text = _read_text(base / str(pid) / "stat")
    if stat_text is None:
        return None
    comm, starttime = parse_stat(stat_text)
    status = parse_status(_read_text(base / str(pid) / "status") or "")
    dumpable, tracer, uid = status_fields(status)
    cmdline = read_cmdline(pid, base)
    regions: list[Region] = []
    maps_error = ""
    if with_maps:
        regions, maps_error = read_maps(pid, base)
    return ProcInfo(
        pid=pid,
        comm=comm,
        cmdline=cmdline,
        starttime=starttime,
        uid=uid,
        summary=summarize(regions),
        dumpable=dumpable,
        tracer_pid=tracer,
        maps_ok=not maps_error,
        error=maps_error,
    )


def looks_like(proc: ProcInfo, markers: tuple[str, ...]) -> bool:
    """命令行或 comm 里出现标记（小写比较）。"""
    haystack = " ".join((proc.comm, *proc.cmdline)).lower()
    return any(m in haystack for m in markers)


def discover(markers: tuple[str, ...], *, root: Path | None = None,
             limit: int = 400) -> list[ProcInfo]:
    """扫全系统的进程，返回命令行/comm 命中标记的那几个。

    先读 `cmdline`（很小、很快）做粗筛，命中后才解析 `maps`（可能几 MB）——
    否则每跑一次探针都要把几百个进程的映射全读一遍。
    """
    found: list[ProcInfo] = []
    for pid in iter_pids(root):
        if len(found) >= limit:
            break
        cmdline = read_cmdline(pid, root)
        # 先用便宜的信息判断，避免为无关进程读 maps
        haystack = " ".join(cmdline).lower()
        stat_text = _read_text((root or PROC) / str(pid) / "stat")
        if stat_text is None:
            continue
        comm, starttime = parse_stat(stat_text)
        if not any(m in haystack or m in comm.lower() for m in markers):
            continue
        status = parse_status(_read_text((root or PROC) / str(pid) / "status") or "")
        dumpable, tracer, uid = status_fields(status)
        regions, maps_error = read_maps(pid, root)
        found.append(ProcInfo(
            pid=pid, comm=comm, cmdline=cmdline, starttime=starttime, uid=uid,
            summary=summarize(regions), dumpable=dumpable, tracer_pid=tracer,
            maps_ok=not maps_error, error=maps_error,
        ))
    return found


def find_vrchat(*, root: Path | None = None) -> list[ProcInfo]:
    """所有看起来像 VRChat 的进程（游戏本体 + 受保护启动器 + EAC 引导）。"""
    return discover(GAME_MARKERS, root=root)


def find_eac(*, root: Path | None = None) -> list[ProcInfo]:
    """所有看起来像 EAC 的进程。"""
    return discover(EAC_MARKERS, root=root)


# ---------------------------------------------------------------- UDP 端点
#
# 为什么要知道「游戏自己绑了哪些本地端口」：抓包 socket 是按「端口 27002」过滤的，
# 但那只能说明方向，不能说明**这是谁的流量**。同一台机器上任何进程连到 27002 都会
# 混进来；反过来，如果分辨不出方向（本机恰好绑到 27002、或者流量走加速器），原实现
# 会把包归错方向 —— 归错方向的后果是「自己的声音被当成别人的」。
#
# `/proc/<pid>/fd` 里的 `socket:[inode]` 与 `/proc/net/udp` 的 inode 是同一个数字，
# 所以这套（只读）对应关系能精确回答「这个五元组属不属于游戏进程」。


@dataclass(frozen=True)
class UdpEndpoint:
    """一个 UDP socket 的端点（`/proc/net/udp` 的一行 + 进程归属）。"""

    local_addr: str
    local_port: int
    remote_addr: str | None
    remote_port: int | None
    inode: int

    @property
    def connected(self) -> bool:
        return self.remote_port is not None


def _hex_ip(text: str) -> str:
    """`/proc/net/*` 的地址是**小端**十六进制（`0100007F` → 127.0.0.1）。"""
    raw = bytes.fromhex(text)
    if len(raw) != 4:
        raise ValueError(f"不是 IPv4 地址：{text!r}")
    return ".".join(str(b) for b in reversed(raw))


def parse_udp_line(line: str) -> tuple[str, int, str | None, int | None, int] | None:
    """`/proc/net/udp` 的一行 → (本地地址, 本地端口, 远端地址, 远端端口, inode)。"""
    fields = line.split()
    if len(fields) < 10 or not fields[0].endswith(":"):
        return None
    try:
        local_ip, local_port = fields[1].split(":")
        remote_ip, remote_port = fields[2].split(":")
        inode = int(fields[9])
        local_addr = _hex_ip(local_ip)
        local = int(local_port, 16)
    except (ValueError, IndexError):
        return None
    if remote_port == "0000" or remote_ip == "00000000":
        return local_addr, local, None, None, inode
    try:
        remote_addr = _hex_ip(remote_ip)
        remote = int(remote_port, 16)
    except (ValueError, IndexError):
        return None
    return local_addr, local, remote_addr, remote, inode


def socket_inodes(pid: int, *, root: Path | None = None) -> set[int]:
    """进程持有的 socket inode 集合（`fd` 目录里的 `socket:[N]` 符号链接）。"""
    base = (root or PROC) / str(pid) / "fd"
    inodes: set[int] = set()
    try:
        names = os.listdir(base)
    except OSError:
        return inodes
    for name in names:
        try:
            target = os.readlink(base / name)
        except OSError:
            continue
        if not target.startswith("socket:["):
            continue
        try:
            inodes.add(int(target[8:-1]))
        except ValueError:
            continue
    return inodes


def udp_endpoints(pid: int, *, root: Path | None = None) -> list[UdpEndpoint]:
    """游戏进程的 UDP 端点（IPv4）。

    `/proc/net/udp` 是全系统一张表，必须靠 inode 与进程的 fd 对上 —— 只按端口猜
    「哪个是 VRChat 的」会把同机别的进程也算进来。
    """
    inodes = socket_inodes(pid, root=root)
    if not inodes:
        return []
    table = (root or PROC) / "net" / "udp"
    try:
        text = table.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[UdpEndpoint] = []
    for line in text.splitlines()[1:]:
        parsed = parse_udp_line(line)
        if parsed is None:
            continue
        local_ip, local_port, remote_ip, remote_port, inode = parsed
        if inode not in inodes:
            continue
        out.append(UdpEndpoint(local_addr=local_ip, local_port=local_port,
                               remote_addr=remote_ip, remote_port=remote_port,
                               inode=inode))
    return out


def udp_ports(pid: int, *, root: Path | None = None) -> set[int]:
    """游戏进程绑定的本地 UDP 端口集合（方向判定用）。"""
    return {e.local_port for e in udp_endpoints(pid, root=root)}


def pick_host(procs: Iterable[ProcInfo]) -> ProcInfo | None:
    """挑出「托管堆就在这里面」的那个进程。

    排序键（从强到弱）：有 `GameAssembly.dll` 映射 > 匿名 rw 更大 > 总映射更大。
    VRChat 的启动器/崩溃上报进程没有 GameAssembly，匿名 rw 也小得多，会被排到后面。
    """
    candidates = [p for p in procs if p.maps_ok]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.summary.has_gameassembly,
                                          p.summary.anon_rw_bytes,
                                          p.summary.total_bytes))


def human_bytes(n: int) -> str:
    """给探针输出用的可读单位（1024 进制，保留一位小数）。"""
    value = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
