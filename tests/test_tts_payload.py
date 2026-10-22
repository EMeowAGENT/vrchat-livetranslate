"""迁移到 Qwen-Audio-TTS 后的**请求体口径**验收（字段名/类型/默认值/语言缺口）。

## 迁移规格（千问云 2026-07-14 起主推的离线 TTS 家族）

    端点  /api/v1/services/audio/tts/SpeechSynthesizer   （由 endpoints.tts_url 派生）
    模型  qwen-audio-3.1-tts-flash                       （3.1 才有四个多语音色）
    音色  longanfengyue_v3.1                             （女声，日语在列）
    请求  {"model": ..., "input": {"text", "voice", "format", "sample_rate", "language"}}
          format=wav（整段，下游 miniaudio 要容器）／pcm（流式，分片是裸 PCM）
          sample_rate=24000
          language 是**短码**（zh/en/ja…），字段名不是老的 `language_type`

## 为什么单钉这几条

字段名或格式错一处，服务端会回 400，而报错体是空的（`{}`）→ 只能靠用例守住：
`language_type` 写回去、`format` 漏了、`sample_rate` 不是 24k、流式误用 wav
（分片带 RIFF 头会被当 PCM 播成噪声）—— 任何一条都是「功能看着跑了、声音不对」。

## 语言缺口（必须可见，不许静默）

3.1 四个多语音色的官方覆盖：zh/en/ja/ko/fr/de/pt/it/vi/id。
ru/es/th 等**不在列表**（老 Cherry 是含 ru/es 的）→ 仍合成，但每种语言每进程打一行 WARN。
这里验「打一次、不刷屏」，以及官方覆盖内的语言**不打**。

离线可跑：假 opener 抓请求对象；`_decode_to_24k_mono` 换成直通，不碰 miniaudio/网络。
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import vlt.tts as tts  # noqa: E402
from vlt import endpoints  # noqa: E402
from vlt.voices import TTS_VOICES  # noqa: E402

MULTI_VOICES = ("longanfengyue_v3.1", "longanhuan_v3.1",
                "longanlingxin_v3.1", "xunanchuan_v3.1")


# ================================================================ 假 HTTP 层

class _Resp:
    def __init__(self, body: bytes, ctype: str = "application/json") -> None:
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        return iter(self._body.splitlines(keepends=True))

    def close(self) -> None:
        pass


class _Opener:
    """回一个合法响应，并把请求对象留下（用例从 req.data 里读请求体）。"""

    def __init__(self, body: bytes, ctype: str = "application/json") -> None:
        self._body, self._ctype = body, ctype
        self.req = None
        self.reqs: list = []

    def open(self, req, timeout=None):                       # noqa: ANN001, ANN201
        self.req = req
        self.reqs.append(req)
        return _Resp(self._body, self._ctype)


def _integral_body() -> bytes:
    return json.dumps({"output": {"audio": {"data": base64.b64encode(b"WAVDATA").decode()}}}).encode()


def _sse_body(pcm: bytes) -> bytes:
    """一个多行 JSON 的 SSE 事件（新端点的真实形状）。"""
    obj = {"output": {"type": "sentence-synthesis",
                      "audio": {"data": base64.b64encode(pcm).decode()}}}
    lines = ["id:1", "event:result", ":HTTP_STATUS/200"]
    lines += [f"data:{ln}" for ln in json.dumps(obj, indent=2).splitlines()]
    lines.append("")
    return ("\n".join(lines) + "\n").encode()


class _Patch:
    def __init__(self, **kw) -> None:                        # noqa: ANN003
        self._kw = kw
        self._old: dict = {}

    def __enter__(self):                                     # noqa: ANN204
        for name, value in self._kw.items():
            self._old[name] = getattr(tts, name)
            setattr(tts, name, value)
        tts._LANG_WARNED.clear()                             # noqa: SLF001  每个用例从零开始
        return self

    def __exit__(self, *exc):                                # noqa: ANN002
        for name, value in self._old.items():
            setattr(tts, name, value)
        tts._LANG_WARNED.clear()                             # noqa: SLF001
        return False


def _payload_of(opener: _Opener) -> dict:
    return json.loads(opener.req.data.decode("utf-8"))


# ================================================================ 用例

def test_defaults_are_migrated() -> None:
    """默认值必须是迁移后的那一套（模型/音色/端点/采样率），且音色表只放多语音色。"""
    assert tts.DEFAULT_MODEL == "qwen-audio-3.1-tts-flash", tts.DEFAULT_MODEL
    assert tts.DEFAULT_VOICE == "longanfengyue_v3.1", tts.DEFAULT_VOICE
    assert tts.ENDPOINT == ("https://maas.qianwenaiapi.com/api/v1/services/audio/tts/"
                            "SpeechSynthesizer"), tts.ENDPOINT
    assert tts.ENDPOINT == endpoints.tts_url(endpoints.default_base_url("qianwen")), \
        "tts.ENDPOINT 必须与 endpoints.tts_url 派生的结果一致（否则两条线路会各写一份域名）"
    assert tts.SAMPLE_RATE == 24000
    assert tuple(TTS_VOICES) == MULTI_VOICES, f"下拉应只放 3.1 的四个多语音色：{TTS_VOICES}"
    for v in MULTI_VOICES:
        assert v.endswith("_v3.1"), f"音色 id 版本后缀不对：{v}"
    print(f"  默认值已迁移（{tts.DEFAULT_MODEL} / {tts.DEFAULT_VOICE}）+ 音色表 4 个多语音色 OK")


def test_example_config_matches_defaults() -> None:
    """`config.example.yaml` 的 text_input.tts 段必须与新默认值同步（否则新用户一开始就错）。"""
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    ttscfg = (data.get("text_input") or {}).get("tts") or {}
    assert ttscfg.get("model") == tts.DEFAULT_MODEL, ttscfg
    assert ttscfg.get("voice") == tts.DEFAULT_VOICE, ttscfg
    print("  config.example.yaml 的 tts.model / tts.voice 与代码默认值一致 OK")


def test_integral_payload_uses_wav_and_language_code() -> None:
    """整段：`format=wav` + `sample_rate=24000` + `language` 短码（不是 language_type）。"""
    opener = _Opener(_integral_body())
    with _Patch(_get_opener=lambda: opener,                               # noqa: SLF001
                _decode_to_24k_mono=lambda raw: b"PCM:" + raw):           # noqa: SLF001
        pcm = tts.synthesize("こんにちは", api_key="k", language="ja-JP")
    inp = _payload_of(opener)["input"]
    assert _payload_of(opener)["model"] == tts.DEFAULT_MODEL
    assert inp["text"] == "こんにちは"
    assert inp["voice"] == tts.DEFAULT_VOICE
    assert inp["format"] == "wav", f"整段必须用容器格式（miniaudio 要解）：{inp}"
    assert inp["sample_rate"] == 24000
    assert inp["language"] == "ja", f"语言要取短码（ja-JP → ja）：{inp}"
    assert "language_type" not in inp, f"新端点不认 language_type：{inp}"
    assert pcm == b"PCM:WAVDATA"
    print(f"  整段请求体：format=wav / sample_rate=24000 / language={inp['language']} OK")


def test_stream_payload_uses_pcm() -> None:
    """流式：`format=pcm`（分片是裸 PCM），其余字段同一口径。"""
    opener = _Opener(_sse_body(b"\x07\x08" * 30), ctype="text/event-stream")
    with _Patch(_get_opener=lambda: opener,                               # noqa: SLF001
                _decode_to_24k_mono=lambda raw: raw):                     # noqa: SLF001
        parts = list(tts.synthesize_stream("你好", api_key="k", language="zh-CN"))
    inp = _payload_of(opener)["input"]
    assert parts == [b"\x07\x08" * 30], f"流式分片不对：{parts!r}"
    assert inp["format"] == "pcm", f"流式必须用裸 PCM（wav 会被当噪声播）：{inp}"
    assert inp["sample_rate"] == 24000
    assert inp["language"] == "zh"
    assert inp["voice"] == tts.DEFAULT_VOICE
    print("  流式请求体：format=pcm / sample_rate=24000 / language=zh OK")


def test_unknown_language_is_not_sent() -> None:
    """认不出的语言码 → 整个 `language` 字段都不传（也绝不退回 language_type）。"""
    opener = _Opener(_integral_body())
    with _Patch(_get_opener=lambda: opener,                               # noqa: SLF001
                _decode_to_24k_mono=lambda raw: raw):                     # noqa: SLF001
        tts.synthesize("hi", api_key="k", language="klingon")
    inp = _payload_of(opener)["input"]
    assert "language" not in inp, inp
    assert "language_type" not in inp, inp
    opener2 = _Opener(_integral_body())
    with _Patch(_get_opener=lambda: opener2,                              # noqa: SLF001
                _decode_to_24k_mono=lambda raw: raw):                     # noqa: SLF001
        tts.synthesize("hi", api_key="k", language=None)
    assert "language" not in _payload_of(opener2)["input"]
    print("  未知语言 / 未给语言：不带 language（且不退回 language_type）OK")


def test_lang_gap_warns_once_per_language() -> None:
    """官方不覆盖的语言（ru）留一行 WARN 且只留一次；覆盖内的（ja/en）不留痕。"""
    opener = _Opener(_integral_body())
    with _Patch(_get_opener=lambda: opener,                               # noqa: SLF001
                _decode_to_24k_mono=lambda raw: raw):                     # noqa: SLF001
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tts.synthesize("привет", api_key="k", language="ru")
            tts.synthesize("привет", api_key="k", language="ru")     # 第二次不许再刷
            tts.synthesize("こんにちは", api_key="k", language="ja")   # 覆盖内 → 不打
            tts.synthesize("hello", api_key="k", language="en")
        out = buf.getvalue()
    assert out.count("不在当前 TTS 音色官方覆盖内") == 1, f"ru 的 WARN 次数不对：{out!r}"
    assert "目标语言 ru" in out, out
    assert "目标语言 ja" not in out and "目标语言 en" not in out, f"覆盖内语言不该告警：{out!r}"
    # 语言缺口是**文档化的已知项**，不是悄悄吞掉：覆盖集本身也要在
    assert tts.TTS_SUPPORTED_LANGS == frozenset(
        {"zh", "en", "ja", "ko", "fr", "de", "pt", "it", "vi", "id"}), tts.TTS_SUPPORTED_LANGS
    assert "ru" not in tts.TTS_SUPPORTED_LANGS and "ru" in tts.LANG_CODES
    print("  ru 的已知缺口 WARN 只打一次、覆盖内语言不告警 OK")


def test_empty_http_body_gets_readable_message() -> None:
    """服务端拒收时 body 常是空的（`{}`）→ 文案必须说人话，不能是「HTTP 400：{}」。"""
    from urllib.error import HTTPError

    class _Boom:
        def open(self, req, timeout=None):                   # noqa: ANN001, ANN201
            raise HTTPError(req.full_url, 400, "Bad Request", {},
                            io.BytesIO(b"{}"))

    with _Patch(_get_opener=_Boom,                            # noqa: SLF001
                _decode_to_24k_mono=lambda raw: raw):         # noqa: SLF001
        err = None
        try:
            tts.synthesize("hi", api_key="k")
        except tts.TtsError as exc:
            err = exc
    assert err is not None, "400 应抛 TtsError"
    msg = str(err)
    assert "HTTP 400" in msg, msg
    assert "服务端拒收但未给原因" in msg and "该 TTS 模型" in msg, f"文案要能看懂：{msg!r}"
    print(f"  空 body 的报错文案可读（{msg}）OK")


if __name__ == "__main__":
    print("test_tts_payload:")
    test_defaults_are_migrated()
    test_example_config_matches_defaults()
    test_integral_payload_uses_wav_and_language_code()
    test_stream_payload_uses_pcm()
    test_unknown_language_is_not_sent()
    test_lang_gap_warns_once_per_language()
    test_empty_http_body_gets_readable_message()
    print("ALL PASSED")
