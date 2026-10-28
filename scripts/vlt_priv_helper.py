#!/usr/bin/env python
"""源码运行时的提权助手入口（实现在 `vlt/players/priv_helper.py`）。

保留这个文件是因为：文档、`scripts/probe_player_audio.py` 与手工排查都用它；
而 AppImage（冻结产物）里 `sys.executable` 不是解释器，走的是主程序的
`--priv-helper` 开关（见 `vlt/players/helper.py:helper_command`）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players.priv_helper import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
