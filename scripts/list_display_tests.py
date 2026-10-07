#!/usr/bin/env python
"""列出「传递依赖 tkinter」的用例 —— 供 CI 在解释器没有 tkinter 时跳过。

## 为什么要有这个脚本（而不是一张手抄名单）

`.github/workflows/ci.yml` 的 `linux-tests` 里，xvfb-run 已经给**所有**用例统一提供虚拟
DISPLAY，所以真正需要单独对待的只有一件事：**解释器有没有 tkinter**。没有 tkinter 时，
凡是（直接或经 `vlt` 模块）会 import tkinter 的用例都会 import 失败，得跳过而不是判红。

这份名单以前是**手抄**的，结果必然漂移：写这个脚本时实际有 32 个用例传递依赖 tkinter，
而手抄名单只列了 11 个 —— 漏掉的那 21 个（如 `test_langs.py` 在函数体内
`from vlt.gui import ...`、`test_selfcheck.py` 走 `from vlt import selfcheck`）一旦环境缺
tkinter 就会从「跳过」变成「红」。

所以改成**按 import 图自动发现**：扫 `vlt/` 与 `tests/` 的 import，记忆化地判断每个模块
是否传递地 import 了 tkinter。判定覆盖：

  · 函数体内的惰性 import（`ast.walk` 会走到函数体）—— 本仓库大量用例是这样写的；
  · `from vlt import gui_chat` 这类「把子模块当名字导入」的写法（要把 `vlt.gui_chat`
    也算进依赖，只记 `vlt` 会漏）；
  · `vlt` 内部的相对 import（`from ..platform import x`）按文件所在包正确解析。

## 自检（判据要有分辨力）

脚本末尾会断言 `vlt.gui` / `vlt.ui_tk` 确实被判为需要 tkinter、且命中用例数不低于阈值。
任何一条不满足就报错退出 —— 否则「解析器某天坏了」会静默变成「名单为空、全部跳过」，
比手抄名单更危险。

## 用法

    python scripts/list_display_tests.py            # 单行空格分隔，CI 直接吃
    python scripts/list_display_tests.py --lines    # 每行一个，给人看
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 命中用例数低于这个值就认为「解析器坏了」，宁可红也不要静默全跳过。
_MIN_EXPECTED = 10
# 这几个模块是 Tk 依赖图的锚点：脚本必须能认出它们需要 tkinter。
_ANCHORS = ("vlt.gui", "vlt.ui_tk")


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(path: Path) -> str:
    """文件所在包（相对 import 的解析基点）。"""
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1])


def _resolve_relative(module: str, level: int, package: str) -> str:
    parts = package.split(".") if package else []
    # level=1 指当前包；每多一级再往上退一层。
    base = parts[: len(parts) - (level - 1)] if level >= 1 else parts
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _parse_imports(path: Path) -> set[str]:
    """返回该文件引用到的所有模块点分名（含 `from M import x` 里的 `M.x`）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = _package_of(path)
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                deps.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(node.module or "", node.level, package)
            else:
                base = node.module or ""
            if base:
                deps.add(base)
            # `from pkg import sub`：sub 可能是子模块（如 `from vlt import gui`）。
            # 一并记 `pkg.sub`；若 sub 是普通符号，这个名字不是模块、查找时自然落空。
            for alias in node.names:
                if alias.name != "*":
                    deps.add(f"{base}.{alias.name}" if base else alias.name)
    return deps


def _build_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for py in sorted(Path(ROOT, "vlt").rglob("*.py")) + sorted(
        Path(ROOT, "tests").glob("*.py")
    ):
        graph[_module_name(py)] = _parse_imports(py)
    return graph


_GRAPH = _build_graph()


def _is_tk(dep: str) -> bool:
    return dep == "tkinter" or dep.startswith("tkinter.")


def _compute_needs_tk() -> set[str]:
    """传递闭包：返回「直接或间接 import tkinter」的模块集合。

    用不动点迭代而不是 DFS —— 模块之间**存在真 import 环**（例如 `vlt.gui` 与
    `vlt.gui_*` 互相引用），DFS + 记忆化在环里会把「因环暂时按 False」的结果错误缓存。
    迭代法天然免疫：起点是直接 import tkinter 的模块，然后不断把「依赖了已知需要 tkinter
    的模块」的模块加进来，直到不再增长。
    """
    needed = {
        mod
        for mod, deps in _GRAPH.items()
        if any(_is_tk(dep) for dep in deps)
    }
    changed = True
    while changed:
        changed = False
        for mod, deps in _GRAPH.items():
            if mod in needed:
                continue
            if any(dep in needed for dep in deps):
                needed.add(mod)
                changed = True
    return needed


_NEEDS_TK = _compute_needs_tk()


def _needs_tk(module: str) -> bool:
    """module 是否（传递地）import tkinter。未知模块（第三方）只认字面 tkinter。"""
    if module in _NEEDS_TK:
        return True
    return _is_tk(module)


def discover() -> list[str]:
    """返回所有传递依赖 tkinter 的 `tests/test_*.py`（相对仓库根，已排序）。

    路径统一用 **POSIX 分隔符**（`as_posix()`）：输出会被 CI 的 bash 用
    `[[ " $GUI_TESTS " == *" $t "* ]]` 逐字匹配，而 `$t` 来自 `tests/test_*.py`
    这种正斜杠写法 —— 若在 Windows 上产出反斜杠，匹配会静默失效。
    """
    found = []
    for test in sorted(Path(ROOT, "tests").glob("test_*.py")):
        if _needs_tk(_module_name(test)):
            found.append(test.relative_to(ROOT).as_posix())
    return found


def _self_check() -> list[str]:
    missing = [a for a in _ANCHORS if not _needs_tk(a)]
    if missing:
        print(
            f"::error::依赖图异常：{'、'.join(missing)} 未被判为需要 tkinter"
            "（解析器坏了？）",
            file=sys.stderr,
        )
        sys.exit(1)
    found = discover()
    if len(found) < _MIN_EXPECTED:
        print(
            f"::error::只发现 {len(found)} 个需要 tkinter 的用例（预期 ≥ {_MIN_EXPECTED}）"
            "，疑似解析器坏了",
            file=sys.stderr,
        )
        sys.exit(1)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--lines", action="store_true", help="每行一个（默认单行空格分隔）"
    )
    args = ap.parse_args()

    found = _self_check()
    print("\n".join(found) if args.lines else " ".join(found))
    print(f"发现 {len(found)} 个需要 tkinter 的用例", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
