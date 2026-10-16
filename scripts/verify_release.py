"""独立复核线上 Release 附件（不依赖 CI 的自检结论）。

用法：.venv/Scripts/python.exe scripts/verify_release.py v0.11.3 "DEGRADED_NOTE_RECOVERED,resolve_script_font_path,load_script_fonts,fetch_latest_manifest,_silence_tail,INPUT_GATE_DEFAULT_TAIL_MS"

第二个参数 = 本版代码里必定出现的字符串，**可以用逗号给多个**（每个都要命中才算过）。
判据是「在解包出来的字节码里搜得到」—— 不是搜 exe 原始字节（那是压缩过的 PYZ，永远搜不到）。

⚠️ **本版（v0.11.3：一个功能 + 三处修复）各取新增符号名做 needle，一条一个、互不替代**：
  ① 更新主源切到 Cloudflare R2（#103）：`fetch_latest_manifest`（`vlt/update_check.py` 新增的主源清单取回入口）
     —— v0.11.2 里不存在（旧版只有 GitHub 一条路）；
  ② 韩文显示成方框（#104）：`resolve_script_font_path`（按书写系统解析字体的统一入口）与
     `load_script_fonts`（一次性加载各书写系统字体）—— 两串都是本版新增。注意 `_split_script_runs`
     **不能**当 needle：切 run 的框架在 v0.11.2（泰文那版）里就有，本版只是把它泛化到多书写系统；
  ③ 输入门限关闸补数字静音（#105）：`_silence_tail`（补等长静音的新方法）与 `INPUT_GATE_DEFAULT_TAIL_MS`
     （600ms 常量）—— 两串在 v0.11.2 里都不存在；
  ④ 手腕屏降级说明进状态栏（#100）：`DEGRADED_NOTE_RECOVERED`（降级原因恢复可见的新文案常量）。
六串已用 `git grep <符号> v0.11.2 -- vlt/ scripts/` 逐一核对（全部 0 命中），并在本地包（与 tag 同源）里预跑过命中。
⚠️ 本版也有**没有新符号可搜**的改动（#100 的「定点重试遵守 `_closed` 纪律」「`start()` 归零自愈状态」）——
它们的证据是用例与故障注入，不是 needle；别因为搜不到就以为那部分没进包。

"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 产物名 = 客户端契约（vlt/update_check.py 里的同名常量，由 tests/test_update_check.py 对着
# release.yml 钉住）。这里 import 而不是抄一份：抄一份就会在改名时静默过期。
from vlt.update_check import APPIMAGE_ASSET_NAME, EXE_ASSET_NAME, SUMS_ASSET_NAME  # noqa: E402

TAG = sys.argv[1] if len(sys.argv) > 1 else "v0.0.2"
# 本版新功能里必定出现的字符串；**逗号可给多个**（每个都要命中才算过）
NEEDLES = [s.strip() for s in
           (sys.argv[2] if len(sys.argv) > 2 else "俄语").split(",") if s.strip()]
ROOT = Path(__file__).resolve().parents[1]
REPO_SLUG = "nixi-agent/vrchat-livetranslate"
# 客户端的主更新源（Cloudflare R2 公开域）。域名与键名是**客户端契约**：
#   vlt/update_check.py 的 MANIFEST_URL / ALLOWED_HOSTS ↔ scripts/publish_r2.py 的 BASE_URL + MANIFEST_KEY
R2_BASE = "https://dl.kcm-nixi.cn"
R2_MANIFEST_KEY = "latest.json"
WORK = Path(tempfile.mkdtemp(prefix="verify_release_"))

ok: list[str] = []
bad: list[str] = []
skipped: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (ok if cond else bad).append(f"{name}{('：' + detail) if detail else ''}")
    print(f"  {'✅' if cond else '❌'} {name}{('：' + detail) if detail else ''}")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def r2_manifest() -> dict | None:
    """取客户端主更新源（Cloudflare R2）上的清单；连不上返回 None（调用方按「跳过」记一笔）。"""
    url = f"{R2_BASE}/{R2_MANIFEST_KEY}"
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "vlt-verify-release/1.0"}),
                timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 — 探针失败只记一笔，别把复核本身搞崩
        print(f"  （R2 清单取不到：{type(exc).__name__}: {exc}）")
        return None


def r2_probe_size(url: str) -> tuple[int | None, str]:
    """Range 探针（1 字节）拿对象总字节数：返回 `(总长, 说明)`。

    ⚠️ **别只认 `Content-Range`**（评审 §7）：这条域名下实测回的是 `206` + `Content-Range`，
    但缓存层级策略一变就可能回 `200` 全量 —— 那时总长在 `Content-Length` 里（206 的
    `Content-Length` 只有 1 字节，所以必须先看 `Content-Range`）。两条都读，
    读不到就把原因交回去，让调用方能区分「拿不到」和「对不上」（否则日志只会印一个
    `R2 None`，看着像文件有问题）。

    为什么不整份下回来对 sha：这两份加起来 75MB，本机（大陆直连）实测要十几分钟 ——
    而「逐字节一致」这一步由 scripts/publish_r2.py 在**发布时**（CI，出口在美国）做过并卡门禁；
    这里复核的是「公开域上现在确实是那一版」，存在性 + 大小 + 清单里的 sha256 已经够判。
    """
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0",
                                                   "User-Agent": "vlt-verify-release/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", None)
            content_range = resp.headers.get("Content-Range", "")
            content_length = resp.headers.get("Content-Length", "")
            resp.read(16)
    except Exception as exc:  # noqa: BLE001
        print(f"  （R2 探针失败 {url}：{type(exc).__name__}: {exc}）")
        return None, f"探针失败 {type(exc).__name__}"
    if "/" in content_range:                             # "bytes 0-0/37440285"
        try:
            return int(content_range.rsplit("/", 1)[1]), f"{status} Content-Range"
        except ValueError:
            pass
    if status == 200 and content_length.isdigit():       # 缓存层回了全量：总长就是 Content-Length
        return int(content_length), "200 Content-Length"
    return None, f"响应头里没有可用的总长（status={status} Content-Range={content_range!r}）"


def r2_sums_lines() -> dict[str, str] | None:
    """取公开域上那份 `SHA256SUMS.txt`，按附件名切成 `{名字: sha256}`；取不到返回 None。

    为什么值得单独对一次账：这份文件是 GUIDE 让**用户自己核校验值**用的，
    AppImage 的 sha256 以前只存在于 `latest.json` 里（Linux 用户照文档核不了）——
    所以「线上那份里两个平台各一行、且值与清单一致」值得在发布后复核一遍，
    而不是只信发布时那一刻的本地文件（对象可能被换掉、CDN 可能回旧内容）。
    它只有 200 字节上下，整份下回来比 exe 便宜得多。
    """
    url = f"{R2_BASE}/{SUMS_ASSET_NAME}"
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "vlt-verify-release/1.0"}),
                timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — 探针失败只记一笔
        print(f"  （R2 上的 {SUMS_ASSET_NAME} 取不到：{type(exc).__name__}: {exc}）")
        return None
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[1].lstrip("*")] = parts[0].lower()
    return out


print(f"== 复核 {TAG} ==")
print(f"下载目录：{WORK}")
# ⚠️ 必须显式 `--repo` + `-p`。`gh release download` 不带 `--repo` 时会按**当前目录的 git
#    远端**推断仓库 —— 在 fork / 别的 checkout 里跑，就会下到**同名 tag 的另一个仓库**的资产
#    （上游的 release 还带 AppImage）；随后拿本仓库的 `.digest` 去对账，sha 必然不符，
#    看着像「发布出去的不是我们构建的那份」，实际只是下错了源。
#    实测踩到：复核因此自己把自己判红，白查一轮。
_dl = None
for _try in range(1, 4):
    r = subprocess.run(["gh", "release", "download", TAG, "--repo", REPO_SLUG,
                        "-p", "VRChatLiveTranslate.exe", "--dir", str(WORK), "--clobber"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0 and list(WORK.glob("*.exe")):
        _dl = r
        break
    print(f"  下载第 {_try} 次没成功（{(r.stderr or '').strip()[:80]}），重试…")
    time.sleep(4)
if _dl is None:
    print("gh release download 失败：", r.stderr)
    raise SystemExit(2)

exes = list(WORK.glob("*.exe"))
check("附件齐了（exe，且只有本仓库的那一个）", len(exes) == 1,
      f"{[p.name + ' ' + f'{p.stat().st_size:,}B' for p in exes]}")

# 尺寸也和服务端对一次账：下载被截断 / 半途重试时 sha 会莫名其妙不符，
# 先比尺寸能一眼区分「下载坏了」和「产物真的不是构建的那份」。
_want_size = subprocess.run(
    ["gh", "api", f"repos/{REPO_SLUG}/releases/tags/{TAG}",
     "--jq", '.assets[] | select(.name=="VRChatLiveTranslate.exe") | .size'],
    capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
check("附件尺寸 == 服务端 size（排除下载被截断）",
      bool(_want_size) and exes and exes[0].stat().st_size == int(_want_size),
      f"服务端 {_want_size or '?'}B / 本地 {exes[0].stat().st_size if exes else 0:,}B")

exe = exes[0]
mine = sha256(exe)
# 与 GitHub 的 asset digest 对账：这份是 GitHub 服务端对上传字节算出来的，
# 不经过我们的 CI，才真正能证明「下载到的 === 当初发布的那份」。
_api = subprocess.run(
    ["gh", "api", f"repos/{REPO_SLUG}/releases/tags/{TAG}",
     "--jq", f'.assets[] | select(.name=="{exe.name}") | .digest'],
    capture_output=True, text=True, encoding="utf-8", errors="replace")
declared = (_api.stdout or "").strip().removeprefix("sha256:").lower()
check("sha256 实测 == GitHub 服务端 digest", bool(declared) and declared == mine,
      f"\n     GitHub: {declared or (_api.stderr or '取不到 digest').strip()[:120]!r}"
      f"\n     实测:   {mine}")

# ---- 主更新源（Cloudflare R2）与 GitHub 附件必须是同一份字节 -------------------------------
# 客户端 v0.11.3 起**主要从 R2 收更新**（GitHub 只是兜底），于是「发布出去的东西」有了两个落点，
# 而本脚本前面复核的只是 GitHub 那份 —— 两边对不上，用户从主源拿到的就不是我们验过的字节。
# 三层判据：清单版本 == 本次 tag；清单里每个产物的 sha256 == GitHub 的服务端 digest；
# 公开域上那个对象的字节数 == GitHub 附件 size（Range 探针 1 字节拿 total，不整下）。
_gh_assets: dict[str, tuple[int, str]] = {}
for _line in subprocess.run(
        ["gh", "api", f"repos/{REPO_SLUG}/releases/tags/{TAG}",
         "--jq", '.assets[] | "\\(.name) \\(.size) \\(.digest)"'],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.splitlines():
    _p = _line.split()
    if len(_p) == 3:
        _gh_assets[_p[0]] = (int(_p[1]), _p[2].removeprefix("sha256:").lower())

_r2m = r2_manifest()
if _r2m is None:
    skipped.append(f"R2 清单取不到（{R2_BASE}/{R2_MANIFEST_KEY}）：主更新源这一次没被复核")
else:
    check("R2 清单版本 == 本次 tag", str(_r2m.get("version")) == TAG.lstrip("v"),
          f"清单 {_r2m.get('version')!r} / tag {TAG!r}")
    _r2_assets = _r2m.get("assets") if isinstance(_r2m.get("assets"), dict) else {}
    check("R2 清单里两个平台的产物都在",
          {EXE_ASSET_NAME, APPIMAGE_ASSET_NAME} <= set(_r2_assets), f"{sorted(_r2_assets)}")
    for _name, _entry in _r2_assets.items():
        if not isinstance(_entry, dict):
            # 清单被写坏时给「判红」而不是 AttributeError 崩掉（评审 §三）
            check(f"{_name}：清单条目是个对象", False, f"拿到的是 {type(_entry).__name__}")
            continue
        _want = _gh_assets.get(_name)
        if not _want:
            check(f"{_name}：GitHub Release 里有同名附件", False, "R2 清单里这个附件在 GitHub 上不存在")
            continue
        _sha = str(_entry.get("sha256") or "").lower()
        _total, _how = r2_probe_size(str(_entry.get("url") or ""))
        if _name == SUMS_ASSET_NAME:
            # 这个文件不按字节跟 GitHub 那比：R2 那份自 2026-10 起是**两个平台各一行**，
            # 而**已发布**的旧 Release（v0.11.2 及更早）附件里仍是一行 —— 旧附件改不了
            # （发布动作要维护者点头），所以「R2 与 GitHub 逐字节相等」在这份文件上不再成立。
            # 真正要守的两条在下面单独判（两个平台的 sha256 都要在、且等于清单里的值），
            # 而清单里那两个值又各自与 GitHub digest 强比对 —— 内容是有传递约束的，
            # 不是"放水"。唯一不允许的方向是 R2 那份**少行**（少了某个附件的校验值）。
            check(f"{SUMS_ASSET_NAME}：R2 那份不少于 GitHub 那份（R2 可能多行）",
                  isinstance(_total, int) and _total >= _want[0],
                  f"R2 {_total}（{_how}）/ GitHub {_want[0]}"
                  + (f"；行内容不同（R2 {_sha[:12]}… / GitHub {_want[1][:12]}…）"
                     f"，属 2026-10 口径变更后的预期差异" if _sha != _want[1] else ""))
            continue
        check(f"{_name}：R2 清单 sha256 == GitHub digest", _sha == _want[1],
              f"R2 {_sha[:12]}… / GitHub {_want[1][:12]}…")
        check(f"{_name}：公开域对象字节数 == GitHub 附件 size", _total == _want[0],
              f"R2 {_total}（{_how}）/ GitHub {_want[0]}")

    # 那份人人都能下、要拿去核校验值的文件：线上这两个平台必须各占一行、值对得上
    _sums = r2_sums_lines()
    if _sums is None:
        skipped.append(f"公开域上的 {SUMS_ASSET_NAME} 取不到：校验值清单这一次没被复核")
    else:
        for _name in (EXE_ASSET_NAME, APPIMAGE_ASSET_NAME):
            _ent = _r2_assets.get(_name)
            # 只读 dict（同一轮循环前面已经对非 dict 条目报过红了，这里别崩得难看）
            _want_sha = str(_ent.get("sha256") or "").lower() if isinstance(_ent, dict) else ""
            check(f"{SUMS_ASSET_NAME} 里有 {_name} 一行且与清单一致",
                  _sums.get(_name) == _want_sha,
                  f"文件里 {(_sums.get(_name) or '缺这一行')[:12]}… / 清单 {_want_sha[:12] or '清单里没有可用的值'}…")

# 真跑一次自检
print("  跑 --self-test（可能要十几秒）…")
# ⚠️ 必须用**剥掉 API key 环境变量**的环境跑：应用解析密钥的优先级是
#    配置 → 凭据存储 → 环境变量，而环境变量优先级最低却最容易被顺手注入 ——
#    本地跑过 GUI 测试后，测试里那个假 key 可能还留在会话环境里，一跑自检就是 401
#    （实测踩过：复核因此从 9/9 掉到 7/9，白白怀疑了一遍产物）。
#    剥掉它，应用就会去用自己那份真凭据。
_env = {k: v for k, v in os.environ.items() if k != "DASHSCOPE_API_KEY"}
if _env != dict(os.environ):
    print("  （本机环境里有 DASHSCOPE_API_KEY，已剥掉后运行 —— 用应用自己保存的凭据）")
r = subprocess.run([str(exe), "--self-test"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=300, env=_env)
out = (r.stdout or "") + (r.stderr or "")
check("--self-test 退出码 0", r.returncode == 0, f"rc={r.returncode}")
check("--self-test 打出 GUI_SELFTEST_OK", "GUI_SELFTEST_OK" in out,
      out.strip().splitlines()[-1][:90] if out.strip() else "(无输出)")

# 启动日志里的版本行
# 打包 exe 的可写目录是 %APPDATA%\vrchat-livetranslate（不是 exe 旁边、也不是 LOCALAPPDATA），
# 见 vlt/paths.py：exe 旁边放 portable.txt 才会写到 exe 目录。
appdata_logs = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "vrchat-livetranslate" / "logs"
logs = sorted(WORK.rglob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
if appdata_logs.is_dir():
    logs += sorted(appdata_logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
ver = f"v{TAG.lstrip('v')}"
found_ver = None
for lg in logs[:20]:
    txt = lg.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"\[startup\]\s*版本\s*(\S+?)（([^）]*)）", txt)
    if m:
        found_ver = (m.group(1), m.group(2), lg)
        break
check("启动日志版本行 = 本次 tag 且标明打包 exe",
      bool(found_ver) and found_ver[0] == ver and "exe" in found_ver[1],
      f"{found_ver[0] if found_ver else '(没找到版本行)'}"
      f"（{found_ver[1] if found_ver else ''}）")

# 新增功能真在产物里。
# ⚠️ 不能直接 grep exe 原始字节：PyInstaller 把 .py 编译后**压缩**塞进 PYZ 归档，
#    源码字符串一个字都搜不到（实测连 v0.0.1 就有的「西班牙语」「赞助」也是 0 命中，
#    拿它当判据会得出「功能没打包进去」的假结论）。必须先解包再搜。
def _needles_via_embedded_pyz(target: Path, needles: list[str]) -> tuple[dict[str, list[str]], str]:
    """回落到**进程内**读内嵌 PYZ（不依赖外部工具）：返回 (needle → 命中模块, `vlt` 模块的字符串块)。

    为什么要有这条：`pyinstxtractor-ng` 是外部工具，解包步骤**偶发不产出 pyc**
    （实测一次：同一份产物、同一命令，这一轮复核「0 个 pyc 里命中」→ 六个 needle 全红，
    而包本身是好的 —— 我用这条回落路径在同一份下载物上六个 needle 全命中）。
    工具链抖动不该被读成「发布缺陷」，所以这里用仓库自己的依赖（PyInstaller 自带的归档读取器）
    再扫一遍。判据与原路径一致：模块字节码里的常量 / 名字里搜 needle，注释不算数。
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    ar = CArchiveReader(str(target))
    pyz_name = next((n for n in ar.toc if n.lower().endswith("pyz.pyz")), None)
    if not pyz_name:
        raise RuntimeError("产物里找不到 PYZ 条目")
    tmp = Path(tempfile.mkdtemp(prefix="verify_release_pyz_")) / "PYZ.pyz"
    tmp.write_bytes(ar.extract(pyz_name))
    z = ZlibArchiveReader(str(tmp))

    def _walk(code, acc: set) -> None:
        for c in code.co_consts:
            if isinstance(c, str):
                acc.add(c)
            elif isinstance(c, types.CodeType):
                _walk(c, acc)
        acc.update(code.co_names)

    hits: dict[str, list[str]] = {n: [] for n in needles}
    vlt_blob = ""
    for mod in list(z.toc):
        try:
            code = z.extract(mod)
        except Exception:                      # noqa: BLE001 — 单个模块读不出不该让整轮复核失败
            continue
        if not isinstance(code, types.CodeType):
            continue
        acc: set = set()
        _walk(code, acc)
        blob = "\n".join(acc)
        for n in needles:
            if n in blob:
                hits[n].append(mod)
        if mod == "vlt":                       # 包根模块 = vlt/__init__，版本号常量在这里
            vlt_blob = blob
    return hits, vlt_blob


