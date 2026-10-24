"""打字输入的**译音**：把译文合成成音频，喂给虚拟声卡那条腿。

为什么打字要单独一步 TTS：
    打字走的是**文本翻译**接口（实时模型不接受文本入口，见 `textin.py` 模块注释），
    它只回文本、不回音频 —— 不加这一步，打字内容就永远进不了虚拟声卡、对面听不到。
    语音那条腿的音频是实时模型直出的，这里补的是同格式的替代品。

## 当前后端：Qwen-Audio-TTS（2026-07-14 起千问云主推的离线 TTS 家族）

- 端点 `/api/v1/services/audio/tts/SpeechSynthesizer`（**不是**老的
  `.../aigc/multimodal-generation/generation`）—— 由 `endpoints.tts_url(base_url)` 按线路派生。
- 模型 `qwen-audio-3.1-tts-flash`。为什么 3.1 而不是 3.0：3.0 只有中文/英语；
  3.1 的四个**多语音色**覆盖 zh/en/ja/ko/fr/de/pt/it/vi/id（方言另算）。
  首包实测 3.1 ≈780-800ms、3.0 ≈600ms —— 为多语言能力选 3.1。
- 请求体：`{"model", "input": {"text", "voice", "format", "sample_rate", "language_hints"}}`
  （`language_hints` 是**数组**，服务端只取第一个元素）。
  `format`：**整段用 `wav`**（下游 `_decode_to_24k_mono` 走 miniaudio，需要容器格式；
  响应 `output.audio.data` 可能为空而给 `url`，现有下载逻辑照旧可用）；
  **流式用 `pcm`**（分片是裸 PCM，`_chunk_to_pcm` 直接过）。
  `language` 是**短码**（zh/en/ja…，见 `LANG_CODES`），字段名不是老的 `language_type`。
- 音频是 24kHz 单声道，与实时模型译音**同格式**，所以下游直接复用
  `resample_24k_mono_to_48k_stereo` 和 `VirtualMic`，不需要任何新管线。

## ⚠️ 已知语言缺口（不是 bug，不许掩盖）

老 `qwen3-tts-flash` 的 Cherry 覆盖：中/英/法/德/**俄**/意/**西**/葡/日/韩。
新 3.1 的四个多语音色官方列表：中/英/日/韩/法/德/葡/意/**越/印尼** ——
**官方不含俄语/西班牙语/泰语**。实测新端点对 ru/es/th 文本仍返回音频（不报错），
但官方没承诺发音质量。所以目标语言落在 `TTS_SUPPORTED_LANGS` 之外时：
**照常合成**（不阻断功能），但每次进程每种语言留一行 WARN（见 `_note_lang_gap`），
日志/`config.example.yaml` 里也写明这是已知缺口。

## 流式（`synthesize_stream`）

请求头 `X-DashScope-SSE: enable`。⚠️ 新端点的 SSE 是**多行 JSON**（一个事件的 JSON 被
拆成多行、每行都以 `data:` 起头，**只在空行处才算一个完整事件**）：
逐行 `json.loads` 的老写法在新端点上**一个分片都解析不出来**（每行都不是合法 JSON，
全被吞掉 → 退化成「一个分片都没拿到 → 整段兜底」，首包退回 ~1.7s）。
解析必须按 SSE 规范累积 data 行，见 `_iter_sse_events`。
服务端在流**末尾**可能补发一片「整段汇总」（老端点实测与前面所有分片逐字节相同）：
保留去重守卫，命中就丢，否则整句会被念两遍。
"""
from __future__ import annotations

import atexit
import base64
import http.client
import json
import ssl
import threading
import time
from typing import Iterator
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .voices import DEFAULT_TTS_MODEL, DEFAULT_TTS_VOICE

# Qwen-Audio-TTS 端点（千问云·国内默认线路）。实际调用走 `endpoint=` 传入的
# `endpoints.tts_url(base_url)`——那是唯一真相源；这里只是「不传时」的默认值。
ENDPOINT = "https://maas.qianwenaiapi.com/api/v1/services/audio/tts/SpeechSynthesizer"
# 默认模型/音色从 `voices.py` 取（单一真相源，config 迁移也用同一份）。
DEFAULT_MODEL = DEFAULT_TTS_MODEL
DEFAULT_VOICE = DEFAULT_TTS_VOICE
DEFAULT_TIMEOUT_S = 30.0
#: 合成采样率（下游按 24k 单声道处理，改这里要同步 `resample_24k_mono_to_48k_stereo`）
SAMPLE_RATE = 24000

