"""`scripts/run_tests.py`（仓库唯一的测试运行器）的行为守卫。

为什么要有它：run_tests.py 现在是 **CI 两个 job 的门禁本体**，它一旦「多跳了 / 吞了
失败 / 少挂了 xvfb」，CI 会**静默变绿** —— 比内联 shell 更隐蔽。所以这里钉住它的关键
判据是纯函数级的、不依赖真跑全量：

  * 用例发现 / `--only` 子串与通配；
  * `build_cmd`：Linux 上是否挂 `xvfb-run -a`、**虚拟屏是否被显式钉死**（`-s -screen 0 …`）、
    覆盖率模式参数；
  * `xvfb_screen` / `gha_enabled` 的 env 口径；
  * `Annotator`：GHA 注解开/关（开时输出 `::group::` 等，关时**一行都不发**）；
  * **`--require-tk`**：用「PYTHONPATH 上放一个会抛 ImportError 的假 tkinter」模拟缺
    tkinter —— 带 `--require-tk` 必须判红（rc=3），不带则退化为跳过 + 无匹配用例（rc=1），
    绝不静默成功。
  * **`--require-xvfb`**：用「PATH 里不含 xvfb-run」模拟缺失 —— 带 flag 判红（rc=3），
    不带才降级；且 **`PASS=0`（只剩跳过）也要判非 0**。

**不**在本用例里调 `run_tests.main()` 跑全量（那会自递归）。
"""
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run_tests.py"

# 以文件路径加载 run_tests.py（scripts/ 不是包）。执行模块级代码只有常量/函数定义，
# 没有副作用（不会跑测试）。
_spec = importlib.util.spec_from_file_location("vlt_run_tests", RUNNER)
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)

FAILED = []


def check(cond, msg):
    if cond:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
        FAILED.append(msg)


# ---------------------------------------------------------------- 发现 / 选择
def test_discover_and_select():
    tests = rt.discover()
    names = [t.name for t in tests]
    check(len(tests) > 0, f"discover() 找到 {len(tests)} 个用例")
    check(names == sorted(names), "discover() 按文件名排序（报告可复现）")
    check(rt.ENGINE_TEST in names, f"{rt.ENGINE_TEST} 在发现名单里（默认跳过由主循环处理）")

    sub = rt.select(tests, "device")
    check(all("device" in t.name for t in sub) and len(sub) >= 1,
          f"--only device（子串）命中 {len(sub)} 个")

    wild = rt.select(tests, "test_room_*")
    check(all(t.name.startswith("test_room_") for t in wild) and len(wild) >= 1,
          f"--only 'test_room_*'（通配）命中 {len(wild)} 个")

    check(rt.select(tests, None) == tests, "--only 为空时返回全部")


# ---------------------------------------------------------------- build_cmd
def test_build_cmd():
    t = ROOT / "tests" / "test_xxx.py"

    bare = rt.build_cmd(t, coverage=False, xvfb=False, screen="1920x1080x24")
    check(bare == [sys.executable, str(t)], f"裸跑命令：{bare}")

    xv = rt.build_cmd(t, coverage=False, xvfb=True, screen="1920x1080x24")
    check(xv[0] == rt.XVFB_RUN and xv[1] == "-a",
          f"xvfb 模式以 `{rt.XVFB_RUN} -a` 开头：{xv[:2]}")
    check("-s" in xv and "-screen 0 1920x1080x24" in xv,
          "xvfb 模式的虚拟屏被**显式钉死**（-s '-screen 0 <WxHxD>'）")
    # 分辨率必须是**一个** argv（否则 xvfb-run 会把它拆错）
    i = xv.index("-s")
    check(xv[i + 1] == "-screen 0 1920x1080x24", "-screen 串作为单个 argv 传入")
    check(xv[-2:] == [sys.executable, str(t)] or xv[-3:] == [sys.executable, str(t)],
          "被测命令仍在 xvfb-run 之后")

    cov = rt.build_cmd(t, coverage=True, xvfb=False, screen="1920x1080x24")
    check(cov[:4] == [sys.executable, "-m", "coverage", "run"] and "-a" in cov
          and "--source=vlt" in cov,
          "覆盖率模式走 `coverage run -a --source=vlt`")


