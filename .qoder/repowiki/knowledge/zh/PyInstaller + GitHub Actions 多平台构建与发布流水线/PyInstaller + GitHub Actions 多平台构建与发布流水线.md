---
kind: build_system
name: PyInstaller + GitHub Actions 多平台构建与发布流水线
category: build_system
scope:
    - '**'
source_files:
    - scripts/build_exe.py
    - scripts/build_appimage.sh
    - build_exe.bat
    - .github/workflows/ci.yml
    - .github/workflows/release.yml
    - .github/workflows/appimage.yml
    - scripts/gen_buildinfo.py
    - scripts/check_platform_purity.py
    - scripts/verify_appimage.py
    - server/package.json
    - requirements-windows.txt
    - requirements-linux.txt
    - requirements-dev.txt
---

## 1. 总体方案

项目是 Python 桌面客户端（`vlt/`）+ Cloudflare Worker 房间中继（`server/`），构建体系围绕 **PyInstaller** 打包为可执行文件，并通过 **GitHub Actions** 在 Windows 和 Linux 上并行完成测试、打包与发布。

- 桌面端：Windows → `scripts/build_exe.py`（单文件 exe，无控制台）；Linux → `scripts/build_appimage.sh`（PyInstaller onedir → AppDir → appimagetool）。
- 服务端：`server/package.json` 通过 `wrangler dev` / `wrangler deploy` 部署到 Cloudflare Workers。
- CI/CD：`.github/workflows/ci.yml`（PR/main 触发）、`.github/workflows/release.yml`（打 `v*` 标签触发）、`.github/workflows/appimage.yml`（被前两者复用的可复用 workflow）。

## 2. 关键文件

- 打包脚本：`scripts/build_exe.py`（Windows 单文件 exe）、`scripts/build_appimage.sh`（Linux AppImage）、`build_exe.bat`（Windows 入口壳）。
- CI 工作流：`.github/workflows/ci.yml`、`.github/workflows/release.yml`、`.github/workflows/appimage.yml`。
- 依赖清单：`requirements.txt`、`requirements-windows.txt`、`requirements-linux.txt`、`requirements-dev.txt`。
- 版本注入：`scripts/gen_buildinfo.py`（生成 `build/buildinfo.json`，由 PyInstaller `--add-data` 打入产物）。
- 平台纯度检查：`scripts/check_platform_purity.py`（CI 中校验 Windows 产物不含 Linux 独占模块）。
- 验收脚本：`scripts/verify_appimage.py`（独立对任意 AppImage 做平台纯度、导入、xr 瘦身、离线渲染、字体检查）。
- 服务端：`server/package.json` + `server/wrangler.toml`。

## 3. 架构与约定

### 3.1 双平台打包策略

- **Windows**：`scripts/build_exe.py` 调用 PyInstaller `--onefile --noconsole`，入口 `run_gui.py`。动态导入的模块通过 `HIDDEN` 列表显式声明；带二进制数据的库通过 `COLLECT_ALL` 收集；Windows 独占实现（`vlt.platform.linux`、`vlt.output.openxr_overlay`、`xr` 等）通过 `EXCLUDE_WIN` 排除，确保产物字节码里不出现 Linux 相关字样。
- **Linux**：`scripts/build_appimage.sh` 走 PyInstaller `--onedir`，然后组装 AppDir 并用 `appimagetool` 生成 squashfs。关键定制：
  - 自编 Xft + 关 CUPS 的 Tk（`--enable-xft --disable-libcups`），替换 PyInstaller 自带的 Tcl/Tk，避免 fontconfig 不可用和 libcups 拖入 14 个系统库。
  - 反向裁剪：把 X11 客户端栈（libX11/libXext/libXrender/libXau/libXdmcp/libXss/libXft）和 libpipewire 从包内删除，交给宿主提供；断言构建机 ldconfig 能提供这些 soname。
  - pyopenxr 瘦身：按当前平台目录名（api_layer/library）保留，删其他平台目录与三个调试层（api_dump/core_validation/best_practices），并设体积上限 `XR_SLIM_MAX_MB=60`。
  - 数据写路径遵循 `APPIMAGE`/`APPDIR` 环境变量，config.yaml/logs/out 落到 `$XDG_DATA_HOME/vrchat-livetranslate`。

