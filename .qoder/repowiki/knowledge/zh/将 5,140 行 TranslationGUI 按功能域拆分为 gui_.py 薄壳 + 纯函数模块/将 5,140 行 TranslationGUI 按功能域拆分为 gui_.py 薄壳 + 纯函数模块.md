---
kind: design
name: 将 5,140 行 TranslationGUI 按功能域拆分为 gui_*.py 薄壳 + 纯函数模块
source: session
category: adr
---

# 将 5,140 行 TranslationGUI 按功能域拆分为 gui_*.py 薄壳 + 纯函数模块

_来源：6680394 → 6083052 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
vlt/gui.py 单个类包含约 170 个方法、5,140 行代码，新增功能（如麦克风代理）难以定位且测试风险高；现有拆分已产出 ui_theme/ui_text/ui_state/ui_tk/glossary_text/gui_selftest 六个模块，剩余 ~4,200 行仍集中在一处。

## 决策驱动
- 每块独立可测
- 禁止循环引用
- 外部 from vlt.gui import XXX 不变
- Tk 控件参数显式传递

## 备选方案
- **按文件行数平均切分** _（已否决）_ — 优点：简单机械；缺点：忽略功能依赖顺序，易产生循环 import
- **把每个方法搬成 gui_xxx.py 中的纯函数，gui.py 仅留薄壳委托** — 优点：纯函数可独立单元测试；参数显式传入消除 self 隐式依赖；重导出保持 API 稳定；缺点：部分 Tk 控件强耦合导致纯函数化困难，需保留薄壳方法

## 决策
按依赖顺序分批拆出 gui_update / gui_room / gui_settings / gui_audio / gui_voice / gui_desktop / gui_chat / gui_engine 等模块，每个模块接收 Tk 控件与状态作为参数；TranslationGUI 中同名薄方法只做 1-3 行委托；gui.py 通过 `from .gui_xxx import ...` 重导出，外部 import 路径不变。

## 影响
gui.py 目标缩至 <500 行，新增功能可单独定位和测试；模块间严格单向依赖，无循环引用；重构过程以 pytest 为回归门禁，每次 Phase 完成后全量跑测。