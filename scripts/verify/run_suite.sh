#!/usr/bin/env bash
# 跑全量**离线**测试并汇总（跳过需要真 API key 的 test_engine.py）。
#
# 本脚本只管「离线口径 + 结果落盘」；**逐用例怎么跑交给仓库唯一的运行器
# `scripts/run_tests.py`**（与 CI 完全同一份逻辑：跳过语义、覆盖率、Linux 上逐用例
# `xvfb-run`、虚拟屏分辨率都只在那处维护 —— 别再各写一份 for 循环）。
#
# 口径（与仓库测试铁律一致）：
#   * 全部测试必须离线 → 用死代理跑一遍（HTTP_PROXY/HTTPS_PROXY 指向 127.0.0.1:1），
#     仍全绿才算真离线；死代理同时能抓出偷偷联网的用例。
#   * unset DASHSCOPE_API_KEY：会话里可能残留测试注入的假 key，会顶掉真凭据（见 skill）。
#   * 一次只跑一份：各用例共用 out/ 沙箱与仓库根的 config.yaml，并发跑会互相踩出假红
#     （run_tests.py 严格逐个起子进程，天然满足）。
#
# 用法：
#   bash scripts/verify/run_suite.sh                # 跑本仓库
#   bash scripts/verify/run_suite.sh <worktree 路径> [标签]   # 跑别的 worktree（用它自己的 .venv）
#
# 强制裸跑（不挂 xvfb）：`VLT_NO_XVFB=1 bash scripts/verify/run_suite.sh`。
# 虚拟屏分辨率：`VLT_XVFB_SCREEN` 透传给 run_tests.py（默认 1920x1080x24）。
#
# 结果写到 <worktree>/out/<标签>_results.txt（在 gitignore 的 out/ 里），含**全部**用例
# 输出（run_tests.py 把子进程输出直接透传），失败用例的细节也在其中、按用例分组。
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_WT="$(cd "$HERE/../.." && pwd)"
WT="${1:-$DEFAULT_WT}"
TAG="${2:-$(basename "$WT")}"

# venv 解释器：Windows 是 Scripts/python.exe，其余是 bin/python
PY="$WT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$WT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "❌ 找不到 $WT 下的 venv 解释器（既非 .venv/Scripts/python.exe 也非 .venv/bin/python）"
    echo "   先在仓库根跑 setup；worktree 里用 cmd /c mklink /J <wt>\\.venv <仓库>\\.venv 接上 venv。"
    exit 2
fi

RUNNER="$WT/scripts/run_tests.py"
if [ ! -e "$RUNNER" ]; then
    echo "❌ 找不到 $RUNNER（worktree 没同步到带 run_tests.py 的版本？）"
    exit 2
fi

cd "$WT" || exit 2
mkdir -p out
unset DASHSCOPE_API_KEY
export PYTHONUTF8=1
export HTTP_PROXY=http://127.0.0.1:1 HTTPS_PROXY=http://127.0.0.1:1
# ⚠️ 必须给本机回环放行代理：新版 `websockets`（≥15，本机 17.x）会自动读取 HTTP(S)_PROXY，
#    连 `ws://127.0.0.1:<port>` 也塞进死代理 → 房间类用例（test_room_client 等）会**假红**
#    （实测：不设 NO_PROXY 时 test_room_client 报「等了 10s 仍未上线」，设上即全绿）。
#    放行 localhost 不影响「抓偷偷连外网」这个初衷 —— 本机回环本来就不算联网。
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
RES="$WT/out/${TAG}_results.txt"

# 界面（Tk）用例的虚拟 X 由 run_tests.py 处理（Linux 上逐用例挂，虚拟屏显式钉死）；
# 这里只把「强制裸跑」转成它的 --no-xvfb。
xvfb_flag=""
[ "${VLT_NO_XVFB:-0}" = "1" ] && xvfb_flag="--no-xvfb"

echo "[$TAG] 离线（死代理 127.0.0.1:1，放行回环）+ run_tests.py（与 CI 同口径）" | tee "$RES"
echo "结果文件：$RES"

"$PY" "$RUNNER" $xvfb_flag 2>&1 | tee -a "$RES"
rc="${PIPESTATUS[0]}"
echo "退出码：$rc（详见 $RES）" | tee -a "$RES"
exit "$rc"
