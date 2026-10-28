"""把提权助手拼成一个**自包含脚本**，经 stdin 交给 `pkexec python3 -`。

## 为什么不是 `pkexec <挂载点里的程序>`（AppImage）

AppImage 是 FUSE 挂载，**默认没有 `allow_root`** —— root 连 `access()` 都会
`EACCES`，所以 pkexec 在弹密码框之前就失败：

    Error accessing /tmp/.mount_xxx/usr/bin/...: Permission denied

## 为什么不用"先装到 /usr/local"

装一份当然能用，但多了一个用户必须做的步骤（还要跟着版本更新）。而且如果改成
"先复制到 /tmp 再 sudo 执行"，那执行的就是**用户可改写的文件** —— 任何同用户
进程都能在 exec 前替换它，等于给了一个本地提权面。

## 这里的做法

- 把助手需要的 6 个模块（全纯 stdlib）在内存里拼成一个脚本；
- `pkexec /usr/bin/python3 - <参数…>`，脚本从 **stdin** 进去；
- root 执行的只有系统自带的 `python3`，脚本内容来自我们进程（argv/stdin 都不是
  别人能替换的路径）；
- 不落盘、不需要安装、不依赖 AppImage 挂载点可读。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# 加载顺序 = 依赖顺序（被 import 的必须先注册进 sys.modules）。
MODULES = (
    ("vlt", "vlt/__init__.py"),
    ("vlt.players", "vlt/players/__init__.py"),
    ("vlt.players.procfs", "vlt/players/procfs.py"),
    ("vlt.players.gcm", "vlt/players/gcm.py"),
    ("vlt.players.capture_linux", "vlt/players/capture_linux.py"),
    ("vlt.players.key_linux", "vlt/players/key_linux.py"),
    ("vlt.players.priv_helper", "vlt/players/priv_helper.py"),
)

_BOOTSTRAP = '''\
import sys, types

_ORDER = {order!r}
_SRC = {sources!r}

def _load(name, package):
    mod = types.ModuleType(name)
    mod.__package__ = package
    mod.__file__ = "<vlt-helper>/" + name.replace(".", "/") + ".py"
    sys.modules[name] = mod
    exec(compile(_SRC[name], mod.__file__, "exec"), mod.__dict__)
    return mod

for _name in _ORDER:
    _load(_name, _name.rpartition(".")[0])

sys.modules["vlt.players.priv_helper"].main()
'''


def bundle_root() -> Path:
    """冻结产物里的资源根目录（`sys._MEIPASS`）；源码运行时是仓库根。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent.parent


def system_python() -> str | None:
    """跑助手用的解释器（宿主 python3）——AppImage 里不打包解释器。"""
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return None


def build_payload(root: Path | None = None) -> bytes:
    """生成自包含脚本（含 6 个模块）。缺文件时抛 FileNotFoundError。"""
    base = root or bundle_root()
    sources: dict[str, str] = {}
    order: list[str] = []
    for name, rel in MODULES:
        path = base / rel
        if not path.is_file():
            raise FileNotFoundError(f"打包内容缺失：{path}")
        sources[name] = path.read_text(encoding="utf-8")
        order.append(name)
    script = _BOOTSTRAP.format(order=tuple(order), sources=sources)
    return script.encode("utf-8")