unpacked = WORK / f"{exe.name}_extracted"
# 发现顺序：PATH → 本仓库 venv → 旧的一次性环境（历史路径，留着兜底）。
# 以前只写死最后一条，工具一旦不装在那个临时环境里，就永远「跳过=未验证」。
_cands = [
    # ⚠️ 最可信的是**跑本脚本那个解释器自己的 venv**（正常用法 = 仓库的 .venv）：
    #    放在 PATH 之前 —— PATH 上可能挂着坏掉的同名工具（实测 qrvenv 那份缺模块，会挡住好工具）。
    str(Path(sys.executable).parent / "pyinstxtractor-ng.exe"),
    shutil.which("pyinstxtractor-ng"),
    shutil.which("pyinstxtractor-ng.exe"),
    str(ROOT / ".venv" / "Scripts" / "pyinstxtractor-ng.exe"),
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Temp/qrvenv/Scripts/pyinstxtractor-ng.exe"),
]
xtractor = next((Path(p) for p in _cands if p and Path(p).exists()), None)
if xtractor is None:
    # 缺工具 = **未验证**，不是发布缺陷 —— 别记成 ❌（那会让整轮复核看着像产物坏了）。
    print("  ⚠️ 跳过「exe 内含新增符号」核对：找不到 pyinstxtractor-ng")
    print("     装它：python -m pip install pyinstxtractor-ng")
    for _nd in NEEDLES:
        skipped.append(f"exe 内含「{_nd}」（缺 pyinstxtractor-ng，本轮未验证）")
