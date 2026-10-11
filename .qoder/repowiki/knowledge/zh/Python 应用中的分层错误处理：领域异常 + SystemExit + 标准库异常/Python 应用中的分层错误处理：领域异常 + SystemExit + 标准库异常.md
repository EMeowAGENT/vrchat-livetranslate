---
kind: error_handling
name: Python 应用中的分层错误处理：领域异常 + SystemExit + 标准库异常
category: error_handling
scope:
    - '**'
source_files:
    - vlt/tts.py
    - vlt/app.py
    - vlt/ui_text.py
    - vlt/update_check.py
    - vlt/config.py
    - vlt/credentials.py
    - vlt/config_io.py
    - vlt/crashlog.py
    - vlt/gui_update.py
---

## 1. 总体方案

仓库是 Python 桌面应用（Tk GUI + asyncio），没有引入第三方异常框架。错误处理采用**分层策略**：
- **进程级退出**：入口层用 `SystemExit` 表达「参数/配置错误」，由 `main()` 统一 `raise SystemExit(main())` 或 `asyncio.run(run(args))` 捕获后返回非零退出码。
- **领域级异常**：在需要向调用方传递语义化错误时定义少量自定义 Exception 子类（如 `TtsError`、`UpdateCheckError`、`_DownloadCancelled`）。
- **系统级异常**：IO、网络、解析等底层错误直接抛出标准库异常（`OSError`、`FileNotFoundError`、`ValueError`、`HTTPError`、`URLError`、`RuntimeError`、`AttributeError`），由上层 try/except 捕获并包装为领域异常或直接处理。

没有全局 panic/recover 式机制；所有错误通过异常向上冒泡或由明确的 try/except 分支消费。

## 2. 关键文件与类型

| 文件 | 角色 |
|---|---|
| `vlt/tts.py` | 最完整的领域异常示例：定义 `TtsError(RuntimeError)` 和 `TtsStreamTruncated(TtsError)`，并在同步/流式合成路径中把 HTTP/URL/JSON/解码错误统一包装成 `TtsError` |
| `vlt/ui_text.py` | 定义 `_DownloadCancelled(Exception)`，用于下载线程取消信号 |
| `vlt/update_check.py` | 定义 `UpdateCheckError(RuntimeError)` |
| `vlt/app.py` | 顶层入口：`run()` 里 `raise SystemExit(...)` 表达配置错误；`KeyboardInterrupt` 被捕获后打印提示并返回 130 |
| `vlt/config.py` | 启动期校验失败走 `SystemExit`（缺 API key、方向 key 不存在） |
| `vlt/credentials.py` | 参数校验抛 `ValueError`（槽名非法、密钥为空/含空白/长度不足） |
| `vlt/config_io.py` | 写回 YAML 失败抛 `RuntimeError`（带原始 exc） |
| `vlt/crashlog.py` | 崩溃日志兜底：大量 `except OSError` / `except Exception as exc: # noqa: BLE001`，保证日志本身不挂进程 |
| `vlt/gui_update.py` | 更新流程：构造 `FileNotFoundError(path)` 表示缺失包，`_DownloadCancelled` 表示用户取消 |
| `vlt/ui_text.py` | 本地播放失败抛 `RuntimeError(f"pw-cat 播放失败：退出码 {res.returncode}")` |

## 3. 架构与约定

### 3.1 领域异常继承链
`tts.py` 定义了清晰的层次：
```python
class TtsError(RuntimeError): ...
class TtsStreamTruncated(TtsError): ...
```
`update_check.py` 的 `UpdateCheckError(RuntimeError)` 与之并列。调用方可以 `except TtsError` 捕获所有 TTS 相关错误，同时用 `isinstance(exc, TtsStreamTruncated)` 区分「部分音频已输出」的特殊场景。