# 说话译音用的是 Qwen-Omni 系列音色（Tina/Cindy/…），离线 TTS 家族 **不认这些 id**
# （会 InvalidParameter）。要试听它们只能走非实时 Qwen-Omni —— 同一个 compatible-mode
# 端点（textin.py 已在用、同一把 key、无需 workspace），但音频输出**强制流式**。
OMNI_ENDPOINT = "https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions"
DEFAULT_OMNI_MODEL = "qwen3.5-omni-flash"
DEFAULT_OMNI_VOICE = "Tina"

# 请求体 `input.language_hints`（**数组**）认的**短码**（不是老的 `language_type` 英文名）。
# 取 `lang.split("-")[0].lower()`；不在这个集合里就**不传**（让服务端自己判），
# 绝不再退回 `language_type` —— 新端点不认那个字段。
LANG_CODES = frozenset({
    "zh", "en", "ja", "ko", "fr", "de", "es", "ru", "it", "pt", "th",
    "id", "vi", "ms", "fil", "ar",
})

# 3.1 的四个多语音色**官方**覆盖集（中文/英语/日语/韩语/法语/德语/葡萄牙语/意大利语/
# 越南语/印尼语）。不在这里的语言（ru/es/th…）仍会合成，但要留痕说明质量未验证。
TTS_SUPPORTED_LANGS = frozenset({"zh", "en", "ja", "ko", "fr", "de", "pt", "it", "vi", "id"})

# 语言缺口 WARN 的「每种语言每进程只打一次」去重表（别每句都刷）。
_LANG_WARNED: set[str] = set()


def _lang_code(language: str | None) -> str | None:
    """目标语言 → `input.language_hints[0]` 的短码；不认识 / 没给 → None（该字段不传）。"""
    code = (language or "").split("-")[0].strip().lower()
    return code if code in LANG_CODES else None


def _note_lang_gap(language: str | None) -> None:
    """目标语言不在官方覆盖集里 → 留一行痕（**照常合成**，只是别让用户以为有保证）。

    为什么必须留痕：迁移前 Cherry 覆盖俄语/西班牙语，迁移后官方列表里没有这两个 ——
    对 ru/es 用户是**能力回退**。静默不管就成了「悄悄变差还查不到原因」；
    也不能因此拒绝合成（实测新端点仍会出声），所以只警告、不阻断。
    每种语言每进程只打一次（逐句合成会打很多次，刷屏等于没打）。
    """
    raw = (language or "").strip().lower()
    if not raw:
        return
    code = raw.split("-")[0]
    if code in TTS_SUPPORTED_LANGS or code in _LANG_WARNED:
        return
    _LANG_WARNED.add(code)
    _note(f"⚠️ 目标语言 {code} 不在当前 TTS 音色官方覆盖内（发音质量未验证）")


def _tts_payload(text: str, *, voice: str, model: str, language: str | None,
                 fmt: str) -> dict:
    """合成请求体（整段/流式共用一处口径，`fmt` 只是 `wav` / `pcm` 的差别）。"""
    payload: dict = {"model": model or DEFAULT_MODEL,
                     "input": {"text": text,
                               "voice": voice or DEFAULT_VOICE,
                               "format": fmt,
                               "sample_rate": SAMPLE_RATE}}
    code = _lang_code(language)
    if code:
        # ⚠️ 字段名是 `language_hints`（**数组**，当前版本只处理第一个元素），
        #    不是 `language`/`language_type`。写错不会报错 —— 服务端会**静默忽略**，
        #    合成照常出声但语言提示完全没生效（实测：用 `language` 也返回 200）。
        payload["input"]["language_hints"] = [code]
    return payload

_opener = None
_DEFAULT_OPENER = None        # 模块自建的那个 opener（被替换过就不走连接池，见 _pool_allowed）


def _get_opener():
    """直连 opener（禁用系统代理）——与 textin 同一取舍：国内端点走代理是纯负担。"""
    global _opener, _DEFAULT_OPENER
    if _opener is None:
        _opener = build_opener(ProxyHandler({}))
        _DEFAULT_OPENER = _opener
    return _opener


