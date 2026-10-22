"""语言表一致性：源/目标两侧必须对称，且同一个语言码两侧叫法一致。

## 为什么要有这个测试

语言表在 `vlt/gui.py` 里是**两份独立 dict**（源语言那份多一个「自动检测」），而
「语言镜像」逻辑是**按语言码在两侧互查**的：选「中文→俄语」时，「别人说」方向要
自动变成「俄语→中文」。任何一边漏加，界面都不会报错 —— 它会**静默回落**
（`_target_name` 找不到就返回「英语」，`_source_name` 返回「自动检测」），
用户看到的现象是「我明明选了俄语，但它没生效」。这种静默回落必须被钉住。

实测背景（2026-09-25）：用户要求加俄语。模型（qwen3.8-livetranslate-flash-realtime）
两个方向都支持，实测：
  en→ru 译文 `Привет. Я посещаю ваш мир из Канады. …`
  ru→zh 译文 `你好。我来拜访你们的世界，来自加拿大。…`

实测背景（2026-10-02）：用户要求加泰语（th）。同一个模型两个方向都支持，实测：
  zh→th 译文 `สวัสดี, ฉันคือ Nixi. วันนี้เราจะมาทดสอบกัน …`
  th→zh 译文 `你好，我叫尼克西。很高兴认识你。今天天气很好。`
文本腿 `qwen-mt-flash`（zh↔th / en↔th 四条）与老 `qwen3-tts-flash` 也都能处理 th；
但迁移到 Qwen-Audio-TTS 3.1 后，th **不在四个多语音色的官方覆盖集里** —— 仍会合成、
发音质量未验证，属已知缺口（见 `vlt/tts.py` 的 `TTS_SUPPORTED_LANGS` 与下面的用例）。
泰语比俄语多一层风险：**默认 CJK 字体（微软雅黑）不含泰文字形**，界面选得到、
模型也翻得出，画到手腕屏上却是静默的豆腐块 —— 那一层由 `tests/test_thai_font.py`
用位图比对钉住，本文件只守「语言表两侧都登记了 th」。

实测背景（2026-10-04）：用户要求加意大利语（it）。**真链路实测通过** ——

  语音腿 zh→it：源「你好，我叫 SAND。今天我们测试意大利语的翻译。」→
    译文 `Ciao. Mi chiamo Sand.`（模型自带音频 2.08s；探针只取到第一个 turn 的译文，
    是探针按句喂、每个 turn 各出一段响应所致 —— 同一句文本腿译全了，见下）
  语音腿 it→zh：把上面那段意大利语音频**回喂** → `你好！`（往返闭环，模型认得出自己的意大利语）
  文本腿 `qwen-mt-flash`：zh→it `Ciao, mi chiamo SAND. Oggi testiamo la traduzione in italiano.`
    ／ it→zh `你好。我叫桑德。`
文档级依据：`qwen3-livetranslate-flash` 的 18 语种表里 `it` Italian 标注「音频+文本」；
老内置音色 `Cherry` / `Nofish` 的支持语言表含 Italian（迁移后 `it` 仍在新音色的官方覆盖集里）。
意大利语是拉丁字母，
不像泰语那样要字形守卫（无需 `test_thai_font.py` 那类处理）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_lang_tables_are_symmetric() -> None:
    """源语言表（去掉「自动检测」）与目标语言表必须有一模一样的语言码集合。"""
    from vlt.gui import SOURCE_LANGS, TARGET_LANGS

    src = {v for v in SOURCE_LANGS.values() if v}
    tgt = set(TARGET_LANGS.values())
    assert src == tgt, (f"源/目标语言表不对称：只在源有 {sorted(src - tgt)}，"
                        f"只在目标有 {sorted(tgt - src)}")

    # 同一个码两侧叫法要一致（否则下拉框里同一个码会出现两种名字）
    src_names = {v: k for k, v in SOURCE_LANGS.items() if v}
    tgt_names = {v: k for k, v in TARGET_LANGS.items()}
    for code, name in tgt_names.items():
        assert src_names[code] == name, \
            f"语言码 {code} 两侧叫法不一致：源={src_names[code]!r} 目标={name!r}"
    print(f"  语言表对称 OK（{len(tgt)} 种：{sorted(tgt)}）")


def test_russian_supported() -> None:
    """用户要求：俄语（ru）。模型本身支持，界面两个方向都必须能选到。"""
    from vlt.gui import SOURCE_LANGS, TARGET_LANGS

    assert TARGET_LANGS.get("俄语") == "ru", f"目标语言表里没有俄语：{TARGET_LANGS}"
    assert SOURCE_LANGS.get("俄语") == "ru", f"源语言表里没有俄语：{SOURCE_LANGS}"
    print("  俄语在源/目标两侧都可选 OK")


def test_name_lookup_does_not_silently_fall_back() -> None:
    """取名函数必须能认出俄语，不能回落到「英语 / 自动检测」。"""
    from vlt.gui import _source_name, _target_name

    assert _target_name("ru") == "俄语", f"目标取名回落了：{_target_name('ru')!r}"
    assert _source_name("ru") == "俄语", f"源取名回落了：{_source_name('ru')!r}"
    print("  俄语取名不回落 OK")


def test_thai_supported() -> None:
    """用户要求：泰语（th）。模型侧两个方向都已实测，界面两个方向都必须能选到。"""
    from vlt.gui import SOURCE_LANGS, TARGET_LANGS

    assert TARGET_LANGS.get("泰语") == "th", f"目标语言表里没有泰语：{TARGET_LANGS}"
    assert SOURCE_LANGS.get("泰语") == "th", f"源语言表里没有泰语：{SOURCE_LANGS}"
    print("  泰语在源/目标两侧都可选 OK")


def test_thai_name_lookup_does_not_silently_fall_back() -> None:
    """★ 泰语取名不许回落 —— 回落是静默的（表里没 th 就默默给你「英语」/「自动检测」），
    现象是「我明明选了泰语，它没生效」，和上面俄语那条是同一类坑。"""
    from vlt.gui import _source_name, _target_name

    assert _target_name("th") == "泰语", f"目标取名回落了：{_target_name('th')!r}"
    assert _source_name("th") == "泰语", f"源取名回落了：{_source_name('th')!r}"
    print("  泰语取名不回落 OK")


def test_thai_has_ui_label_and_tts_code() -> None:
    """泰语还得有两处配套词条，漏了同样是静默降级：

    - 界面译名：`_lang_label("泰语")` 在 en/ja/ko/ru 四套词表下都必须翻出东西，
      否则外国用户在方向下拉里看到的是汉字「泰语」（词表缺条目时 t() 原样返回）。
    - TTS 语言码：请求体的 `input.language` 认短码，`LANG_CODES` 缺 th 就等于不告诉
      服务端读什么语言（老实现是写自然语言语种名 `LANG_NAMES`，迁移后改成短码）。
    - **已知缺口**：th 不在 3.1 音色的官方覆盖集里 → 仍合成，但必须留一行 WARN
      （`_note_lang_gap`，每种语言每进程一次）。这条不许静默。
    """
    import contextlib
    import io as _io

    from vlt import i18n
    from vlt.gui import _lang_label
    from vlt.tts import LANG_CODES, TTS_SUPPORTED_LANGS, _lang_code, _note_lang_gap

    want = {"zh": "泰语", "en": "Thai", "ja": "タイ語", "ko": "태국어", "ru": "Тайский"}
    try:
        for lang, label in want.items():
            i18n.set_language(lang)
            got = _lang_label("泰语")
            assert got == label, f"界面语言 {lang} 下泰语显示 {got!r}，期望 {label!r}"
    finally:
        i18n.set_language("zh")
    assert "th" in LANG_CODES, f"TTS 语言码表缺泰语：{sorted(LANG_CODES)}"
    assert _lang_code("th") == "th"
    assert "th" not in TTS_SUPPORTED_LANGS, "前提变了：th 进了官方覆盖集就该删掉这条缺口用例"
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        _note_lang_gap("th")
        _note_lang_gap("th")          # 第二次不许再刷（每种语言每进程一次）
    out = buf.getvalue()
    assert out.count("不在当前 TTS 音色官方覆盖内") == 1, f"泰语的已知缺口 WARN 不对：{out!r}"
    assert "th" in out
    print("  泰语界面译名（zh/en/ja/ko/ru）+ TTS 语言码 + 缺口 WARN（只一次）OK")


def test_italian_supported() -> None:
    """用户要求：意大利语（it）。模型侧已实测（见模块文档），界面两个方向都必须能选到。"""
    from vlt.gui import SOURCE_LANGS, TARGET_LANGS

    assert TARGET_LANGS.get("意大利语") == "it", f"目标语言表里没有意大利语：{TARGET_LANGS}"
    assert SOURCE_LANGS.get("意大利语") == "it", f"源语言表里没有意大利语：{SOURCE_LANGS}"
    print("  意大利语在源/目标两侧都可选 OK")


def test_italian_name_lookup_does_not_silently_fall_back() -> None:
    """★ 意大利语取名不许回落 —— 回落是静默的（表里没 it 就默默给你「英语」/「自动检测」），
    现象是「我明明选了意大利语，它没生效」，与俄语/泰语那两条同类。"""
    from vlt.gui import _source_name, _target_name

    assert _target_name("it") == "意大利语", f"目标取名回落了：{_target_name('it')!r}"
    assert _source_name("it") == "意大利语", f"源取名回落了：{_source_name('it')!r}"
    print("  意大利语取名不回落 OK")


def test_italian_has_ui_label_and_tts_code() -> None:
    """意大利语的两处配套词条：界面译名（四套词表）+ TTS 语言码。

    漏了界面译名的现象：外国用户在方向下拉里看到汉字「意大利语」；
    漏了语言码的现象：请求体不带 `input.language`，服务端只能自己猜读什么语言。
    意大利语在 3.1 的官方覆盖集里（不该有缺口 WARN）。
    """
    from vlt import i18n
    from vlt.gui import _lang_label
    from vlt.tts import LANG_CODES, TTS_SUPPORTED_LANGS, _lang_code

    want = {"zh": "意大利语", "en": "Italian", "ja": "イタリア語",
            "ko": "이탈리아어", "ru": "Итальянский"}
    try:
        for lang, label in want.items():
            i18n.set_language(lang)
            got = _lang_label("意大利语")
            assert got == label, f"界面语言 {lang} 下意大利语显示 {got!r}，期望 {label!r}"
    finally:
        i18n.set_language("zh")
    assert "it" in LANG_CODES, f"TTS 语言码表缺意大利语：{sorted(LANG_CODES)}"
    assert _lang_code("it") == "it"
    assert "it" in TTS_SUPPORTED_LANGS, "意大利语必须在官方覆盖集里（不该打缺口 WARN）"
    print("  意大利语界面译名（zh/en/ja/ko/ru）+ TTS 语言码 都就位 OK")



if __name__ == "__main__":
    print("test_langs:")
    test_lang_tables_are_symmetric()
    test_russian_supported()
    test_name_lookup_does_not_silently_fall_back()
    test_thai_supported()
    test_thai_name_lookup_does_not_silently_fall_back()
    test_thai_has_ui_label_and_tts_code()
    test_italian_supported()
    test_italian_name_lookup_does_not_silently_fall_back()
    test_italian_has_ui_label_and_tts_code()
    print("ALL PASSED")
