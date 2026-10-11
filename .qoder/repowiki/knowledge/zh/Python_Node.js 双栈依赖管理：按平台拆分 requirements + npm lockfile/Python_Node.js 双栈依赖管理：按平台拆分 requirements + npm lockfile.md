---
kind: dependency_management
name: Python/Node.js 双栈依赖管理：按平台拆分 requirements + npm lockfile
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
    - .github/workflows/ci.yml
---

## 1. 使用的工具与方案

- **Python 侧**：使用 `pip` + 多个 `requirements*.txt` 清单，无 `pyproject.toml` / `poetry` / `Pipfile`。
- **Node.js 侧（server 子项目）**：使用 `npm`（`package.json` + `package-lock.json`），仅依赖 `wrangler` 作为 devDependency。
- **CI 驱动安装**：`.github/workflows/ci.yml` 中显式调用 `pip install -r requirements-dev.txt`，确保开发门禁与运行依赖分离。
- **无私有仓库配置**：未发现 `.pip.conf`、`setup.cfg` 的 `index-url`、`pip.conf` 或 `~/.npmrc`；所有包均从 PyPI / npmjs.org 拉取。
- **无 vendoring**：没有 `vendor/`、`third_party/` 等目录，全部走 pip/npm 解析。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `requirements.txt` | 公共 Python 依赖（Windows/Linux 通用） |
| `requirements-windows.txt` | Windows 独占依赖（PyAudioWPatch、sounddevice、openvr），通过 `-r requirements.txt` 复用公共列表 |
| `requirements-linux.txt` | Linux 独占依赖（pyopenxr），同样 `-r requirements.txt` |
| `requirements-dev.txt` | 纯开发期依赖（ruff、coverage），注释明确“不进产品、不随打包走” |
| `server/package.json` | Node 子项目声明，仅 `wrangler` 为 devDependency |
| `server/package-lock.json` | npm v3 lockfile，锁定 wrangler 及其二进制平台包 |
| `.github/workflows/ci.yml` | CI 中安装 `requirements-dev.txt` 并执行测试/检查 |

## 3. 架构与约定

### 3.1 Python 依赖分层

三个 requirements 文件形成清晰的三层结构：

```
requirements.txt          ← 公共依赖（python-osc, Pillow, websockets, numpy, miniaudio, PyYAML）
requirements-windows.txt  ← 在公共之上追加 Windows 专属
requirements-linux.txt    ← 在公共之上追加 Linux 专属
requirements-dev.txt      ← 完全独立，仅用于静态检查与覆盖率
```

- 公共清单注释写明“本机验证过的版本写在注释里；用 >= 是为了在别的机器上能解析到可用 wheel”，说明采用**宽松下限 + 本地 pin 注释**的策略，而非严格锁定。
- 平台独占依赖被刻意隔离：`requirements-windows.txt` 和 `requirements-linux.txt` 都通过 `-r requirements.txt` 引用公共列表，避免重复声明。
- 每个平台清单顶部都有大段注释解释为什么选择该依赖（例如 Linux 侧走 PipeWire 原生 `pw-record`/`pw-cat` 而不需要 sounddevice/PortAudio；Linux 手腕屏走自建 OpenXR 而非 openvr），这些注释本身即作为决策文档。

### 3.2 Node.js 依赖

`server/` 是独立的 Cloudflare Worker 子项目：

- `package.json` 标记 `"private": true`，仅包含 `wrangler` 作为 `devDependencies`。
- `package-lock.json` 存在且为 lockfileVersion 3，锁定了 wrangler 及其平台特定的 workerd 二进制（darwin/linux/win32 arm64/x64 等均为 optional）。这保证了构建可复现。
- 无 `dependencies`，说明运行时依赖由 Cloudflare Workers 环境提供，本地只装开发工具。

### 3.3 CI 中的依赖安装

`.github/workflows/ci.yml` 中同时安装 Python 与 Node 依赖：
- Python 端安装 `requirements-dev.txt`（ruff + coverage），并在 ruff 中以 `F,E9` 作为阻断规则。
- Node 端通过 `npm ci`（由 lockfile 保证）安装 wrangler。

## 4. 约定与约束

- **平台隔离约定**：公共 `requirements.txt` 禁止放入平台独占依赖——两个平台清单的注释都明确警告“不要加进公共 requirements.txt”，否则另一平台会解析失败（如 PyAudioWPatch 在 Linux 无 wheel，openvr 的 Linux 用法另走 pyopenxr）。
- **开发/生产依赖分离**：`requirements-dev.txt` 的注释规定“只在做静态检查 / 跑覆盖率时需要；运行程序本身与 CI 的测试用例都用不到它们”，并通过单独文件让「跑得起来」和「开发门禁」两套依赖互不牵连。
- **版本策略**：Python 依赖统一使用 `>=X.Y` 宽松下限（见 requirements.txt 注释“用 >= 是为了在别的机器上能解析到可用 wheel”），不锁定精确版本；Node 侧则通过 `package-lock.json` 精确锁定。
- **系统级依赖不在 pip 内**：Linux 清单注释列出非 pip 依赖（pipewire、OpenXR 运行时、libEGL、libwayland-client），由 `setup.sh` 体检，表明系统库与 Python 包解耦。
- **无私有源**：全仓未发现任何私有 registry / GOPRIVATE / .npmrc 配置，依赖来源均为公开 PyPI 与 npmjs.org。
- **无 vendoring**：仓库未使用 pip-tools、pipenv --lock、Poetry lock 或 npm/yarn 的 vendor 模式，所有第三方代码均通过包管理器动态解析。