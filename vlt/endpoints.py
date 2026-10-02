"""出网端点的**单一真相源**：两条服务线路（千问云 / 阿里云百炼·国际版）的地址派生。

## 为什么要「宿主派生」

本项目有 4 条出网调用（实时 WS、打字翻译 HTTP、打字译音 TTS、音色试听 Omni），
过去每条腿各写一份国内域名常量（`maas.qianwenaiapi.com`），海外用户根本用不了。
支持两条**互斥**线路后，如果继续让每条腿各存一份域名，就会出现「切了线路、
但某条腿还连着老域名」这类最难查的漂移 —— 这正是 #12「同一件事写两遍漏一半」
的翻版。

所以这里定一条铁律：**`session.base_url` 是唯一的地址真相源**，另外两条 HTTP
端点（chat / multimodal）一律从它的 **host 派生**（`chat_url` / `multimodal_url`），
禁止在别处再写第二份域名常量。`provider` 只负责界面默认值 / key 槽 / 校验 /
日志摘要，**实际连接地址永远以 base_url 为准**。

## 关键事实：两条线路的三条路径**同形**

已按官方文档核实：千问云与百炼国际版只有 **host 不同**，三条路径后缀完全一致 ——

    WebSocket 实时：  /api-ws/v1/realtime
    OpenAI 兼容：     /compatible-mode/v1/chat/completions
    多模态（TTS）：    /api/v1/services/aigc/multimodal-generation/generation

正因同形，才可以「拿 base_url 的 host + 固定路径」拼出另外两条腿，
换线路时用户只需要改一个 base_url，三条腿自动跟着走。

## 依赖纪律

只依赖标准库（`urllib.parse`）。**绝不 import `vlt.engine`**：engine 反向依赖
本模块（接线时调 `chat_url` / `multimodal_url` / `describe`），import 回去会成环。
"""
from __future__ import annotations

from urllib.parse import urlsplit

# ---------------------------------------------------------------- 线路（provider）

PROVIDER_QIANWEN = "qianwen"
PROVIDER_BAILIAN_INTL = "bailian_intl"
PROVIDERS = (PROVIDER_QIANWEN, PROVIDER_BAILIAN_INTL)
DEFAULT_PROVIDER = PROVIDER_QIANWEN

# 线路中文名（**只进日志**，不是界面文案；界面文案第 2 轮走 t()）。
_PROVIDER_CN = {
    PROVIDER_QIANWEN: "千问云",
    PROVIDER_BAILIAN_INTL: "阿里云百炼·国际版",
}

# ---------------------------------------------------------------- 路径（两条线路同形）

WS_PATH = "/api-ws/v1/realtime"
CHAT_PATH = "/compatible-mode/v1/chat/completions"
MULTIMODAL_PATH = "/api/v1/services/aigc/multimodal-generation/generation"

# ---------------------------------------------------------------- host

QIANWEN_HOST = "maas.qianwenaiapi.com"
# 百炼国际版 host 模板：`{workspace_id}` 是**字面量占位符**（由 resolve_base_url 替换），
# `{region}` 由 default_base_url 用实参代入。
BAILIAN_HOST_TMPL = "{workspace_id}.{region}.maas.aliyuncs.com"

# 国际站地域（id, 英文显示名）。显示名是英文、**不进词表**（不是界面文案）。
REGIONS: tuple[tuple[str, str], ...] = (
    ("ap-southeast-1", "Singapore"),
    ("ap-northeast-1", "Japan (Tokyo)"),
    ("us-east-1", "US (Virginia)"),
    ("eu-central-1", "Germany (Frankfurt)"),
    ("cn-hongkong", "China (Hong Kong)"),
)
DEFAULT_REGION = "ap-southeast-1"

