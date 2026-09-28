"""断言打包产物里**没有混进另一个平台的实现**。

用法：
    python scripts/check_platform_purity.py dist/VRChatLiveTranslate.exe --platform windows
    python scripts/check_platform_purity.py dist/VRChatLiveTranslate.AppImage --platform linux

退出码：0 = 干净；1 = 发现违规（CI 里当红灯用）。

## 为什么要这个脚本

「Windows 版不含 pipewire / openxr」「Linux 版不含 WASAPI 那套」这两条要求，
如果只写在构建脚本的注释里，迟早有人加个 `--hidden-import` 就破了 ——
而且破了没有任何征兆，只是 exe 悄悄变胖、或者在别的机器上冒出莫名其妙的报错。

所以把它变成**可执行的断言**：构建完自动跑，红了就说明隔离被破坏。

## 判据为什么不是「搜 exe 原始字节」

单文件 exe 里的 PYZ 是 **zlib 压缩**的，直接对 exe 做字符串搜索永远搜不到
（这个坑 `scripts/verify_release.py` 的注释里已经记过一次）。正确做法是
用 PyInstaller 自己的读取器解出目录表与各条目字节，再搜解压后的内容。

* 模块级判据：`pkg_archive_contents()` 列出的名字（含 PYZ 内的模块名）
* 内容级判据：逐条目 `extract()` 出字节再搜（能抓到内嵌的 XML / conf 常量）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterator

# 每个平台**不允许**出现的东西。
#
#   modules —— 模块名（PyInstaller 的 TOC，含 PYZ 内部），靠 --exclude-module 保证
#   strings —— 内嵌内容里不允许出现的字样（实现代码、资源常量、平台工具名）
#
# ⚠️ 加新平台独占模块时，**同时**加进 scripts/build_exe.py 的 EXCLUDE_WIN
#    （或 AppImage 构建脚本的反向排除），否则这里会红。
FORBIDDEN: dict[str, dict[str, list[str]]] = {
    "windows": {
        "modules": [
            "vlt.platform.linux",          # PipeWire 设备枚举/采集/虚拟声卡（pw-dump/record/cat）
            "vlt.output.openxr_overlay",   # 自建 OpenXR 手腕屏（pyopenxr + EGL/Wayland）
            "xr",                          # pyopenxr 本体
        ],
        "strings": [
            # Linux 侧的平台工具名/API 名：只该出现在上面那几个被排除的模块里。
            # （加新字符串前先确认它**没**出现在共享文件里 —— 文档字符串也算，
            #   否则会把注释里的说明也判成违规。）
            b"libpipewire-module-loopback",
            b"pw-dump",
            b"pw-loopback",
            b"libwayland-client",
            b"eglGetPlatformDisplay",
            b"GraphicsBindingEGLMNDX",
            b"XR_MNDX_egl_enable",
            b"XR_EXTX_overlay",
            b"pyopenxr",
        ],
    },
    "linux": {
        "modules": [
            "vlt.platform.win",       # WASAPI / pyaudiowpatch 那套
            "pyaudiowpatch",
            "pycaw",
            "comtypes",
        ],
        "strings": [
            b"pyaudiowpatch",
            b"paWASAPI",
            b"VoiceMeeter",
            b"VB-Audio",
            b"msyh.ttc",              # Windows 专有字体路径
            b"GetUserDefaultUILanguage",
        ],
    },
}


def _fail(msg: str) -> None:
    print(f"  ❌ {msg}")


def _ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def load_names(artifact: Path) -> list[str]:
    """产物里收录的全部名字（含 PYZ 内部模块）。"""
    from PyInstaller.archive.readers import CArchiveReader, pkg_archive_contents
    return list(pkg_archive_contents(str(artifact)))


def _string_blobs(node) -> Iterator[bytes]:
    """从 code 对象里递归取出所有字符串/字节常量（供子串搜索）。

    为什么不直接 `marshal.dumps(code)`：那要求检查时用的解释器与打包时**完全同版本**，
    否则 marshal 反序列化会失败。直接遍历 `co_consts` / `co_names` 没有版本约束，
    而且更精确 —— 不会像原始字节搜索那样撞上无关的字节序列。
    """
    import types
    stack = [node]
    while stack:
        cur = stack.pop()
        for const in getattr(cur, "co_consts", ()) or ():
            if isinstance(const, types.CodeType):
                stack.append(const)          # 函数/类内部还有一层
            elif isinstance(const, bytes):
                yield const
            elif isinstance(const, str):
                yield const.encode("utf-8", "replace")
        for name in getattr(cur, "co_names", ()) or ():
            if isinstance(name, str):
                yield name.encode("utf-8", "replace")


def iter_entry_bytes(artifact: Path):
    """产出 (名字, 字节) —— 逐条目解压，含 PYZ 内的每条模块。

    ⚠️ 判类型要用 PyInstaller 自己的常量（`PKG_ITEM_PYZ` 的值是 `'z'` 不是 `'PYZ'`）——
    写字符串字面量会静默不匹配，于是 PYZ 从不被递归、检查全部漏过（踩过）。
    ⚠️ `ZlibArchiveReader.extract()` 返回的是 **code 对象**而不是字节（也踩过）。
    """
    from PyInstaller.archive.readers import (CArchiveReader, PKG_ITEM_PYZ,
                                             PKG_ITEM_ZIPFILE)
    arch = CArchiveReader(str(artifact))
    for name, toc_entry in arch.toc.items():
        *_, typecode = toc_entry
        try:
            if typecode == PKG_ITEM_PYZ:
                pyz = arch.open_embedded_archive(name)
                for mod in pyz.toc:
                    try:
                        data = pyz.extract(mod)
                    except Exception:        # noqa: BLE001 — 个别条目解不出不致命
                        continue
                    if isinstance(data, (bytes, bytearray)):
                        yield mod, bytes(data)
                    else:
                        for blob in _string_blobs(data):
                            yield mod, blob
            elif typecode == PKG_ITEM_ZIPFILE:
                # base_library.zip：CPython 标准库的 .pyc。里面不会有本项目的字样，
                # 但顺手搜掉，免得将来有人把东西塞进 zip 就绕过了检查。
                import io
                import zipfile
                with zipfile.ZipFile(io.BytesIO(arch.extract(name))) as zf:
                    for member in zf.namelist():
                        try:
                            yield f"{name}!{member}", zf.read(member)
                        except Exception:    # noqa: BLE001
                            continue
            else:
                yield name, arch.extract(name)
        except Exception:                    # noqa: BLE001
            continue


def check(artifact: Path, platform: str) -> bool:
    rules = FORBIDDEN[platform]
    print(f"== 检查 {platform} 产物：{artifact.name} ==")
    print(f"   大小：{artifact.stat().st_size / 1024 / 1024:.1f} MB")

    try:
        names = load_names(artifact)
    except ImportError:
        print("  ⚠️ 装不上 PyInstaller（需要它的读取器解包）→ **跳过 = 未验证**")
        print("     装法：pip install pyinstaller")
        return False
    except Exception as exc:                 # noqa: BLE001
        _fail(f"解包失败：{type(exc).__name__}: {exc}")
        return False

    print(f"   收录条目：{len(names)}")
    ok = True

    # ---- 判据 1：模块名（最硬的一条，直接反映 --exclude-module 有没有生效）
    name_set = set(names)
    for mod in rules["modules"]:
        hits = sorted(n for n in name_set
                      if n == mod or n.startswith(mod + "."))
        if hits:
            _fail(f"混进了 {platform} 不该有的模块：{hits[:5]}"
                  f"{' …' if len(hits) > 5 else ''}")
            ok = False
    if ok:
        _ok(f"模块级干净（{len(rules['modules'])} 条禁列一条都没混进来）")

    # ---- 判据 2：内嵌内容里的字样（实现代码 / 资源常量 / 平台工具名）
    needles = rules["strings"]
    found: dict[bytes, list[str]] = {}
    for name, data in iter_entry_bytes(artifact):
        for needle in needles:
            if needle in data:
                found.setdefault(needle, []).append(name)
    for needle in needles:
        if needle in found:
            where = found[needle][:3]
            _fail(f"内嵌内容里出现 {needle.decode()}（来自 {where}）")
            ok = False
    if not found:
        _ok(f"内容级干净（{len(needles)} 条禁列字样一处都没出现）")

    print(("== 结论：干净 ==" if ok else "== 结论：**隔离被破坏** =="))
    if not ok:
        print("   修法：把对应模块加进构建脚本的排除列表（Windows 见 "
              "scripts/build_exe.py 的 EXCLUDE_WIN），"
              "并确认它没有出现在共享代码的顶层 import 里。")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="断言打包产物没有混进另一个平台的实现")
    ap.add_argument("artifact", type=Path, help="打包产物（单文件 exe / 目录 / AppImage）")
    ap.add_argument("--platform", choices=sorted(FORBIDDEN), required=True)
    args = ap.parse_args()

    if not args.artifact.exists():
        print(f"找不到产物：{args.artifact}")
        return 2
    return 0 if check(args.artifact, args.platform) else 1


if __name__ == "__main__":
    raise SystemExit(main())
