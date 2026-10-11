---
kind: design
name: Windows 平台引入 MicProxy 独占虚拟声卡输出流实现原声/译音一键切换
source: session
category: adr
---

# Windows 平台引入 MicProxy 独占虚拟声卡输出流实现原声/译音一键切换

_来源：6680394 → 6083052 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
VRChat 翻译场景需要在原声直出与 TTS 译音之间实时切换，但两条音频路径（引擎自建 VirtualMic 与代理）若同时持有同一虚拟声卡设备会导致切换时“上条流没说完”的爆音和状态不一致。

## 决策驱动
- 切换瞬间无爆音
- 单设备独占避免竞态
- 失败只降级不抛异常
- Linux/CLI 向后兼容

## 备选方案
- **由 Engine 直接管理 VirtualMic 并暴露 set_mode** _（已否决）_ — 优点：改动集中在 engine.py，无需新模块；缺点：Engine 需感知音频回调线程模型；切换语义复杂；Linux 路径仍需分支
- **在 GUI 层直接操作 PortAudio 流** _（已否决）_ — 优点：离 UI 最近；缺点：GUI 线程与音频回调线程耦合；无法被 CLI/self-test 复用
- **新增 vlt/output/micproxy.MicProxy 独占输出流 + TranslatedSink 垫片** — 优点：与 Engine 生命周期解耦；Engine 仅通过鸭子类型注入 sink；失败可静默降级；Linux 零改动；缺点：翻译运行时麦克风双开（引擎+代理各一条 16k 流），WASAPI 共享模式允许同开但增加资源占用

## 决策
在 Windows 下由 GUI 启动常驻的 MicProxy，独占一条 48kHz stereo RawOutputStream 写虚拟声卡；Engine 通过 audio_sink=TranslatedSink 注入译音路径，不再自建 VirtualMic。直通原声走环形缓冲（丢最旧保低延迟），译音走抖动缓冲（宁慢不切句），set_mode 双向清空对侧缓冲保证切换立刻生效。

## 影响
Windows 用户获得一键切换能力；Linux/CLI/self-test 完全走原有 VirtualMic/PwCatVirtualMic 路径不受影响；翻译期间程序打开两条麦克风流是已接受的代价；欠载自动告警但不自愈，用户需在设置→音频调大直通缓冲。