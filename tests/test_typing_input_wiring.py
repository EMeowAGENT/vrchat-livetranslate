"""打字输入**真实接线**验收：开始翻译后 engines_ref 必须指向当前引擎。

## 真问题（#110 / #113 都没抓到的第二层）

`gui_chat.send_typed()` 从 `ChatCtx.engines_ref` 取引擎：

    targets = [e for e, dirn in zip(engines, engine_dirs) if dirn == "mine"]

而 `gui_engine.start()` / `stop()` 会把引擎列表**整体换新**（`ctx.engines = []`）。
`engines_ref` 只在 `gui._poll()` 的第一跳里由 `setup_poll_ctx()` 绑定一次；
之后 `gui_chat.poll()` 重排下一跳时直接调模块级 `poll`，**不再走** `setup_poll_ctx`。
于是 `engines_ref` 永远停在启动前的空列表：

* 点「开始翻译」→ 引擎在跑、语音/chatbox 都正常；
* 打字→回车/发送 → `targets=[]` → 静默不发送，输入框里的字也不清空；
* Windows / Linux、源码 / exe / AppImage **全覆盖**，与平台无关。

`tests/test_typing_input.py` 是假绿：它手工塞了 `ctx.engines_ref = [fake]`，
没有走真实的 `_start()` → poll 接线。

## 这里钉住什么

1. `_start()` 之后**立刻**（不等下一跳 poll）就能发送；
2. poll 跑过若干跳之后仍能发送（活引用每跳刷新）；
3. 真实的「发送」按钮 command 也走同一条路；
4. `_stop()` 之后引用跟着清空，不会把文字发给已停止的引擎。

故障注入：把 `_unsync_engine_ctx()` 里回写 `_chat_ctx.engines_ref` 的两行删掉，
再让 `poll()` 不刷新活引用 → 第 1 条立刻变红。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

# 断言中文状态/日志口径：CI 可能是英文系统，钉死语言。
import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

# 绝不读开发者本机的 config.yaml / API key。
from _cfgbox import sandbox_config  # noqa: E402

sandbox_config(reset=True)

from vlt import credentials  # noqa: E402

_KEY_STORE = Path(tempfile.mkdtemp(prefix="vlt-typing-wiring-key-"))
credentials._storage_dir_override = lambda: _KEY_STORE
credentials.save_api_key("sk" + "-ws-" + "typingwiringtest0123456789", slot="qianwen")
os.environ.pop("DASHSCOPE_API_KEY", None)

import vlt.gui_engine as gui_engine  # noqa: E402
import vlt.gui_chat as gui_chat  # noqa: E402
from vlt.gui import TranslationGUI  # noqa: E402
from vlt.gui_chat import _submit_typed  # noqa: E402


class _FakeEngine:
    """只实现接线用到的接口；不碰音频、网络、真实 API。"""

    instances: list["_FakeEngine"] = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self.running = False
        self.sent: list[str] = []
        self.chatbox = None
        self.direction = kw.get("direction", "mine")
        _FakeEngine.instances.append(self)

    def start(self) -> None:
        self.running = True

    def send_text(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def request_stop(self) -> None:
        self.running = False

    def wait_stopped(self, _timeout: float) -> bool:
        return True

    def set_chatbox_enabled(self, _want: bool) -> bool:
        return True

    def set_languages(self, _src: str, _tgt: str) -> None:
        pass

    def rebind_audio_sink(self, *_a, **_kw) -> None:
        pass


def _pump(gui, seconds: float) -> None:
    """推动 Tk 事件循环（让 after(50) 的 poll 真正跑起来）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        gui._root.update()
        time.sleep(0.01)


def _close_gui(gui: TranslationGUI) -> None:
    """取消挂起的 after 任务再销毁：否则下一条用例初始化时会刷 `invalid command name`。"""
    _pump(gui, 0.12)                       # 消化 build 期 60ms 的 after（_apply_dark_titlebar）
    try:
        gui_chat.cancel_poll(gui._chat_ctx, gui._root)
    except Exception:                          # noqa: BLE001 — 清理失败不该盖住断言
        pass
    for attr in ("_update_check_job", "_relayout_job", "_gate_save_job", "_start_job"):
        job = getattr(gui, attr, None)
        if job:
            try:
                gui._root.after_cancel(job)
            except Exception:                  # noqa: BLE001
                pass
    try:
        gui._root.update()
    except Exception:                          # noqa: BLE001
        pass
    gui._root.destroy()


