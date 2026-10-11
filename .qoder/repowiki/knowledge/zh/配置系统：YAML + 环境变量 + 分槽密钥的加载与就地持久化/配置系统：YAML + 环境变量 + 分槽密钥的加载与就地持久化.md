---
kind: configuration_system
name: 配置系统：YAML + 环境变量 + 分槽密钥的加载与就地持久化
category: configuration_system
scope:
    - '**'
source_files:
    - vlt/config.py
    - vlt/config_io.py
    - vlt/credentials.py
    - config.example.yaml
---

## 1. 总体方案

应用使用 **PyYAML** 解析 `config.yaml`，并通过 `DASHSCOPE_API_KEY` 环境变量和百炼 CLI 的 `~/.bailian/config.json` 补充 API key。配置文件模板 `config.example.yaml` 随程序分发，首次运行复制到用户目录下的 `config.yaml`（该文件被 `.gitignore`，不入库）。API key 不落进 `config.yaml`，而是单独保存在 `%USERPROFILE%/.vrchat-livetranslate/api_key.txt`（千问云槽）或 `api_key_<slot>.txt`（其它线路槽）。

核心模块分工：
- `vlt/config.py`：读取 YAML、校验/归一化字段、合并热词、构建 `AppConfig` / `Direction` dataclass。
- `vlt/config_io.py`：对 `config.yaml` 做**就地文本改写**（保留注释、空行、键顺序），供 GUI 热重载。
- `vlt/credentials.py`：API key 的分槽保存/读取/打码/来源判定。

## 2. 关键文件

- `vlt/config.py` — 配置加载主入口 `load_config()`、`ensure_config()`、`load_api_key()`、`merge_hotwords()`。
- `vlt/config_io.py` — `_yaml_set_in_text` / `_yaml_set_or_create` / `_yaml_set_mapping` / `write_leaf`。
- `vlt/credentials.py` — `save_api_key` / `load_saved_key` / `mask_key` / `key_source`。
- `config.example.yaml` — 全量配置模板（session / capture / directions / glossary / chatbox / merger / overlay / desktop_overlay / output / text_input / room / ui）。
- `vlt/endpoints.py`（被引用）— provider 枚举、base_url 派生、`key_slot` 映射。
- `vlt/session/base.py`（被引用）— 会话默认值常量（`DEFAULT_FINAL_SILENCE_S` 等）。

## 3. 架构与约定

### 3.1 配置加载流程

1. `ensure_config()`：若 `APP_DIR/config.yaml` 不存在且 `BUNDLE_DIR/config.example.yaml` 存在，则复制模板生成。
2. `load_config()`：用 `yaml.safe_load` 解析；若 YAML 语法损坏，备份为 `<name>.broken` 并从模板重建，保证下次仍可启动。
3. 解析 `session` 段：provider 经 `endpoints.normalize_provider` 归一化；`base_url` 走 `endpoints.default_base_url`；老 `bailian_intl` 地址自动迁移到千问云·海外版并留痕。
4. 构造 `session_base` dict，再遍历 `directions` 构建 `Direction` dataclass；若无方向则默认 `mine: zh→en`。
5. 组装 `output.capture`、`output.audio.proxy`、`text_input.tts` 等子结构，最终返回 `AppConfig`。

### 3.2 字段校验策略

所有手写数值型配置都通过 `_opt_float` / `_float_or_default` / `_int_clamped` 包装：**非法值 → 打印 `[config] ⚠️ ...` 并回落默认值**，绝不抛异常让程序起不来。映射表（`glossary`、`hotwords`）通过 `_as_str_map` 清洗：非 dict 直接丢弃并警告，空键/空值条目跳过。

### 3.3 Provider 与 base_url 一致性

`_warn_provider_host_mismatch()` 比较 `provider` 与 `base_url` 解析出的 host，不一致时只 WARN（不报错），以 `base_url` 为准——因为用户可能手改了其中一个而忘记改另一个。

### 3.4 API Key 优先级（`load_api_key`）

显式参数 → 界面保存（按 slot 分槽）→ `DASHSCOPE_API_KEY` 环境变量 → `~/.bailian/config.json`（兼容 `api_key` / `apiKey` / `DASHSCOPE_API_KEY` 三个键名）→ 抛 `SystemExit`。

- 前两条按 slot 区分（千问云 vs 千问云·海外版各存一份）。
- 后两条是阿里官方共用，不按 slot 区分。
- 日志中 key 一律经 `mask_key` 打码（如 `sk-****3f7a`）。

### 3.5 热词合并口径

`merge_hotwords(glossary, hotwords)` 是唯一合并点：方向级 `hotwords` 覆盖全局 `glossary`。`Direction.to_session_config` 与 `AppConfig.merged_hotwords` 都走此函数，避免“说话时对、打字时不对”的漂移。

### 3.6 配置持久化（config_io）

GUI 修改设置不走 `yaml.dump` 整文件重写（会抹掉注释、重排键序），而是 `config_io.py` 中的正则+缩进解析器原地替换单行叶子值：

- `_yaml_set_in_text`：父路径必须存在，否则静默 no-op。
- `_yaml_set_or_create`：父路径缺失时按缩进补建整条链。
- `_yaml_set_mapping`：整段块映射（如 `glossary:`）整体替换，内部注释会被覆盖（说明写在段上方）。
- `_write_config_text`：写前先 `yaml.safe_load` 验证合法性，失败则抛出 `RuntimeError`，调用方 catch 并留痕，**绝不把用户的配置写坏**。
- 标量输出用 `_yaml_scalar`（`yaml.safe_dump(..., width=10**9)`）或 `_yaml_quote`（JSON 双引号风格），确保含 `#`、`:`、空格的外部值（设备名、音色 id）不被误解析为 YAML 特殊字符。

### 3.7 密钥存储安全约束（credentials）

- 存储目录：`Path.home() / ".vrchat-livetranslate"`，权限 `0o700`（POSIX）。
- 文件名：`api_key.txt`（qianwen 槽，保持向后兼容）或 `api_key_<slot>.txt`。
- 槽名白名单：仅 `[a-z0-9_]`，防路径穿越。
- 写入原子性：先写临时文件再 `os.replace`，失败清理临时文件。
- 输入校验：长度 ≥ 16、不含空白字符，否则抛 `ValueError`。
- 日志/界面回显一律经 `mask_key` 打码。

## 4. 约定与约束

- **配置文件位置**：可写配置在 `APP_DIR/config.yaml`，模板在 `BUNDLE_DIR/config.example.yaml`；`ensure_config` 负责缺省生成。
- **YAML 损坏恢复**：解析失败时备份为 `<name>.broken` 并从模板重建，保证程序可启动。
- **手写配置容错**：数值型字段非法 → 留痕 + 回落默认值；映射型字段非法 → 丢弃并留痕。
- **Provider 迁移**：已下线 `bailian_intl` 的 `base_url` 自动迁移到千问云·海外版并提示用户重新填 key。
- **API key 不落盘到 config.yaml**：只通过环境变量或 `~/.bailian/config.json` 注入，或保存到独立的用户目录文件。
- **config.yaml 就地改写**：禁止整文件 dump，必须保留注释、空行、键顺序；写前必验合法 YAML。
- **密钥槽隔离**：不同线路的 key 分文件存储，切换线路无需重填。
- **词库合并唯一口径**：`merge_hotwords` 是全局唯一合并点，实时会话与打字翻译共享同一份结果。
- **UI 状态与业务配置分离**：`ui` 段仅记录上次界面选择（方向、缩放、勾选），业务参数在各自段内。