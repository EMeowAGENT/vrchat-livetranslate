---
kind: logging_system
name: 基于 Python logging + 自定义 crashlog 的日志系统
category: logging_system
scope:
    - '**'
source_files:
    - vlt/crashlog.py
    - vlt/engine.py
    - vlt/platform/audio.py
    - vlt/platform/win.py
    - tests/test_crashlog_stamp.py
    - tests/test_log_rotation.py
    - tests/test_loopback_log_throttle.py
---

## 1. 使用的框架与工具

- **Python 标准库 `logging`**：模块级 logger 通过 `log = logging.getLogger(__name__)` 获取（如 `vlt/engine.py`、`vlt/platform/audio.py`、`vlt/platform/win.py`），使用 `.debug()` / `.warning()` 等级别。
- **自定义崩溃日志子系统 `vlt/crashlog.py`**：不依赖第三方日志框架，自行实现 stdout/stderr Tee、文件滚动、faulthandler 集成、未捕获异常钩子、Tk 回调异常接管。
- **无结构化日志字段**：所有输出均为纯文本行，没有 JSON/键值对结构；时间戳由 `_Tee._stamp` 在每行行首追加 `HH:MM:SS.mmm`（本地 UTC+8）。

## 2. 关键文件

- `vlt/crashlog.py` — 崩溃日志安装、Tee、滚动、导出、启动信息写入。
- `vlt/engine.py` — 业务模块中 `logging.getLogger(__name__)` 的典型用法示例。
- `vlt/platform/audio.py`、`vlt/platform/win.py` — 平台层使用 `log.warning` / `log.debug` 记录音频采集相关事件。
- `tests/test_crashlog_stamp.py`、`tests/test_log_rotation.py`、`tests/test_loopback_log_throttle.py` — 覆盖日志时间戳、滚动、节流行为。

## 3. 架构与约定

### 3.1 启动流程
应用启动时调用 `crashlog.install(log_dir, tag)`，完成以下初始化（幂等）：
1. 创建 `logs/<tag>_<YYYYMMDD_HHMMSS>.log`。
2. 用自定义 `_Tee` 替换 `sys.stdout` / `sys.stderr`，把输出同时写回原流和日志文件，并在每行行首加时间戳。
3. 若 root logger 无 handler，附加一个 `StreamHandler(sys.stderr)`（即同一个 Tee），格式为 `%(message)s`，并将 root level 从默认的 WARNING 降到 INFO，确保 `logging.info(...)` 能输出。
4. 启用 `faulthandler.enable(file=_LOG_FILE, all_threads=True)`，让原生段错误也能落盘。
5. 挂接 `sys.excepthook` 与 `threading.excepthook`，统一以分隔线头打印未捕获异常。
6. 通过 `crashlog.install_tk(root)` 接管 Tk 回调异常（默认只打印不退出）。

### 3.2 日志滚动与清理
- 单文件上限 `MAX_LOG_FILE_BYTES = 2MB`，达到后 `_Tee._rotate` 关闭当前文件并打开 `<stem>.<seg>.log`，同时 `faulthandler` 重新指向新段。
- 总磁盘上限 `MAX_LOG_TOTAL_BYTES = 5MB`，按 mtime 升序删除最旧的非当前日志文件。
- 滚动失败或加戳失败会向 `sys.__stderr__` 写一次告警，不影响主流程。

### 3.3 日志导出
`export_logs(dest_zip, ...)` 收集 `*.log*` 以及 `config.yaml` / `config.yaml.broken`，打包前用正则 `sk-[A-Za-z0-9_\-]{10,}` 脱敏 API Key，返回 `(zip路径, 文件数, 原始字节数, 已脱敏文件列表)`。

### 3.4 启动诊断信息
`log_startup_info(tag)` 写入版本、python/platform、git HEAD、程序路径（含非 ASCII 字符警告）、经 `mask_key` 脱敏后的 API Key 摘要。

### 3.5 业务代码中的日志风格
- 模块级 logger：`log = logging.getLogger(__name__)`。
- 使用级别：`debug`（Windows mic callback 状态）、`warning`（设备打不开、线程未退出、采集源关闭失败等）。未见 `info` / `error` 的直接调用。
- 大量诊断走 `print(f"[xxx] ...", flush=True)`（带方括号标签），与 crashlog 的行首时间戳配合。

## 4. 约定与约束

- **日志位置**：`logs/` 目录（已在 gitignore 中），一次运行生成一个 `tag_timestamp.log`，滚动后追加 `.N.log` 后缀。
- **时间戳格式**：每行行首 `HH:MM:SS.mmm`，注释明确说明是「本机本地时间 = UTC+8」。
- **root logger 级别**：`crashlog.install` 会把 root level 设为 INFO（如果之前是 NOTSET 或 WARNING），保证 `logging.info` 不被 `logging.lastResort` 丢弃。
- **日志格式**：仅 `%(message)s`，与 print 风格一致，不添加额外字段。
- **文件大小策略**：单文件 2MB 切段，总量 5MB 删最旧；当前正在写的文件永不删除。
- **密钥保护**：日志中不应出现完整密钥；`export_logs` 二次扫描 `sk-xxxx` 模式进行脱敏。
- **线程安全**：`_Tee.write` 内部持有 `threading.Lock`，避免多线程并发写入破坏行结构。
- **异常隔离**：Tee 的每个下游写入都 try/except pass，日志文件损坏不会阻断主程序；faulthandler 失败也静默忽略。
- **测试覆盖**：`test_crashlog_stamp.py` 验证时间戳注入、`test_log_rotation.py` 验证滚动、`test_loopback_log_throttle.py` 验证节流逻辑。