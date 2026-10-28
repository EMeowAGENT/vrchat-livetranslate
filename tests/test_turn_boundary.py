#!/usr/bin/env python
"""轮次边界（分玩家字幕「同一段反复出现 / 终版越滚越长」）的离线验收。

## 治什么（真机症状）

分玩家字幕里同一段内容被反复显示；以及最终的「译文」里带上前几句。
根因：会话层的累积缓冲只在 `response.created` 清空，而快封句（`tick()`）与
`response.done` 终结后都不清 —— 下一轮若没收到（或晚收到）`response.created`，
新句就接在旧文本后面，每来一个快照就再发一遍「旧 + 新」。

## 这里钉住

1. 早一步快封句之后，晚到的增量只发**新增部分**，不会重复旧句；
2. 同一轮里 `text.done` 与 `response.done` 只终结一次（标点差异算同一句）；
3. 同时收到 `response.text.*` 与 `response.audio_transcript.*` 时只累加一族（不翻倍）；
4. 空终版不发（不会凭空多一条空气泡）；
5. `_src_buf`（原文）跨轮清空；
6. `Merger` 兜底：终版后的「旧前缀 + 新增」只发新增；重复终版丢弃。

跑法：.venv/bin/python tests/test_turn_boundary.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.output.merger import FINAL_DEDUPE_WINDOW_S, Merger  # noqa: E402
from vlt.session.base import SessionConfig, TextDelta, normalize_text  # noqa: E402
from vlt.session.qwen38 import QwenLiveTranslateSession  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


class Collector:
    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []   # (text, final, source)

    def __call__(self, delta: TextDelta) -> None:
        self.items.append((delta.confirmed, delta.is_final, delta.source))

    def finals(self) -> list[str]:
        return [text for text, final, _src in self.items if final]

    def texts(self) -> list[str]:
        return [text for text, _final, _src in self.items]


def _session() -> tuple[QwenLiveTranslateSession, Collector]:
    session = QwenLiveTranslateSession(SessionConfig(api_key="x"))
    sink = Collector()
    session.on_text = sink
    return session, sink


def _event(session, etype: str, **fields) -> None:
    session._handle_event({"type": etype, **fields})     # noqa: SLF001 - 测的就是这里


def _created(session) -> None:
    _event(session, "response.created", response={"id": "r"})


def test_tick_then_late_delta() -> None:
    print("[turn] 快封句之后晚到的增量")
    session, sink = _session()
    _created(session)
    _event(session, "response.text.delta", delta="それは正しい")
    _event(session, "response.text.delta", delta="ですか")
    # 让 tick 走慢路径（文字静默 ≥ 3s）
    session._last_text_at = time.perf_counter() - 5.0      # noqa: SLF001
    session.tick()
    check("先发出第一段终版", sink.finals() == ["それは正しいですか"], str(sink.finals()))

    # 服务端在这一轮里又补了半句（没有新的 response.created）
    _event(session, "response.text.delta", delta="ちょっと")
    _event(session, "response.text.done", text="それは正しいですか。ちょっと")
    finals = sink.finals()
    check("第二段终版只含新增部分", finals[-1] == "ちょっと", str(finals))
    check("没有把第一句再发一遍",
          not any("それは" in f and "ちょっと" in f for f in finals), str(finals))
    check("总共只终结两次", len(finals) == 2, str(finals))


def test_done_and_response_done() -> None:
    print("[turn] 同一轮的两条终结路径")
    session, sink = _session()
    _created(session)
    _event(session, "response.text.delta", delta="それは正しいですか")
    _event(session, "response.text.done", text="それは正しいですか？")   # 标点不同
    _event(session, "response.done", response={"usage": {}})
    check("只发一次终版", len(sink.finals()) == 1, str(sink.finals()))
    check("终版内容正确", sink.finals()[0].startswith("それは正しいですか"),
          str(sink.finals()))


def test_no_empty_final() -> None:
    print("[turn] 空终版不发")
    session, sink = _session()
    _created(session)
    _event(session, "response.text.delta", delta="はい")
    session._last_text_at = time.perf_counter() - 5.0      # noqa: SLF001
    session.tick()
    _event(session, "response.done", response={})          # 没有新内容
    check("只有一条终版", len(sink.finals()) == 1, str(sink.finals()))
    check("没有空气泡", all(t.strip() for t, _f, _s in sink.items), str(sink.items))


def test_dual_stream_not_doubled() -> None:
    print("[turn] text / audio_transcript 双流只认一族")
    session, sink = _session()
    _created(session)
    _event(session, "response.text.delta", delta="こんにちは")
    _event(session, "response.audio_transcript.delta", delta="こんにちは")
    session._last_text_at = time.perf_counter() - 5.0      # noqa: SLF001
    session.tick()
    check("文本没有翻倍", sink.finals() == ["こんにちは"], str(sink.finals()))


def test_source_resets_per_turn() -> None:
    print("[turn] 原文（_src_buf）跨轮清空")
    session, sink = _session()
    _created(session)
    _event(session, "conversation.item.input_audio_transcription.completed",
           transcript="第一句原文")
    _event(session, "response.text.delta", delta="第一句译文")
    session._last_text_at = time.perf_counter() - 5.0      # noqa: SLF001
    session.tick()

    _created(session)
    _event(session, "conversation.item.input_audio_transcription.completed",
           transcript="第二句原文")
    _event(session, "response.text.delta", delta="第二句译文")
    session._last_text_at = time.perf_counter() - 5.0      # noqa: SLF001
    session.tick()
    sources = [src for _t, final, src in sink.items if final]
    check("第二句原文不含第一句", sources[-1] == "第二句原文", str(sources))


def test_same_word_next_turn_allowed() -> None:
    print("[turn] 下一轮真的重复同一句要照发")
    session, sink = _session()
    for _ in range(2):
        _created(session)
        _event(session, "response.text.delta", delta="はい")
        session._last_text_at = time.perf_counter() - 5.0  # noqa: SLF001
        session.tick()
    check("两轮各发一次", sink.finals() == ["はい", "はい"], str(sink.finals()))


def test_merger_dedupe() -> None:
    print("[merger] 终版后的重复 / 旧前缀")
    out: list[tuple[str, bool]] = []
    merger = Merger(lambda text, final: out.append((text, final)), interval_s=0.0)
    merger.push(TextDelta(confirmed="それは正しいですか", is_final=False))
    merger.push(TextDelta(confirmed="それは正しいですか", is_final=True))
    before = len(out)
    # 同一句重复终结（标点不同）→ 丢弃
    merger.push(TextDelta(confirmed="それは正しいですか？", is_final=True))
    check("重复终版被丢弃", len(out) == before and merger.deduped_finals == 1,
          f"{out} dedup={merger.deduped_finals}")
    # 「旧前缀 + 新增」的非终版快照 → 只发新增
    merger.push(TextDelta(confirmed="それは正しいですか ちょっと", is_final=False))
    check("剥掉旧前缀只发新增", out[-1][0] == "ちょっと", str(out[-1]))
    check("或者整段等于旧终版时丢弃", merger.trimmed_prefixes == 1,
          str(merger.trimmed_prefixes))
    # 窗口外不再剥（10 秒后是真的新句子）
    merger._last_final_at -= FINAL_DEDUPE_WINDOW_S + 1      # noqa: SLF001
    merger.push(TextDelta(confirmed="それは正しいですか 新しい文", is_final=False))
    check("窗口外不再误剥", out[-1][0] == "それは正しいですか 新しい文", str(out[-1]))
    check("normalize_text 去标点", normalize_text(" はい。 ") == "はい")


def main() -> int:
    test_tick_then_late_delta()
    test_done_and_response_done()
    test_no_empty_final()
    test_dual_stream_not_doubled()
    test_source_resets_per_turn()
    test_same_word_next_turn_allowed()
    test_merger_dedupe()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {', '.join(FAILS)}")
        return 1
    print("ALL PASSED（轮次边界）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
