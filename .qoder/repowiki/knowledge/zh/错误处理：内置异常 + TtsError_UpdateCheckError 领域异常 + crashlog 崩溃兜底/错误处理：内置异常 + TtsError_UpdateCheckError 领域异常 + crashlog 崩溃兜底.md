---
kind: error_handling
name: 错误处理：内置异常 + TtsError/UpdateCheckError 领域异常 + crashlog 崩溃兜底
category: error_handling
scope:
    - '**'
source_files:
    - vlt/crashlog.py
    - vlt/tts.py
    - vlt/update_check.py
    - vlt/app.py
    - vlt/config.py
    - vlt/config_io.py
    - vlt/credentials.py
    - vlt/gui.py
    - vlt/selfcheck.py
    - tests/test_crashlog_stamp.py
    - tests/test_play_pcm_local.py
---

## 1. 总体方案

仓库是 Python 桌面应用（Tk GUI + 音频采集 + VRChat OSC），没有引入第三方错误框架。错误处理采用三层结构：

- **业务层**：使用 Python 内置异常（`ValueError`、`FileNotFoundError`、`OSError`、`SystemExit`）表达参数校验失败、配置缺失、IO 失败等；在少数领域场景定义自定义异常子类。
- **诊断层**：`vlt/crashlog.py` 通过 `sys.excepthook`、`threading.excepthook`、`faulthandler`、Tk `report_callback_exception` 四道钩子，把原生崩溃、未捕获异常、Tk 回调异常全部写入 `logs/` 下的带时间戳日志文件，并做 stdout/stderr Tee。
- **测试层**：用 pytest 的 `raises` / 直接断言异常类型验证错误路径。

没有统一的错误码枚举、没有全局中间件、没有 panic/recover（Python 无此概念）。

## 2. 关键文件与包

- `vlt/crashlog.py`：崩溃日志基础设施（Tee、滚动切段、日志脱敏、启动信息注入）。
- `vlt/tts.py`：定义领域异常 `TtsError(RuntimeError)`，集中包装 HTTP/网络/解码错误。
- `vlt/update_check.py`：定义领域异常 `UpdateCheckError(RuntimeError)`。
- `vlt/app.py`、`vlt/config.py`、`vlt/gui.py`、`vlt/selfcheck.py`：顶层入口统一用 `raise SystemExit(...)` 终止进程并返回非零退出码。
- `vlt/credentials.py`、`vlt/config_io.py`、`vlt/ui_text.py`、`vlt/gui_update.py`：参数校验与 IO 失败抛出 `ValueError` / `RuntimeError` / `FileNotFoundError`。
- `tests/test_crashlog_stamp.py`、`tests/test_play_pcm_local.py`：对异常路径进行断言。

## 3. 架构与约定

### 3.1 领域异常
仅两个自定义异常类，均继承自 `RuntimeError`：

```python
# vlt/tts.py:239
class TtsError(RuntimeError): ...
# vlt/update_check.py:58
class UpdateCheckError(RuntimeError): ...
```

`tts.py` 中所有 TTS 相关错误都经 `TtsError` 包装后抛出，包括：
- 音频解码失败（`HTTPError` → `TtsError`）
- 下载失败（`requests.RequestException` → `TtsError`）
- base64 解析失败
- 空内容 / 未配置 API key
- HTTP 状态码错误（`urllib.error.HTTPError` → `TtsError(f"HTTP {exc.code}: ...") from exc`）
- 网络不可达（`TtsError(f"网络不可达：{exc.reason}") from exc`）

### 3.2 进程级退出
CLI 入口统一以 `raise SystemExit(message)` 结束，并在 `__main__` 处 `raise SystemExit(main())` 传递退出码：

- `vlt/app.py:165`：`raise SystemExit(main())`，Ctrl+C 返回 130。
- `vlt/config.py:104`：找不到 API key 时 `raise SystemExit("找不到 API key：...")`。
- `vlt/config.py:177`：方向 key 不存在时 `raise SystemExit(f"配置里没有方向的 key：...")`。
- `vlt/gui.py:497`、`vlt/selfcheck.py:201`：同样模式。