else:
    # ⚠️ 这个工具**没有** -o 选项（只有 filename / -d / -i），传 -o 会被它忽略参数直接失败；
    #    而且它按 **cwd** 落产物（`<exe名>_extracted/`），必须用 cwd= 指定目录，
    #    否则会把上千个 pyc 解到仓库工作区里。两处都踩过，所以这里校验返回码 + 固定 cwd。
    # ⚠️ 再加一层：它偶发「跑完却没有 pyc」。这一版做了**重试一次 + 回落到进程内 PYZ 读取器**，
    #    两条都不成时记「未验证」，**不记 ❌**（工具链抖动 ≠ 产物缺陷，见 _needles_via_embedded_pyz）。
    # ⚠️ 候选要**逐个真试**：PATH 上可能挂着一个**坏掉的**同名工具（实测本机
    #    `%LOCALAPPDATA%\Temp\qrvenv\Scripts\pyinstxtractor-ng.exe` 缺模块 →
    #    `ModuleNotFoundError: No module named 'pyinstxtractor_ng'` → rc=1，
    #    而它排在候选表最前，把仓库 venv 里的好工具整个挡住 —— 这才是本轮
    #    「六个 needle 全红」的真因（不是产物问题，也不是「解包跑完没产出」）。
    #    判据 = **真跑出 pyc**；失败时把工具自己的输出尾巴打出来，下次不用猜。
    # ⚠️ 紧跟 `--self-test` 之后那一下也可能失败（exe 刚被加载过）→ 每个候选失败后隔 3 秒再试一次。
    pycs: list[Path] = []
    attempts: list[str] = []
    r = None
    for cand in [Path(p) for p in _cands if p and Path(p).exists()]:
        for attempt in (1, 2):
            r = subprocess.run([str(cand), exe.name], cwd=str(WORK), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=900)
            pycs = [p for p in unpacked.rglob("*.pyc")] if unpacked.is_dir() else []
            attempts.append(f"{Path(cand).name}#{attempt}:rc={r.returncode}/pyc={len(pycs)}")
            if pycs:
                break
            if attempt == 1:
                tail = [ln for ln in ((r.stderr or "") + (r.stdout or "")).strip().splitlines() if ln.strip()]
                for ln in tail[-2:]:
                    print(f"      {Path(cand).name} 输出｜{ln[:150]}")
                time.sleep(3.0)
        if pycs:
            break
    print("  解包工具尝试：" + " | ".join(attempts))
    if pycs:
        check("pyinstxtractor-ng 解包成功", True,
              f"rc={r.returncode}，{len(pycs)} 个 pyc（{' | '.join(attempts[-1:])}）")
        for nd in NEEDLES:
            hits = [p.relative_to(unpacked).as_posix() for p in pycs
                    if nd.encode("utf-8") in p.read_bytes()]
            check(f"exe 内含新增的「{nd}」（解包后在字节码里搜到）", bool(hits),
                  f"{len(pycs)} 个 pyc 里命中：{hits[:3]}")
        # 顺带核：包内版本号是这次的、不是上一个版本的残留
        init = next((p for p in pycs if p.name == "__init__.pyc" and "vlt" in p.as_posix()), None)
        if init is not None:
            blob = init.read_bytes()
            want = TAG.lstrip("v").encode()
            check("包内 vlt/__init__.pyc 版本号 = 本次 tag", want in blob and b"0.0.1" not in blob,
                  f"含 {TAG.lstrip('v')}：{want in blob}；仍含 0.0.1：{b'0.0.1' in blob}")
    else:
        print(f"  ⚠️ 所有候选解包工具都没跑出 pyc（{' | '.join(attempts) or '无可用工具'}）"
              f"→ 回落到进程内 PYZ 读取器再扫一遍")
        try:
            pyz_hits, vlt_blob = _needles_via_embedded_pyz(exe, NEEDLES)
        except Exception as exc:               # noqa: BLE001 — 回落路径也失败 = 未验证，不是缺陷
            print(f"  ⚠️ 回落扫描也没成：{exc}")
            for nd in NEEDLES:
                skipped.append(f"exe 内含「{nd}」（解包与回落扫描都没成功，本轮未验证）")
        else:
            for nd in NEEDLES:
                check(f"exe 内含新增的「{nd}」（回落：进程内 PYZ 读取器）", bool(pyz_hits.get(nd)),
                      f"命中模块：{pyz_hits.get(nd, [])[:3]}")
            want_s = TAG.lstrip("v")
            check("包内 vlt 模块版本号 = 本次 tag（回落路径）",
                  want_s in vlt_blob and "0.0.1" not in vlt_blob,
                  f"含 {want_s}：{want_s in vlt_blob}；仍含 0.0.1：{'0.0.1' in vlt_blob}")

