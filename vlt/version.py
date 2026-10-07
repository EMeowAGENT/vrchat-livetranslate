"""版本号：**正式发布** vs **测试/源码构建**。

## 硬约束（先读这段，别把 `__version__` 改成带后缀）

`vlt/__init__.py` 的 `__version__` 必须是**严格 `X.Y.Z`**，它同时是：

  * `vlt/update_check.py:parse_version()` 的输入（正则只认 `X.Y.Z`）——
    带后缀会让「检查更新」全部失效；
  * `release.yml` 的 tag 对账依据（`__version__` 与 tag 不一致直接红）；
  * `scripts/verify_release.py` 的断言目标（冻结产物里的 `__version__.pyc`）。

所以「带后缀的**展示**版本」在本模块单独算，`__version__` 一个字不动。

## 展示规则（PEP 440 local version）

  * HEAD 正好在 `v*` tag **且工作区干净** → 正式：`0.10.0`
  * 否则（非 tag / 有提交差 / dirty）        → 测试：`0.10.0+7.gb04129b`、
    dirty 再加 `.dirty` → `0.10.0+7.gb04129b.dirty`
  * 拿不到 git 元数据（无 `.git`、浅克隆取不到 tag、没装 git）→ 未知：`0.10.0+unknown`

## 元数据从哪来

  * **冻结产物**：`.git` 不随包走，所以构建期由 `scripts/gen_buildinfo.py` 把
    `git describe` 结果写成 `buildinfo.json`，构建脚本 `--add-data` 打进
    `BUNDLE_DIR`（见 build_exe.py / build_appimage.sh）；
  * **源码运行**：`BUNDLE_DIR`（= 仓库根）下没有 `buildinfo.json`，直接问 git。

`parse_describe()` 是**纯函数**，离线单测就钉它（`tests/test_version.py`），不依赖真实 git。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from . import __version__
from .paths import BUNDLE_DIR

#: 构建期烘焙的元数据文件名（冻结产物里由 `--add-data` 放进 `BUNDLE_DIR`）。
BUILDINFO_NAME = "buildinfo.json"

#: `git describe --tags --long` 在正好一个 tag 上会给出 `vX.Y.Z`（不带 --long 时）。
_PLAIN_RE = re.compile(r"^v?(?P<ver>\d+\.\d+\.\d+)$")
#: `git describe --tags --long [--dirty]`：`v0.10.0-7-gb04129b[-dirty]`。
_DESCRIBE_RE = re.compile(
    r"^v?(?P<ver>\d+\.\d+\.\d+)-(?P<count>\d+)-g(?P<hash>[0-9a-f]+)(?P<dirty>-dirty)?$"
)


@dataclass(frozen=True)
class BuildMeta:
    """一次构建的版本身份。`known=False` 表示「拿不到 git 元数据」。"""

    base: str                    # 干净版本（= `__version__`）
    count: int = 0               # 距上一个正式 tag 的提交数
    hash: str = ""               # 短哈希
    dirty: bool = False          # 工作区有改动
    known: bool = False          # 元数据是否可用

    @property
    def official(self) -> bool:
        """是否按「正式版」展示（正好在 tag 上、干净、无提交差）。"""
        return self.known and self.count == 0 and not self.dirty

    @property
    def display(self) -> str:
        if self.official:
            return self.base
        if not self.known:
            return f"{self.base}+unknown"
        suffix = f"{self.count}.g{self.hash}" if self.hash else str(self.count)
        if self.dirty:
            suffix += ".dirty"
        return f"{self.base}+{suffix}"


def parse_describe(describe: str | None, base: str = __version__) -> BuildMeta:
    """把 `git describe` 输出解析成 `BuildMeta`（纯函数，离线可测）。

    认三种形态：`v0.10.0` / `v0.10.0-0-gabc1234`（正好在 tag）/
    `v0.10.0-7-gabc1234[-dirty]`。无法识别（含 `None`）→ `known=False`（展示 `+unknown`）。
    """
    text = (describe or "").strip()
    if not text:
        return BuildMeta(base=base)
    m = _DESCRIBE_RE.match(text)
    if m:
        return BuildMeta(
            base=base,
            count=int(m.group("count")),
            hash=m.group("hash"),
            dirty=bool(m.group("dirty")),
            known=True,
        )
    if _PLAIN_RE.match(text):
        return BuildMeta(base=base, known=True)
    return BuildMeta(base=base)


#: `buildinfo.json` **不存在**（源码运行 / 旧产物）—— 与「文件在、describe 为空」区分：
#: 后者是构建期已经明确记录「拿不到元数据」，运行时不该再去问 git 覆盖它。
_ABSENT = object()


def _read_baked() -> object:
    """读构建期烘焙的 `buildinfo.json`（冻结产物）。

    返回：describe 串（测试/正式构建）/ `None`（文件在但 describe 为空 = 构建期无元数据）/
    `_ABSENT`（文件不存在或坏了 → 调用方回退去问 git）。
    """
    try:
        raw = (BUNDLE_DIR / BUILDINFO_NAME).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return _ABSENT
    if not isinstance(data, dict):
        return _ABSENT
    value = data.get("describe")
    return value if isinstance(value, str) else None


def _git_describe() -> str | None:
    """问 git（源码运行）。没装 git / 不是仓库 / 浅克隆取不到 tag → None。"""
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--match", "v*", "--long", "--dirty"],
            cwd=str(BUNDLE_DIR), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


@lru_cache(maxsize=1)
def build_meta() -> BuildMeta:
    """本次运行的构建身份（进程内只算一次：git 子进程只起一次）。

    优先用构建期烘焙的 `buildinfo.json`（冻结产物，权威）；文件不在才回退去问 git（源码运行）。
    """
    baked = _read_baked()
    describe = _git_describe() if baked is _ABSENT else baked
    return parse_describe(describe)


def display_version() -> str:
    """用户可见的版本号：正式 `0.10.0` / 测试 `0.10.0+7.gb04129b[.dirty]` / `+unknown`。"""
    return build_meta().display


def is_official() -> bool:
    """是否正式发布构建。"""
    return build_meta().official
