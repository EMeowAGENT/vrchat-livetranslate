#!/usr/bin/env python
"""分玩家 P0-a 的离线验收：`/proc` 解析 + EAC 姿态闸门。

## 为什么这些用例必须存在

P0-a 的全部价值在于**判对方向**：

* 把 EAC Linux v2 误判成旧版 → 真正读内存时才拿到 `EPERM`，用户看到的是
  「跑一半失败」，而不是「这条路上不了」；
* 把 `TracerPid`/`Dumpable` 解析错 → 要么白提权、要么误以为能直接读；
* 把 `Secure Boot` 读不到当成 `off` → 闸门被**判松**，正好是最危险的错误方向
  （见 `guard.parse_secure_boot` 的注释）。

所以这里用**合成文本**把每条解析与判定钉死，全程不碰真实 `/proc`、不需要
VRChat 在跑、不提权。

跑法：.venv/bin/python tests/test_players_env.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import guard, procfs  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


def _stat_line(comm: str, starttime: int = 881234) -> str:
    """造一条 `/proc/<pid>/stat`。

    字段顺序：pid(1) comm(2) state(3) … itrealvalue(21) starttime(22)。
    解析器取的是「切掉前两个字段后的第 20 个」，所以这里正好放 20 个前导字段。
    """
    head = [ "S", "1", "12345", "12345", "0", "-1", "4194560",  # state..flags
             "1234", "0", "0", "0", "5", "6", "0", "0",          # minflt..cstime
             "20", "0", "12", "0" ]                              # priority..itrealvalue
    assert len(head) == 19, len(head)
    return f"12345 ({comm}) " + " ".join(head) + f" {starttime} 12345678 1234"


def test_maps_parsing() -> None:
    print("[maps] 行解析与匿名 rw 判定")
    r = procfs.parse_maps_line(
        "55d0c1a00000-55d0c1a21000 rw-p 00000000 00:00 0 \n")
    check("空路径的 rw 映射解析成功", r is not None and r.pathname == "")
    check("匿名 rw 判定", r is not None and r.is_anon_rw)
    check("大小 = end - start", r is not None and r.size == 0x21000)

    r2 = procfs.parse_maps_line(
        "7f00-7f10 r-xp 00000000 08:02 1234   /mnt/game/SteamLibrary/common/VRChat/"
        "GameAssembly.dll\n")
    check("带空格路径不截断", r2 is not None and r2.pathname.endswith("GameAssembly.dll"))
    check("只读映射不算匿名 rw", r2 is not None and not r2.is_anon_rw)

    r3 = procfs.parse_maps_line("7f00-7f10 rw-p 0 0 0 [heap]\n")
    check("[heap] 不算托管堆候选", r3 is not None and not r3.is_anon_rw)
    r4 = procfs.parse_maps_line("7f00-7f10 rw-p 0 0 0 [anon:vlt-heap]\n")
    check("[anon:…] 算候选", r4 is not None and r4.is_anon_rw)
    check("坏行返回 None", procfs.parse_maps_line("not a maps line") is None)

    r5 = procfs.parse_maps_line("7f00-7f10 rw-p 0 0 0 /tmp/x.dll (deleted)\n")
    check("去掉 (deleted) 后缀", r5 is not None and r5.pathname == "/tmp/x.dll")


def test_summarize() -> None:
    print("[maps] 汇总与标记识别（大小写不敏感）")
    regions = [
        procfs.parse_maps_line("1000-2000 rw-p 0 0 0 \n"),
        procfs.parse_maps_line("2000-5000 rw-p 0 0 0 \n"),
        procfs.parse_maps_line("5000-6000 r-xp 0 0 0 /game/gameassembly.DLL\n"),
        procfs.parse_maps_line("6000-7000 r--p 0 0 0 /game/vrchat.exe\n"),
        procfs.parse_maps_line("7000-8000 rw-p 0 0 0 /usr/lib/easyanticheat_x64.so\n"),
    ]
    clean = [r for r in regions if r is not None]
    s = procfs.summarize(clean)
    check("段数", s.region_count == 5, str(s.region_count))
    check("匿名 rw 字节数（只算两段匿名）", s.anon_rw_bytes == 0x1000 + 0x3000,
          hex(s.anon_rw_bytes))
    check("总字节数", s.total_bytes == 0x7000, hex(s.total_bytes))
    check("识别 GameAssembly（大小写不敏感）", s.has_gameassembly)
    check("识别 VRChat.exe", s.has_vrchat_exe)
    check("识别 EAC 模块", s.has_eac)


def test_stat_and_status() -> None:
    print("[proc] stat / status 解析")
    comm, start = procfs.parse_stat(_stat_line("VRChat.exe"))
    check("comm 正常", comm == "VRChat.exe", comm)
    check("starttime 取第 22 字段", start == 881234, str(start))

    comm2, start2 = procfs.parse_stat(_stat_line("weird (name) here", 42))
    check("comm 含括号时不误切", comm2 == "weird (name) here", comm2)
    check("comm 含括号时 starttime 仍正确", start2 == 42, str(start2))

    status = ("Name:\tVRChat.exe\nUid:\t1000\t1000\t1000\t1000\n"
              "TracerPid:\t0\nDumpable:\t1\n")
    fields = procfs.status_fields(procfs.parse_status(status))
    check("Dumpable", fields[0] == "1", str(fields[0]))
    check("TracerPid", fields[1] == 0, str(fields[1]))
    check("uid 取第一个", fields[2] == 1000, str(fields[2]))

    empty = procfs.status_fields(procfs.parse_status("Name:\tx\n"))
    check("缺字段给 None 而不是猜 0", empty == (None, None, None), str(empty))


def test_pick_host() -> None:
    print("[proc] 宿主选择")
    launcher = procfs.ProcInfo(
        pid=10, comm="VRChat.exe", cmdline=("VRChat.exe",), starttime=1, uid=1000,
        summary=procfs.MapSummary(total_bytes=100, anon_rw_bytes=10,
                                  has_gameassembly=False, has_vrchat_exe=True))
    host = procfs.ProcInfo(
        pid=11, comm="VRChat.exe", cmdline=("VRChat.exe",), starttime=2, uid=1000,
        summary=procfs.MapSummary(total_bytes=5000, anon_rw_bytes=3000,
                                  has_gameassembly=True, has_vrchat_exe=True))
    other = procfs.ProcInfo(
        pid=12, comm="wine", cmdline=("wine",), starttime=3, uid=1000,
        summary=procfs.MapSummary(total_bytes=9000, anon_rw_bytes=8000,
                                  has_gameassembly=False, has_vrchat_exe=False))
    got = procfs.pick_host([launcher, other, host])
    check("有 GameAssembly 的优先于「更大但无关」的进程", got is not None and got.pid == 11,
          str(got.pid if got else None))
    broken = procfs.ProcInfo(pid=13, comm="x", cmdline=(), starttime=0, uid=1000,
                             summary=procfs.MapSummary(), maps_ok=False, error="EPERM")
    check("maps 读不到的进程不参选", procfs.pick_host([broken]) is None)
    check("空列表返回 None", procfs.pick_host([]) is None)


def test_markers_are_tight() -> None:
    """标记必须**不可混淆**：本项目目录名里就有 `vrchat`。

    这条是实测踩出来的回归：早期用裸 `"vrchat"` 当标记，结果把沙盒的 bwrap 和
    zsh（命令行里带着 `/home/.../vrchat-livetranslate`）选成了「宿主」。
    """
    print("[proc] 标记紧致性（防目录名误判）")
    lookalike = procfs.ProcInfo(
        pid=100, comm="zsh", starttime=1, uid=1000,
        cmdline=("zsh", "-c", "cd /home/me/vrchat-livetranslate && ls"),
        summary=procfs.MapSummary())
    check("目录名里的 vrchat 不算游戏", not procfs.looks_like(lookalike, procfs.GAME_MARKERS),
          str(procfs.GAME_MARKERS))
    real = procfs.ProcInfo(
        pid=101, comm="VRChat.exe", starttime=2, uid=1000,
        cmdline=("Z:\\mnt\\game\\VRChat\\VRChat.exe",),
        summary=procfs.MapSummary())
    check("真正的 VRChat.exe 能认出来", procfs.looks_like(real, procfs.GAME_MARKERS))
    eac = procfs.ProcInfo(
        pid=102, comm="EasyAntiCheat_EOS.exe", starttime=3, uid=1000,
        cmdline=("C:\\Program Files (x86)\\EasyAntiCheat_EOS\\EasyAntiCheat_EOS.exe",),
        summary=procfs.MapSummary())
    check("EAC 进程能认出来", procfs.looks_like(eac, procfs.EAC_MARKERS))


def test_guard_parsers() -> None:
    print("[guard] 解析")
    check("lockdown 取中括号项",
          guard.parse_lockdown("[none] integrity confidentiality\n") == "none")
    check("lockdown 切到 integrity",
          guard.parse_lockdown("[integrity] none confidentiality\n") == "integrity")
    check("lockdown 乱码 → unknown", guard.parse_lockdown("garbage") == "unknown")
    check("lockdown 空 → unknown", guard.parse_lockdown("") == "unknown")

    check("lsm 去重排序", guard.parse_lsm("capability,landlock,lockdown,yama,bpf\n")
          == ("bpf", "capability", "landlock", "lockdown", "yama"))

    check("SecureBoot 字节 1 = on", guard.parse_secure_boot(b"\x06\x00\x00\x00\x01") == "on")
    check("SecureBoot 字节 0 = off", guard.parse_secure_boot(b"\x06\x00\x00\x00\x00") == "off")
    check("SecureBoot 太短 → unknown", guard.parse_secure_boot(b"\x06\x00") == "unknown")

    check("kexec=1 → yes", guard.parse_kexec_disabled("1\n") == "yes")
    check("kexec=0 → no", guard.parse_kexec_disabled("0\n") == "no")
    check("kexec 乱码 → unknown", guard.parse_kexec_disabled("?") == "unknown")


def test_guard_assess() -> None:
    print("[guard] 闸门判定")
    legacy = guard.Posture(kernel="7.2.11-zen1", lockdown="none", lsm=("bpf", "yama"),
                           secure_boot="off", iommu="on", kexec_disabled="no")
    check("lockdown=none + SB off → legacy", guard.assess(legacy).level == "legacy")

    v2 = guard.Posture(kernel="6.8-generic", lockdown="integrity", lsm=("bpf",),
                       secure_boot="on", iommu="on", kexec_disabled="yes")
    check("lockdown=integrity → blocked", guard.assess(v2).level == "blocked")

    unit_only = guard.Posture(kernel="6.8", lockdown="none", lsm=(),
                              secure_boot="off", eac_units=("easyanticheat.service",))
    check("出现 EAC systemd 单元 → blocked",
          guard.assess(unit_only).level == "blocked")

    sb_bpf = guard.Posture(kernel="6.8", lockdown="none", lsm=("bpf",),
                           secure_boot="on")
    check("SB on + BPF LSM → blocked", guard.assess(sb_bpf).level == "blocked")

    unsure = guard.Posture(kernel="6.8", lockdown="none", lsm=("bpf",),
                           secure_boot="unknown")
    check("SB 读不到 + BPF LSM → unknown（不判松）",
          guard.assess(unsure).level == "unknown")

    sb_only = guard.Posture(kernel="6.8", lockdown="none", lsm=("yama",),
                            secure_boot="on")
    check("只有 SB、没有 BPF LSM → legacy", guard.assess(sb_only).level == "legacy")


def test_guard_collect() -> None:
    print("[guard] 从假根目录采集（不碰真实 /sys）")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "proc/sys/kernel").mkdir(parents=True)
        (root / "proc/sys/kernel/osrelease").write_text("6.8.0-test\n")
        (root / "proc/sys/kernel/kexec_load_disabled").write_text("1\n")
        (root / "proc/sys/kernel/yama").mkdir(parents=True)
        (root / "proc/sys/kernel/yama/ptrace_scope").write_text("1\n")
        (root / "sys/kernel/security").mkdir(parents=True)
        (root / "sys/kernel/security/lockdown").write_text(
            "[none] integrity confidentiality\n")
        (root / "sys/kernel/security/lsm").write_text("capability,bpf,yama\n")
        (root / "usr/lib/systemd/system").mkdir(parents=True)
        (root / "usr/lib/systemd/system/easyanticheat.service").write_text("x")

        p = guard.collect(root)
        check("内核版本", p.kernel == "6.8.0-test", p.kernel)
        check("lockdown = none", p.lockdown == "none", p.lockdown)
        check("bpf LSM 识别", p.bpf_lsm)
        check("无 EFI 目录 → Secure Boot = off", p.secure_boot == "off", p.secure_boot)
        check("kexec 禁用", p.kexec_disabled == "yes", p.kexec_disabled)
        check("Yama ptrace_scope 采集", p.ptrace_scope == "1", p.ptrace_scope)
        check("EAC 单元被找到", p.eac_units == ("easyanticheat.service",),
              str(p.eac_units))
        check("采到单元后判定 blocked", guard.assess(p).level == "blocked")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)      # 什么文件都没有：必须给 unknown 而不是猜
        p2 = guard.collect(root)
        check("全空时 lockdown=unknown", p2.lockdown == "unknown", p2.lockdown)
        check("全空时 lsm 为空", p2.lsm == ())


def main() -> int:
    test_maps_parsing()
    test_summarize()
    test_stat_and_status()
    test_pick_host()
    test_markers_are_tight()
    test_guard_parsers()
    test_guard_assess()
    test_guard_collect()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（分玩家 P0-a 离线验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
