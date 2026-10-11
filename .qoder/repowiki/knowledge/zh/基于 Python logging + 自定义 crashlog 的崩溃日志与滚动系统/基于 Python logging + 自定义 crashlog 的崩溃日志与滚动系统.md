---
kind: logging_system
name: 基于 Python logging + 自定义 crashlog 的崩溃日志与滚动系统
category: logging_system
scope:
    - '**'
source_files:
    - vlt/crashlog.py
    - tests/test_log_rotation.py
    - tests/test_loopback_log_throttle.py
    - tests/test_crashlog_stamp.py
    - vlt/engine.py
    - vlt/platform/audio.py
    - vlt/platform/win.py
---

## 1. 使用的框架与工具

- **标准库 `logging`**：模块内通过 `logging.getLogger(__name__)` 获取 logger（如 `vlt/engine.py`、`vlt/platform/audio.py`、`vlt/platform/win.py`），但仓库中实际使用点极少，绝大多数诊断输出走的是 `print()`。
- **标准库 `faulthandler`**：在进程原生崩溃（段错误/访问违规）时 dump 所有线程栈。
- **自定义 `vlt/crashlog.py`**：整个日志系统的核心，负责 stdout/stderr Tee、时间戳注入、文件切段、总量裁剪、zip 导出与脱敏。
- **测试覆盖**：`tests/test_log_rotation.py`、`tests/test_loopback_log_throttle.py`、`tests/test_crashlog_stamp.py`。

## 2. 关键文件

| 文件 | 职责 |
|---|---|
| `vlt/crashlog.py` | 安装崩溃钩子、Tee stdout/stderr、日志滚动、zip 导出、startup 信息写入 |
| `tests/test_log_rotation.py` | 验证 `_prune_logs`、单文件切段、`export_logs` 密钥脱敏 |
| `tests/test_loopback_log_throttle.py` | 验证设备枚举失败日志节流（同一理由只打一行，恢复补一行） |
| `tests/test_crashlog_stamp.py` | 验证 crashlog 时间戳注入行为 |
| `vlt/engine.py` | 使用 `logging.getLogger(__name__)` 的典型业务 logger；同时实现 loopback 枚举失败的节流逻辑 |
| `vlt/platform/audio.py` / `vlt/platform/win.py` | 使用 `logging.getLogger(__name__)` 的平台层 logger |
| `logs/` | 运行时日志目录（已 `.gitignore`），一次运行一个 `tag_YYYYMMDD_HHMMSS.log` 文件 |

## 3. 架构与约定

### 启动链路
1. 应用启动时调用 `crashlog.install(log_dir, tag)`，创建 `logs/tag_YYYYMMDD_HHMMSS.log`。
2. 用自定义 `_Tee` 类替换 `sys.stdout` 和 `sys.stderr`，把输出同时送到原流和日志文件。
3. 每条行首自动注入 `HH:MM:SS.mmm` 本地时间戳（UTC+8），由 `_Tee._stamp` 保证。
4. 将 root logger 的 handler 指向同一个 stderr Tee，格式为 `%(message)s`，级别设为 `INFO`（默认 `logging.lastResort` 是 WARNING，会丢弃 info 级消息）。
5. 启用 `faulthandler` 以捕获原生崩溃。
6. 接管 `sys.excepthook` 与 `threading.excepthook`，记录未捕获异常（主线程 + 子线程）。
7. Tk 应用额外调用 `crashlog.install_tk(root)` 接管 `report_callback_exception`。

### 日志滚动策略
- **单文件上限**：`MAX_LOG_FILE_BYTES = 2MB`，达到后 `_rotate()` 关闭旧句柄、打开 `xxx.N.log` 新段继续写，并重新启用 faulthandler 指向新段。
- **总量上限**：`MAX_LOG_TOTAL_BYTES = 5MB`，每写 500 次触发 `_prune_logs`，按 mtime 排序删除最旧的 `*.log*` 文件，当前正在写的文件永远保留。
- 切段而非停止写入——注释明确说明“日志末尾才是出问题的地方”。

### 日志导出与脱敏
- `export_logs(dest_zip, log_dir, extra_files)` 打包 `*.log*` + `config.yaml` + `config.yaml.broken`，生成带元信息的 zip。
- 打包前逐文件扫描 `sk-[A-Za-z0-9_-]{10,}` 正则，匹配则替换为 `sk-****（已脱敏）`，并在返回的 redacted 列表中标记被处理文件。
- 附带 `说明.txt`，包含导出时间、日志目录、文件数、原始字节数、脱敏文件清单。

### 日志节流
- `engine.pick_loopback_target()` 的枚举失败分支使用 `_note_loopback_enum_error` 函数，对同一错误理由只打印一次；从失败恢复到正常时补一行 ✅；理由变化立即再打一行。该行为由 `test_loopback_log_throttle.py` 断言。

### 启动信息
- `log_startup_info(tag)` 写入版本、Python 版本、平台、cwd、程序路径（含非 ASCII 字符警告）、git HEAD/dirty 状态、API 密钥（经 `mask_key` 脱敏）等诊断头。

## 4. 约定与约束

- **输出风格**：诊断输出以 `print()` 为主，格式为 `[tag] 消息`（如 `[startup]`、`[loopback]`、`[vmic]`），时间戳由 crashlog 统一加在前缀上，业务代码不自行格式化时间。
- **logger 命名**：使用 `logging.getLogger(__name__)` 作为模块级 logger，但仅少数模块（engine、platform.audio、platform.win）真正使用它。
- **root logger 级别**：`install()` 会把 root logger 级别提升到 `INFO`，确保 `logging.info(...)` 不被默认的 WARNING 阈值丢弃。
- **日志位置**：固定落在 `logs/` 目录下，文件名形如 `{tag}_{YYYYMMDD_HHMMSS}.log`，每个运行实例独立文件。
- **磁盘保护**：单文件 2MB 切段、总量 5MB 裁剪，由 `_prune_logs` 强制实施；测试 `test_log_rotation.py` 断言当前日志永不删除且切段后内容不丢失。
- **安全约束**：`export_logs` 在打包前对 `sk-*` 模式进行脱敏，测试断言 zip 中不会出现完整密钥（`FAKE_KEY not in blob`）。
- **线程安全**：`_Tee.write` 使用 `threading.Lock` 保护时间戳注入；stdout 的 Tee 负责体积控制，stderr 的 Tee 共享同一日志文件以避免互相关闭句柄（见 `install` 注释）。
- **容错设计**：所有日志相关 I/O 操作均 try/except 包裹，失败时仅向 `sys.__stderr__` 写一条告警且不中断主流程（`_stamp_warned`、`_rotate_warned` 去重）。