### 3.2 版本溯源

构建期运行 `scripts/gen_buildinfo.py` 写入 `build/buildinfo.json`，内容被 PyInstaller `--add-data` 打入产物。运行时 `vlt/version.py` 读取该 JSON（取不到则显示 `+unknown`），用于区分 release 版与测试版。CI 中 `release.yml` 还强制比对 git tag 与 `vlt/__init__.py` 中的 `__version__`。

### 3.3 CI 流水线

- `ci.yml`：在 `windows-latest` 和 `ubuntu-latest` 上分别跑语法检查（`compileall`）、静态检查（`ruff check --select F,E9`）、凭据扫描（`scripts/check_no_secrets.py`）、全部离线测试（`scripts/run_tests.py --coverage --require-tk --require-xvfb`），最后再跑一次打包确认。
- `appimage.yml`：被 `ci.yml` 和 `release.yml` 通过 `uses: ./.github/workflows/appimage.yml` 复用，避免「两处各写一份、改漏一半」的问题。包含系统依赖安装、uv 独立 Python 3.11、Tcl/Tk 版本缓存 key、AppImage 构建与独立验收。
- `release.yml`：打 `v*` 标签时先对账 tag 与 `__version__`，再构建 Windows exe 与 Linux AppImage，上传 artifact 后通过 `softprops/action-gh-release` 创建 Release。产物含 `VRChatLiveTranslate.exe`、`SHA256SUMS.txt`（兼容 ≤ v0.2.0 老客户端）、`VRChatLiveTranslate-x86_64.AppImage`。

### 3.4 服务端构建

`server/package.json` 仅定义 `dev` 和 `deploy` 两个 npm script，使用 `wrangler` 工具链部署到 Cloudflare Workers，无额外构建步骤。

## 4. 约定与约束

- **Python 版本**：CI 固定使用 Python 3.11（`setup-python@v7` with `python-version: "3.11"`），README 也要求 3.11（3.12 未测）。
- **PyInstaller 版本**：所有构建路径都要求 `pyinstaller>=6.6`。
- **UTF-8 输出**：所有 workflow 设置 `env.PYTHONUTF8="1"`，因为 Windows runner 默认 cp1252，带 emoji/中文的 print 会直接崩。
- **平台隔离门禁**：Windows 产物不得包含 Linux 独占模块（`vlt.platform.linux`、`vlt.output.openxr_overlay`、`xr`），由 `scripts/check_platform_purity.py` 在 CI 中作为红灯门禁强制执行。
- **Linux 音频栈**：自 2026-10 起 Linux 全程走 PipeWire 子进程（`pw-dump/pw-record/pw-cat`），不再依赖 PortAudio/sounddevice/JACK；`requirements-linux.txt` 与 `build_appimage.sh` 的 EXCLUDES 同步维护这一事实。
- **AppImage 字体**：AppImage 不自带任何字体，依赖宿主机的 fontconfig；构建机需装 `fonts-noto-cjk` 以支持中日韩离线渲染。
- **AppImage 系统库**：X11 客户端栈与 libpipewire 必须存在于宿主 ldconfig；构建脚本会在构建期断言这些 soname 可用，并在 ldd 扫描中拒绝 `not found`。
- **xr 瘦身上限**：`XR_SLIM_MAX_MB=60`，超过即失败；未瘦身约 126MB，瘦身后可达 ~25MB。
- **Release 产物大小下限**：exe ≥ 20MB、AppImage ≥ 30MB（2026-10 去掉 PortAudio 后从 40MB 降到约 39.5MB，下限已下调）。
- **文档链接检查**：Linux job 额外运行 `scripts/check_doc_links.py`。
- **并发控制**：CI 设置 `concurrency.cancel-in-progress: true`，避免旧 run 卡死占用 runner。
- **超时保护**：每个 job 设置 `timeout-minutes: 30`，防止 GitHub 默认的 6 小时挂住。
- **服务部署**：`server/package.json` 的 `deploy` 脚本调用 `wrangler deploy`，无 Docker 或 Makefile 参与。

## 5. 不适用项

仓库没有 Makefile、Dockerfile 或通用构建编排器；构建逻辑集中在 shell/Python 脚本与 GitHub Actions YAML 中。