# ---------------------------------------------------------------- 连接复用
# 为什么要（2026-10-07 实测）：每段 TTS 都要重付一次 DNS+TCP+TLS ≈ **520ms**，占「第一句开口」
# 的一大半；而同一句 20~30 字的流式合成整段也才 1.6~1.7s，这笔开销不可忽略。
# 复用同一条连接后这段只剩发送/首字节 —— 连续多段实测后段省 **94~186ms/段**。
# ⚠️ 不变式：本模块刻意**直连、不走代理**（见 `_get_opener`），池化连接同样直连；
#    `reuse_conn=False` 时行为与以前**完全一致**（不碰下面任何东西，仍走 urllib）。
_POOL: dict[tuple[str, str, int], tuple[object, float]] = {}
_POOL_LOCK = threading.Lock()
_POOL_IDLE_MAX_S = 20.0        # 空闲超过这么久就丢掉（服务端多半已关，留着反而多一次失败重连）


def _pool_key(url: str) -> tuple[tuple[str, str, int], str]:
    """把 URL 拆成 (池键=(scheme,host,port), 请求路径)。纯函数，离线可测。"""
    u = urlsplit(url)
    scheme = (u.scheme or "https").lower()
    host = u.hostname or ""
    port = u.port or (443 if scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return (scheme, host, port), path


def _close_quiet(conn: object) -> None:
    try:
        conn.close()                                       # type: ignore[attr-defined]
    except Exception:                                      # noqa: BLE001
        pass


def _open_conn(url: str, timeout: float):
    """取一条连接：优先复用池里那条还新鲜的，否则新建。返回 (conn, path, key)。"""
    key, path = _pool_key(url)
    with _POOL_LOCK:
        held = _POOL.pop(key, None)
    if held is not None:
        conn, used_at = held
        if time.monotonic() - used_at <= _POOL_IDLE_MAX_S:
            return conn, path, key
        _close_quiet(conn)
    scheme, host, port = key
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    return conn, path, key


def _release_conn(key: tuple[str, str, int], conn: object, *, reusable: bool) -> None:
    """把连接放回池（只留一条/主机）；不可复用就关掉。"""
    if not reusable:
        _close_quiet(conn)
        return
    with _POOL_LOCK:
        old = _POOL.pop(key, None)
        _POOL[key] = (conn, time.monotonic())
    if old is not None:
        _close_quiet(old[0])


def _post_pooled(req, timeout: float):
    """用池化连接发一个 urllib Request（GET/POST 都行），返回 (resp, conn, key)。

    连接层异常（被服务端悄悄掐掉最常见）→ 丢连接、**重连一次**并留痕；
    ≥400 是服务端的明确答复、**不当成连接问题**：转成 urllib 的 `HTTPError` 抛出去 ——
    调用方本来就是 `except HTTPError` + `exc.read()` 取详情，这里必须保持同一口径。
    """
    url = req.full_url
    method = req.get_method()
    body = req.data
    headers = {k: v for k, v in req.headers.items()
               if k.lower() not in ("host", "content-length", "connection")}
    last: Exception | None = None
    for attempt in (1, 2):
        conn, path, key = _open_conn(url, timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            if resp.status >= 400:
                _release_conn(key, conn, reusable=not resp.will_close)
                raise HTTPError(url, resp.status, resp.reason, resp.headers, resp)
            return resp, conn, key
        except HTTPError:
            raise
        except (http.client.HTTPException, OSError) as exc:
            last = exc
            _release_conn(key, conn, reusable=False)
            if attempt == 1:
                _note(f"复用连接失效（{type(exc).__name__}: {exc}）→ 重连一次")
                continue
            raise
    raise last if last else TtsError("连接失败")


class _Response:
    """`with _open_response(...) as r` 的包装：用完把连接放回池。

    ⚠️ 刻意**不用 generator**（`@contextlib.contextmanager`）：实测过 generator 版本会把
    进程收尾的时序搅乱 —— 在跑 Tk 的用例（`tests/test_textin.py`）里表现为退出码非 0，
    而所有断言其实全过（`Tcl_AsyncDelete: async handler deleted by the wrong thread`）。
    写成普通对象后该用例恢复全绿（同机 6/6）。
    """

    __slots__ = ("_resp", "_conn", "_key", "_pooled")

    def __init__(self, resp, conn=None, key=None) -> None:   # noqa: ANN001
        self._resp = resp
        self._conn = conn
        self._key = key
        self._pooled = key is not None

    def __enter__(self):                                     # noqa: ANN204
        return self._resp

    def __exit__(self, *exc) -> bool:                        # noqa: ANN002
        if self._pooled:
            reusable = not getattr(self._resp, "will_close", True)
            _release_conn(self._key, self._conn, reusable=reusable)
        else:
            try:                                             # 与原来的 `with opener.open()` 一致
                self._resp.close()
            except Exception:                                # noqa: BLE001
                pass
        return False


def _open_response(req, timeout: float, *, reuse_conn: bool):
    """取响应：优先**池化复用**（连续多段省一次 DNS+TCP+TLS ≈ 520ms）；连接层出问题回退直连。

    回退与重连都留痕（本仓库约定：禁静默降级）。`reuse_conn=False` 时与以前**完全一致**。
    """
    if reuse_conn and _pool_allowed():
        try:
            resp, conn, key = _post_pooled(req, timeout)
            return _Response(resp, conn, key)
        except HTTPError:
            raise
        except Exception as exc:                              # noqa: BLE001
            _note(f"连接复用不可用（{type(exc).__name__}: {exc}）→ 本次回退直连")
    return _Response(_get_opener().open(req, timeout=timeout))


def _pool_allowed() -> bool:
    """是否允许走连接池。

    规则：**取 opener 的路径被替换过就不池化**。两种替换方式都要挡住：
      * `module._opener = fake`（直接塞 opener）；
      * `module._get_opener = lambda: fake`（换掉取 opener 的函数）。
    替换者（用例里的假 opener、或别的接管 HTTP 层的代码）期望自己看到每一个请求；
    池化会绕过它 —— 实测后果是打了假 opener 的用例直接打到真端点上去（401）。
    """
    return (_get_opener is _DEFAULT_GET_OPENER
            and (_opener is None or _opener is _DEFAULT_OPENER))


def _close_pooled() -> None:
    """进程退出时把池里的连接关掉。

    不关的后果实测过：连接会一直留到解释器收尾阶段，把收尾顺序搅乱 ——
    在跑 Tk 的用例里表现为 `Tcl_AsyncDelete: async handler deleted by the wrong thread`
    （退出码非 0，而所有断言其实全过）。退出前主动关掉，顺序就确定了。
    """
    with _POOL_LOCK:
        held = list(_POOL.values())
        _POOL.clear()
    for conn, _ in held:
        _close_quiet(conn)


atexit.register(_close_pooled)
_DEFAULT_GET_OPENER = _get_opener      # 供上面的守卫比对（模块导入时就固定下来）


class TtsError(RuntimeError):
    """合成失败（缺 key / 网络 / 参数 / 空音频）。消息给用户看，带原因不带堆栈。"""


class TtsStreamTruncated(TtsError):
    """流式中途断了，但**已经 yield 出去的分片有效**（少半句，不整句丢）。

    调用方约定：不要把已经推给声卡的部分撤掉，也不要在状态栏报「合成失败」——
    改成一条 warn 级提示（用户听出「这句好像没说完」时，界面上得有个交代）。
    """


class _Rejected(TtsError):
    """服务端**明确**回了个 HTTP 状态码（不是网络问题）。

    为什么要单独一个类型：流式那条路要按状态码决定「退回整段合成」（400/406/415
    = 协议不认 SSE）还是「直接放弃」（401/403/429/5xx：再发一次同样会失败）。
    普通 `TtsError` 把状态码糊进了文案里，没法可靠判定。
    """

    def __init__(self, code: int, detail: str) -> None:
        self.code = int(code)
        self.detail = detail or ""
        super().__init__(f"HTTP {self.code}：{_http_reason_text(self.detail)}")


def _note(msg: str) -> None:
    """兜底/降级路径的留痕（本仓库约定：**禁静默降级**，每一处降级都要能查）。

    比 print 多一层保护：日志本身绝不能把主流程搞挂。
    """
    try:
        print(f"[tts] {msg}", flush=True)
    except Exception:  # noqa: BLE001
        pass


# 「协议不认流式」这类码：退回整段还有意义。其余（401/403/429/5xx）再发一次也是白搭。
_SSE_FALLBACK_CODES = frozenset({400, 406, 415})

# ⚠️ 这里**故意没有**「主端点被拒 → 换旧域名重试」的回落：
# 2026-10 千问云把离线 TTS 切到了 Qwen-Audio-TTS（新端点 + 新音色），旧
# `qwen3-tts-flash` 在 maas 网关只回 `HTTP 400：{}`。维护者明确要求**不掩盖这个问题** ——
# 就用新模型/新端点，失败就如实报错（带「线路是否支持该 TTS 模型」的人话），
# 而不是偷偷换域名把问题藏起来。


def _http_reason_text(detail: str) -> str:
    """HTTP 错误里「原因」那一段：空 body 也要说人话。

    实测千问云在拒收 TTS 请求时会回 **空 body**（就一个 `{}`）。直接拼成
    「HTTP 400：{}」用户完全看不懂，还会以为是自己请求写错了；这里换成
    「服务端拒收但未给原因 + 最可能的原因」，让人知道该查线路/模型。
    """
    d = (detail or "").strip()
    if d in ("", "{}", "null", "[]"):
        return "服务端拒收但未给原因（请确认线路是否支持该 TTS 模型）"
    return d[:300]


def _err_detail(exc: HTTPError) -> str:
    """取 HTTP 错误响应体（最多 300 字）。拿不到就当空 body（文案见 `_http_reason_text`）。"""
    try:
        return exc.read().decode("utf-8", "replace")[:300]
    except Exception:  # noqa: BLE001
        return ""


def _post_tts(url: str, payload: dict, api_key: str, timeout: float,
              *, reuse_conn: bool) -> str:
    """POST 一次**整段**合成请求，返回响应体文本。

    HTTP 状态码 → `_Rejected`；网络/其它异常 → `TtsError`。
    """
    req = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json"}, method="POST")
    try:
        with _open_response(req, timeout, reuse_conn=reuse_conn) as r:
            return r.read().decode("utf-8", "replace")
    except HTTPError as exc:
        raise _Rejected(exc.code, _err_detail(exc)) from exc
    except URLError as exc:
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except TtsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc


def _cut_note(got: int, exc: BaseException) -> str:
    """流式中途断掉：留一行痕 + 生成抛给调用方的消息（同一份文案，不写两处）。"""
    msg = f"流式中途中断（{type(exc).__name__}: {exc}），已保留 {got} 个分片（少半句、不整句丢）"
    _note(msg)
    return msg


def _decode_to_24k_mono(raw: bytes) -> bytes:
    """任意容器（WAV/MP3/…）→ 24kHz 单声道 s16le PCM。"""
    try:
        import miniaudio

        dec = miniaudio.decode(raw, output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=24000)
        return bytes(dec.samples)
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"音频解码失败：{type(exc).__name__}: {exc}") from exc


def _fetch(url: str, timeout: float, *, reuse_conn: bool = True) -> bytes:
    req = Request(url, headers={"Accept": "*/*"})
    try:
        with _open_response(req, timeout, reuse_conn=reuse_conn) as r:
            return r.read()
    except (HTTPError, URLError) as exc:
        raise TtsError(f"下载音频失败：{exc}") from exc


def _extract_audio(obj: dict, timeout: float, *, reuse_conn: bool = True) -> bytes:
    """从响应体里取音频原始字节：优先 base64 的 `data`，退回 `url` 下载。"""
    audio = (obj.get("output") or {}).get("audio") or {}
    if audio.get("data"):
        try:
            return base64.b64decode(audio["data"])
        except Exception as exc:  # noqa: BLE001
            raise TtsError(f"base64 音频解析失败：{exc}") from exc
    if audio.get("url"):
        return _fetch(str(audio["url"]), timeout, reuse_conn=reuse_conn)   # URL 有有效期，能不用就不用
    return b""


def _raise_if_error(obj: dict) -> None:
    """服务端错误有两种外壳：`{"error": {...}}` 与带 code/message 的扁平形态。"""
    if isinstance(obj.get("error"), dict):
        raise TtsError(str((obj["error"] or {}).get("message") or obj["error"])[:300])
    if obj.get("code") and obj.get("message"):
        raise TtsError(str(obj["message"])[:300])


def _chunk_to_pcm(chunk: bytes) -> bytes:
    """SSE 分片 → 24k 单声道 s16le。文档说分片是裸 PCM；万一是容器（RIFF）就解一次。"""
    if chunk[:4] == b"RIFF":
        return _decode_to_24k_mono(chunk)
    return chunk[: len(chunk) - (len(chunk) % 2)]


def synthesize(
    text: str,
    *,
    reuse_conn: bool = True,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    language: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    endpoint: str | None = None,
) -> bytes:
    """把一段文本合成为 24kHz 单声道 s16le PCM（与实时模型译音同格式）。

    同步函数（调用方丢线程池里跑）；`language` 是目标语言码（zh/en/ja…），
    取短码塞进请求体的 `input.language_hints`（数组），不在 `LANG_CODES` 里就不传（服务端自己判）。

    `endpoint`：TTS 地址由**调用方**按当前线路从 base_url 的 host 派生后传入
    （见 endpoints.tts_url）；不传则回落模块常量 ENDPOINT（千问云默认）。
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("内容为空")
    if not (api_key or "").strip():
        raise TtsError("还没配置 API key（见界面右上角「设置」）")

    # 整段用 `format=wav`：下游 `_decode_to_24k_mono` 走 miniaudio，需要容器格式才能解；
    # 响应里 `output.audio.data` 可能为空、只给 `url`，`_extract_audio` 会去下载（同一把 key）。
    payload = _tts_payload(text, voice=voice, model=model, language=language, fmt="wav")
    _note_lang_gap(language)          # 官方不覆盖的语言（ru/es/th…）留一行痕，照常合成
    # 直连当前线路的 TTS 端点；**不做跨域回落**（见 `_SSE_FALLBACK_CODES` 上方的说明）。
    body = _post_tts(endpoint or ENDPOINT, payload, api_key, timeout, reuse_conn=reuse_conn)

    try:
        resp = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"响应解析失败：{exc}") from exc
    _raise_if_error(resp)
    raw = _extract_audio(resp, timeout, reuse_conn=reuse_conn)
    if not raw:
        raise TtsError("服务端没返回音频")
    return _decode_to_24k_mono(raw)


#: SSE 的 data 行拼接符（规范里同一事件的多个 data 行之间保留换行，多行 JSON 靠它拼回文本）
_SSE_NL = chr(10)


def _json_event(text: str) -> dict | None:
    """把一个已拼好的 data 文本解析成事件字典（解析不了 → None）。"""
    if not text or text.strip() in ("[DONE]", "null", ""):
        return None
    try:
        obj = json.loads(text)
    except Exception:                                               # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def _iter_sse_events(resp) -> Iterator[dict]:                    # noqa: ANN001
    """把一个 SSE 响应逐**事件**解析成 JSON 字典（按规范累积 `data:` 行，空行才算结束）。

    ⚠️ 为什么不能「逐行 json.loads」：新端点（Qwen-Audio-TTS，
    `/api/v1/services/audio/tts/SpeechSynthesizer`）回的是**多行 JSON** ——
    一个事件的 JSON 被拆成多行、每行都以 `data:` 起头，**只有空行才代表一个事件结束**：

        id:1
        event:result
        :HTTP_STATUS/200
        data:{
        data:  "output" : {
        data:    "type" : "sentence-synthesis",
        data:    "audio" : { "data" : "<base64 PCM>" }
        data:  }
        data:}

    逐行解析时每一行都不是合法 JSON → 全被 `except: continue` 吞掉 → 一个分片都拿不到
    → 退化成「整段兜底」，首包从 ~0.8s 退回 ~1.7s（**功能不报错、只是变慢**，最难查）。

    所以这里**严格按 SSE 规范**：`data:` 行按出现顺序累积（多个 data 行之间保留换行），
    遇空行才 `json.loads` 一次；`[DONE]` 丢弃。单行 JSON 的服务端同样适用 ——
    它们的每个事件也以空行结尾，只是 data 行只有一行。

    `event:` / `id:` / `retry:` / `:`（注释，如 `:HTTP_STATUS/200`）一律跳过。
    流不以空行结尾时，残余内容也试解析一次（否则最后一片会丢）。
    """
    buf: list[str] = []
    for raw in resp:
        line = (raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray))
                else str(raw)).rstrip()
        if not line:
            # 空行 = 一个事件结束（SSE 的边界只看空行，不看 JSON 是否凑巧能解析）
            if buf:
                obj = _json_event(_SSE_NL.join(buf))
                buf.clear()
                if obj is not None:
                    yield obj
            continue
        if line.startswith(":"):
            continue                                            # SSE 注释（:HTTP_STATUS/200 等）
        field, _, value = line.partition(":")
        if field == "data":
            val = value[1:] if value.startswith(" ") else value
            if val.strip() == "[DONE]":
                buf.clear()
                continue
            buf.append(val)
            continue
        if field in ("event", "id", "retry"):
            continue
        # 非标准字段：个别网关会把裸 JSON 直接推过来 → 当 data 的一行攒着，别丢
        buf.append(line)
    if buf:
        obj = _json_event(_SSE_NL.join(buf))
        if obj is not None:
            yield obj

def _stream_once(url: str, payload: dict, api_key: str, timeout: float,
                 *, reuse_conn: bool) -> Iterator[bytes]:
    """对**一个**地址发起流式（SSE）合成，逐片 yield 24k 单声道 s16le PCM。

    一次请求自成一个事务（末尾「整段汇总」去重也按本次累计），
    上层据此决定「退回整段合成」还是「直接失败」。HTTP 状态码 → `_Rejected`（带 code）。
    """
    req = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json",
                           # 实测：这两个头一起给，服务端才按 SSE 分片推
                           "Accept": "text/event-stream",
                           "X-DashScope-SSE": "enable"}, method="POST")
    acc = bytearray()          # 本地址已发出的音频（用来识别末尾那片「整段汇总」）
    try:
        with _open_response(req, timeout, reuse_conn=reuse_conn) as resp:
            ctype = str(resp.headers.get("Content-Type", "") or "")
            if "event-stream" not in ctype:            # 服务端降级成了整段响应
                # ⚠️ 不能就地 `_decode_to_24k_mono(raw)`：本请求带的是 `format=pcm`
                # （裸 PCM、没有容器头），miniaudio 解不了；而且解失败会抛 TtsError，
                # 外层 `got == 0` 会直接 re-raise，**走不到**调用方的整段兜底
                # （离线复现：`音频解码失败：DecodeError ('failed to decode data', -1)`）。
                # 正解：重新发一次整段请求（`format=wav`），解码交给 `synthesize()`。
                _note(f"服务端没按 SSE 回（Content-Type={ctype!r}）→ 重新发一次整段合成（wav）")
                inp = payload.get("input") or {}
                hints = inp.get("language_hints") or []
                # ⚠️ 必须 `yield` 整块，不能 `yield from`：`synthesize()` 返回的是 **bytes**，
                #    `yield from bytes` 会把每个字节当成一个分片（int）吐出去。
                yield synthesize(
                    str(inp.get("text") or ""),
                    voice=str(inp.get("voice") or ""),
                    model=str(payload.get("model") or ""),
                    api_key=api_key,
                    language=str(hints[0]) if hints else None,
                    timeout=timeout,
                    endpoint=url,
                    reuse_conn=reuse_conn,
                )
                return
            # ⚠️ 新端点的 SSE 是**多行 JSON**：必须按规范累积 data 行到空行再解析
            # （逐行 json.loads 会一个分片都拿不到，见 `_iter_sse_events` 的说明）。
            for obj in _iter_sse_events(resp):
                _raise_if_error(obj)
                out = obj.get("output") or {}
                if isinstance(out, dict) and out.get("type") == "sentence-begin":
                    # 句子开头事件：`audio.data` 为空（实测），跳过即可 —— 它不是音频分片。
                    continue
                raw = _extract_audio(obj, timeout, reuse_conn=reuse_conn)
                if not raw:
                    continue
                pcm = _chunk_to_pcm(raw)
                if not pcm:
                    continue
                # ⚠️ 服务端在流末尾可能补发一片「整段汇总」（老端点实测与前面所有分片逐字节相同）：
                # 吃掉它，否则虚拟声卡会把整句念两遍。新端点是否也有，未定 —— 守卫留着无害。
                # 判据是**经验性**的：实测中真分片不会与已累计音频等长且逐字节相同
                # （要触发得正好等于此前所有分片之和 + 内容一致）—— 是经验保证，不是不变式。
                if acc and len(pcm) == len(acc) and pcm == bytes(acc):
                    continue
                acc += pcm
                yield pcm
    except HTTPError as exc:                            # ⚠️ 必须排在 URLError 前面（HTTPError 是它的子类）
        raise _Rejected(exc.code, _err_detail(exc)) from exc
    except URLError as exc:
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except TtsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc


def synthesize_stream(
    text: str,
    *,
    reuse_conn: bool = True,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    language: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    endpoint: str | None = None,
) -> Iterator[bytes]:
    """流式合成（SSE）：边生成边 yield 24k 单声道 s16le 的 PCM 分片。

    为什么值得：Qwen-Audio-TTS 3.1 实测同一句 20~30 字，首段音频 **~0.78~0.81s**、整段
    ~1.9s（旧的 qwen3-tts-flash 是首包 0.36~0.42s，但那个模型在 maas 网关已停服）；
    下游虚拟声卡是抖动缓冲（攒到 buffer_ms 起播 / 停更 0.35s 强制起播），
    拿到第一个分片就能开口 —— 比整段合成（~1.9s 才给音频）早约 1s。

    `endpoint`：同 `synthesize`（由调用方按线路派生传入，不传回落 ENDPOINT）。
    ⚠️ 退回整段时要把 endpoint **一路透传**给 `synthesize`，否则兜底那条腿会悄悄
    读回模块常量、连到默认线路去（切了海外线路时正是最难查的那种漂移）。

    退回策略（调用方不必写两套逻辑；**每一处降级都留痕，禁静默降级**）：
    - 服务端没给 `text/event-stream`（或一个分片都没拿到）→ 退回整段 `synthesize()`，yield 一整块；
    - 流式被**协议性**拒绝（HTTP 400/406/415）→ 退回整段 `synthesize()`；
    - 中途断了（网络/服务端）→ **保留已经 yield 的分片**，随后抛 `TtsStreamTruncated`
      （少半句，不整句丢；调用方据此给一条 warn 提示）。
    - 其余错误（401/403/429/5xx、网络不可达）直接抛：再发一次同样会失败，只会把
      失败延迟翻倍、白耗一次配额。
    - **不做跨域回落**：千问云把离线 TTS 换到了 Qwen-Audio-TTS，旧模型在新网关上不可用；
      维护者要求如实报错，不靠偷偷换域名掩盖（见 `_SSE_FALLBACK_CODES` 上方的说明）。
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("内容为空")
    if not (api_key or "").strip():
        raise TtsError("还没配置 API key（见界面右上角「设置」）")

    # 流式用 `format=pcm`（分片就是裸 PCM，`_chunk_to_pcm` 直接过，不需要解容器）
    payload = _tts_payload(text, voice=voice, model=model, language=language, fmt="pcm")
    _note_lang_gap(language)          # 官方不覆盖的语言（ru/es/th…）留一行痕，照常合成
    url = endpoint or ENDPOINT
    got = 0
    try:
        for pcm in _stream_once(url, payload, api_key, timeout, reuse_conn=reuse_conn):
            got += 1
            yield pcm
    except _Rejected as exc:
        if got:                                             # 已唱出去的部分不撤
            raise TtsStreamTruncated(_cut_note(got, exc)) from exc
        # 只有「协议不认流式」的码才值得退回整段：401/403/429/5xx 再发一次同样会失败，
        # 只会把失败延迟翻倍、白耗一次配额 → 直接抛。
        if exc.code not in _SSE_FALLBACK_CODES:
            raise
        _note(f"服务端不认流式（HTTP {exc.code}）→ 退回整段合成")
        try:
            yield synthesize(text, voice=voice, model=model, api_key=api_key,
                             language=language, timeout=timeout, endpoint=url)
        except TtsError:
            raise exc
        return
    except TtsError as exc:
        if got:
            raise TtsStreamTruncated(_cut_note(got, exc)) from exc
        raise
    except Exception as exc:  # noqa: BLE001
        if got:
            raise TtsStreamTruncated(_cut_note(got, exc)) from exc
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc
    if got:
        return
    # 流式没给东西 → 整段兜底（`endpoint=url` 透传当前地址，别让它悄悄读回模块常量）
    _note("流式一个分片都没拿到 → 退回整段合成")
    yield synthesize(text, voice=voice, model=model, api_key=api_key,
                     language=language, timeout=timeout, endpoint=url)


def synthesize_omni(
    text: str,
    *,
    reuse_conn: bool = True,
    voice: str = DEFAULT_OMNI_VOICE,
    model: str = DEFAULT_OMNI_MODEL,
    api_key: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
    endpoint: str | None = None,
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

    `endpoint`：音色试听走的是 chat/completions（与打字翻译同一条），由调用方按线路
    从 base_url 的 host 派生后传入（见 endpoints.chat_url）；不传回落 OMNI_ENDPOINT。
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
    req = Request(endpoint or OMNI_ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json",
                           "Accept": "text/event-stream"}, method="POST")
    b64: list[str] = []
    try:
        with _open_response(req, timeout, reuse_conn=reuse_conn) as r:
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
        raise _Rejected(exc.code, _err_detail(exc)) from exc
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