### 3.3 参数校验
`vlt/credentials.py` 集中校验密钥槽名与密钥值，全部抛 `ValueError`，消息走 i18n `t()`：

```python
raise ValueError(t("密钥不能为空"))
raise ValueError(t("密钥不能包含空白字符"))
raise ValueError(t("密钥长度不足（{n} < 16）", n=len(stripped)))
```

### 3.4 崩溃日志（crashlog）
`install(log_dir, tag)` 完成以下初始化（幂等）：

1. 创建 `logs/{tag}_{timestamp}.log`，stdout/stderr 被 `_Tee` 同时输出到原流和日志文件。
2. 每行自动加 `HH:MM:SS.mmm` 本地时间戳。
3. 单文件超 `MAX_LOG_FILE_BYTES`（2MB）切段，总量超 `MAX_LOG_TOTAL_BYTES`（5MB）删最旧。
4. 启用 `faulthandler.enable(all_threads=True)` 记录原生崩溃栈。
5. 替换 `sys.excepthook` 与 `threading.excepthook`，记录主线程与子线程未捕获异常。
6. 调用 `install_tk(root)` 接管 Tk 回调异常（默认只打印不退出）。
7. 若 root logger 无 handler，附加一个 `StreamHandler(sys.stderr)`，格式为 `%(message)s`，级别设为 `INFO`，使 `logging.info` 也能落到同一日志文件。

`export_logs(dest_zip, ...)` 提供打包导出能力，打包前用正则 `sk-[A-Za-z0-9_\-]{10,}` 对 API key 做脱敏。

### 3.5 logging 使用
项目内使用标准库 `logging.getLogger(__name__)`，但不在模块内 `basicConfig`，而是由 `crashlog.install` 统一接管 root logger。典型用法：

```python
# vlt/engine.py:19
log = logging.getLogger(__name__)
# vlt/platform/audio.py:27
log = logging.getLogger(__name__)
# vlt/platform/win.py:22
log = logging.getLogger(__name__)
```

## 4. 约定与约束

- **业务异常优先使用内置异常**：参数非法用 `ValueError`，文件缺失用 `FileNotFoundError`，IO 失败用 `OSError`，进程退出用 `SystemExit`。未见自定义基类或统一错误响应对象。
- **领域异常继承 `RuntimeError`**：`TtsError`、`UpdateCheckError` 都是 `RuntimeError` 的子类，便于上层按基类捕获。
- **异常链保留原始异常**：多处使用 `from exc` 保留原始堆栈（如 `config_io.py:282`、`tts.py` 中多个位置）。
- **i18n 化用户可见错误**：`credentials.py` 中的 `ValueError` 消息通过 `t()` 翻译，面向用户的错误文本不硬编码英文。
- **崩溃兜底必须安装**：GUI 启动时必须先 `crashlog.install(...)` 再建 Tk 根窗口，然后 `crashlog.install_tk(root)`，否则 Tk 回调异常会静默吞掉。
- **日志安全**：`export_logs` 强制对 `sk-xxxx` 形式的密钥做脱敏；`log_startup_info` 通过 `mask_key` 显示脱敏后的 API key。
- **日志体积有硬性上限**：`MAX_LOG_FILE_BYTES = 2 * 1024 * 1024`、`MAX_LOG_TOTAL_BYTES = 5 * 1024 * 1024`，超出即切段/删除最旧文件，避免磁盘写满。
- **测试中对异常路径的断言**：`tests/test_play_pcm_local.py:test_linux_playback_failure_raises` 验证 Linux 播放失败抛出预期异常；`tests/test_crashlog_stamp.py` 验证 `logging.info` 能落入 crashlog 生成的日志文件。
- **未观察到**：全局错误中间件、统一错误码枚举、`try/except Exception` 吞异常的通用封装器、panic/recover 机制。