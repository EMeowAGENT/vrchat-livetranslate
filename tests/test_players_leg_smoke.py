#!/usr/bin/env python
"""分玩家腿的**启动/停止**冒烟（不需要 VRChat、不需要提权）。

只验证生命周期不会卡死、状态回调会说话：把 `_wait_host` 打桩成"永远找不到游戏"，
启动后 0.3s 内应进入等待态，`stop()` 能在一个超时内把线程收掉。

跑法：xvfb-run -a .venv/bin/python tests/test_players_leg_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players.leg import PlayerLeg  # noqa: E402
from vlt.session.base import SessionConfig  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


def main() -> int:
    status: list[tuple[str, str]] = []
    leg = PlayerLeg(SessionConfig(api_key="x"), on_status=lambda l, m: status.append((l, m)),
                    max_sessions=1, pre_seconds=1.0)
    leg._wait_host = lambda seconds: None          # noqa: SLF001 - 测试打桩
    leg.start()
    check("线程起来了", leg.running)
    time.sleep(0.6)
    check("等待游戏时状态栏有话说",
          any("VRChat" in msg for _lvl, msg in status), str(status))
    t0 = time.monotonic()
    leg.stop(timeout=5.0)
    elapsed = time.monotonic() - t0
    check("stop 能及时收掉线程", not leg.running, str(leg.running))
    check("stop 不卡（<3s）", elapsed < 3.0, f"{elapsed:.2f}s")
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（分玩家腿生命周期）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
