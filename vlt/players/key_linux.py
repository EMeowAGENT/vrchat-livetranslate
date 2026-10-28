"""从游戏进程里找 Photon 的 32 字节会话密钥（**只读**，`process_vm_readv`）。

## 为什么不读 `/proc/<pid>/mem`

两者在内核里的权限检查**完全一样**（`ptrace_may_access`），但可观测性不一样：

- 打开 `/proc/<pid>/mem` 会留下一个 **fd**，`/proc/<我们的 pid>/fd/N → /proc/<游戏>/mem`
  是同用户下**可枚举**的 —— 这是唯一会直接暴露"谁在读游戏内存"的东西；
- `process_vm_readv` 是一次系统调用，无 fd、无 `TracerPid`、不改目标内存、不注入。

所以这里**只用** `process_vm_readv`，且由一次性的提权助手执行（助手读完把候选
交出去就退出，主程序永远拿不到原始内存）。

## 怎么找

托管 `byte[]` 在 64 位 IL2CPP 里的布局是
`[klass 8][monitor 8][bounds 8][max_length 8][data…]`，`max_length` 是 8 字节小端。
所以「32 字节的数组」在内存里就是 `<u64 32>` 紧跟 32 字节 —— 用这条 needle 扫，
命中后用真实抓到的报文做 GCM 认证（密码学级别的确认，零误报）。

候选还要过一道**熵预筛**（≥20 个不同字节值）：GCM 认证一次要几微秒，而 needle
可能命中几万个窗口，先用几乎不要钱的判据削掉绝大多数。

## 读多大

实测 VRChat 的**可写**映射约 **8.3 GB**（P0-b 数据，随世界/人数变化）。按 4MB 分块
+ 8 字节重叠，实测约 **0.76 GB/s**（8.3 GB ≈ 11s）；找到就停。这里不做"只扫 GC 堆"
的猜测 —— 猜错就是找不到，而多读几 GB 的代价只是内存带宽（读的是页表，不改变
游戏状态）。

**不要设小候选上限**：P0-b 第一次跑就踩过 —— 上限 4096 让扫描在 2.75 GiB 处提前
结束，真钥在后半段，现象是"候选都不对"。上限只该用来防爆内存（默认 5 万）。
"""
from __future__ import annotations

import ctypes
import errno
import os
import struct
import time
from dataclasses import dataclass, field

from . import procfs
from .gcm import key_fingerprint

KEY_LEN = 32
MIN_CANDIDATE_DISTINCT = 20
# 两种长度字段布局（都是"长度在前、数据紧跟"）：
#   q —— 8 字节小端长度（IL2CPP 的 `max_length` 是 size_t）← 首选
#   i —— 4 字节小端长度（32 位风格/结构体内嵌）
# 只有 q 失败才会试 i；i 的 4 字节特征命中率高得多，靠更严的熵门槛压住误报。
NEEDLES = {
    "q": (struct.pack("<Q", KEY_LEN), 8, MIN_CANDIDATE_DISTINCT),
    "i": (struct.pack("<I", KEY_LEN), 4, 24),
}
CHUNK = 4 << 20          # 4MB/次：太小则系统调用占主导，太大则单次失败浪费
OVERLAP = KEY_LEN + 8    # 跨块命中不漏
SKIP_ON_FAULT = 64 << 10


class _Iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_vm_readv = _libc.process_vm_readv
_vm_readv.restype = ctypes.c_ssize_t
_vm_readv.argtypes = [ctypes.c_int, ctypes.POINTER(_Iovec), ctypes.c_ulong,
                      ctypes.POINTER(_Iovec), ctypes.c_ulong, ctypes.c_ulong]


class MemoryBlocked(PermissionError):
    """内核/EAC 拒绝了读内存（EPERM）。P0-b 必须把它和"没找到"区分开。"""


@dataclass
class ScanResult:
    candidates: list[bytes] = field(default_factory=list)
    scanned_bytes: int = 0
    elapsed_ms: float = 0.0
    needle_hits: int = 0
    regions: int = 0
    stopped: str = "done"        # done | deadline | full
    needle: str = "q"
    region_bytes: int = 0        # 计划扫描的总量（用来判断覆盖率）
    fingerprints: list[str] = field(default_factory=list)


