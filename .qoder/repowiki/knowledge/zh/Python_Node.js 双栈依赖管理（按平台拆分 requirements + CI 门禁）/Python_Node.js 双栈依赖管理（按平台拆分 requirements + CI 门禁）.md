---
kind: dependency_management
name: Python/Node.js 双栈依赖管理（按平台拆分 requirements + CI 门禁）
category: dependency_management
scope:
    - '**'
source_files:
    - requirements.txt
    - requirements-windows.txt
    - requirements-linux.txt
    - requirements-dev.txt
    - server/package.json
    - server/package-lock.json
    - setup.sh
    - setup.bat
    - .github/workflows/ci.yml
---

## 1. 使用的工具与策略

- **Python 侧**：使用 `pip` + 多份 `requirements*.txt`，通过 `-r` 组合公共与平台独占清单；虚拟环境由 `setup.sh` / `setup.bat` 创建，优先走 `uv`（自带 Python 3.11 standalone），回退到系统 `python3.11 -m venv`。
- **Node.js 侧**（`server/` Cloudflare Worker）：使用 `npm`（`package.json` + `package-lock.json`），仅一个开发依赖 `wrangler ^4.0.0`。
- **CI 验证**：GitHub Actions `.github/workflows/ci.yml` 在 Windows 与 Linux job 中分别安装对应 `requirements-*.txt` + `requirements-dev.txt`，并显式 `import openvr, pyaudiowpatch` 断言 Windows 独占依赖可导入。
- **无 vendoring、无私有 registry、无 lockfile 用于 Python**：所有包直接来自 PyPI，版本约束以 `>=` 下限形式声明（见 `requirements.txt` 注释“用 >= 是为了在别的机器上能解析到可用 wheel”）。

## 2. 关键文件

- `requirements.txt` — 公共依赖（Windows/Linux 通用）：`python-osc`, `Pillow`, `websockets`, `numpy`, `miniaudio`, `PyYAML`。
- `requirements-windows.txt` — Windows 独占：`PyAudioWPatch`（WASAPI loopback）、`sounddevice`（麦克风/TTS）、`openvr`（SteamVR 手腕屏）。
- `requirements-linux.txt` — Linux 独占：`pyopenxr`（自建 OpenXR overlay）；其余音频走 PipeWire 原生 `pw-record`/`pw-cat` 子进程，不装 `sounddevice`/PortAudio。
- `requirements-dev.txt` — 纯开发期依赖：`ruff>=0.6`（静态检查）、`coverage>=7.0`（覆盖率统计）；注释明确“不进产品、不随打包走”。
- `server/package.json` + `server/package-lock.json` — Node.js 依赖（仅 `wrangler` devDependency）。
- `setup.sh` / `setup.bat` — 创建 `.venv`、安装依赖、Linux 端还体检 `pw-dump`/`pw-record`/`pw-cat`、OpenXR 运行时、`libEGL`/`libwayland-client`。
- `.github/workflows/ci.yml` — CI 中固定 Python 3.11，分别安装两套 requirements，并执行 ruff F,E9 阻断门禁。

## 3. 架构与约定

- **三层清单组合**：`requirements-linux.txt` / `requirements-windows.txt` 首行 `-r requirements.txt` 复用公共依赖，再追加平台独占项。公共清单的注释强制要求“平台独占的依赖不要写在这里（否则另一个平台会解析失败）”。
- **最低版本而非精确锁定**：所有 Python 依赖都使用 `>=X.Y` 形式，目的是让不同机器的 pip 能解析到可用的 wheel，而不是冻结构建产物。
- **平台隔离是硬性约束**：`ci.yml` 在 tests 与 package job 中都执行 `python -c "import openvr, pyaudiowpatch"` 作为红灯门禁；同时 `scripts/check_platform_purity.py` 在打包后扫描 PYZ 字节码，禁止 Windows 产物中出现 Linux 独占实现（PipeWire/OpenXR 路径），该规则在 `docs/平台约束记录.md` 第三节有文档化。
- **开发依赖与运行依赖解耦**：`requirements-dev.txt` 单独维护，注释说明“只在做静态检查/跑覆盖率时需要”，CI 与本机均通过 `.venv` 单独安装，避免污染产品依赖集。
- **Node.js 侧保持极简**：`server/package.json` 仅声明 `wrangler` 为 devDependency，部署脚本 `wrangler deploy` 由 npm script 暴露。

## 4. 观察到的约定与规则

- **约定（描述性）**：公共依赖放在 `requirements.txt`，平台独占依赖分别放入 `requirements-windows.txt` / `requirements-linux.txt`，并通过 `-r` 组合。
- **约定（描述性）**：Python 依赖统一使用 `>=` 下限版本约束，不在仓库内生成 `requirements.lock` 或 `poetry.lock`。
- **约定（描述性）**：Linux 音频路径自 2026-10 起全部走 PipeWire 原生 `pw-record`/`pw-cat` 子进程，不再依赖 `sounddevice`/PortAudio；Windows 则保留 `PyAudioWPatch` + `sounddevice`。
- **约定（描述性）**：Linux 手腕屏走自建 OpenXR overlay（`vlt/output/openxr_overlay.py` + `pyopenxr`），不使用 Node/Python 的 `openvr` 绑定。
- **规则（CI 强制）**：`.github/workflows/ci.yml` 中 `python -c "import openvr, pyaudiowpatch"` 步骤失败即整 job 失败，确保 Windows 独占依赖确实被安装。
- **规则（CI 强制）**：ruff 静态检查以 `--select F,E9` 运行，注释明确其为“阻断门禁”，任何 F/E9 错误都会阻止 PR 合并。
- **规则（CI 强制）**：打包 job 必须产出 `dist/VRChatLiveTranslate.exe` 且体积 ≥ 20MB，否则报错。
- **规则（CI 强制）**：`scripts/check_platform_purity.py dist/VRChatLiveTranslate.exe --platform windows` 必须在 CI 中通过，禁止 Windows 产物包含 Linux 独占实现。
- **规则（安装脚本）**：`setup.sh` 优先使用 `uv venv --python 3.11`，若不可用则要求系统已安装 `python3.11`；`setup.bat` 同样要求 Python 3.11 在 PATH 中。
- **规则（Node.js）**：`server/package.json` 声明 `"private": true`，表明该子项目不作为 npm 包发布。