# 图标资源比对（SHGetFileInfoW 取图标 → 画到 DIB → 与 assets/app.ico 同尺寸帧比像素）
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
                ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", wintypes.WCHAR * 260),
                ("szTypeName", wintypes.WCHAR * 80)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


# ⚠️ 句柄类参数**必须**声明原型：Windows 64 位下 HICON / HDC / HBITMAP / HGDIOBJ 是指针，
#    不声明时 ctypes 按 32 位 C int 传 → `ctypes.ArgumentError: argument 4: OverflowError:
#    int too long to convert`（实测就在 DrawIconEx 上炸；HICON 的实际值形如 18446744071929928539）。
#    同类坑：GetCurrentThread() 的伪句柄 -2 也会因此被截断，只是那次表现为「静默无效」。
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DrawIconEx.restype = wintypes.BOOL
user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HICON,
                              wintypes.UINT]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def hicon_to_rgba(hicon, size: int) -> Image.Image:
    hdc_screen = user32.GetDC(0)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, size, size)
    old = gdi32.SelectObject(hdc, bmp)
    user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, 0x0003)  # DI_NORMAL
    bi = BITMAPINFOHEADER()
    bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(bi), size, -size
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(size * size * 4)
    gdi32.GetDIBits(hdc, bmp, 0, size, ctypes.byref(buf), ctypes.byref(bi), 0)
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(0, hdc_screen)
    return Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1).convert("RGBA")


