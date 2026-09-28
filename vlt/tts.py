"""打字输入的**译音**：把译文合成成音频，喂给虚拟声卡那条腿。

为什么打字要单独一步 TTS：
    打字走的是**文本翻译**接口（实时模型不接受文本入口，见 `textin.py` 模块注释），
    它只回文本、不回音频 —— 不加这一步，打字内容就永远进不了虚拟声卡、对面听不到。
    语音那条腿的音频是实时模型直出的，这里补的是同格式的替代品。

实测（2026-09）：
- 模型 `qwen3-tts-flash`（也可用 `qwen3-tts-instruct-flash`），返回 `output.audio`
  同时带 `data`(base64) 与 `url`；**优先用 data**，省一次下载且不受 URL 过期影响。
- 音频是 24kHz 单声道 WAV，用 `miniaudio` 解成 **24k 单声道 s16le PCM** ——
  与实时模型译音**同格式**，所以下游可以直接复用 `resample_24k_mono_to_48k_stereo`
  和 `VirtualMic`，不需要任何新管线。
- 音色 `Cherry` 中/英/日都能读（实测），故默认一个音色就够；要换按 config 改。
"""
from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ENDPOINT = "https://maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen3-tts-flash"
DEFAULT_VOICE = "Cherry"
DEFAULT_TIMEOUT_S = 30.0

# 说话译音用的是 Qwen-Omni 系列音色（Tina/Cindy/…），qwen3-tts-flash **不认这些 id**
# （会 InvalidParameter）。要试听它们只能走非实时 Qwen-Omni —— 同一个 compatible-mode
# 端点（textin.py 已在用、同一把 key、无需 workspace），但音频输出**强制流式**。
OMNI_ENDPOINT = "https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions"
DEFAULT_OMNI_MODEL = "qwen3.5-omni-flash"
DEFAULT_OMNI_VOICE = "Tina"

# 目标语言码 → DashScope 的 language_type（可选参数；拿不准就不传，服务端自己判）
LANG_NAMES = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian",
    "it": "Italian", "pt": "Portuguese",
}

_opener = None


def _get_opener():
    """直连 opener（禁用系统代理）——与 textin 同一取舍：国内端点走代理是纯负担。"""
    global _opener
    if _opener is None:
        _opener = build_opener(ProxyHandler({}))
    return _opener


class TtsError(RuntimeError):
    """合成失败（缺 key / 网络 / 参数 / 空音频）。消息给用户看，带原因不带堆栈。"""


def _decode_to_24k_mono(raw: bytes) -> bytes:
    """任意容器（WAV/MP3/…）→ 24kHz 单声道 s16le PCM。"""
    try:
        import miniaudio

        dec = miniaudio.decode(raw, output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=24000)
        return bytes(dec.samples)
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"音频解码失败：{type(exc).__name__}: {exc}") from exc


def _fetch(url: str, timeout: float) -> bytes:
    req = Request(url, headers={"Accept": "*/*"})
    try:
        with _get_opener().open(req, timeout=timeout) as r:
            return r.read()
    except (HTTPError, URLError) as exc:
        raise TtsError(f"下载音频失败：{exc}") from exc


def synthesize(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    language: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """把一段文本合成为 24kHz 单声道 s16le PCM（与实时模型译音同格式）。

    同步函数（调用方丢线程池里跑）；`language` 是目标语言码（zh/en/ja…），
    会映射成 service 的 `language_type`，拿不准就不传。
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("内容为空")
    if not (api_key or "").strip():
        raise TtsError("还没配置 API key（见界面右上角「设置」）")

    payload: dict = {"model": model or DEFAULT_MODEL,
                     "input": {"text": text, "voice": voice or DEFAULT_VOICE}}
    lang_name = LANG_NAMES.get((language or "").lower())
    if lang_name:
        payload["input"]["language_type"] = lang_name

    req = Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json"}, method="POST")
    try:
        with _get_opener().open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise TtsError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except URLError as exc:
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc

    try:
        resp = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"响应解析失败：{exc}") from exc
    if isinstance(resp.get("error"), dict):
        raise TtsError(str((resp["error"] or {}).get("message") or resp["error"])[:300])

    audio = ((resp.get("output") or {}).get("audio") or {})
    raw: bytes | None = None
    if audio.get("data"):
        try:
            raw = base64.b64decode(audio["data"])
        except Exception as exc:  # noqa: BLE001
            raise TtsError(f"base64 音频解析失败：{exc}") from exc
    elif audio.get("url"):
        raw = _fetch(str(audio["url"]), timeout)      # URL 有有效期，能不用就不用
    if not raw:
        raise TtsError("服务端没返回音频")
    return _decode_to_24k_mono(raw)


def synthesize_omni(
    text: str,
    *,
    voice: str = DEFAULT_OMNI_VOICE,
    model: str = DEFAULT_OMNI_MODEL,
    api_key: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """用**非实时 Qwen-Omni** 合成一段文本 → 24kHz 单声道 s16le PCM（与 `synthesize` 同格式）。

    为何单独一条路：说话译音的音色（Tina/Cindy/Liora Mira…）属于 Qwen-Omni 系列，
    `qwen3-tts-flash` 不支持（跨模型混用会 InvalidParameter），要试听只能走 Omni。

    实现要点（均有官方文档依据）：
    - Omni 是对话模型，音频输出**必须** `stream=True`；自己解 SSE，把分片的
      `choices[0].delta.audio.data`（base64）**拼接后一次解码**（官方示例就是这么干的）。
    - 它不是逐字 TTS：下一条指令让它朗读样例句，个别措辞可能略有出入 —— 试听音色足够。
    - 回的是 24k 单声道音频（可能裸 PCM、也可能带 WAV 头），统一过一遍解码器，
      解不动就当裸 s16le PCM 直接用（本就是目标格式）。
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("内容为空")
    if not (api_key or "").strip():
        raise TtsError("还没配置 API key（见界面右上角「设置」）")

    payload = {
        "model": model or DEFAULT_OMNI_MODEL,
        "messages": [{"role": "user",
                      "content": f"请逐字朗读下面引号内的这句话，只朗读、不要回答或补充任何内容：「{text}」"}],
        "modalities": ["text", "audio"],
        "audio": {"voice": voice or DEFAULT_OMNI_VOICE, "format": "wav"},
        "stream": True,                             # ⚠️ Omni 音频输出必须流式
        "stream_options": {"include_usage": True},
    }
    req = Request(OMNI_ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json",
                           "Accept": "text/event-stream"}, method="POST")
    b64: list[str] = []
    try:
        with _get_opener().open(req, timeout=timeout) as r:
            for raw_line in r:                        # 逐行读 SSE
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:  # noqa: BLE001 — 心跳/不完整帧直接略过
                    continue
                if isinstance(obj.get("error"), dict):
                    err = obj["error"]
                    raise TtsError(str(err.get("message") or err)[:300])
                choices = obj.get("choices") or []
                if not choices:
                    continue
                aud = (choices[0].get("delta") or {}).get("audio") or {}
                if aud.get("data"):
                    b64.append(aud["data"])
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise TtsError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except URLError as exc:
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except TtsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc

    if not b64:
        raise TtsError("服务端没返回音频")
    try:
        raw = base64.b64decode("".join(b64))
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"base64 音频解析失败：{exc}") from exc
    try:
        return _decode_to_24k_mono(raw)
    except TtsError:
        return raw                                  # 已是裸 24k 单声道 s16le PCM，直接用
