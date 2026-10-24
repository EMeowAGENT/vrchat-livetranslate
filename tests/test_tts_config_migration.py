"""打字译音配置迁移验收：旧模型/旧音色 + 「音色实验室」复刻模型漂移。

## 真问题

配置可能来自带「音色实验室」的分支：

```yaml
text_input:
  tts:
    model: qwen3-tts-vc-2026-01-22   # 复刻目标模型
    voice: Maia                      # 内置音色（不是复刻出来的 voice id）
```

音色实验室会把 **目标模型** 写进 `text_input.tts.model`，并把 `voice` 写成
`qwen-tts-vc-…-voice-…` 这种自定义 id。主线的内置音色下拉**只改 voice、不改
model** —— 用户把音色切回内置音色后，model 仍停在复刻模型上，合成接口直接失败
（老端点 404 `Model not exist`，新端点 400 空 body）。

这里钉住三条：

1. `load_config()`：旧模型（`LEGACY_TTS_MODELS`）/ 旧音色（`LEGACY_TTS_VOICES`）
   → 新默认并留痕；自定义 voice id + 复刻模型 → **保持原样**（那是对的复刻配置）。
2. `load_config()`：内置音色（含旧音色迁移后的默认音色）+ 复刻/设计模型
   → 回落 `DEFAULT_TTS_MODEL` 并留痕。
3. 设置里改「打字译音」音色时：选内置音色 → 把 **磁盘上** 的 model 也写回默认。

## 为什么第 3 条必须读磁盘

`load_config()` 已在**内存**里把漂移修干净了，设置窗拿的是同一份 `cfg` ——
只看内存的话，「启动时就已漂移」的用户永远不会触发落盘修复（评审 ⚠️2 实测）。
所以 `on_tts_voice_change()` 读一次 config.yaml 的 `text_input.tts.model` 再决定。
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from _cfgbox import sandbox_config  # noqa: E402

sandbox_config(reset=True)

import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

from vlt import config as _cfg  # noqa: E402
from vlt import gui_voice  # noqa: E402
from vlt.config import load_config  # noqa: E402
from vlt.voices import DEFAULT_TTS_MODEL, DEFAULT_TTS_VOICE, TTS_VOICES  # noqa: E402

CLONE_MODEL = "qwen3-tts-vc-2026-01-22"
DESIGN_MODEL = "qwen3-tts-vd-2026-01-26"
CUSTOM_VOICE = "qwen-tts-vc-MetroPolice-voice-20261003201838644-0a4b"
OTHER_BUILTIN = next(v for v in TTS_VOICES if v != DEFAULT_TTS_VOICE)


def _cfg_body(tts: dict) -> dict:
    return {
        "session": {"base_url": "wss://example.invalid/api-ws/v1/realtime"},
        "text_input": {"enabled": True, "tts": tts},
    }


def _write_cfg(tts: dict) -> Path:
    p = Path(tempfile.mkdtemp(prefix="vlt-tts-mig-")) / "config.yaml"
    p.write_text(yaml.safe_dump(_cfg_body(tts), allow_unicode=True), encoding="utf-8")
    return p


def _write_default(tts: dict) -> Path:
    """把漂移配置写到**沙箱 DEFAULT_CONFIG**（设置页读的正是这份磁盘文件）。"""
    p = sandbox_config(reset=True)
    p.write_text(yaml.safe_dump(_cfg_body(tts), allow_unicode=True), encoding="utf-8")
    return p


def _load(tts: dict):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cfg = load_config(_write_cfg(tts), api_key="sk-test", require_key=False)
    return cfg, buf.getvalue()


def _disk(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["text_input"]["tts"]


class _Var:
    def __init__(self, value: str) -> None:
        self._v = value

    def get(self) -> str:
        return self._v


def _change_voice(cfg, voice: str) -> None:
    ctx = SimpleNamespace(tts_voice_var=_Var(voice))
    gui_voice.on_tts_voice_change(ctx, cfg, set_status=lambda *_a: None)


# ---------------------------------------------------------------- load_config

def test_builtin_voice_with_clone_model_falls_back() -> None:
    cfg, out = _load({"model": CLONE_MODEL, "voice": OTHER_BUILTIN})
    got = cfg.text_input["tts"]["model"]
    assert got == DEFAULT_TTS_MODEL, f"复刻模型 + 内置音色应回落，实际 {got!r}"
    assert "不匹配" in out and CLONE_MODEL in out, f"必须留痕，实际输出：{out!r}"
    print(f"  内置音色 + 复刻模型 → {DEFAULT_TTS_MODEL}（留痕）OK")


def test_builtin_voice_with_design_model_falls_back() -> None:
    cfg, out = _load({"model": DESIGN_MODEL, "voice": OTHER_BUILTIN})
    assert cfg.text_input["tts"]["model"] == DEFAULT_TTS_MODEL
    assert "不匹配" in out, out
    print("  内置音色 + 设计模型 → 默认模型（留痕）OK")


def test_legacy_voice_with_clone_model_falls_back() -> None:
    """旧音色（Maia）+ 复刻模型：两条迁移必须叠起来，不能只换音色留下复刻模型。"""
    cfg, out = _load({"model": CLONE_MODEL, "voice": "Maia"})
    tts = cfg.text_input["tts"]
    assert tts["voice"] == DEFAULT_TTS_VOICE, tts
    assert tts["model"] == DEFAULT_TTS_MODEL, tts
    assert "旧模型的音色" in out and "不匹配" in out, out
    print("  旧音色 + 复刻模型 → 默认音色 + 默认模型（两条迁移叠加）OK")


def test_custom_voice_keeps_clone_model() -> None:
    cfg, out = _load({"model": CLONE_MODEL, "voice": CUSTOM_VOICE})
    assert cfg.text_input["tts"]["model"] == CLONE_MODEL, "自定义音色配复刻模型是合法配置，不能改"
    assert cfg.text_input["tts"]["voice"] == CUSTOM_VOICE
    assert "不匹配" not in out, out
    print("  自定义 voice id + 复刻模型 → 保持原样 OK")


def test_builtin_voice_with_default_model_unchanged() -> None:
    cfg, out = _load({"model": DEFAULT_TTS_MODEL, "voice": OTHER_BUILTIN})
    assert cfg.text_input["tts"]["model"] == DEFAULT_TTS_MODEL
    assert "不匹配" not in out, out
    print("  内置音色 + 默认模型 → 不改动 OK")


# ---------------------------------------------------------------- 设置页改音色

def test_voice_change_repairs_model_and_persists() -> None:
    """合法复刻配置切回内置音色：内存 + config.yaml 都要把 model 改回默认。"""
    path = _write_default({"model": CLONE_MODEL, "voice": CUSTOM_VOICE})
    cfg = SimpleNamespace(text_input={"tts": {"model": CLONE_MODEL, "voice": CUSTOM_VOICE}})

    statuses: list[tuple[str, str]] = []
    ctx = SimpleNamespace(tts_voice_var=_Var(OTHER_BUILTIN))
    gui_voice.on_tts_voice_change(ctx, cfg, set_status=lambda lvl, msg: statuses.append((lvl, msg)))

    assert cfg.text_input["tts"]["model"] == DEFAULT_TTS_MODEL, cfg.text_input
    assert cfg.text_input["tts"]["voice"] == OTHER_BUILTIN
    saved = _disk(path)
    assert saved["model"] == DEFAULT_TTS_MODEL, saved
    assert saved["voice"] == OTHER_BUILTIN, saved
    assert statuses and statuses[-1][0] == "info"
    print("  合法复刻配置切内置音色 → 模型改回默认并落盘 OK")


def test_drifted_at_startup_repairs_disk_on_voice_change() -> None:
    """评审 ⚠️2（A）：启动时已漂移 → load_config 只修内存 → 改音色必须把磁盘修好。"""
    path = _write_default({"model": CLONE_MODEL, "voice": "Maia"})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cfg = load_config(path, api_key="sk-test", require_key=False)

    # 前置：内存已回落，但磁盘还是复刻模型（这正是评审实测的状态）
    assert cfg.text_input["tts"]["model"] == DEFAULT_TTS_MODEL, cfg.text_input["tts"]
    assert _disk(path)["model"] == CLONE_MODEL, "用例前置不成立：磁盘一开始应是漂移值"

    _change_voice(cfg, OTHER_BUILTIN)

    saved = _disk(path)
    assert saved["model"] == DEFAULT_TTS_MODEL, f"漂移路径没落盘：{saved}"
    assert saved["voice"] == OTHER_BUILTIN, saved
    print("  启动前已漂移 → 改音色把磁盘 model 修好（评审 A）OK")


def test_drifted_at_startup_repairs_when_reselecting_default_voice() -> None:
    """评审 ⚠️2（A2）：改选另一个内置音色（这里用默认音色）同样要落盘。"""
    path = _write_default({"model": CLONE_MODEL, "voice": "Maia"})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cfg = load_config(path, api_key="sk-test", require_key=False)

    _change_voice(cfg, DEFAULT_TTS_VOICE)

    saved = _disk(path)
    assert saved["model"] == DEFAULT_TTS_MODEL, saved
    assert saved["voice"] == DEFAULT_TTS_VOICE, saved
    print("  启动前已漂移 → 重选默认音色也落盘修模型（评审 A2）OK")


def test_custom_voice_keeps_model_on_change() -> None:
    cfg = SimpleNamespace(text_input={"tts": {"model": CLONE_MODEL, "voice": OTHER_BUILTIN}})
    _write_default({"model": CLONE_MODEL, "voice": OTHER_BUILTIN})
    _change_voice(cfg, CUSTOM_VOICE)
    assert cfg.text_input["tts"]["model"] == CLONE_MODEL, "自定义音色不许被强改模型"
    assert cfg.text_input["tts"]["voice"] == CUSTOM_VOICE
    print("  改选自定义音色 → 模型保持 OK")


def test_missing_config_file_does_not_crash() -> None:
    """磁盘读不到（文件被删/路径不可用）→ 只留痕，内存照修，绝不把保存流程弄崩。"""
    old = _cfg.DEFAULT_CONFIG
    _cfg.DEFAULT_CONFIG = Path(tempfile.mkdtemp(prefix="vlt-tts-missing-")) / "nope" / "config.yaml"
    try:
        cfg = SimpleNamespace(text_input={"tts": {"model": CLONE_MODEL, "voice": OTHER_BUILTIN}})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _change_voice(cfg, OTHER_BUILTIN)
        assert cfg.text_input["tts"]["model"] == DEFAULT_TTS_MODEL, cfg.text_input
        print("  磁盘不可读 → 不崩、内存照修 OK")
    finally:
        _cfg.DEFAULT_CONFIG = old


def main() -> int:
    print("test_tts_config_migration:")
    test_builtin_voice_with_clone_model_falls_back()
    test_builtin_voice_with_design_model_falls_back()
    test_legacy_voice_with_clone_model_falls_back()
    test_custom_voice_keeps_clone_model()
    test_builtin_voice_with_default_model_unchanged()
    test_voice_change_repairs_model_and_persists()
    test_drifted_at_startup_repairs_disk_on_voice_change()
    test_drifted_at_startup_repairs_when_reselecting_default_voice()
    test_custom_voice_keeps_model_on_change()
    test_missing_config_file_does_not_crash()
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