def _open_gui() -> TranslationGUI:
    _FakeEngine.instances.clear()
    gui = TranslationGUI()
    # 身份/网络/设备相关的回调一律替掉：这个用例只验证「引擎列表接线」。
    gui._overlay_var.set(False)
    gui._desktop_var.set(False)
    gui._chatbox_var.set(True)
    gui._vmic_var.set(False)
    gui._direction_var.set("mine")
    c = gui._engine_ctx
    c.refresh_api_key_fn = lambda: None
    c.sync_gate_probe_fn = lambda: None
    c.stop_gate_probe_fn = lambda: None
    c.start_room_fn = lambda: None
    c.stop_room_fn = lambda: None
    return gui


def test_start_then_send_immediately() -> None:
    """_start() 之后不等 poll：回车入口必须立刻能看到 mine 腿。"""
    gui = _open_gui()
    try:
        gui._start()
        assert len(gui._engines) == 1, f"启动后应有 1 条腿，实际 {gui._engines}"
        engine = gui._engines[0]
        ctx = gui._chat_ctx
        assert ctx.engines_ref is gui._engines, (
            "开始翻译后 engines_ref 没跟到新列表："
            f"{ctx.engines_ref!r} is not {gui._engines!r}")
        ctx.text_var.set("立即发送")
        _submit_typed(ctx)
        assert engine.sent == ["立即发送"], f"引擎没收到文字：{engine.sent}"
        assert ctx.text_var.get() == "", "发送成功后输入框应清空"
        print("  开始翻译后立刻发送 OK（engines_ref 已同步）")
    finally:
        _close_gui(gui)


def test_send_after_poll_ticks_and_button() -> None:
    """poll 跑过十几跳后仍能发；真实按钮 command 同一条路。"""
    gui = _open_gui()
    try:
        gui._start()
        _pump(gui, 0.35)                      # 至少 6 跳 poll
        ctx = gui._chat_ctx
        engine = gui._engines[0]
        assert ctx.engines_ref is gui._engines, (
            "poll 之后 engines_ref 仍是旧列表："
            f"{ctx.engines_ref!r} is not {gui._engines!r}")
        ctx.text_var.set("轮询后")
        _submit_typed(ctx)
        assert engine.sent == ["轮询后"], engine.sent
        ctx.text_var.set("按钮发送")
        ctx.send_btn.invoke()                 # 真实按钮 command
        assert engine.sent == ["轮询后", "按钮发送"], engine.sent
        print("  poll 多跳后回车 / 发送按钮 OK")
    finally:
        _close_gui(gui)


def test_stop_clears_refs() -> None:
    """停止后引用必须清空：不许把文字塞给已经停止的引擎。"""
    gui = _open_gui()
    try:
        gui._start()
        engine = gui._engines[0]
        gui._stop()
        ctx = gui._chat_ctx
        assert ctx.engines_ref == [], f"停止后 engines_ref 未清空：{ctx.engines_ref}"
        ctx.text_var.set("停止后")
        _submit_typed(ctx)
        assert engine.sent == [], "停止后仍把文字发给了引擎"
        print("  停止后引用清空、不再发送 OK")
    finally:
        _close_gui(gui)


def main() -> int:
    old_engine = gui_engine.Engine
    old_overlay = gui_engine.start_overlay
    old_desktop = gui_engine.start_desktop
    old_ear = gui_engine.apply_ear_monitor_policy
    gui_engine.Engine = _FakeEngine
    gui_engine.start_overlay = lambda ctx, force=False: None
    gui_engine.start_desktop = lambda ctx, force=False: None
    gui_engine.apply_ear_monitor_policy = lambda ctx: None
    try:
        print("test_typing_input_wiring:")
        test_start_then_send_immediately()
        test_send_after_poll_ticks_and_button()
        test_stop_clears_refs()
        print("ALL PASSED")
        return 0
    finally:
        gui_engine.Engine = old_engine
        gui_engine.start_overlay = old_overlay
        gui_engine.start_desktop = old_desktop
        gui_engine.apply_ear_monitor_policy = old_ear
        credentials._storage_dir_override = None


if __name__ == "__main__":
    raise SystemExit(main())