### 3.2 错误包装模式
`tts.py` 中几乎所有外部 I/O 都遵循同一模式：
```python
try:
    ...  # HTTP/JSON/base64/miniaudio 调用
except HTTPError as exc:
    raise TtsError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
except URLError as exc:
    raise TtsError(f"网络不可达：{exc.reason}") from exc
except Exception as exc:  # noqa: BLE001
    raise TtsError(f"{type(exc).__name__}: {exc}") from exc
```
使用 `from exc` 保留原始堆栈，但对外只暴露人类可读消息（注释明确说「消息给用户看，带原因不带堆栈」）。

### 3.3 降级与重试
`tts._post_pooled` 对连接层异常做**最多一次重连**：
```python
for attempt in (1, 2):
    conn, path, key = _open_conn(url, timeout)
    try:
        ...
    except (http.client.HTTPException, OSError) as exc:
        last = exc
        _release_conn(key, conn, reusable=False)
        if attempt == 1:
            _note(f"复用连接失效...→ 重连一次")
            continue
        raise
```
而 `synthesize_stream` 对服务端 SSE 协议拒绝（400/406/415）自动**退回整段合成**，其他错误（401/403/429/5xx）直接上抛——因为再发一次同样失败且白耗配额。

### 3.4 流式中断语义
`TtsStreamTruncated` 承载「已经 yield 出去的分片有效，不要整句丢弃」的语义。调用方约定把它当 warn 级提示，而不是「合成失败」。这是该仓库中最具业务特色的错误语义。

### 3.5 进程退出规范
- CLI 入口统一：`app.py`、`gui.py`、`selfcheck.py` 末尾都是 `raise SystemExit(main())`。
- 启动期配置错误用 `raise SystemExit("...")`（见 `config.py`、`app.py`）。
- `KeyboardInterrupt` 捕获后打印 `[app] 已停止（Ctrl+C）` 并返回 130（POSIX SIGINT 的标准退出码）。

### 3.6 静默降级的约束
`tts._note` 的注释声明了仓库约定：**禁静默降级，每一处降级都要能查**。`_note` 自身用 try/except 包裹 print，确保日志写入失败不会反噬主流程。`tts.py` 中所有降级路径（连接池回退直连、SSE 回退整段、流中断保留分片）都通过 `_note` 留痕。

## 4. 约定与约束

- **观察到的约定**：
  - 领域错误优先用自定义 `*Error(RuntimeError)` 子类，而非裸 `RuntimeError`。
  - 外部 I/O 错误一律 `raise X(...) from exc` 保留原始堆栈。
  - 启动期参数/配置错误用 `SystemExit`，运行时业务错误用自定义异常。
  - 流式 API 的中断用专门的 `TtsStreamTruncated` 表达「部分成功」语义。
  - 所有降级路径通过 `_note` 落盘，禁止静吞错误。
  - 捕获宽泛异常时使用 `# noqa: BLE001` 标记（表明是有意放宽 ruff 的 BLE001 规则）。

- **可验证的规则**：
  - `TtsError` 及其子类 `TtsStreamTruncated` 是唯一在 `vlt/tts.py` 中定义的领域异常基类，所有 TTS 路径最终抛出它们（代码中所有 `raise TtsError` / `raise TtsStreamTruncated` 均指向这两个类）。
  - 三个入口模块（`app.py`、`gui.py`、`selfcheck.py`）均以 `raise SystemExit(main())` 结尾，形成统一的进程退出契约。
  - `KeyboardInterrupt` 仅在 `app.py` 的 `run()` 和 `main()` 两处被捕获，其余模块未捕获，说明 Ctrl+C 的处理集中在 P1 主程序入口。
  - 下载取消信号 `_DownloadCancelled` 仅被 `ui_text.py` 内部使用，注释明确其生命周期：在下载线程抛出 → `download_and_verify` 清理 `.new` 后原样上抛，调用方无需额外处理。

- **未发现的模式**：
  - 没有全局错误处理器、中间件、错误码枚举或结构化错误响应对象。
  - 没有 `logging.exception` 集中上报；错误信息以字符串形式随异常携带。
  - 没有重试装饰器或通用重试库；重试逻辑内联在 `tts._post_pooled` 中。
