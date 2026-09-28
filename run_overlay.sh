#!/usr/bin/env bash
# 别人说话 → 中文显示在手腕屏（与 run_overlay.bat 对应）。
#
# Linux 上的手腕屏由 **WayVR** 承担 XR 渲染：我们只往它的自定义面板里改文本，
# 不自己写 OpenXR 客户端。需要：
#   1) WayVR 已安装（wayvr + wayvrctl）
#   2) WayVR 正在运行（Monado / WiVRn / SteamVR 任一）
#   3) 面板已在 WayVR 里注册（首次运行由本程序写入 ~/.config/wayvr/conf.d/panels.yaml）
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PY=".venv/bin/python"

echo "============================================"
echo "  别人说话 -> 中文显示在手腕屏"
echo "============================================"
echo "  停止：按 Ctrl+C"
echo

if ! command -v wayvrctl >/dev/null 2>&1; then
    echo "[!] 没装 wayvrctl —— 手腕屏起不来（其它腿不受影响）。"
    echo "    装法见 https://wayvr.org/docs/basics/installation/"
    echo "    或改用 ./run_chatbox.sh（纯文本气泡，不需要 VR）"
    echo
elif ! wayvrctl window-list >/dev/null 2>&1; then
    echo "[!] WayVR 没在运行（连不上它的 IPC）—— 手腕屏起不来，其它腿不受影响。"
    echo "    先启动 Monado / WiVRn / SteamVR，再启动 wayvr。"
    echo
fi

echo "说明：采集的是**系统播放输出**（PipeWire 的 sink monitor），"
echo "      所以 VRChat 的声音必须真的在放（静音/未开始播放就采不到）。"
echo

exec "$PY" -m vlt.app --direction theirs --loopback --sink overlay "$@"
