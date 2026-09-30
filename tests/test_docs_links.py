#!/usr/bin/env python
"""文档链接门禁：`README.md` 与 `docs/*.md` 里的**相对链接必须真的存在**。

## 为什么要有这条（2026-09 的现场）

`8428825 / 7d753c7「docs: 多语言文档移入 docs/」` 把多语言文档从仓库根搬进了 `docs/`，
但文件里的相对链接还是按「仓库根」写的 —— 一次性留下 **16 条死链**，而且没人会发现：
GitHub 上的相对链接是**相对当前文件所在目录**解析的，`docs/GUIDE.md` 里写
`docs/GUIDE.en.md` 会解析成 `docs/docs/GUIDE.en.md`（404）而不是根目录下那份。

这类错**没有测试就永远靠人肉点**，而搬文档是低频操作、每次都会再犯一遍。
所以这里用标准库扫一遍：不用联网、不依赖任何第三方包，两个平台都能跑。

## 它不是什么

不是 Markdown 渲染器（不校验语法、不校验外链可访问性）。
只钉两件事：**本地相对链接的目标文件存在**、**`#锚点` 能在目标文件的标题里找到**。
锚点校验按 GitHub 的规则（小写 → 去掉标点 → 空格转 `-`）算，中文标题同样适用；
同名标题的去重后缀（`-1`）不处理 —— 我们的文档里没这种情况，真出现也不该判红。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 参与检查的文档：入口 README + docs/ 下所有 md（含本地化与实测记录）
DOCS = [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md"))

# `[文本](目标)` —— 目标里不含空格（我们的链接要么无空格、要么是 URL）
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")
_SKIP_PREFIX = ("http://", "https://", "#", "mailto:", "/")


def slug(text: str) -> str:
    """标题 → GitHub 锚点（小写、去标点、空格转 `-`）。`\\w` 在 Python 里包含中日韩。"""
    s = re.sub(r"[^\w\s-]", "", text.strip().lower(), flags=re.UNICODE)
    return re.sub(r"\s+", "-", s)


def anchors_of(path: Path) -> set[str]:
    return {slug(h) for h in _HEADING_RE.findall(path.read_text(encoding="utf-8"))}


def main() -> int:
    bad: list[str] = []
    checked = 0
    cache: dict[Path, set[str]] = {}

    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        rel = doc.relative_to(ROOT)
        for m in _LINK_RE.finditer(text):
            target = m.group(1)
            if target.startswith(_SKIP_PREFIX):
                continue
            fpart, _, anchor = target.partition("#")
            line = text[: m.start()].count("\n") + 1
            checked += 1

            dest = (doc.parent / fpart).resolve() if fpart else doc.resolve()
            if not dest.exists():
                bad.append(f"{rel}:{line} 链接目标不存在：{target}")
                continue
            if not anchor or dest.suffix != ".md":
                continue
            if dest not in cache:
                cache[dest] = anchors_of(dest)
            if slug(anchor) not in cache[dest]:
                bad.append(f"{rel}:{line} 锚点不存在：{target}"
                           f"（{dest.relative_to(ROOT)} 里没有这个标题）")

    print("test_docs_links:")
    if bad:
        for b in bad:
            print(f"  ❌ {b}")
        print(f"\n❌ {len(bad)} 条链接有问题（共检查 {checked} 条）")
        return 1
    print(f"  ✓ {len(DOCS)} 份文档、{checked} 条相对链接全部有效")
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