shell32.SHGetFileInfoW.restype = ctypes.c_void_p
shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.POINTER(SHFILEINFOW), wintypes.UINT, wintypes.UINT]
user32.DestroyIcon.argtypes = [wintypes.HICON]


def grab_exe_icon(exe_path: str, attempts: int = 5, delay: float = 1.0):
    """取 exe 的图标句柄，失败就重试。

    ⚠️ 这一步会**瞬时**失败：`--self-test` 刚把 exe 加载过，紧接着调 SHGetFileInfoW 会
    「返回成功但 hIcon 为空」。这跟产物好坏无关 —— v0.4.1 发版时就因此误报过一次，
    白查了一轮，所以这里必须重试，别把瞬时失败当成「包坏了」。
    返回 (hIcon 或 None, 尝试次数, 最后一次的返回值)。
    """
    shfi = SHFILEINFOW()
    got = 0
    for i in range(attempts):
        got = shell32.SHGetFileInfoW(exe_path, 0, ctypes.byref(shfi),
                                     ctypes.sizeof(shfi), 0x100)  # SHGFI_ICON
        if got and shfi.hIcon:
            return shfi.hIcon, i + 1, got
        time.sleep(delay)
    return None, attempts, got


# 路径必须是 Windows 原生形式（反斜杠）：正斜杠同样取不到图标。
exe_native = os.path.normpath(os.path.abspath(str(exe)))
hicon, icon_tries, icon_got = grab_exe_icon(exe_native)
ico_path = ROOT / "assets" / "app.ico"
if hicon and ico_path.exists():
    diffs = []
    for size in (32, 16):
        img = hicon_to_rgba(hicon, size)
        ref = Image.open(ico_path)
        ref.size = (size, size)
        ref = ref.convert("RGBA")
        box = [sum(abs(a - b) for a, b in zip(img.getpixel((x, y)), ref.getpixel((x, y))))
               for x in range(size) for y in range(size)]
        diffs.append((size, sum(box) / len(box) / 4))
    user32.DestroyIcon(hicon)
    detail = "；".join(f"{s}px 平均差 {d:.2f}/255" for s, d in diffs)
    if icon_tries > 1:
        detail += f"（第 {icon_tries} 次才取到图标，前 {icon_tries - 1} 次是瞬时失败）"
    check("exe 图标 == assets/app.ico", all(d < 40 for _, d in diffs), detail)
else:
    check("exe 图标 == assets/app.ico", False,
          f"重试 {icon_tries} 次仍取不到图标（got={icon_got}；exe={exe_native}）")

print("\n" + "=" * 64)
print(f"通过 {len(ok)} 项，失败 {len(bad)} 项"
      + (f"，跳过 {len(skipped)} 项（未验证，不算失败）" if skipped else ""))
for _sk in skipped:
    print(f"  ⚠️ 跳过：{_sk}")
for b in bad:
    print("  ❌", b)
print("下载目录保留在：", WORK)
raise SystemExit(1 if bad else 0)
