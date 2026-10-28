#!/usr/bin/env python
"""玩家名解析的离线验收（actor → usr_id → displayName）。

## 这里钉住的事

1. **两种真实布局都要认**：Event 255 的 actor 列表是 `actor → 属性`（标记在
   `usr_` 前），Event 42 是 `属性 → actor`（标记在 `usr_` 后）——用"最近标记"
   一条规则覆盖两者；
2. 字符串是 `07 <LEB 长度> <UTF-8>`，中文名必须原样还原；
3. `displayName` 是 `08`（null）表示玩家隐藏了名字 → 回落 `usr_` 前 8 位；
4. **Event 74 不能污染名字表**（它也有 `usr_`，但没有 `displayName`）；
5. 离开事件要清掉 actor（actor 号会复用，不清就会把新玩家显示成旧名字）；
6. `output_log` 兜底与文件名安全化。

跑法：.venv/bin/python tests/test_players_names.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import names  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


def _str_key(key: bytes) -> bytes:
    """属性字典里的 key：`07 <len> <utf8>`。"""
    return b"\x07" + bytes([len(key)]) + key


def _props(user_id: bytes, name: bytes | None) -> bytes:
    """模仿真实属性字典：user → id → displayName。"""
    value = (b"\x08" if name is None else b"\x07" + bytes([len(name)]) + name)
    return _str_key(b"user") + _str_key(b"id") + b"\x07" + bytes([len(user_id)]) + user_id \
        + _str_key(b"displayName") + value


def _usr(n: int) -> bytes:
    return f"usr_{n:08x}-1111-2222-3333-444444444444".encode()


def _actor(n: int) -> bytes:
    return b"\xfe\x0d" + n.to_bytes(2, "little")


def test_parse_records() -> None:
    print("[names] 两种布局 + 中文名")
    # Event 42 风格：属性在前，actor 在尾部
    p42 = b"\xf3\x04\x2a\x02\xf5\x15\x0d" + _props(_usr(1), "阿诺owo".encode()) + _actor(1527)
    recs = names.parse_records(p42)
    check("Event 42（actor 在尾部）", len(recs) == 1 and recs[0].actor == 1527,
          str(recs))
    check("中文名原样还原",
          bool(recs) and recs[0].display_name == "阿诺owo",
          recs[0].display_name if recs else "?")
    check("usr id 被记下来",
          bool(recs) and recs[0].user_id.startswith("usr_00000001"),
          recs[0].user_id if recs else "?")

    # Event 255 风格：actor 列表，每条是 actor → 属性
    p255 = (b"\xf3\x04\xff\x03\xfc\x49\x00"
            + _actor(1526) + b"\xf9\x15\x0d" + _props(_usr(2), "伤感身法绝活哥".encode())
            + _actor(1528) + b"\xf9\x15\x0d" + _props(_usr(3), "学余鱼嘉".encode()))
    recs2 = {r.actor: r.display_name for r in names.parse_records(p255)}
    check("Event 255 列表里两条都被解出", len(recs2) == 2, str(recs2))
    check("列表第一条绑定到正确 actor",
          recs2.get(1526) == "伤感身法绝活哥", str(recs2))
    check("列表第二条绑定到正确 actor", recs2.get(1528) == "学余鱼嘉", str(recs2))

    # 隐藏名字：displayName 是 null
    hidden = b"\xf3\x04\x2a\x02\xf5\x15\x0d" + _props(_usr(4), None) + _actor(1600)
    recs3 = names.parse_records(hidden)
    check("隐藏名字仍建出记录（名字为空）",
          len(recs3) == 1 and recs3[0].display_name == "", str(recs3))

    # Event 74：只有 usr_，没有 displayName → 不许产生记录
    p74 = b"\xf3\x04\x4a\x02\xf5\x14\x03\x00" + b"\x07" + bytes([40]) + _usr(9) + _actor(1700)
    check("Event 74（avatar 记录）不产生玩家名", names.parse_records(p74) == [],
          str(names.parse_records(p74)))


def test_dict_records() -> None:
    """Event 253 的「actor → 属性字典」布局（没有 fe 0d 标记）。

    实测：进房间时服务器会发一条 223KB 的 Event 253，里面 26 个玩家、78 个 usr_。
    旧实现只认 `fe 0d` 标记 → 整条名单被丢掉（现象：重进世界也永远只有 actorN）。
    """
    print("[names] Event 253 字典布局")

    def entry(actor: int, usr: bytes, name: bytes) -> bytes:
        # 真实布局：props 里先有 `user` 键，`user` 的值才是 {id, displayName}
        props = (b"\x15\x02"
                 + _str_key(b"user")
                 + b"\x15\x02"
                 + _str_key(b"id") + b"\x07" + bytes([len(usr)]) + usr
                 + _str_key(b"displayName") + b"\x07" + bytes([len(name)]) + name)
        return b"\x0d" + actor.to_bytes(2, "little") + props

    blob = (b"\xf3\x04\xfd\x03\xfb\x15" + bytes([2])
            + entry(1509, _usr(11), "ฅ星野桑ฅ".encode())
            + entry(1476, _usr(12), "燃え尽きない星".encode()))
    recs = {r.actor: r.display_name for r in names.parse_records(blob)}
    check("字典布局解出两条", len(recs) == 2, str(recs))
    check("actor 1509 的名字", recs.get(1509) == "ฅ星野桑ฅ", str(recs))
    check("actor 1476 的名字", recs.get(1476) == "燃え尽きない星", str(recs))


def test_name_table() -> None:
    print("[names] NameTable：更新 / 离开 / 回落")
    table = names.NameTable()
    p42 = b"\xf3\x04\x2a\x02\xf5\x15\x0d" + _props(_usr(1), "学余鱼嘉".encode()) + _actor(1528)
    table.feed(p42, code=42)
    check("label 用真名", table.label(1528) == "学余鱼嘉", table.label(1528))
    check("长度正确", len(table) == 1, str(len(table)))

    # 改名
    p42b = b"\xf3\x04\x2a\x02\xf5\x15\x0d" + _props(_usr(1), "新名字".encode()) + _actor(1528)
    changed = table.feed(p42b, code=42)
    check("改名产生 change 事件", len(changed) == 1 and changed[0].display_name == "新名字",
          str(changed))
    check("改名后 label 跟着变", table.label(1528) == "新名字", table.label(1528))

    # 离开 → 清掉（防 actor 复用串名）
    table.feed(_actor(1528), code=254)
    check("离开后回落 actorN", table.label(1528) == "actor1528", table.label(1528))
    check("离开后表为空", len(table) == 0, str(len(table)))

    # 隐藏名字 + output_log 兜底
    log_names = {_usr(4).decode(): "从日志来的名字"}
    t2 = names.NameTable(log_names)
    hidden = b"\xf3\x04\x2a\x02\xf5\x15\x0d" + _props(_usr(4), None) + _actor(1600)
    t2.feed(hidden, code=42)
    check("名字空时用日志兜底", t2.label(1600) == "从日志来的名字", t2.label(1600))

    # 只有 id、日志里也没有 → 用 usr 前 8 位（至少能区分人）
    t3 = names.NameTable()
    t3.feed(hidden, code=42)
    check("完全没名字时用 usr 片段", t3.label(1600).startswith("usr_"),
          t3.label(1600))

    # 非玩家事件不喂
    check("Event 13 不产生记录",
          table.feed(b"\xf3\x04\x0d\x02" + _props(_usr(5), b"x") + _actor(1900),
                     code=13) == [])


def test_output_log_and_filename() -> None:
    print("[names] output_log 兜底 + 文件名安全化")
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "output_log_2026-10-27_13-44-48.txt"
        log.write_text(
            "2026.10.27 13:45:08 Debug      -  [Behaviour] OnPlayerJoined EMeowSystem "
            "(usr_3a6252c9-88e9-4062-a5a4-7a10aa0517dc)\n"
            "2026.10.27 13:45:42 Debug      -  [Behaviour] OnPlayerJoined 学余鱼嘉 "
            "(usr_4fa0dcff-d95c-4371-a3b1-56841b91d7b2)\n"
            "2026.10.27 13:46:00 Debug      -  [Behaviour] OnPlayerLeft 某人 "
            "(usr_00000000-0000-0000-0000-000000000000)\n",
            encoding="utf-8")
        parsed = names.parse_output_log(log)
        check("日志里两条加入都被解析", len(parsed) == 2, str(parsed))
        check("英文名", parsed.get("usr_3a6252c9-88e9-4062-a5a4-7a10aa0517dc") == "EMeowSystem",
              str(parsed))
        check("中文名", parsed.get("usr_4fa0dcff-d95c-4371-a3b1-56841b91d7b2") == "学余鱼嘉",
              str(parsed))
        check("latest_output_log 选得到文件",
              names.latest_output_log(tmp) == log, str(names.latest_output_log(tmp)))

    check("路径分隔符被替换",
          names.safe_filename("a/b:c*d?e") == "a_b_c_d_e",
          names.safe_filename("a/b:c*d?e"))
    check("中文与空格", names.safe_filename("伤感 身法") == "伤感_身法",
          names.safe_filename("伤感 身法"))
    check("过长会被截断", len(names.safe_filename("x" * 80)) <= 40,
          str(len(names.safe_filename("x" * 80))))
    check("不留下前后点", names.safe_filename("...abc...") == "abc",
          names.safe_filename("...abc..."))


def main() -> int:
    test_parse_records()
    test_dict_records()
    test_name_table()
    test_output_log_and_filename()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（玩家名解析）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