def read_mem(pid: int, addr: int, size: int) -> bytes:
    """读一段远端内存，返回**实际读到**的字节（可能因坏页而短）。

    失败语义（刻意区分，别把两种情况糊成一种）：
      * EPERM/EACCES ⇒ `MemoryBlocked`（被拦，闸门应该已经先判过，说明判断错了）
      * ESRCH        ⇒ 进程没了，返回空串（调用方会自行收尾）
      * EFAULT/EIO   ⇒ 这一段不可读，返回空串，跳过
    """
    buf = ctypes.create_string_buffer(size)
    local = _Iovec(ctypes.cast(buf, ctypes.c_void_p), size)
    remote = _Iovec(ctypes.c_void_p(addr), size)
    n = _vm_readv(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    if n < 0:
        err = ctypes.get_errno()
        if err in (errno.EPERM, errno.EACCES):
            raise MemoryBlocked(err, os.strerror(err))
        if err in (errno.ESRCH, errno.EFAULT, errno.EIO, errno.ENOMEM):
            return b""
        raise OSError(err, os.strerror(err))
    try:
        return buf.raw[:n]
    finally:
        # 读到的是游戏内存（可能含任何东西）—— 用完立刻抹掉。
        ctypes.memset(buf, 0, size)


def _distinct(buf: bytes) -> int:
    return len(set(buf))


def _scan_buffer(data: bytes, out: list[bytes], seen: set[bytes], *,
                 needle: bytes, offset: int = 8,
                 min_distinct: int = MIN_CANDIDATE_DISTINCT) -> int:
    """在一块数据里找 needle + 32 字节。返回命中数。"""
    hits = 0
    start = 0
    while True:
        pos = data.find(needle, start)
        if pos < 0:
            return hits
        hits += 1
        start = pos + 1
        cand = data[pos + offset: pos + offset + KEY_LEN]
        if len(cand) != KEY_LEN or cand in seen:
            continue
        if _distinct(cand) < min_distinct:
            continue
        seen.add(cand)
        out.append(cand)


def scan_key(pid: int, *, deadline_s: float = 30.0, max_candidates: int = 50_000,
             needle: str = "q",
             regions: list[procfs.Region] | None = None,
             on_progress=None) -> ScanResult:
    """扫描目标进程的匿名可读映射，返回候选密钥列表（**不含**原始内存）。"""
    if regions is None:
        regions, _err = procfs.read_maps(pid)
    if needle not in NEEDLES:
        raise ValueError(f"未知 needle 变体 {needle!r}")
    needle_bytes, needle_offset, min_distinct = NEEDLES[needle]

    targets = [r for r in regions if r.is_scan_candidate]
    targets.sort(key=lambda r: r.size, reverse=True)

    result = ScanResult(regions=len(targets), region_bytes=sum(r.size for r in targets))
    seen: set[bytes] = set()
    t0 = time.monotonic()
    deadline = t0 + max(0.1, deadline_s)

    for region in targets:
        if len(result.candidates) >= max_candidates:
            result.stopped = "full"
            break
        addr = region.start
        while addr < region.end:
            if time.monotonic() >= deadline:
                result.stopped = "deadline"
                break
            size = min(CHUNK, region.end - addr)
            data = read_mem(pid, addr, size)
            if not data:
                addr += SKIP_ON_FAULT
                continue
            result.scanned_bytes += len(data)
            result.needle_hits += _scan_buffer(
                data, result.candidates, seen, needle=needle_bytes,
                offset=needle_offset, min_distinct=min_distinct)
            if on_progress is not None:
                on_progress(result.scanned_bytes, time.monotonic() - t0)
            if len(data) <= OVERLAP:
                break
            addr += len(data) - OVERLAP
        if result.stopped == "deadline":
            break

    result.elapsed_ms = (time.monotonic() - t0) * 1000
    result.fingerprints = [key_fingerprint(c) for c in result.candidates[:8]]
    result.needle = needle
    return result
