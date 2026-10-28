"""从 VRChat 的 `output_log_*.txt` 读「世界加载好了没」。

## 为什么需要

扫内存要等世界真的加载完才有意义：加载中扫，候选密钥还没在流量里出现，白扫
十几秒；而且那段时间抓到的包还是上一个世界的。VRCS 的 `world.rs` 就是干这个的。

只认 4 个标记（都在日志行 `时间戳 级别 - 内容` 的**内容**部分）：

| 标记 | 含义 |
|---|---|
| `Memory Usage: after world loaded` | 世界已加载 |
| `[Behaviour] Finished entering world` | 世界已加载（另一种写法） |
| `[Behaviour] Entering Room:` / `[Behaviour] Joining wrld_` | 正在进世界 |
| `[Behaviour] OnLeftRoom` | 已离开，等下一个世界 |

## 纪律

* **只读**：只 `stat` + `open(..., "rb")`，不写日志、不改游戏文件；
* `unknown` ≠ `loading`：找不到日志、或日志里没有标记时**不阻塞**取钥，
  只在状态栏说明（禁止静默 —— 但也绝不因为猜不到就让人干等）；
* 从头扫整份日志太贵（几十 MB），所以从尾部按块倒着找最后一个标记。
"""
from __future__ import annotations

from pathlib import Path

from . import names

STATE_READY = "ready"
STATE_LOADING = "loading"
STATE_UNKNOWN = "unknown"

READY_MARKERS = (
    "Memory Usage: after world loaded",
    "[Behaviour] Finished entering world",
)
LOADING_MARKERS = (
    "[Behaviour] Entering Room:",
    "[Behaviour] Joining wrld_",
    "[Behaviour] OnLeftRoom",
)

SCAN_CHUNK = 256 << 10        # 倒着找标记时每块多大
MAX_BACK_SCAN = 32 << 20      # 最多往回找这么多（再往前就不是"当前世界"了）
LINE_LIMIT = 4096             # 日志行的合理上限（超长的不是标记行）
READ_CHUNK = 1 << 20          # 增量跟读时每次最多读多少


def log_message(line: str) -> str | None:
    """`2026.10.25 03:00:00 Debug      -  [Behaviour] OnLeftRoom` → 内容部分。

    时间戳形状不对时退回「按 ` - ` 切一刀」——VRChat 换日志格式时不至于整条失效，
    但仍要求有分隔符，避免把玩家名 / 堆栈行当成标记。
    """
    line = line.rstrip("\r\n")
    if not line:
        return None
    stamp = line[:19]
    looks_like_stamp = (
        len(stamp) == 19 and stamp[4] == "." and stamp[7] == "." and stamp[10] == " "
        and stamp[13] == ":" and stamp[16] == ":"
    )
    rest = line[19:] if looks_like_stamp else line
    sep = rest.find(" - ")
    if sep < 0:
        return None
    return rest[sep + 3:].lstrip()


def marker_of(message: str) -> bool | None:
    """内容 → True=已加载 / False=加载中 / None=不是标记。"""
    if message.startswith(READY_MARKERS):
        return True
    if message.startswith(LOADING_MARKERS):
        return False
    return None


def last_marker(text: str) -> bool | None:
    """一段日志里**最后**一个标记（没有完整行结尾时忽略最后一行）。"""
    end = text.rfind("\n")
    if end < 0:
        return None
    for line in reversed(text[:end].split("\n")):
        message = log_message(line)
        if message is None:
            continue
        found = marker_of(message)
        if found is not None:
            return found
    return None


def last_marker_bytes(chunk: bytes) -> bool | None:
    return last_marker(chunk.decode("utf-8", "replace"))


class WorldGate:
    """跟一份最新的 `output_log_*.txt`，回答「现在能不能开始取钥」。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._path: Path | None = None
        self._offset = 0
        self._partial = b""
        self._state = STATE_UNKNOWN
        self._detail = "没有 output_log（世界状态未知）"

    # ------------------------------------------------------------ 查询

    @property
    def state(self) -> str:
        return self._state

    @property
    def ready(self) -> bool:
        return self._state == STATE_READY

    @property
    def loading(self) -> bool:
        """**只有**这个状态才值得等；`unknown` 不该拦人。"""
        return self._state == STATE_LOADING

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def path(self) -> Path | None:
        return self._path

    # ------------------------------------------------------------ 轮询

    def poll(self) -> str:
        """刷新一次，返回状态（ready / loading / unknown）。"""
        latest = names.latest_output_log(self.directory) if self.directory else None
        if latest is None:
            self._reset(None, STATE_UNKNOWN, "没有 output_log（世界状态未知）")
            return self._state
        try:
            size = latest.stat().st_size
        except OSError:
            return self._state
        if latest != self._path or size < self._offset:
            self._path = latest
            self._offset = size
            self._partial = b""
            found = self._scan_back(latest, size)
            if found is None:
                self._state = STATE_UNKNOWN
                self._detail = f"{latest.name}：没有加载完成/正在加载的标记"
            else:
                self._state = STATE_READY if found else STATE_LOADING
                self._detail = (f"{latest.name}：" +
                                ("世界已加载" if found else "世界加载中/已离开"))
            return self._state
        if size > self._offset:
            self._read_forward(latest, size)
        return self._state

    # ------------------------------------------------------------ 内部

    def _reset(self, path: Path | None, state: str, detail: str) -> None:
        self._path = path
        self._offset = 0
        self._partial = b""
        self._state = state
        self._detail = detail

    def _scan_back(self, path: Path, size: int) -> bool | None:
        end = size
        scanned = 0
        try:
            with path.open("rb") as fh:
                while end > 0 and scanned < MAX_BACK_SCAN:
                    begin = max(0, end - SCAN_CHUNK)
                    fh.seek(begin)
                    chunk = fh.read(end - begin)
                    scanned += len(chunk)
                    found = last_marker_bytes(chunk)
                    if found is not None:
                        return found
                    if begin == 0:
                        return None
                    # 往左挪一行，覆盖被块边界切断的那一行
                    end = max(0, begin - LINE_LIMIT)
        except OSError as exc:
            self._detail = f"读不了 {path.name}：{exc.strerror or exc}"
            return None
        return None

    def _read_forward(self, path: Path, size: int) -> None:
        try:
            with path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(min(READ_CHUNK, size - self._offset))
        except OSError as exc:
            self._detail = f"读不了 {path.name}：{exc.strerror or exc}"
            return
        if not chunk:
            return
        self._offset += len(chunk)
        self._partial += chunk
        end = self._partial.rfind(b"\n")
        if end < 0:
            if len(self._partial) > LINE_LIMIT:
                self._partial = b""
            return
        found = last_marker_bytes(self._partial[:end + 1])
        self._partial = self._partial[end + 1:]
        if found is None:
            return
        self._state = STATE_READY if found else STATE_LOADING
        self._detail = (f"{path.name}：" + ("世界已加载" if found else "世界加载中/已离开"))