# 注册/开通入口（界面「去哪申请 key」按钮用；未知 provider 回落千问云那条）。
# 百炼国际版这里给的是**新加坡地域的模型市场页**（用户实测确认的地址）：进去就能看到
# 模型清单与「开通 / 创建 API key」入口，比控制台首页少点两下。地域段固定写
# `ap-southeast-1`（= DEFAULT_REGION）；在别的国际站地域建了空间的用户，页面内可自己切地域。
SIGNUP_URLS = {
    PROVIDER_QIANWEN: "https://www.qianwenai.com/",
    PROVIDER_BAILIAN_INTL: ("https://modelstudio.console.alibabacloud.com"
                            "/ap-southeast-1/model/market"),
}


# ---------------------------------------------------------------- 归一化（非法值回落 + 留痕，绝不静默）


def normalize_provider(raw: object) -> str:
    """把任意取值规范成一个合法线路 id；非法值**留痕后**回落默认（qianwen）。

    为什么不直接抛：配置是手写的，写错一个字母就让程序起不来太粗暴；
    但**绝不静默**——打一行告诉用户「按默认线路处理」，否则「我明明填了海外线路、
    怎么还连国内」根本无从查起。
    """
    val = str(raw).strip().lower()
    if val in PROVIDERS:
        return val
    print(f"[endpoints] ⚠️ 未识别的服务线路 {raw!r}，按默认 {DEFAULT_PROVIDER} 处理",
          flush=True)
    return DEFAULT_PROVIDER


def normalize_region(raw: object) -> str:
    """把任意取值规范成一个合法地域 id；非法值**留痕后**回落默认（新加坡）。

    与 `normalize_provider` 同一纪律：脏值不致命，但必须留痕。
    """
    val = str(raw).strip().lower()
    if any(val == rid for rid, _ in REGIONS):
        return val
    print(f"[endpoints] ⚠️ 未识别的地域 {raw!r}，按默认 {DEFAULT_REGION} 处理", flush=True)
    return DEFAULT_REGION


def provider_name(provider: str) -> str:
    """线路的中文名（日志/诊断用）。未知 provider 先归一化再取名，绝不 KeyError。"""
    return _PROVIDER_CN[normalize_provider(provider)]


def signup_url(provider: str) -> str:
    """该线路的注册入口 URL；未知 provider 回落千问云那条（绝不 KeyError）。"""
    return SIGNUP_URLS.get(normalize_provider(provider), SIGNUP_URLS[DEFAULT_PROVIDER])


# ---------------------------------------------------------------- 地址派生


def default_base_url(provider: str, workspace_id: str = "",
                     region: str = DEFAULT_REGION) -> str:
    """某条线路的**默认** base_url（实时 WS 端点，唯一真相源）。

    - qianwen      → `wss://maas.qianwenaiapi.com/api-ws/v1/realtime`
    - bailian_intl → `wss://{workspace_id}.{region}.maas.aliyuncs.com/api-ws/v1/realtime`

    ⚠️ 百炼线路**刻意保留字面量 `{workspace_id}` 占位符**（只用实参代入 region）：
    这样用户日后改 workspace_id 不必回头改 base_url —— 占位符由 `resolve_base_url`
    在连接时用真正的 `session.workspace_id` 替换。故 `workspace_id` 形参在此**不参与**
    拼接（保留在签名里只为对称/未来扩展）。
    """
    prov = normalize_provider(provider)
    reg = normalize_region(region)
    if prov == PROVIDER_BAILIAN_INTL:
        host = BAILIAN_HOST_TMPL.format(workspace_id="{workspace_id}", region=reg)
        return f"wss://{host}{WS_PATH}"
    return f"wss://{QIANWEN_HOST}{WS_PATH}"


def resolve_base_url(base_url: str, workspace_id: str = "") -> str:
    """把 base_url 里的 `{workspace_id}` 占位符换成实参，返回可直接连接的地址。

    占位符在但 workspace_id 为空 → 抛 ValueError（**绝不**拿空串拼出
    `wss://.ap-southeast-1...` 这种残废 host 再连上去，那样只会得到一个看不懂的
    握手失败）。异常里只讲「去哪找 workspace_id」，绝不含 key。
    """
    base = str(base_url or "")
    if "{workspace_id}" in base:
        ws = str(workspace_id or "").strip()
        if not ws:
            raise ValueError(
                "该 base_url 需要 workspace_id（百炼控制台「业务空间详情 → API Host」的前缀）")
        base = base.replace("{workspace_id}", ws)
    return base


