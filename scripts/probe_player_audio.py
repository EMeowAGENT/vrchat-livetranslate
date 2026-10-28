#!/usr/bin/env python
"""分玩家语音探针：P0-a（零风险环境采样）与 P0-b（一次性提权取钥 + 抓包 + 解码）。

## 两个子命令

    # P0-a：只看 /proc 与 /sys 元数据，不提权、不读内存
    .venv/bin/python scripts/probe_player_audio.py env

    # P0-b：需要一次 pkexec（会弹密码）；完成后把密钥留在内存里解密语音
    .venv/bin/python scripts/probe_player_audio.py run --seconds 90

## P0-b 到底做了什么（按顺序）

1. **闸门**：先跑 `guard.collect()/assess()`。EAC Linux v2 迹象命中就**直接拒绝**——
   那种机器上 eBPF LSM 会拒绝读内存，硬跑只会得到 EPERM。
2. **定位宿主**：`/proc/*/maps` 里有 `GameAssembly.dll` 的那个进程（P0-a 已实测）。
3. **拉起助手**（`scripts/vlt_priv_helper.py`，pkexec）：
   - 助手复核 pid 是不是 VRChat、`starttime` 有没有变（防 PID 复用）；
   - 开 `AF_PACKET` + BPF（`udp port 27002`），把 **socket fd** 交给主程序；
   - 按需扫描匿名可读映射，把**候选 32 字节**交回（不交原始内存），然后退出。
4. **验钥**：主程序用真实报文的 AES-256-GCM 认证逐个试候选（按五元组分组，
   同一 flow 的抽样必须**全部**通过）。密码学级别的零误报。
5. **解码**：解密 → Photon 命令 / 分片 → 事件 1（USpeak）→ 参数 245 音频体 →
   Opus 帧（用 libopus 的帧自校验裁决切法）→ 16 kHz 单声道 wav。

## 产物（`--out`，默认 `out/players-p0`）

| 文件 | 内容 | 敏感性 |
|---|---|---|
| `cap.pcap` | 抓到的**密文**帧 | 中（不含音频明文） |
| `report.json` | 姿态、扫描统计、密钥**指纹**、事件直方图 | 低（无密钥、无音频） |
| `player_actor<N>.wav` | 每位玩家的 16 kHz 单声道人声 | **高**（他人语音，用完请删） |

## 权限与风险

- 只在 P0-b 需要一次 `pkexec`；助手开完 fd、扫完候选就退出，**不留常驻 root**。
- 主程序全程普通用户，永远拿不到原始内存，只拿到候选密钥。
- 这条路属于 ToS 灰区（读游戏进程内存），作者不保证账号安全 —— **先小号验证**。
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import (INSTALL_HINT, capture_linux, gcm, guard, names,  # noqa: E402
                         photon, procfs)
from vlt.players.collector import VoiceCollector  # noqa: E402
from vlt.players.helper import HelperSession, _recv_json  # noqa: E402,F401
from vlt.players.pending import PreKeyBuffer  # noqa: E402

HELPER = ROOT / "scripts" / "vlt_priv_helper.py"

EXIT_OK = 0
EXIT_BLOCKED = 10
EXIT_NO_PROCESS = 20
EXIT_HELPER_FAILED = 21
EXIT_NO_PACKETS = 22
EXIT_NO_KEY = 23
EXIT_NO_VOICE = 24
EXIT_MEM_BLOCKED = 25


# ================================================================ P0-a


def _fmt_proc(p: procfs.ProcInfo, host_pid: int | None) -> str:
    s = p.summary
    mark = "  ← 宿主（托管堆在这里）" if p.pid == host_pid else ""
    lines = [
        f"  pid {p.pid:<7d} {p.comm or '?':<22s} starttime={p.starttime}{mark}",
        f"    映射 {procfs.human_bytes(s.total_bytes)} / 匿名 rw {procfs.human_bytes(s.anon_rw_bytes)}"
        f"（{s.region_count} 段）",
        f"    标记：GameAssembly={'✓' if s.has_gameassembly else '✗'}"
        f"  VRChat.exe={'✓' if s.has_vrchat_exe else '✗'}"
        f"  EAC 模块={'✓' if s.has_eac else '✗'}",
        f"    Dumpable={p.dumpable or '未暴露'}"
        f"  TracerPid={p.tracer_pid if p.tracer_pid is not None else '?'}"
        f"  maps={'可读' if p.maps_ok else '读不到（' + p.error + '）'}",
    ]
    return "\n".join(lines)


def cmd_env(args: argparse.Namespace) -> int:
    posture = guard.collect()
    verdict = guard.assess(posture)

    print("=" * 68)
    print("  分玩家语音 P0-a：环境姿态探针（不读内存、不提权）")
    print("=" * 68)
    print()

    print("== 1. 内核与安全姿态 ==")
    print(f"  内核            : {posture.kernel}")
    print(f"  lockdown        : {posture.lockdown}")
    print(f"  已激活 LSM      : {', '.join(posture.lsm) or '（读不到）'}")
    print(f"  Secure Boot     : {posture.secure_boot}")
    print(f"  IOMMU           : {posture.iommu}")
    print(f"  kexec 已禁用    : {posture.kexec_disabled}")
    print(f"  Yama ptrace_scope: {posture.ptrace_scope}"
          + ("（1 = 只允许祖先进程，我们不是 VRChat 的祖先 ⇒ 必须提权）"
             if posture.ptrace_scope == "1" else ""))
    print(f"  EAC systemd 单元: {'、'.join(posture.eac_units) or '无'}")
    for note in posture.notes:
        print(f"  ⚠️ {note}")
    print()

    print("== 2. EAC 进程 ==")
    eac = procfs.find_eac()
    if not eac:
        print("  没找到 EAC 进程（VRChat 没在跑，或本机没装）")
        print("  · 这是探针的已知盲区：EAC 只在游戏运行期间存在")
    else:
        for p in eac:
            print(_fmt_proc(p, None))
    print()

    print("== 3. VRChat 进程 ==")
    procs = procfs.find_vrchat()
    host = procfs.pick_host(procs)
    if not procs:
        print("  没找到 VRChat 相关进程 —— 先启动游戏并进入一个世界，再跑一次")
        print("  （系统姿态与闸门结论已经有效，这一节只是缺进程数据）")
    else:
        for p in procs:
            print(_fmt_proc(p, host.pid if host else None))
        print()
        if host is not None:
            print(f"  选中宿主：pid {host.pid}")
            print(f"  · P0-b 只需扫约 {procfs.human_bytes(host.summary.anon_rw_bytes)}"
                  f" 的匿名可读映射（4MB 分块，预计 1~3 秒）")
            if host.dumpable == "0":
                print("  · Dumpable=0：普通用户读不了，**必须**走提权助手（符合原计划）")
            elif host.dumpable == "1":
                print("  · Dumpable=1：注意 —— 这只说明「没有主动设不可读」，"
                      "不等于同用户就能读；Yama ptrace_scope=1 下仍需提权")
            else:
                print("  · 本内核的 /proc/<pid>/status 没有 Dumpable 字段（实测 7.2-zen 如此）；"
                      "以 Yama ptrace_scope 与 P0-b 的实际读取结果为准")
            if host.tracer_pid not in (None, 0):
                print(f"  ⚠️ TracerPid={host.tracer_pid}：已经有调试器 attach 着 —— "
                      "如果那是你开的 gdb/strace，先关掉；我们的工具绝不制造这种状态")
    print()

    print("== 4. 闸门结论 ==")
    icon = {"legacy": "✅", "unknown": "⚠️", "blocked": "⛔"}.get(verdict.level, "?")
    print(f"  {icon} {verdict.level}")
    for reason in verdict.reasons:
        print(f"    · {reason}")
    print(f"  → {verdict.advice}")
    print()

    print("== 5. 下一步 ==")
    if verdict.level == "blocked":
        print("  停在这里：EAC Linux v2 已具备条件，读内存会被内核拒绝。")
        print("  可用的替代：房间模式（对方也装 VLT）或声纹分离。")
    else:
        print("  可以跑 P0-b：scripts/probe_player_audio.py run")
        print("  提醒：这条路属于 ToS 灰区，先用小号验证。")

    if args.json:
        payload = {
            "posture": {
                "kernel": posture.kernel,
                "lockdown": posture.lockdown,
                "lsm": list(posture.lsm),
                "secure_boot": posture.secure_boot,
                "iommu": posture.iommu,
                "kexec_disabled": posture.kexec_disabled,
                "eac_units": list(posture.eac_units),
                "notes": list(posture.notes),
            },
            "verdict": {"level": verdict.level, "reasons": list(verdict.reasons),
                        "advice": verdict.advice},
            "eac_processes": [_proc_json(p) for p in eac],
            "vrchat_processes": [_proc_json(p) for p in procs],
            "host_pid": host.pid if host else None,
        }
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  已写入 {out}")

    return EXIT_BLOCKED if verdict.level == "blocked" else EXIT_OK


def _proc_json(p: procfs.ProcInfo) -> dict:
    return {
        "pid": p.pid,
        "comm": p.comm,
        "cmdline": list(p.cmdline),
        "starttime": p.starttime,
        "uid": p.uid,
        "dumpable": p.dumpable,
        "tracer_pid": p.tracer_pid,
        "maps_ok": p.maps_ok,
        "total_bytes": p.summary.total_bytes,
        "anon_rw_bytes": p.summary.anon_rw_bytes,
        "has_gameassembly": p.summary.has_gameassembly,
        "has_vrchat_exe": p.summary.has_vrchat_exe,
        "has_eac": p.summary.has_eac,
    }


# ================================================================ P0-b








def _sniff_summary(frames: list[bytes], port: int = 27002) -> dict:
    """把一批链路层帧压成「到底有什么流量」的直方图（纯函数，可离线测）。

    诊断要回答的问题只有三个：**有没有帧**、**是什么链路层/协议**、**有没有目标端口**。
    """
    link: Counter = Counter()
    proto: Counter = Counter()
    ports: Counter = Counter()
    hits = 0
    for frame in frames:
        if not frame:
            link["empty"] += 1
            continue
        off = -1
        if len(frame) >= 14:
            ethertype = int.from_bytes(frame[12:14], "big")
            if ethertype == capture_linux.ETH_P_VLAN and len(frame) >= 18:
                ethertype = int.from_bytes(frame[16:18], "big")
                off = 18
            elif ethertype in (capture_linux.ETH_P_IP, capture_linux.ETH_P_IPV6):
                off = 14
            link[f"eth:0x{ethertype:04x}"] += 1
        if off < 0:
            if frame[0] >> 4 == 4 and len(frame) >= 20:
                link["raw-ip"] += 1
                off = 0
            else:
                link["other"] += 1
                continue
        if len(frame) < off + 20:
            continue
        ip_proto = frame[off + 9]
        proto["udp" if ip_proto == 17 else "tcp" if ip_proto == 6
              else f"ip:{ip_proto}"] += 1
        ihl = (frame[off] & 0x0F) * 4
        if ihl < 20 or len(frame) < off + ihl + 4:
            continue
        sport = int.from_bytes(frame[off + ihl:off + ihl + 2], "big")
        dport = int.from_bytes(frame[off + ihl + 2:off + ihl + 4], "big")
        if ip_proto in (6, 17):
            ports[f"{sport}->{dport}"] += 1
            if port in (sport, dport):
                hits += 1
    return {
        "frames": len(frames),
        "link": dict(link.most_common()),
        "proto": dict(proto.most_common()),
        "port_hits": hits,
        "top_ports": [[k, v] for k, v in ports.most_common(8)],
    }


def _print_sniff(summary: dict, port: int) -> None:
    print(f"[diag] 收到 {summary['frames']} 帧")
    print(f"  链路层：{summary['link'] or '无'}")
    print(f"  协议  ：{summary['proto'] or '无'}")
    print(f"  端口 top：{summary['top_ports'] or '无'}")
    print(f"  与端口 {port} 相关的包：{summary['port_hits']}")
    if summary["frames"] == 0:
        print("  → ✗ 一个帧都没收到：先确认 VRChat 已进入世界并在联机；"
              "若确定在联机，说明抓包 socket 没收到（接口/权限/内核过滤）")
    elif summary["port_hits"] == 0:
        print(f"  → 有流量但没有 {port}：看上面的端口表。VRChat 可能换了端口、"
              "或流量走的是代理/别的网卡（裸 IP 会显示 raw-ip）")
    else:
        print(f"  → ✓ 有 {port} 流量，说明抓包本身没问题（问题会在密钥/解析那一步）")


def _diagnose(sess: "HelperSession", *, port: int, seconds: float,
              max_frames: int = 200_000) -> dict:
    """用**不过滤**的 socket 采样一段时间，回答"到底有没有流量"。"""
    sess.start_capture(filter_mode="none")
    frames: list[bytes] = []
    deadline = time.monotonic() + max(0.5, seconds)
    sock = sess.packet_socket
    if sock is None:
        return {"frames": 0, "link": {}, "proto": {}, "port_hits": 0, "top_ports": []}
    while time.monotonic() < deadline and len(frames) < max_frames:
        for _ts, raw in capture_linux.drain(sock, timeout=0.2, max_frames=1024):
            frames.append(raw)
    return _sniff_summary(frames, port)


def _wait_host(seconds: float) -> procfs.ProcInfo | None:
    deadline = time.monotonic() + max(0.0, seconds)
    announced = False
    while True:
        host = procfs.pick_host(procfs.find_vrchat())
        if host is not None:
            return host
        if time.monotonic() >= deadline:
            return None
        if not announced:
            announced = True
            print(f"[p0b] 等待 VRChat 进程（最多 {seconds:.0f}s）…", flush=True)
        time.sleep(2.0)


def _ensure_crypto() -> bool:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print(f"❌ 缺少 cryptography：\n   {INSTALL_HINT}", flush=True)
        return False
    return True


def _collect_packets(sock: socket.socket, *, port: int, seconds: float,
                     want: int, pcap: capture_linux.PcapWriter | None,
                     on_datagram=None,
                     local_ports: set[int] | None = None) -> list[capture_linux.Datagram]:
    """收一批包（最多 `seconds` 秒或 `want` 个加密入向包）。

    `local_ports` 是游戏进程自己的 UDP 端口（`procfs.udp_ports`）：给了它，方向
    判定就要求「本机这一端确实是游戏」，别的进程连 27002 的流量不会混进来。
    """
    got: list[capture_linux.Datagram] = []
    deadline = time.monotonic() + seconds
    while len(got) < want and time.monotonic() < deadline:
        frames = capture_linux.drain(sock, timeout=0.2)
        if not frames:
            continue
        for ts, frame in frames:
            if pcap is not None:
                pcap.write(ts, frame)
            dg = capture_linux.parse_frame(frame, port=port,
                                           local_ports=local_ports)
            if dg is None:
                continue
            if on_datagram is not None:
                on_datagram(dg)
            if dg.is_encrypted and dg.direction == "in":
                got.append(dg)
    return got


def cmd_run(args: argparse.Namespace) -> int:
    posture = guard.collect()
    verdict = guard.assess(posture)
    if verdict.level == "blocked":
        print("⛔ 闸门判为 blocked：")
        for reason in verdict.reasons:
            print(f"   · {reason}")
        print(f"   {verdict.advice}")
        return EXIT_BLOCKED
    if verdict.level == "unknown":
        print("⚠️ 闸门证据不足：")
        for reason in verdict.reasons:
            print(f"   · {reason}")
        print(f"   {verdict.advice}")

    if not _ensure_crypto():
        return EXIT_HELPER_FAILED

    out = Path(args.out)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"❌ 建不了输出目录 {out}：{exc}")
        return EXIT_HELPER_FAILED

    host = _wait_host(args.wait)
    if host is None:
        print("❌ 没找到 VRChat 宿主进程（先进世界再跑）")
        return EXIT_NO_PROCESS
    print(f"[p0b] 宿主 pid={host.pid} starttime={host.starttime} "
          f"匿名 rw={procfs.human_bytes(host.summary.anon_rw_bytes)}", flush=True)

    # 游戏自己的 UDP 端口：方向判定要靠它（读不到就退回只比端口）
    local_ports = procfs.udp_ports(host.pid) or None
    if local_ports:
        print(f"[p0b] 游戏 UDP 端点：{sorted(local_ports)}", flush=True)

    log_names: dict[str, str] = {}
    log_path = Path(args.log_dir) if args.log_dir else names.default_log_dir()
    latest = names.latest_output_log(log_path) if log_path.exists() else None
    if latest is not None:
        log_names = names.parse_output_log(latest)
        print(f"[p0b] output_log 兜底：{latest.name}（{len(log_names)} 个名字）", flush=True)
    else:
        print("[p0b] 没找到 output_log（名字只能来自事件广播）", flush=True)

    report: dict = {
        "posture": {"kernel": posture.kernel, "lockdown": posture.lockdown,
                    "secure_boot": posture.secure_boot, "lsm": list(posture.lsm),
                    "verdict": verdict.level},
        "host": {"pid": host.pid, "starttime": host.starttime,
                 "anon_rw_bytes": host.summary.anon_rw_bytes},
        "scans": [],
        "packets": {},
        "key": None,
        "events": {},
        "actors": {},
    }

    try:
        lifetime = (args.pre_seconds + args.seconds
                    + args.scan_seconds * 2 * max(1, args.scan_retries) + 90)
        with HelperSession(host, port=args.port, lifetime_s=lifetime) as sess:
            sess.start_capture(filter_mode="loose" if args.loose_filter else "strict")
            assert sess.packet_socket is not None
            pcap = capture_linux.PcapWriter(out / "cap.pcap")
            try:
                print(f"[p0b] 等待加密流量（最多 {args.pre_seconds:.0f}s）…",
                      flush=True)
                print("[p0b] 💡 现在进入世界：玩家名只在「加入 / 属性变更」时广播，"
                      "先开工具再进世界才能拿到全员名字", flush=True)
                samples: list[tuple[gcm.Flow, bytes]] = []
                # 取钥期间抓到的包要**有上限地**留着：离线也能复现「开头那几十秒」，
                # 但不能无限吃内存（长 pre_seconds + 吵的房间）。
                pre_key = PreKeyBuffer(world_ready=True)

                def _keep(dg: capture_linux.Datagram) -> None:
                    if dg.is_encrypted:
                        samples.append((gcm.Flow.of(dg.src, dg.dst), dg.payload))
                        pre_key.add(dg)

                _collect_packets(sess.packet_socket, port=args.port,
                                 seconds=args.pre_seconds,
                                 want=args.min_samples, pcap=pcap,
                                 local_ports=local_ports,
                                 on_datagram=_keep)
                flows = Counter(str(f) for f, _p in samples)
                print(f"[p0b] 抓到 {sum(flows.values())} 个加密包，"
                      f"{len(flows)} 条 flow：{dict(flows)}", flush=True)
                if len(samples) < 2:
                    print("❌ 加密包太少 —— VRChat 在跑吗？进世界了吗？")
                    print("[p0b] 自动做一次 3s 无过滤采样，看看到底有什么流量…",
                          flush=True)
                    try:
                        summary = _diagnose(sess, port=args.port, seconds=3.0)
                        _print_sniff(summary, args.port)
                        report["diagnose"] = summary
                    except Exception as exc:        # noqa: BLE001 - 诊断失败不影响主结论
                        print(f"[diag] 诊断失败：{type(exc).__name__}: {exc}")
                    report["packets"] = {"total": len(samples), "flows": dict(flows)}
                    _write_report(out, report)
                    return EXIT_NO_PACKETS

                hit = None
                # 依次尝试两种长度字段布局：q（IL2CPP byte[]，首选）→ i（4 字节长度）。
                # 每种都**全量**扫：P0-b 实测过"上限截断导致真钥落在没扫到的后半段"，
                # 所以只有预算用尽才会提前收工，而且会把覆盖率打出来。
                for needle in ("q", "i"):
                    if hit is not None:
                        break
                    for attempt in range(1, args.scan_retries + 1):
                        print(f"[p0b] 第 {attempt} 次请求扫内存"
                              f"（needle={needle}）…", flush=True)
                        reply = sess.scan(deadline_s=args.scan_seconds, needle=needle)
                        if not reply.get("ok"):
                            if reply.get("blocked"):
                                print(f"❌ 读内存被拒绝：{reply.get('error')}\n"
                                      "   → 这台机器上这条路走不通（EAC v2 的特征）")
                                return EXIT_MEM_BLOCKED
                            print(f"❌ 扫描失败：{reply.get('error')}")
                            return EXIT_HELPER_FAILED
                        cands = [bytes.fromhex(h) for h in reply.get("candidates", [])]
                        total_region = reply.get("region_bytes") or 0
                        print(f"[p0b] 扫了 {reply['scanned_bytes'] / 2**30:.2f} / "
                              f"{total_region / 2**30:.2f} GiB / "
                              f"{reply['elapsed_ms']:.0f}ms，候选 {len(cands)}，"
                              f"命中 needle {reply['needle_hits']}"
                              f"（{reply['stopped']}）", flush=True)
                        report["scans"].append({
                            "needle": reply.get("needle", needle),
                            "scanned_bytes": reply["scanned_bytes"],
                            "region_bytes": reply.get("region_bytes"),
                            "elapsed_ms": reply["elapsed_ms"],
                            "candidates": len(cands),
                            "needle_hits": reply["needle_hits"],
                            "regions": reply["regions"],
                            "stopped": reply["stopped"],
                            "fingerprints": reply.get("fingerprints", []),
                        })
                        hit = gcm.validate_candidates(cands, samples)
                        if hit is not None:
                            break
                        if attempt < args.scan_retries:
                            print("[p0b] 候选都不对（或样本不够），再抓 5s 后重扫…",
                                  flush=True)
                            _collect_packets(sess.packet_socket, port=args.port,
                                             seconds=5.0, want=20, pcap=pcap,
                                             local_ports=local_ports,
                                             on_datagram=_keep)

                if hit is None:
                    last = report["scans"][-1] if report["scans"] else {}
                    covered = last.get("scanned_bytes", 0)
                    planned = last.get("region_bytes") or 0
                    print("❌ 没找到能通过 GCM 认证的密钥。")
                    if planned and covered < planned * 0.95:
                        print(f"   只扫了 {covered / 2**30:.2f} / {planned / 2**30:.2f} GiB"
                              f"（{last.get('stopped')}）—— 真钥可能在后半段："
                              "加大 --scan-seconds 再试一次")
                    else:
                        print("   全量覆盖后仍不匹配 ⇒ 更可能是布局假设有误："
                              "\n     · needle 不是 <u64 32>（数组长度字段位置不同）"
                              "\n     · 密钥不在可写映射里（native 缓冲）"
                              "\n     把 report.json 贴出来，我按 needle 命中数判断下一步")
                    _write_report(out, report)
                    return EXIT_NO_KEY
                print(f"✅ 密钥命中：flow={hit.flow} 验证 {hit.ok}/{hit.total} 包，"
                      f"指纹 {hit.fingerprint}，布局 {hit.layout}", flush=True)
                print(f"   {gcm.LAYOUT_DESC.get(hit.layout, hit.layout)}", flush=True)
                if args.save_key_to:
                    key_path = Path(args.save_key_to)
                    key_path.parent.mkdir(parents=True, exist_ok=True)
                    key_path.write_bytes(hit.key)
                    key_path.chmod(0o600)
                    print(f"⚠️ 已把密钥写入 {key_path}（0600）。"
                          "仅用于离线重放；用完请删。", flush=True)
                report["key"] = {"fingerprint": hit.fingerprint, "flow": str(hit.flow),
                                 "validated": f"{hit.ok}/{hit.total}",
                                 "layout": hit.layout}
                # 扫内存那几十秒里内核缓冲收到的包也捞出来（漏了就等于白等）
                _collect_packets(sess.packet_socket, port=args.port, seconds=0.05,
                                 want=10_000, pcap=pcap, local_ports=local_ports,
                                 on_datagram=_keep)
                buffered = pre_key.summary()
                replay = pre_key.release()
                report["replay"] = {"packets": len(replay), **buffered}

                _decode_session(sess, hit, out, args, report, pcap,
                                replay=replay, log_names=log_names,
                                local_ports=local_ports)
            finally:
                pcap.close()
    except (RuntimeError, OSError) as exc:
        print(f"❌ {type(exc).__name__}: {exc}")
        _write_report(out, report)
        return EXIT_HELPER_FAILED

    _write_report(out, report)
    print()
    print("== 结果 ==")
    print(f"  包：{report['packets']}")
    print(f"  事件：{report['events']}")
    for actor, info in report["actors"].items():
        print(f"  玩家 {actor}: {info['frames']} 帧 / {info['seconds']:.1f}s / "
              f"布局 {info['layout']}")
    if not report["actors"]:
        print("  ⚠️ 没有解出任何语音帧。")
        if report.get("voice_events", 0) == 0:
            print("     整段解码期间**没有收到任何入向语音事件（事件码 1）** ——"
                  "最可能是这期间没人说话/对方静音。")
            print("     自己说话产生的是「出向」包，不会出现在这里。")
        else:
            print("     有入向语音事件但切不出帧 ⇒ 看 report.json 的 "
                  "first_voice_body_hex 与 payload_samples，按实际字节改切法。")
        print("     报告新增了：command_kinds / audio_param_hits / payload_samples "
              "（每个 kind:code 的前 48 字节十六进制）")
        return EXIT_NO_VOICE
    print(f"\n  wav 与报告已写入 {out}（wav 是他人语音，用完请删）")
    return EXIT_OK




def _decode_session(sess: HelperSession, hit: gcm.KeyHit, out: Path,
                    args: argparse.Namespace, report: dict,
                    pcap: capture_linux.PcapWriter,
                    *, replay: list[capture_linux.Datagram] | None = None,
                    log_names: dict[str, str] | None = None,
                    local_ports: set[int] | None = None) -> None:
    """用找到的密钥边抓边解，收集每位玩家的 PCM。"""
    collector = VoiceCollector(report, fill_gaps=args.fill_gaps, log_names=log_names)
    print(f"[p0b] 开始实时解码（{args.seconds:.0f}s，Ctrl+C 提前结束）…", flush=True)
    print(f"[p0b] ⚠️ 这 {args.seconds:.0f} 秒请让**别人**说话："
          "自己说话不会出现在「入向」包里", flush=True)

    if replay:
        print(f"[p0b] 回放取钥阶段的 {len(replay)} 个包（那段时间说的话也算）…",
              flush=True)
        for dg in replay:
            collector.handle(dg, hit.key, hit.layout)

    deadline = time.monotonic() + args.seconds
    next_report = time.monotonic() + 10.0
    warned_quiet = False
    try:
        while time.monotonic() < deadline:
            _collect_packets(sess.packet_socket, port=args.port, seconds=0.5,
                             want=10_000, pcap=pcap,
                             local_ports=local_ports,
                             on_datagram=lambda dg: collector.handle(dg, hit.key,
                                                                     hit.layout))
            now = time.monotonic()
            if now >= next_report:
                next_report = now + 10.0
                print(f"[p0b] 进度：{collector.summary_line()}", flush=True)
                if (not warned_quiet and collector.audio_hits
                        and not collector.pcm and now > deadline - args.seconds * 0.5):
                    warned_quiet = True
                    print("[p0b] ⚠️ 有音频消息但切不出帧 —— 报告里的 "
                          "first_voice_body_hex 就是线索", flush=True)
    except KeyboardInterrupt:
        print("\n[p0b] 收到 Ctrl+C，收尾…", flush=True)

    collector.finish(out)


def cmd_replay(args: argparse.Namespace) -> int:
    """离线重放：用 pcap + 密钥重跑解码（不开游戏、不提权）。

    这是给了"改协议假设"用的：第一次抓的 `cap.pcap` 可以反复分析，
    不用每改一次就重开一局。
    """
    key = Path(args.key_file).read_bytes()
    if len(key) != gcm.KEY_LEN:
        print(f"❌ 密钥文件长度不是 {gcm.KEY_LEN} 字节（{len(key)}）")
        return EXIT_HELPER_FAILED
    frames = capture_linux.read_pcap(args.pcap)
    print(f"[replay] {len(frames)} 帧，密钥指纹 {gcm.key_fingerprint(key)}", flush=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    log_names: dict[str, str] = {}
    if args.log_dir:
        latest = names.latest_output_log(args.log_dir)
        if latest is not None:
            log_names = names.parse_output_log(latest)
    collector = VoiceCollector(report, fill_gaps=args.fill_gaps, log_names=log_names)
    handled = 0
    for _ts, raw in frames:
        dg = capture_linux.parse_frame(raw, port=args.port)
        if dg is None:
            continue
        handled += 1
        collector.handle(dg, key, args.layout)
    print(f"[replay] 解析出 {handled} 个 UDP 数据报", flush=True)
    actors = collector.finish(out)
    _write_report(out, report)
    print(f"[replay] {collector.summary_line()}")
    for actor, info in actors.items():
        print(f"  玩家 {actor}: {info['frames']} 帧 / {info['seconds']:.1f}s")
    if not actors:
        print("  ⚠️ 仍然没有语音。看 report.json 的 payload_samples / audio_param_hits")
        return EXIT_NO_VOICE
    return EXIT_OK


def cmd_names(args: argparse.Namespace) -> int:
    """离线看一份 pcap 里的**玩家名事件**：解码 42/253/255/254，逐条打印解出了什么。

    用来回答"为什么只有 actorN"：事件没来 / 来了但没有 usr_+displayName / 有记录但没名字。
    """
    key = Path(args.key_file).read_bytes()
    if len(key) != gcm.KEY_LEN:
        print(f"❌ 密钥长度不是 {gcm.KEY_LEN}（{len(key)}）")
        return EXIT_HELPER_FAILED
    frames = capture_linux.read_pcap(args.pcap)
    fragmenters: dict[str, photon.Fragmenter] = defaultdict(photon.Fragmenter)
    counts: Counter = Counter()
    records: list[names.PlayerRecord] = []
    shown = 0
    for ts, raw in frames:
        dg = capture_linux.parse_frame(raw, port=args.port)
        if dg is None:
            continue
        plain = gcm.try_open(key, dg.payload, args.layout)
        if plain is None:
            continue
        commands, _err = photon.parse_commands(plain)
        for cmd in commands:
            for payload in fragmenters[dg.direction].feed(cmd):
                msg, _skip = photon.parse_message_at(payload)
                if msg is None or msg.kind != photon.TYPE_EVENT:
                    continue
                if msg.code not in names.NAME_EVENT_CODES and msg.code != names.LEAVE_EVENT:
                    continue
                counts[msg.code] += 1
                if msg.code == names.LEAVE_EVENT:
                    continue
                parsed = names.parse_records(msg.payload)
                records.extend(parsed)
                if shown < 12:
                    shown += 1
                    print(f"[{ts % 1000:7.2f}] code={msg.code} len={len(msg.payload)} "
                          f"usr={len(names.USR_RE.findall(msg.payload))} "
                          f"displayName={msg.payload.count(b'displayName')} "
                          f"actor标记={len(names.actor_markers(msg.payload))} "
                          f"→ 记录 {len(parsed)}"
                          + ("" if parsed else f"  head={msg.payload[:40].hex()}"))
    print(f"\n事件计数：{[f'{c}:{n}' for c, n in sorted(counts.items())]}")
    print(f"分片完成：in={fragmenters['in'].completed} out={fragmenters['out'].completed} "
          f"丢弃 {fragmenters['in'].dropped + fragmenters['out'].dropped}")
    if records:
        print(f"\n解出 {len(records)} 条玩家记录：")
        for rec in records:
            print(f"  actor {rec.actor:>5}  {rec.display_name!r:24s} {rec.user_id}")
    else:
        print("\n❌ 一条记录都没解出（看上面的 code/len/usr/displayName 判断原因）")
    return EXIT_OK


def cmd_sniff(args: argparse.Namespace) -> int:
    """独立的诊断子命令：只回答"本机网卡上有什么流量"，不读内存。"""
    host = _wait_host(args.wait)
    if host is None:
        print("❌ 没找到 VRChat 宿主进程")
        return EXIT_NO_PROCESS
    print(f"[sniff] 宿主 pid={host.pid}，采样 {args.seconds:.0f}s（无过滤）…", flush=True)
    try:
        with HelperSession(host, port=args.port) as sess:
            summary = _diagnose(sess, port=args.port, seconds=args.seconds)
    except (RuntimeError, OSError) as exc:
        print(f"❌ {type(exc).__name__}: {exc}")
        return EXIT_HELPER_FAILED
    _print_sniff(summary, args.port)
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已写入 {path}")
    return EXIT_OK


def _write_report(out: Path, report: dict) -> None:
    path = out / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


# ================================================================ CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_player_audio",
        description="分玩家语音探针：env（零风险）/ run（一次性提权取钥 + 解码）。",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    env = sub.add_parser("env", help="采样系统姿态与进程元数据（P0-a，零风险）")
    env.add_argument("--json", default="", help="把结果写成 JSON（指定路径才写）")
    env.set_defaults(func=cmd_env)

    run = sub.add_parser("run", help="P0-b：pkexec 提权取钥 + 抓包 + 解出每位玩家的 wav")
    run.add_argument("--out", default="out/players-p0", help="输出目录（默认 out/players-p0）")
    run.add_argument("--seconds", type=float, default=90.0, help="解码阶段时长（默认 90s）")
    run.add_argument("--pre-seconds", type=float, default=120.0,
                     help="取钥前等加密流量的最长秒数（够 min-samples 就提前结束）")
    run.add_argument("--min-samples", type=int, default=40, help="至少抓多少个入向加密包")
    run.add_argument("--scan-seconds", type=float, default=40.0, help="单次扫描预算")
    run.add_argument("--scan-retries", type=int, default=1,
                     help="每个 needle 的重试次数（两种 needle 会自动各跑一遍）")
    run.add_argument("--wait", type=float, default=120.0, help="等 VRChat 启动的最长秒数")
    run.add_argument("--port", type=int, default=27002)
    run.add_argument("--log-dir", default="",
                     help="VRChat 的 output_log 目录（空 = 自动找 ~/.local/share/VRChat/VRChat）")
    run.add_argument("--loose-filter", action="store_true",
                     help="BPF 只筛 IPv4+UDP（排查抓不到包时用）")
    run.add_argument("--fill-gaps", action="store_true",
                     help="缺口补静音（默认关：缺口多是对方 VAD 停顿，补了会把时间轴撑长）")
    run.add_argument("--save-key-to", default="",
                     help="把密钥写到指定文件（0600，仅用于离线 replay；用完请删）")
    run.set_defaults(func=cmd_run)

    sniff = sub.add_parser("sniff", help="诊断：无过滤采样，看网卡上到底有什么流量")
    sniff.add_argument("--seconds", type=float, default=5.0)
    sniff.add_argument("--wait", type=float, default=60.0)
    sniff.add_argument("--port", type=int, default=27002)
    sniff.add_argument("--json", default="", help="把直方图写成 JSON")
    sniff.set_defaults(func=cmd_sniff)

    names_cmd = sub.add_parser("names", help="离线分析 pcap 里的玩家名事件（不需要游戏）")
    names_cmd.add_argument("--pcap", required=True)
    names_cmd.add_argument("--key-file", required=True)
    names_cmd.add_argument("--port", type=int, default=27002)
    names_cmd.add_argument("--layout", default=gcm.LAYOUTS[0], choices=list(gcm.LAYOUTS))
    names_cmd.set_defaults(func=cmd_names)

    replay = sub.add_parser("replay",
                            help="离线重放：用 cap.pcap + 密钥文件重跑解码（不提权）")
    replay.add_argument("--pcap", required=True)
    replay.add_argument("--key-file", required=True)
    replay.add_argument("--out", default="out/players-replay")
    replay.add_argument("--port", type=int, default=27002)
    replay.add_argument("--layout", default=gcm.LAYOUTS[0],
                        choices=list(gcm.LAYOUTS))
    replay.add_argument("--fill-gaps", action="store_true")
    replay.add_argument("--log-dir", default="", help="output_log 目录（名字兜底）")
    replay.set_defaults(func=cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
