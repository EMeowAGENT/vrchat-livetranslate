"""可选音色表 —— 供「⚙ 设置」里的音色下拉使用（只做数据，不拉 tkinter）。

为什么分两张表：本项目有**两条独立的出声音色**，来自不同模型，音色 id **不通用**
（DashScope 规则：把 A 模型的音色填给 B 模型会 `InvalidParameter`）——
- **说话译音**：实时模型 `qwen3.x-livetranslate-flash-realtime` 直出译音，
  音色写 `session.voice`（或 `directions.<X>.voice` 按方向覆盖）。默认 `Tina`。
- **打字译音**：文本翻译后单独调 Qwen-Audio-TTS（`qwen-audio-3.1-tts-flash`）合成，
  音色写 `text_input.tts.voice`。默认 `longanfengyue_v3.1`。

只收录**多语种**能读的音色（中/英/日/韩… 都能念）：VRChat 里语言对随时切换，
方言音色（上海-阿珍 / 北京-晓东 / 四川-程川…）读外语会翻车，故不进下拉；
真要方言直接在 config.yaml 手填 voice 即可。列表会随服务端扩列而滞后，所以下拉
做成**可编辑**：声音复刻的自定义 voice id、或新出的音色都能手输，不会被表卡住。

来源：千问云「音色列表」（2026-09 实测核对）。改表只动这里，GUI/引擎都从这儿拿。
"""
from __future__ import annotations

# 说话译音（实时模型）：覆盖中/英/日/韩 + 欧语系的主流多语种音色。
REALTIME_VOICES: tuple[str, ...] = (
    "Tina", "Serena", "Maia", "Momo", "Qiao", "Cindy", "Angel", "Liora Mira",
    "Raymond", "Ethan", "Theo Calm", "Evan", "Wil", "Ryan", "Jennifer",
    "Aiden", "Katerina", "Mione", "Sohee", "Lenn", "Ono Anna", "Sonrisa",
    "Bodega", "Alek", "Dolce", "Andre", "Marina", "Chloe",
)

# 打字译音（Qwen-Audio-TTS 3.1）：**只收多语音色**。
# 为什么只放这四个：3.1 家族里的纯中文音色读外语会翻车（VRChat 里语言对随时切换），
# 与上面「方言音色不进下拉」同一条理由；这四个是官方标注覆盖
# 中文/英语/日语/韩语/法语/德语/葡萄牙语/意大利语/越南语/印尼语的。
# ⚠️ 官方列表不含俄语/西班牙语/泰语 —— 那几种语言仍能合成（未报错），但发音质量未验证，
#    见 `vlt/tts.py` 的 `TTS_SUPPORTED_LANGS` 与启动时的 WARN。
# 老 `qwen3-tts-flash` 的音色（Cherry/Serena/…）**不适用**于新模型：跨模型填 voice 会
# InvalidParameter，所以它们已从这张表移除。
TTS_VOICES: tuple[str, ...] = (
    "longanfengyue_v3.1", "longanhuan_v3.1", "longanlingxin_v3.1", "xunanchuan_v3.1",
)


def voice_choices(current: str | None, catalog: tuple[str, ...]) -> list[str]:
    """下拉候选：目录音色 + （若当前值不在表里）把当前值排到最前。

    当前值可能是**手填的自定义 / 复刻音色 id**，不在表里；不排进去的话，
    重新打开设置时下拉会显示成表里的第一项（仿佛被改过），而不是用户真正选的值。
    """
    cur = (current or "").strip()
    if cur and cur not in catalog:
        return [cur, *catalog]
    return list(catalog)