def host_of(base_url: str, workspace_id: str = "") -> str:
    """从 base_url 取出 host（netloc）——另外两条 HTTP 端点都靠它派生。

    解析不出 host（没写 scheme、空串等）→ 抛 ValueError，绝不返回空串让调用方
    拼出 `https:///compatible-mode/...` 这种残废地址。

    ⚠️ 异常消息**刻意不回显 base_url / 解析后的地址**：百炼的 host 形如
    `{workspace_id}.{region}.maas.aliyuncs.com`，一旦用户手写成缺 `wss://` 的地址，
    占位符替换后的串里就带着**完整业务空间 ID**，回显进异常＝把它打进日志
    （config / engine 都会打印捕获到的异常）。这违反「日志/异常绝不出现完整空间 ID」
    的纪律，故这里只讲原因、不带值 —— 真正的 base_url 用户在自己的 config.yaml 里看得到。
    """
    resolved = resolve_base_url(base_url, workspace_id)
    netloc = urlsplit(resolved).netloc
    if not netloc:
        raise ValueError("无法从 base_url 解析出 host（scheme 缺失或地址为空）")
    return netloc


def chat_url(base_url: str, workspace_id: str = "") -> str:
    """打字翻译 / 音色试听（OpenAI 兼容 chat/completions）端点：从 base_url 的 host 派生。"""
    return f"https://{host_of(base_url, workspace_id)}{CHAT_PATH}"


def multimodal_url(base_url: str, workspace_id: str = "") -> str:
    """打字译音 TTS（多模态生成）端点：从 base_url 的 host 派生。"""
    return f"https://{host_of(base_url, workspace_id)}{MULTIMODAL_PATH}"


def key_slot(provider: str) -> str:
    """该线路对应的**密钥槽名**（就是线路 id 本身）。

    千问云与百炼的 key **不通用**，故分槽各存一份（见 credentials.py），切线路不用重填。
    """
    return normalize_provider(provider)


def _mask_workspace_in_host(host: str, workspace_id: str) -> str:
    """把 host 首段里的业务空间 ID 打码成 `llm-ab…`，其余（地域 + 域名）原样保留。

    为什么连 host 也要打码：百炼的 host 首段**就是**业务空间 ID
    （`llm-xxx.ap-southeast-1.maas.aliyuncs.com`），照原样打日志等于把账号标识写进
    用户磁盘上的日志文件。而排查「切了线路没生效」只需要看清**线路 + 地域 + 域名形态**，
    不需要 ID 全文 —— 打码成前 6 字符足够对上控制台里的那一条。
    真要精确地址时用户自己看 config.yaml 的 `session.base_url` 即可。
    """
    ws = str(workspace_id or "").strip()
    if ws and host.lower().startswith(ws.lower() + "."):
        return f"{ws[:6]}…{host[len(ws):]}"
    return host


def describe(provider: str, region: str, base_url: str, workspace_id: str = "") -> str:
    """一行日志摘要：线路 + 地域 + host（空间 ID 段已打码）。换线路排查时第一眼要看它。

    形如：`线路=阿里云百炼·国际版(ap-southeast-1) host=llm-ab….ap-southeast-1.maas.aliyuncs.com`

    **绝不抛**：host 解析不出时降级成一个占位串，日志本身不能把启动搞挂；
    **API key 一律不出现**，业务空间 ID 只留前 6 字符（见 _mask_workspace_in_host）。
    """
    prov = normalize_provider(provider)
    reg = normalize_region(region)
    try:
        host = _mask_workspace_in_host(host_of(base_url, workspace_id), workspace_id)
    except ValueError:
        host = "<host 解析失败>"
    text = f"线路={_PROVIDER_CN[prov]}"
    if prov == PROVIDER_BAILIAN_INTL:
        text += f"({reg})"
    return text + f" host={host}"
