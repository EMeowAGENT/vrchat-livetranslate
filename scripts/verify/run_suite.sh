#!/usr/bin/env bash
# 跑全量**离线**测试并汇总（跳过需要真 API key 的 test_engine.py）。
#
# 口径（与仓库测试铁律一致）：
#   * 全部测试必须离线 → 用死代理跑一遍（HTTP_PROXY/HTTPS_PROXY 指向 127.0.0.1:1），
#     仍全绿才算真离线；死代理同时能抓出偷偷联网的用例。
#   * unset DASHSCOPE_API_KEY：会话里可能残留测试注入的假 key，会顶掉真凭据（见 skill）。
#   * 一次只跑一份：各用例共用 out/ 沙箱与仓库根的 config.yaml，并发跑会互相踩出假红。
#
# 用法：
#   bash scripts/verify/run_suite.sh                # 跑本仓库
#   bash scripts/verify/run_suite.sh <worktree 路径> [标签]   # 跑别的 worktree（用它自己的 .venv）
#
# 界面（Tk）用例：**Linux 上逐用例挂 `xvfb-run -a`**（每个用例一个干净、无窗口管理器的虚拟 X），
#   与 CI 的 `linux-tests` 同口径 —— 否则平铺 WM（Hyprland / niri / sway）会把窗口重排成满屏，
#   `test_desktop_overlay*` / `test_i18n` 这类实测几何的用例会假红。macOS 无 xvfb-run 时自动裸跑。
#   想强制裸跑：`VLT_NO_XVFB=1 bash scripts/verify/run_suite.sh`。
#
# 会不会写盘：结果写到 <worktree>/out/<标签>_results.txt，失败用例的完整输出写到
#   <worktree>/out/<标签>_fail_<用例>.log（都在 gitignore 的 out/ 里）。
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
: > "$RES"

# 界面（Tk）用例的虚拟 X：Linux 且装了 xvfb-run 才挂（对齐 CI 的 linux-tests）。
# Windows 的 Git-Bash 里 `uname -s` 不是 Linux；macOS 也走裸跑（自带桌面会话）。
XVFB=""
if [ "$(uname -s)" = "Linux" ] && command -v xvfb-run >/dev/null 2>&1 \
   && [ "${VLT_NO_XVFB:-0}" != "1" ]; then
    XVFB="xvfb-run -a"
fi
echo "[$TAG] 界面隔离：${XVFB:-裸跑（未用 xvfb）}" | tee -a "$RES"

pass=0; fail=0; skip=0
for t in tests/test_*.py; do
    [ -e "$t" ] || continue
    name=$(basename "$t" .py)
    if [ "$name" = "test_engine" ]; then
        echo "SKIP  $name（需要真 API key）" | tee -a "$RES"
        skip=$((skip+1)); continue
    fi
    o=$( $XVFB "$PY" "$t" 2>&1 ); rc=$?
    last=$(echo "$o" | grep -E "OK$|ALL PASSED|全部通过|PASSED|跳过" | tail -1)
    if [ $rc -eq 0 ]; then
        echo "PASS  $name  | $last" | tee -a "$RES"
        pass=$((pass+1))
    else
        echo "FAIL  $name  (rc=$rc)" | tee -a "$RES"
        echo "$o" > "$WT/out/${TAG}_fail_$name.log"
        fail=$((fail+1))
    fi
done
echo "---- [$TAG] 合计：PASS=$pass FAIL=$fail SKIP=$skip" | tee -a "$RES"
[ $fail -eq 0 ]
