"""新 TTS 端点（Qwen-Audio-TTS）的 **多行 JSON SSE** 解析验收。

## 报文长什么样（实测）

    id:1
    event:result
    :HTTP_STATUS/200
    data:{
    data:  "output" : {
    data:    "type" : "sentence-synthesis",
    data:    "audio" : { "data" : "<base64 PCM>" }
    data:  }
    data:}

一个事件的 JSON 被拆成**多行**、每行都以 `data:` 起头，**只有空行才代表事件结束**。

## 为什么单钉这一条

老实现是「逐行 `json.loads`」—— 在旧端点上正好一行一个 JSON，能跑。新端点每行都不是
合法 JSON，逐行解析会把**每一个分片都静默丢掉**（全被 `except: continue` 吞），然后落到
「一个分片都没拿到 → 退回整段合成」：功能不报错、只是首包从 ~0.8s 退回 ~1.7s。
这种「没坏但变慢」的漂移最难查，所以用例直接喂真实形状的报文，并做故障注入
（把解析改回逐行 → 本用例必须红）。

## 一起验的边界

- `sentence-begin` 事件的 `audio.data` 为空 → 跳过（它不是音频分片，不能当空块 yield）；
- `id:` / `event:` / `:HTTP_STATUS/200` 注释行 → 跳过；
- 流**不以空行结尾**时，最后一片也要交出来（否则整句丢尾巴）；
- 正常路径**不许**打「退回整段合成」那行（打了就说明流式其实没work）。

离线可跑：假 opener 返回构造好的 SSE 字节，不连网络、不需要 key。
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import vlt.tts as tts  # noqa: E402


# ================================================================ 假 HTTP 层

class _Resp:
    """SSE 响应替身：`.headers` 声明 event-stream，可逐行迭代。"""

    def __init__(self, body: bytes, ctype: str = "text/event-stream") -> None:
        self._lines = body.splitlines(keepends=True)
        self.headers = {"Content-Type": ctype}

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> bytes:
        return b"".join(self._lines)

    def close(self) -> None:
        pass


class _Opener:
    """每次请求都回同一份响应，并把请求记下来（用例要确认「没有再打第 2 次」）。"""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.reqs: list = []

    def open(self, req, timeout=None):                       # noqa: ANN001, ANN201
        self.reqs.append(req)
        return _Resp(self._body)


# ================================================================ 报文构造

def _event(obj: dict, *, eid: int = 1) -> bytes:
    """按实测形状拼一个**多行 JSON** 事件（`id:` + `event:` + `:HTTP_STATUS` + 每行 data:）。"""
    lines = [f"id:{eid}", "event:result", ":HTTP_STATUS/200"]
    for line in json.dumps(obj, ensure_ascii=False, indent=2).splitlines():
        lines.append(f"data:{line}")
    lines.append("")                     # 空行 = 一个事件结束
    return ("\n".join(lines) + "\n").encode("utf-8")


def _audio_event(pcm: bytes, *, eid: int = 1) -> bytes:
    return _event({"output": {"type": "sentence-synthesis",
                              "audio": {"data": base64.b64encode(pcm).decode()}}}, eid=eid)


def _begin_event(*, eid: int = 1) -> bytes:
    """句子开头事件：`audio.data` 为空（实测），必须跳过而不是当空分片。"""
    return _event({"output": {"type": "sentence-begin", "audio": {"data": ""}}}, eid=eid)


# ================================================================ 用例

def test_multiline_sse_yields_every_chunk() -> None:
    """真实形状的多行 JSON：三片都要解析出来，且**不许**退回整段合成。"""
    c1, c2, c3 = b"\x11\x12" * 2400, b"\x21\x22" * 2400, b"\x31\x32" * 2400
    body = (_begin_event(eid=1) + _audio_event(c1, eid=2) + _audio_event(c2, eid=3)
            + _audio_event(c3, eid=4) + b"data: [DONE]\n\n")
    opener = _Opener(body)

    with contextlib.redirect_stdout(io.StringIO()) as buf:
        tts._opener = opener                                        # noqa: SLF001
        try:
            parts = list(tts.synthesize_stream("你好", api_key="sk-x", language="zh"))
        finally:
            tts._opener = None                                      # noqa: SLF001
    out = buf.getvalue()

    assert parts == [c1, c2, c3], \
        f"多行 JSON 没解析出全部分片：拿到 {len(parts)} 片 / {[len(p) for p in parts]}"
    assert len(opener.reqs) == 1, \
        f"不许退回整段（那样会多打一次请求），实际 {len(opener.reqs)} 次"
    assert "退回整段合成" not in out, f"正常路径不该出现降级留痕：{out!r}"
    print(f"  多行 JSON SSE：{len(parts)} 片全解析（各 {len(parts[0])}B）、未降级 OK")


def test_sentence_begin_is_skipped() -> None:
    """`sentence-begin` 的 audio.data 为空 → 跳过，不能当成一个空分片。"""
    body = _begin_event(eid=1) + _audio_event(b"\x41\x42" * 100, eid=2) + _begin_event(eid=3)
    opener = _Opener(body)
    with contextlib.redirect_stdout(io.StringIO()):
        tts._opener = opener                                        # noqa: SLF001
        try:
            parts = list(tts.synthesize_stream("你好", api_key="sk-x"))
        finally:
            tts._opener = None                                      # noqa: SLF001
    assert parts == [b"\x41\x42" * 100], f"begin 事件没跳过 / 分片不对：{parts!r}"
    print("  sentence-begin（空 audio.data）被跳过 OK")


def test_last_event_without_trailing_blank_line() -> None:
    """流不以空行结尾（网关提前关连接）→ 最后一片也要交出来，别丢尾巴。"""
    body = _begin_event(eid=1) + _audio_event(b"\x51\x52" * 50, eid=2)
    body = body.rstrip(b"\n")                     # 去掉结尾空行
    opener = _Opener(body)
    with contextlib.redirect_stdout(io.StringIO()):
        tts._opener = opener                                        # noqa: SLF001
        try:
            parts = list(tts.synthesize_stream("你好", api_key="sk-x"))
        finally:
            tts._opener = None                                      # noqa: SLF001
    assert parts == [b"\x51\x52" * 50], f"结尾没有空行时最后一片丢了：{parts!r}"
    print("  结尾缺空行时残余事件仍交付（不丢尾巴）OK")


def test_parser_ignores_comment_and_control_lines() -> None:
    """`id:` / `event:` / `:HTTP_STATUS/200` 不许混进 JSON 正文（混进去就解析失败）。"""
    pcm = b"\x61\x62" * 60
    lines = [
        "id:9", "event:result", ":HTTP_STATUS/200",
        'data:{"output":',
        'data:  {"type": "sentence-synthesis", "audio": {"data": "'
        + base64.b64encode(pcm).decode() + '"}}',
        "data:}",
        "",
    ]
    body = ("\n".join(lines) + "\n").encode("utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        tts._opener = _Opener(body)                                 # noqa: SLF001
        try:
            parts = list(tts.synthesize_stream("你好", api_key="sk-x"))
        finally:
            tts._opener = None                                      # noqa: SLF001
    assert parts == [pcm], f"控制行混入导致解析失败：{parts!r}"
    print("  id/event/注释行不影响 JSON 拼装 OK")


if __name__ == "__main__":
    print("test_tts_sse_multiline:")
    test_multiline_sse_yields_every_chunk()
    test_sentence_begin_is_skipped()
    test_last_event_without_trailing_blank_line()
    test_parser_ignores_comment_and_control_lines()
    print("ALL PASSED")
