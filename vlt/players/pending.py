"""取钥期间的密文缓冲：密钥晚到时，把「已经发生」的包补回来。

## 为什么要这一层

取钥（扫内存）要几秒到十几秒，这段时间里包只留在内核 socket 缓冲里 —— 溢出就
永久丢了。而丢掉的恰好是最值钱的一段：

* **加入世界时那批 Event 255 玩家名单**（每位玩家在一次会话里只播一次）；
* 开头几十秒的语音（没名字的 actor 只能显示 `actor1`）。

VRCS 的同一层（`pending` / `startup_packets`）就是干这个的，这里照它的语义实现：

| 缓冲 | 上限 | 作用 |
|---|---|---|
| 主缓冲 | 4096 包 / 16 MiB | 最近的流量，世界就绪后按 10s TTL 修剪 |
| 启动前缀 | 512 包 / 2 MiB | 被主缓冲挤出去的包转存，专保那批大快照 |

## 纪律

* **纯内存、纯逻辑**：不碰 socket，所以离线重放与单元测试用的是同一套判断；
* 只存密文（`Datagram.payload`），不存明文、不落盘；
* 回放顺序按时间戳，并按「端点对 + 密文」去重 —— 多网卡 / 加速器会把同一份
  密文交两遍，重复喂会给 USpeak 序号制造假重复。
"""
from __future__ import annotations

from collections import deque

from .capture_linux import Datagram

MAX_PACKETS = 4096
MAX_BYTES = 16 << 20
TTL_S = 10.0
PREFIX_PACKETS = 512
PREFIX_BYTES = 2 << 20


class PreKeyBuffer:
    """暂存「密钥还没找到」时的密文数据报，命中后按时间序吐出来。"""

    def __init__(self, *, max_packets: int = MAX_PACKETS, max_bytes: int = MAX_BYTES,
                 ttl_s: float = TTL_S, prefix_packets: int = PREFIX_PACKETS,
                 prefix_bytes: int = PREFIX_BYTES, world_ready: bool = False) -> None:
        self.max_packets = max_packets
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self.prefix_packets = prefix_packets
        self.prefix_bytes = prefix_bytes
        # 世界加载完成前不按 TTL 修剪：加载中的大快照可能比 10 秒更久才等到密钥。
        self.world_ready = world_ready
        self._main: deque[Datagram] = deque()
        self._prefix: deque[Datagram] = deque()
        self._bytes = 0
        self._prefix_bytes = 0
        self.dropped = 0            # 主缓冲里被挤掉/超时的包
        self.kept = 0               # 转存进启动前缀的包

    # ------------------------------------------------------------ 写入

    def add(self, dg: Datagram, *, now: float | None = None) -> None:
        """收一个密文数据报；超出上限时从**最旧**的一端丢。"""
        self._main.append(dg)
        self._bytes += len(dg.payload)
        self._trim(now if now is not None else dg.ts)

    def _trim(self, now: float) -> None:
        while self._main:
            over = (len(self._main) > self.max_packets
                    or self._bytes > self.max_bytes
                    or (self.world_ready and now - self._main[0].ts > self.ttl_s))
            if not over:
                return
            old = self._main.popleft()
            self._bytes -= len(old.payload)
            self.dropped += 1
            # 被挤出去的不是垃圾：加入世界那波属性快照正是「先进先出」里最先
            # 被挤掉的一批，所以单独留一份前缀。
            if (len(self._prefix) < self.prefix_packets
                    and self._prefix_bytes + len(old.payload) <= self.prefix_bytes):
                self._prefix.append(old)
                self._prefix_bytes += len(old.payload)
                self.kept += 1

    # ------------------------------------------------------------ 回放

    def release(self) -> list[Datagram]:
        """取走全部缓冲，按时间序返回（同一份密文只留一次）。"""
        merged = list(self._prefix) + list(self._main)
        self._prefix.clear()
        self._main.clear()
        self._prefix_bytes = 0
        self._bytes = 0
        merged.sort(key=lambda dg: dg.ts)
        out: list[Datagram] = []
        seen: set[tuple] = set()
        for dg in merged:
            key = (dg.src, dg.dst, dg.payload)
            if key in seen:
                continue
            seen.add(key)
            out.append(dg)
        return out

    def snapshot_recent(self, since_ts: float) -> list[Datagram]:
        """返回不早于 `since_ts` 的数据报（**不消费**缓冲，按时间序）。

        预热交接到翻译腿时只回放最近几秒的语音：名字早就解析进表里了，
        把十几分钟的语音一次性灌进新会话只会制造一大段延迟。
        """
        merged = [dg for dg in (*self._prefix, *self._main) if dg.ts >= since_ts]
        merged.sort(key=lambda dg: dg.ts)
        return merged

    # ------------------------------------------------------------ 诊断

    @property
    def packets(self) -> int:
        return len(self._main)

    @property
    def nbytes(self) -> int:
        return self._bytes

    def summary(self) -> dict:
        return {"packets": len(self._main), "bytes": self._bytes,
                "prefix_packets": len(self._prefix), "prefix_bytes": self._prefix_bytes,
                "dropped": self.dropped, "kept": self.kept}
