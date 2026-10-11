---
kind: configuration_system
name: VLT 配置系统：YAML 主配置 + 环境变量/文件密钥分层加载
category: configuration_system
scope:
    - '**'
source_files:
    - config.example.yaml
    - vlt/config.py
    - vlt/config_io.py
    - vlt/credentials.py
    - vlt/room/model.py
    - server/wrangler.toml
---

## 1. 总体方案

项目采用 **YAML 主配置文件**（`config.yaml`）+ **环境变量 / 独立密钥文件** 的分层配置模型。核心入口在 `vlt/config.py`，负责解析 YAML、校验字段、合并方向级热词、注入 API key；运行时写入由 `vlt/config_io.py` 提供「就地改写」能力，保留注释与键顺序。

- 模板：仓库根 `config.example.yaml`（361 行，带详尽中文注释），首次启动时复制到用户目录的 `config.yaml`。
- 运行时配置：`APP_DIR / "config.yaml"`（由 `paths.APP_DIR` 决定，打包后位于应用目录），已被 `.gitignore` 排除版本库。
- 密钥：不写进 `config.yaml`，走 `vlt/credentials.py`，按线路槽（slot）分文件保存在 `%USERPROFILE%/.vrchat-livetranslate/api_key.txt`（千问云）或 `api_key_<slot>.txt`。

## 2. 关键文件

| 文件 | 职责 |
|---|---|
| `config.example.yaml` | 全量配置模板，定义 `session/capture/directions/chatbox/merger/overlay/desktop_overlay/output/text_input/room/ui` 等段 |
| `vlt/config.py` | 配置加载、类型归一化、默认值、API key 解析、`AppConfig`/`Direction` dataclass |
| `vlt/config_io.py` | 对 `config.yaml` 进行就地文本替换（保留注释/空行/缩进），供 GUI 和更新检查模块复用 |
| `vlt/credentials.py` | API key 的保存/读取/打码/来源判定，按 slot 分槽存储 |
| `vlt/room/model.py` | 房间链路配置的 `RoomConfig.from_dict()` —— 脏值回落 + 留痕的统一实现 |
| `server/wrangler.toml` | Cloudflare Worker 服务端侧的配置（与客户端 config.yaml 契约对齐） |

## 3. 架构与设计约定

### 3.1 配置加载流程

`load_config(path, api_key, require_key)` 是统一入口：
1. `ensure_config()` 确保 `config.yaml` 存在，不存在则从 `config.example.yaml` 复制一份。
2. `yaml.safe_load` 解析；若解析失败 → 备份为 `<name>.broken`，再从模板重建，**绝不静默丢弃**。
3. 逐段归一化：`session` 段通过 `endpoints.normalize_provider` 处理 provider/base_url 迁移（含已下线百炼·国际版自动切到 qwencloud）。
4. 构建 `AppConfig` dataclass，其中 `directions` 每个条目映射为 `Direction` dataclass，调用 `to_session_config(base)` 生成下游 `SessionConfig`。
5. `output.capture.text_input.room` 等段以原始 dict 透传，由各自消费方做字段级校验（如 `RoomConfig.from_dict`）。

### 3.2 API Key 解析优先级

`load_api_key(explicit, slot="qianwen")` 严格遵循以下顺序（见 docstring 与实现）：
1. 显式传入参数（测试/CLI 场景）
2. 界面保存的 key（按 slot 取对应线路那份，`load_saved_key(slot)`）
3. 环境变量 `DASHSCOPE_API_KEY`
4. `~/.bailian/config.json`（百炼 CLI 配置，兼容 `api_key`/`apiKey`/`DASHSCOPE_API_KEY` 三个键名）

找不到 key 时抛 `SystemExit`；`require_key=False` 时仅打印提示并返回空串，让 GUI 仍可启动。

### 3.3 密钥安全约束（`vlt/credentials.py` 模块 docstring 明确声明）