# ---------------------------------------------------------------- env 口径
def test_env_knobs():
    saved = {k: os.environ.get(k) for k in ("VLT_XVFB_SCREEN", "GITHUB_ACTIONS")}
    try:
        os.environ.pop("VLT_XVFB_SCREEN", None)
        check(rt.xvfb_screen() == rt.DEFAULT_XVFB_SCREEN,
              f"未设 env 时 xvfb_screen() 用默认 {rt.DEFAULT_XVFB_SCREEN}")
        os.environ["VLT_XVFB_SCREEN"] = "1280x720x24"
        check(rt.xvfb_screen() == "1280x720x24", "VLT_XVFB_SCREEN 可覆盖分辨率")

        os.environ.pop("GITHUB_ACTIONS", None)
        check(rt.gha_enabled() is False, "非 GITHUB_ACTIONS 时 gha_enabled() False")
        os.environ["GITHUB_ACTIONS"] = "true"
        check(rt.gha_enabled() is True, "GITHUB_ACTIONS=true 时 gha_enabled() True")
        os.environ["GITHUB_ACTIONS"] = "TRUE"
        check(rt.gha_enabled() is True, "大小写不敏感")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------- Annotator
def test_annotator():
    import io

    on = io.StringIO()
    a = rt.Annotator(enabled=True, out=on)
    a.group("tests/test_x.py")
    a.error("测试失败", file="tests/test_x.py")
    a.notice("tests/test_y.py 已跳过（理由）")
    a.endgroup()
    out = on.getvalue()
    check("::group::tests/test_x.py" in out, "开启时发 ::group::")
    check("::error file=tests/test_x.py::测试失败" in out, "开启时发 ::error file=")
    check("::notice::" in out, "开启时发 ::notice::")
    check("::endgroup::" in out, "开启时发 ::endgroup::")

    off = io.StringIO()
    b = rt.Annotator(enabled=False, out=off)
    b.group("x")
    b.error("y", file="z")
    b.notice("w")
    b.endgroup()
    check(off.getvalue() == "", "关闭时**一行都不发**（本地保持纯文本）")


# ---------------------------------------------------------------- xvfb 决策
def test_xvfb_decision():
    check(rt.xvfb_decision(disabled=True) is False, "--no-xvfb 明确裸跑")
    decision = rt.xvfb_decision(disabled=False)
    check(isinstance(decision, bool), "xvfb_decision 返回 bool")
    if not sys.platform.startswith("linux"):
        check(decision is False, f"{sys.platform} 不挂 xvfb（自带桌面会话）")


# ---------------------------------------------------------------- tkinter 门禁
def test_tk_gui_tests():
    check(isinstance(rt.tkinter_available(), bool), "tkinter_available() 返回 bool")
    skip = rt.tk_gui_tests()
    check(len(skip) > 0, f"tk_gui_tests() 发现 {len(skip)} 个传递依赖 tkinter 的用例")
    check(all(p.startswith("tests/test_") and p.endswith(".py") for p in skip),
          "名单是 POSIX 相对路径（run_tests.py 与它做集合成员判断）")
    check("tests/test_run_tests.py" not in skip,
          "本用例（不依赖 Tk）不在 GUI 名单里")


