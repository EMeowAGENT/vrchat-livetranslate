#!/usr/bin/env python
"""分玩家语音的**安全不变量**（AST 扫描，静态）。

## 为什么要有这个用例

`vlt/players/` 是**会去碰游戏进程**的代码。它的安全承诺写在包注释里（只读、
不 attach、不注入、不读 `/proc/<pid>/mem`）—— 但注释挡不住以后的自己顺手加一行
"调试用" 的 `ptrace`。所以承诺必须变成**可执行断言**：

1. 代码里不许出现 `ptrace` / `process_vm_writev` / `PTRACE_*` / 抓内存转储的 API；
2. 不许出现任何指向 `/proc/<pid>/mem` 的字符串（打开它会在 `/proc/<我们>/fd`
   留下可枚举的符号链接 —— 那是唯一会暴露"谁在读游戏内存"的东西，比读本身更危险）；
3. 库代码里不许自己提权（`pkexec` / `sudo` / `subprocess` 都属于提权助手的活，
   助手放在 `scripts/` 且单独审）。

## 为什么用 AST 而不是 grep

决定性的理由：**注释与 docstring 里必须能写这些词**。`vlt/players/__init__.py`
开头就写着"不 `ptrace` attach"，grep 会把这段承诺本身当成违规（假红），于是下一个人
只能删掉解释——那才是真正的损失。AST 只扫代码（名字、属性、字符串常量），跳过
docstring，正好是我们要的口径。

## 覆盖率必须看得见

`COVERED_FILES` 会把扫过的文件逐条打印。将来新增模块（`key_linux.py` 等）自动被
`glob` 收进来；如果 glob 一个文件都没扫到，用例直接判红——不许"一个都没扫 = 全绿"。

跑法：.venv/bin/python tests/test_players_safety.py
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 代码里**不许出现**的名字/属性（含将来可能的变体）。
FORBIDDEN_NAMES = {
    "ptrace",
    "process_vm_writev",
    "PTRACE_ATTACH",
    "PTRACE_SEIZE",
    "PTRACE_TRACEME",
    "MiniDumpWriteDump",
    "WriteProcessMemory",
}

# 字符串常量里不许出现的模式（大小写不敏感）。
# ⚠️ `ptrace_scope` 是**内核 sysctl 的名字**（Yama），不是 `ptrace()` 调用；
# 界面上要提示用户「Yama 限制开着」，把这种情况判红会逼着人删掉有用的说明。
FORBIDDEN_PATTERNS = (
    re.compile(r"ptrace(?!_scope)", re.I),
    re.compile(r"process_vm_writev", re.I),
    re.compile(r"/proc/[^\s\"']*mem", re.I),     # /proc/<pid>/mem、/proc/self/mem
)

# 库代码不许自己提权（助手在 scripts/ 里，单独审）。
#
# 唯一例外：`vlt/players/helper.py` 的职责就是「用 pkexec 拉起那个固定脚本」。
# 它被 GUI 和探针共用，不能再塞回 scripts/（否则两边各写一份、口径分裂）。
# 所以这里按**文件白名单**放行"提权字样"这一条，其余规则（ptrace / 读 mem /
# 写内存）对它照样生效。
FORBIDDEN_LIB_NAMES = {"pkexec", "sudo", "subprocess"}
LIB_PRIVILEGE_ALLOWLIST = {
    "vlt/players/helper.py",        # 负责用 pkexec 拉起助手
    "vlt/players/priv_helper.py",   # 助手本体（它的提示语里当然会出现 pkexec）
}

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """收集所有 docstring 常量节点的 id（要跳过它们，注释/说明里可以出现违禁词）。"""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                skip.add(id(body[0].value))
    return skip


def scan_source(text: str, *, lib_code: bool = True,
                allow_privilege: bool = False) -> list[str]:
    """扫一段源码，返回违规描述列表（空 = 干净）。"""
    tree = ast.parse(text)
    skip = _docstring_nodes(tree)
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            findings.append(f"第 {node.lineno} 行：出现名字 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            findings.append(f"第 {node.lineno} 行：出现属性 {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(node.value):
                    findings.append(f"第 {node.lineno} 行：字符串命中 {pattern.pattern}")
                    break
            if lib_code and not allow_privilege:
                for bad in FORBIDDEN_LIB_NAMES:
                    if bad in node.value.lower():
                        findings.append(f"第 {node.lineno} 行：库代码出现提权/子进程字样 {bad}")
        elif isinstance(node, ast.JoinedStr):
            # f-string 的每个字面片段是独立 Constant（`"/proc/"` + 变量 + `"/mem"`），
            # 单看任何一片都匹配不到 —— 拼起来用 `{}` 占位才判得准。
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                else:
                    parts.append("{}")
            joined = "".join(parts)
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(joined):
                    findings.append(f"第 {node.lineno} 行：f-string 命中 {pattern.pattern}")
                    break
    return findings


def _targets() -> list[Path]:
    """要扫的文件：库代码 + 两个碰特权/抓包的脚本（助手是最该被盯的）。"""
    files = sorted((ROOT / "vlt" / "players").glob("*.py"))
    for name in ("probe_player_audio.py", "vlt_priv_helper.py"):
        extra = ROOT / "scripts" / name
        if extra.exists():
            files.append(extra)
    return files


def _is_lib(path: Path) -> bool:
    """只有 `vlt/players/` 算「库代码」——`scripts/` 允许出现 pkexec/subprocess。"""
    try:
        path.relative_to(ROOT / "vlt" / "players")
        return True
    except ValueError:
        return False


def test_self_check() -> None:
    """先证明扫描器**不是假绿**：故意喂一段违规代码，必须能抓到。"""
    print("[自检] 扫描器真的能抓到违规")
    bad = (
        "import os\n"
        "def f(pid):\n"
        '    """文档里写 ptrace 是允许的（docstring 应被跳过）。"""\n'
        "    os.ptrace(0)\n"
        '    p = f"/proc/{pid}/mem"\n'
        "    return p\n"
    )
    findings = scan_source(bad)
    check("抓到 ptrace 调用", any("ptrace" in f for f in findings), str(findings))
    check("抓到 /proc/<pid>/mem 字符串",
          any("/proc/" in f for f in findings), str(findings))
    check("docstring 里的词不误报",
          not any("第 3 行" in f for f in findings), str(findings))

    good = (
        '"""这里解释为什么不做 ptrace attach、也不读 /proc/<pid>/mem。"""\n'
        "def f():\n"
        "    # 注释里也可以出现 ptrace（不在 AST 里）\n"
        "    return 1\n"
    )
    check("纯注释/docstring 干净", scan_source(good) == [], str(scan_source(good)))

    lib_bad = "value = 'pkexec'\n"
    check("库代码里的 pkexec 被抓到",
          any("pkexec" in f for f in scan_source(lib_bad)), str(scan_source(lib_bad)))
    check("同样的字样在非库代码里放行", scan_source(lib_bad, lib_code=False) == [])

    # Yama 的 sysctl 名不是 ptrace() 调用 —— 界面上要提示用户，不能判红。
    check("ptrace_scope（sysctl 名）不误报",
          scan_source('msg = "Yama ptrace_scope=1 下仍需提权"\n') == [],
          str(scan_source('msg = "Yama ptrace_scope=1 下仍需提权"\n')))


def test_tree_is_clean() -> None:
    print("[仓库] vlt/players + 探针脚本 实际扫描")
    targets = _targets()
    check("扫到了文件（不许空跑当绿）", bool(targets), str(targets))
    if not targets:
        return
    bad_files: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        rel_path = str(path.relative_to(ROOT))
        findings = scan_source(text, lib_code=_is_lib(path),
                               allow_privilege=rel_path in LIB_PRIVILEGE_ALLOWLIST)
        rel = path.relative_to(ROOT)
        if findings:
            print(f"  扫描 {rel}: {len(findings)} 项违规")
            bad_files.append(f"{rel}: " + "; ".join(findings))
        else:
            print(f"  扫描 {rel}: 干净")
    check("所有目标文件均无违规", not bad_files, " | ".join(bad_files))


def main() -> int:
    test_self_check()
    test_tree_is_clean()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（分玩家安全不变量）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