- 密钥只存在用户目录 `%USERPROFILE%/.vrchat-livetranslate/` 下，绝不进仓库。
- 完整密钥绝不出现在日志、状态栏、异常信息里；界面回显一律经 `mask_key()` 打码（前 3 + `****` + 末 4 + 总长度）。
- 槽名白名单 `[a-z0-9_]+`，拒绝路径穿越字符。
- 写入使用临时文件 + `os.replace` 原子替换，POSIX 上目录权限 `0o700`、文件 `0o600`。

### 3.4 配置写入策略（`vlt/config_io.py`）

不使用 `yaml.safe_dump` 整文件重写（会抹平注释、重排键序），而是基于正则的**就地文本替换**：
- `_yaml_set_in_text`：修改已存在的叶子值，保留注释与块子行清理。
- `_yaml_set_or_create`：父级缺失时补建整条链（解决旧模板缺少新段的问题）。
- `_yaml_set_mapping`：整段写入映射（如 `glossary:` 专有词库），内部先调 `_yaml_set_or_create` 收敛成 `key: {}` 再展开。
- `_write_config_text`：写前先 `yaml.safe_load` 验证合法性，非法则抛出 `RuntimeError`，**宁可这次改动不生效也不把用户的配置写坏**。

### 3.5 脏值处理铁律

多处代码明确表达同一原则：**手写配置出错不能导致程序起不来**，必须「留痕 + 回落默认」。
- `vlt/config.py`：`_float_or_default` / `_opt_float` / `_int_clamped` / `_as_str_map` 全部打印 `[config] ⚠️ ...` 后返回默认值。
- `vlt/room/model.py`：`from_dict()` 文档字符串写明「任何脏输入都不许抛异常」，所有字段通过 `_as_bool/_as_str/_as_int/_as_float/_num` 转换，非法值经 `_warn` 记录后回落默认。
- `merge_hotwords`：全局 glossary 与方向级 hotwords 合并口径唯一，避免两条腿（实时会话 vs 打字翻译）出现不同行为。

### 3.6 Provider 与 base_url 的双真相源

`provider` 与 `base_url` 可独立配置，但 `load_config` 会检测不一致并警告：`_warn_provider_host_mismatch` 比较 provider 与 base_url host，发现千问云/海外版域名错配时打印引导去界面「设置 → 常规」切换。**最终连接地址始终以 base_url 为准**，provider 仅用于日志/诊断与槽选择。

## 4. 约定与约束

- 配置文件位置：`APP_DIR / "config.yaml"`（`vlt/config.py` 中 `DEFAULT_CONFIG`），模板来自 `BUNDLE_DIR / "config.example.yaml"`。
- 首次运行自动生成 `config.yaml`（`ensure_config`），之后用户编辑该文件，模板不参与运行时。
- API key 不落 `config.yaml`，只通过 `vlt/credentials.py` 管理；`DASHSCOPE_API_KEY` 环境变量与 `~/.bailian/config.json` 作为兜底。
- 配置段命名与 `config.example.yaml` 保持一致：`session`、`capture`、`directions`、`chatbox`、`merger`、`overlay`、`desktop_overlay`、`output`、`text_input`、`room`、`ui`。
- 所有数值型配置都有默认值与越界保护；布尔值支持多种字符串写法（`true/false/yes/on/是` 等，见 `room/model.py::_as_bool`）。
- 房间链路配置（`room:` 段）不做字段级校验于 `config.py`，统一交由 `RoomConfig.from_dict` 处理，避免两份口径。
- 桌面字幕（`desktop_overlay`）与手腕屏（`overlay`）是两条独立的配置段，视觉参数互不影响，前者有自己一套 font/color/size 键集。
- 译音输出（`output.audio`）设备名走回退列表匹配（Windows 专用），Linux 上由程序运行时声明 PipeWire 节点，无需配置。
- 配置变更热重载：GUI 通过 `config_io.write_leaf` 直接改 `config.yaml` 文本，下次启动即生效（无需重启进程）。