#!/usr/bin/env python
"""AppImage 的**独立验收脚本**（既能本地跑，也进 CI）。

用法：
    python scripts/verify_appimage.py dist/VRChatLiveTranslate-x86_64.AppImage
    python scripts/verify_appimage.py <img> --no-render      # 跳过离线渲染
    python scripts/verify_appimage.py <img> --no-font        # 跳过 Tk 字体检查

退出码：0 = 全过；1 = 有检查没过；2 = 用法/产物问题。

## 为什么单独一个脚本（而不是塞在 build_appimage.sh 里）

构建脚本只该负责「把东西做出来」；**验收**要能被单独调用（CI 直接跑它就够了，
不必重新构建一遍），也要能对着**别人给的** AppImage 跑。构建脚本第 5 步只是转调这里。

## 四项检查（都不碰 VR 运行时、不碰音频设备）

1. **平台纯度** —— 转调 `scripts/check_platform_purity.py --platform linux`：
   模块级（不许有 `vlt.platform.win` / openvr 后端 / Windows 依赖）+ 实现字样级。
   这是「Linux 上只用 OpenXR、Windows 上只用 SteamVR」这条假设的**红灯门禁**。
2. **包内导入 + 反向排除 + xr 瘦身** —— 用**包内那份解释器**（不是宿主的）导入核心模块，
   断言 Windows 独占模块**不在**包里；并断言 pyopenxr 只剩**当前平台**的目录
   （保留集由包内 pyopenxr 自己报出，不硬编码 —— 见该段注释）。
3. **离线渲染一帧** —— `python -m vlt.output.overlay --out <png>`：验证 Pillow + 字体
   链路真的能出图，并断言「边距全透明 + 底板半透明」（这两条坏了，手腕屏上就是
   蓝框外面一圈黑边）。不需要显示器。第 ② 步还会断言图层 alpha 的 flag 在包里。
4. **Tk 中日韩字体可见** —— 只有**有显示器**时才跑（无显示器 → 明确提示「跳过 = 未验证」）。
   CI 里用 xvfb-run 让它真的跑起来；本地不想开显示就不跑。构建脚本换的那份 Xft 版 Tk
   对不对，只有这一步能验。
   ⚠️ **AppImage 自 2026-10 起不再自带字体**：这一步验的是「宿主机 fontconfig 里的中日韩
   字体，Tk 看得见吗」。若宿主机压根没装中日韩字体（`fc-list :lang=zh` 为空），属环境问题、
   **跳过 = 未验证**，不判红；只有「宿主机有、Tk 却看不见」才是产物（Xft 版 Tk）坏了。

⚠️ 全程只读：解包到临时目录、跑完即删。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

FAILED: list[str] = []


def _step(title: str) -> None:
    print(f"\n=== {title} ===")


def _fail(msg: str) -> None:
    FAILED.append(msg)
    print(f"  ❌ {msg}")


def _ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def _find(root: Path, pattern: str) -> Path | None:
    hits = sorted(root.glob(pattern))
    return hits[0] if hits else None


def _host_has_cjk_font() -> bool | None:
    """宿主机 fontconfig 里有没有中日韩字体。

    None = 判断不了（没有 fc-list / 执行失败）—— 这时**不判红**，按「未验证」处理。
    AppImage 自 2026-10 起不再自带字体，字体检查只能验「宿主机的字体 Tk 看不看得见」。
    """
    fc = shutil.which("fc-list")
    if not fc:
        return None
    try:
        out = subprocess.run([fc, ":lang=zh"], capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    return bool(out.stdout.strip())


def verify(img: Path, *, do_render: bool = True, do_font: bool = True,
           expect_slim: bool = True) -> bool:
    from check_platform_purity import appdir, check as purity_check

    # ① 平台纯度（模块 + 字样）
    _step("① 平台纯度（Linux 判据）")
    if not purity_check(img, "linux"):
        FAILED.append("平台纯度检查未通过")

    with appdir(img) as root:
        py = _find(root, "usr/python/bin/python3.*")
        app = root / "usr" / "app"
        site = _find(root, "usr/python/lib/python*/site-packages")
        if py is None or site is None or not app.is_dir():
            _fail(f"AppDir 结构不符合预期（py={py}, app={app}, site={site}）")
            return False
        env = dict(os.environ, PYTHONPATH=f"{app}:{site}", PYTHONUTF8="1")

        def _run(args: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
            # ⚠️ `-P` + cwd=AppDir 都是必须的：`python -c/-m` 会把**当前目录**放在
            #    sys.path 最前，而 PYTHONPATH 排在它后面 —— 从仓库里跑本脚本时，
            #    `import vlt` 会解析到**仓库源码**而不是包内代码，检查等于白做
            #    （这个坑真踩过：② 一直报「包里还有 vlt.platform.win」，
            #     其实是它读的是仓库里那份）。
            return subprocess.run([str(py), "-P", *args], env=env, cwd=str(root),
                                  capture_output=True, text=True, timeout=timeout)

        # ② 包内导入 + 反向排除 + 瘦身
        _step("② 包内导入 + 反向排除 + xr 瘦身（用包内解释器）")
        # ★ 瘦身判据**不能硬编码平台目录名**：pyopenxr 1.1.6302 把目录改了名
        #   （win32→windows_x86_64 / aarch64→linux_aarch64 / x86_64→linux_x86_64 /
        #   android→android_arm_v8a）。硬编码的黑名单会与 build_appimage.sh 的 rm
        #   **一起**静默失效（v0.5.0 就是这样把 ~13MB 的其他平台层发出去的，CI 全绿）。
        #   所以改成：让包内 pyopenxr 自己说「当前平台要哪个目录」，再断言除它以外一个不剩，
        #   并加一条体积上限兜底。
        slim_probe = (
            "import os, sysconfig\n"
            "from pathlib import Path\n"
            "import xr.api_layer, xr.library\n"
            "from xr.api_layer.layer_path import py_layer_library_path\n"
            "site = Path(sysconfig.get_paths()['purelib'])\n"
            "want = {'api_layer': {Path(py_layer_library_path()).parent.name},\n"
            "        'library':   {Path(xr.library.openxr_loader_library._name).parent.name}}\n"
            "for _p in os.environ.get('XR_API_LAYER_PATH', '').split(os.pathsep):\n"
            "    if _p.strip():\n"
            "        want['api_layer'].add(Path(_p).name)\n"
            "for sub, keep in want.items():\n"
            "    base = site/'xr'/sub\n"
            "    have = {p.name for p in base.iterdir() if p.is_dir() and p.name != '__pycache__'}\n"
            "    extra = have - keep\n"
            "    assert not extra, f'xr/{sub} 残留其他平台目录（瘦身没生效）：{sorted(extra)}'\n"
            "    assert keep & have, f'xr/{sub} 缺当前平台目录：{sorted(keep)}'\n"
            "size = sum(f.stat().st_size for f in (site/'xr').rglob('*') if f.is_file())\n"
            "assert size <= 60 * 1024 * 1024, f'xr 瘦身后仍有 {size // 1024 // 1024}MB（上限 60MB）'\n"
            "print(f'xr 只留当前平台：{sorted(want[\"api_layer\"])}')\n"
        ) if expect_slim else ""
        probe = (
            "import importlib.util as u, sysconfig\n"
            "from pathlib import Path\n"
            "import vlt.gui, vlt.engine, vlt.output.openxr_overlay, vlt.platform\n"
            "from vlt.output.openxr_overlay import layer_alpha_flags\n"
            # ★ 图层 alpha 的开关必须在**打进包里的那份代码**里：少了它，面板会整层
            #   按不透明合成 = 蓝框外面一圈黑边（贴图侧的透明边距被当实心黑画出来）
            "flags = int(layer_alpha_flags())\n"
            "assert flags & 0x2 and flags & 0x4, f'图层 alpha flag 不对：0x{flags:x}'\n"
            "for bad in ('vlt.platform.win', 'vlt.output.openvr_overlay'):\n"
            "    assert u.find_spec(bad) is None, f'Linux 产物里不该有 {bad}'\n"
            + slim_probe +
            "print('OK')\n"
        )
        res = _run(["-c", probe])
        if res.returncode == 0:
            tail = "，且 Windows 独占模块不在包里" + ("，xr 只留当前平台" if expect_slim else "")
            _ok("核心模块导入正常" + tail)
        else:
            _fail(f"包内导入/反向排除/瘦身检查失败：{(res.stderr or res.stdout).strip()[:400]}")

        # ③ 离线渲染一帧（不碰 VR / 音频 / 显示器）
        if do_render:
            _step("③ 离线渲染一帧（不需要显示器）")
            out = Path(tempfile.mkdtemp(prefix="vlt-verify-render-")) / "panel.png"
            res = _run(["-m", "vlt.output.overlay", "--out", str(out)])
            if res.returncode == 0 and out.exists() and out.stat().st_size > 0:
                _ok(f"渲染成功（{out.stat().st_size} 字节）")
                # 顺带钉住「边距全透明 + 底板半透明」：这一条坏了，手腕屏上就是
                # 「蓝框外面一圈黑」（图层 alpha 那条判据在 ② 里，两条独立失效路径）
                from PIL import Image
                with Image.open(out) as im:
                    w, h = im.size
                    px = im.convert("RGBA").load()
                    corners = [px[xy][3] for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
                    plate = px[(w // 2, h - 20)][3]
                if any(corners):
                    _fail(f"渲染图四角不透明（{corners}）→ 面板边距在屏上会是黑边")
                elif not 0 < plate < 255:
                    _fail(f"底板不透明（alpha={plate}）→ 半透明没有生效")
                else:
                    _ok(f"边距全透明 + 底板半透明（底板 alpha={plate}）")
            else:
                _fail(f"离线渲染失败：{(res.stderr or res.stdout).strip()[:400]}")
        else:
            _step("③ 离线渲染（已按 --no-render 跳过）")

        # ④ Tk 字体（需要显示器；没有就明确「跳过 = 未验证」）
        if do_font:
            _step("④ Tk 中日韩字体可见（需要显示器）")
            font_probe = (
                "import sys\n"
                "try:\n"
                "    import tkinter as tk, tkinter.font as tkfont\n"
                "    root = tk.Tk(); root.withdraw()\n"
                "except Exception as exc:\n"
                "    print(f'SKIP:{type(exc).__name__}'); sys.exit(2)\n"
                "fams = list(tkfont.families(root)); root.destroy()\n"
                "cjk = [f for f in fams if any(k in f for k in ('CJK','Source Han','Noto Sans SC','WenQuanYi'))]\n"
                "print(f'FAMS:{len(fams)} CJK:{len(cjk)}' + (f' FIRST:{cjk[0]}' if cjk else ''))\n"
                "sys.exit(0 if cjk else 1)\n"
            )
            res = _run(["-c", font_probe])
            line = (res.stdout or "").strip().splitlines()[-1] if res.stdout.strip() else ""
            if res.returncode == 0:
                _ok(f"中日韩字体正常（{line}）")
            elif res.returncode == 2:
                print(f"  ⚠️ 无显示器 → **跳过 = 未验证**（{line}）；"
                      f"CI 里用 xvfb-run 会真的跑")
            elif _host_has_cjk_font() is not True:
                # AppImage 不再自带字体；宿主机没装中日韩字体属于环境问题，不是产物缺陷。
                print(f"  ⚠️ 宿主机没有中日韩字体（`fc-list :lang=zh` 为空）→ **跳过 = 未验证**"
                      f"（{line}）；装 `fonts-noto-cjk` 后可复验")
            else:
                _fail(f"宿主机有中日韩字体、Tk 却看不见（Xft 版 Tk 坏了，界面会是豆腐块）：{line}")
        else:
            _step("④ Tk 字体检查（已按 --no-font 跳过）")

    print()
    if FAILED:
        print(f"== 验收失败：{len(FAILED)} 项 ==")
        for m in FAILED:
            print(f"   ✗ {m}")
        return False
    print("== 验收通过 ==")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="AppImage 独立验收（平台纯度 + 导入 + xr 瘦身 + 渲染 + 字体）")
    ap.add_argument("image", type=Path, help="AppImage 产物路径")
    ap.add_argument("--no-render", action="store_true", help="跳过离线渲染")
    ap.add_argument("--no-font", action="store_true", help="跳过 Tk 字体检查")
    ap.add_argument("--allow-fat", action="store_true",
                    help="允许未瘦身（跳过「xr 只留当前平台」的断言）")
    args = ap.parse_args()

    if not args.image.exists():
        print(f"找不到产物：{args.image}")
        return 2
    if not args.image.name.endswith(".AppImage"):
        print(f"这不是 AppImage：{args.image.name}")
        return 2
    ok = verify(args.image, do_render=not args.no_render, do_font=not args.no_font,
                expect_slim=not args.allow_fat)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