def _run_without_tkinter(extra_args):
    """在「PYTHONPATH 上放一个抛 ImportError 的假 tkinter」的环境里跑 runner。"""
    shim = tempfile.mkdtemp(prefix="no-tk-")
    try:
        pkg = pathlib.Path(shim, "tkinter")
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            "raise ImportError('simulated: no tkinter')\n", encoding="utf-8")
        env = dict(os.environ)
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = shim + (os.pathsep + prev if prev else "")
        env.pop("GITHUB_ACTIONS", None)   # 保持纯文本，别让注解干扰断言
        return subprocess.run(
            [sys.executable, str(RUNNER), *extra_args],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
    finally:
        shutil.rmtree(shim, ignore_errors=True)


def test_require_tk_red_when_missing():
    # 带 --require-tk：缺 tkinter 必须判红（rc=3），绝不静默跳过。
    # 用 --only 造一个不存在的匹配，确保即使没走到 tk 门禁也不会真跑用例（防自递归）。
    proc = _run_without_tkinter(["--require-tk", "--only", "zzz-nonexistent"])
    check(proc.returncode == 3,
          f"--require-tk 且缺 tkinter → 判红（rc={proc.returncode}，期望 3）")
    check("tkinter" in (proc.stdout + proc.stderr),
          "判红时说明了是 tkinter 缺失")

    # 不带 --require-tk：退化为「跳过 GUI 用例」，且 --only 无匹配 → rc=1（不是静默 0）。
    proc2 = _run_without_tkinter(["--only", "zzz-nonexistent"])
    check(proc2.returncode == 1,
          f"缺 tkinter 且无 --require-tk：不硬红，走跳过（rc={proc2.returncode}，期望 1）")
    check("没有匹配的用例" in proc2.stdout,
          "无匹配用例时明确报「没有匹配」，不退化成静默成功")


def test_require_xvfb_red_when_missing():
    """缺 xvfb-run 时：带 --require-xvfb 判红；不带才降级。仅 Linux 有此语义。"""
    if not sys.platform.startswith("linux"):
        check(True, f"{sys.platform} 上 --require-xvfb 为空操作（跳过该断言）")
        return
    # 用一个空的 PATH 目录模拟「找不到 xvfb-run」；python 走绝对路径不受影响。
    empty = tempfile.mkdtemp(prefix="no-xvfb-")
    try:
        env = dict(os.environ)
        env["PATH"] = empty
        env.pop("GITHUB_ACTIONS", None)
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--require-xvfb", "--only", "zzz-nonexistent"],
            cwd=ROOT, env=env, capture_output=True, text=True)
        check(proc.returncode == 3,
              f"--require-xvfb 且缺 xvfb-run → 判红（rc={proc.returncode}，期望 3）")
        check("xvfb" in (proc.stdout + proc.stderr), "判红时说明了是 xvfb 缺失")

        proc2 = subprocess.run(
            [sys.executable, str(RUNNER), "--only", "zzz-nonexistent"],
            cwd=ROOT, env=env, capture_output=True, text=True)
        check(proc2.returncode == 1,
              f"缺 xvfb-run 且无 --require-xvfb：降级不硬红（rc={proc2.returncode}，期望 1）")
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def test_no_pass_is_red():
    """PASS=0（只剩跳过）必须判非 0，绝不静默绿。用默认跳过的 test_engine 制造。"""
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--only", "test_engine.py"],
        cwd=ROOT, capture_output=True, text=True)
    check(proc.returncode != 0,
          f"PASS=0（只剩跳过）→ 判非 0（rc={proc.returncode}）")
    check("没有任何用例真正通过" in proc.stdout,
          "并写明「没有任何用例真正通过」")


def main() -> int:
    print("== run_tests.py 行为守卫 ==")
    for fn in (
        test_discover_and_select,
        test_build_cmd,
        test_env_knobs,
        test_annotator,
        test_xvfb_decision,
        test_tk_gui_tests,
        test_require_tk_red_when_missing,
        test_require_xvfb_red_when_missing,
        test_no_pass_is_red,
    ):
        print(f"-- {fn.__name__}")
        fn()
    print()
    if FAILED:
        print(f"FAILED：{len(FAILED)} 项未通过")
        for m in FAILED:
            print(f"  - {m}")
        return 1
    print("ALL PASSED（run_tests.py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
