"""EAC 姿态判定：分玩家模式能不能开，以及为什么。

## 背景（2026-10 实测 + 官方公告）

Epic 在 2026-10-14 公布了 **EAC Linux v2**：不用内核模块，改用「systemd 单元 +
eBPF LSM」来**限制其他进程读写受保护游戏的内存**。它要求签名内核、TPM2、
Secure Boot、measured boot、IOMMU（禁 passthrough）、`lockdown=integrity`、
kexec 禁用、BPF LSM 激活。

对我们的直接影响：

- **v2 生效** ⇒ 提权助手读内存会被内核直接拒绝（不是"风险变高"，是"路没了"），
  所以必须**在开跑之前**判出来，直接拒绝并说明原因，而不是跑一半报 EPERM。
- **旧版（用户态 `easyanticheat_x64.so`）** ⇒ 内核层面没有任何东西拦我们；
  剩下的只是用户态能观察到什么（进程表、`/proc/<pid>` 元数据），那是设计约束，
  不是这里的判定范围。

## 为什么是「静态闸门 + 纯函数」

`collect()` 只读系统状态文件，`assess()` 是对 `Posture` 的纯函数 —— 这样整条
判定链**离线可测**（`tests/test_players_env.py` 喂合成的 Posture，不需要真的
改内核参数）。运行期还有第二道闸门：真正读内存时若拿到 `EPERM`，一样要停下并
按"被拦"处理（见后续 `key_linux.py`）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# EAC Linux v2 的 systemd 单元名里会出现这些词（官方公告：首次启动会提示安装）。
EAC_UNIT_MARKERS = ("easyanticheat", "eac-", "easy-anti-cheat")

# Secure Boot 的 EFI 变量（全局变量 GUID，标准值，不随发行版变）。
SECURE_BOOT_VAR = "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"


@dataclass(frozen=True)
class Posture:
    """一次采集到的系统安全姿态。全部字段都是"原始事实"，不含判断。"""

    kernel: str = ""
    lockdown: str = "unknown"          # none | integrity | confidentiality | unknown
    lsm: tuple[str, ...] = ()
    secure_boot: str = "unknown"       # on | off | unknown
    iommu: str = "unknown"             # on | off | unknown
    kexec_disabled: str = "unknown"    # yes | no | unknown
    # Yama 的 ptrace 作用域（0/1/2/3）。它决定「同用户能不能直接读别人的内存」：
    # 1 = 只允许祖先进程，我们不是 VRChat 的祖先 ⇒ **必须提权**。这台内核的
    # /proc/<pid>/status 不暴露 Dumpable，所以这个值比它更有决定性。
    ptrace_scope: str = "unknown"
    eac_units: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def bpf_lsm(self) -> bool:
        return "bpf" in self.lsm


@dataclass(frozen=True)
class Verdict:
    """闸门结论。

    level:
      * ``legacy``  —— 旧版用户态 EAC，读内存不会被内核拦（风险另说）
      * ``blocked`` —— 有 EAC Linux v2 迹象，**不许**开
      * ``unknown`` —— 证据不足，必须由用户显式确认才继续
    """

    level: str
    reasons: tuple[str, ...] = ()
    advice: str = ""


# ---------------------------------------------------------------- 纯解析


def parse_lockdown(text: str) -> str:
    """`/sys/kernel/security/lockdown` 的格式是 `[none] integrity confidentiality`，
    中括号里的那个是**当前生效**的值。解析不出来就返回 unknown（不猜 none）。
    """
    for token in text.split():
        if token.startswith("[") and token.endswith("]"):
            value = token[1:-1].strip().lower()
            if value in ("none", "integrity", "confidentiality"):
                return value
    return "unknown"


def parse_lsm(text: str) -> tuple[str, ...]:
    """`/sys/kernel/security/lsm` 是逗号分隔的已激活 LSM 列表。"""
    return tuple(sorted({t.strip().lower() for t in text.split(",") if t.strip()}))


def parse_secure_boot(raw: bytes) -> str:
    """EFI 变量前 4 字节是属性，第 5 字节才是值（1 = 开启）。

    efivarfs 常因权限读不到（返回 EPERM）—— 那种情况必须给 unknown，
    绝不能默认当成关闭：那是**把闸门判松**，正是最危险的错误方向。
    """
    if len(raw) < 5:
        return "unknown"
    return "on" if raw[4] else "off"


def parse_kexec_disabled(text: str) -> str:
    """`/proc/sys/kernel/kexec_load_disabled` 为 1 表示已在启动时禁用 kexec。"""
    value = text.strip()
    if value == "1":
        return "yes"
    if value == "0":
        return "no"
    return "unknown"


# ---------------------------------------------------------------- 采集（薄壳）


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def read_secure_boot(root: Path) -> str:
    """读 Secure Boot 状态。

    - 有 EFI 变量 → 按值判定；
    - 读不到变量但系统根本没有 `/sys/firmware/efi` → 是 BIOS 引导，**确定**关着；
    - 有 EFI 但变量读不到（权限/内核没暴露）→ unknown（不能猜成 off）。
    """
    efi_dir = root / "sys/firmware/efi"
    if not efi_dir.exists():
        return "off"
    raw = _read_bytes(efi_dir / "efivars" / SECURE_BOOT_VAR)
    if raw is None:
        return "unknown"
    return parse_secure_boot(raw)


def read_iommu(root: Path) -> str:
    """IOMMU 是否启用：`/sys/kernel/iommu_groups/` 下有组就是启用。

    v2 要求「启用且禁 passthrough」；passthrough 开关没有统一的可读节点，
    所以这里只判"启用与否"，并在 notes 里注明这一项没判。
    """
    groups = root / "sys/kernel/iommu_groups"
    try:
        return "on" if any(groups.iterdir()) else "off"
    except OSError:
        return "unknown"


def find_eac_units(root: Path) -> tuple[str, ...]:
    """找 EAC 的 systemd 单元文件（v2 会装一个，这是他最硬的落地证据）。"""
    names: set[str] = set()
    for rel in ("usr/lib/systemd/system", "lib/systemd/system", "etc/systemd/system",
                "usr/lib/systemd/user", "etc/systemd/user"):
        directory = root / rel
        try:
            # ⚠️ 必须 listdir 后再遍历：`Path.iterdir()` 是惰性生成器，
            # 目录不存在时异常在**迭代时**才抛，套在 try 外面会漏。
            entries = os.listdir(directory)
        except OSError:
            continue
        for name in entries:
            low = name.lower()
            if any(marker in low for marker in EAC_UNIT_MARKERS):
                names.add(name)
    return tuple(sorted(names))


def collect(root: Path = Path("/")) -> Posture:
    """采集当前系统的姿态。`root` 只用于测试注入（默认真根目录）。"""
    notes: list[str] = []
    kernel = _read_text(root / "proc/sys/kernel/osrelease") or ""
    kernel = kernel.strip() or "unknown"

    security = root / "sys/kernel/security"
    lockdown_text = _read_text(security / "lockdown")
    lsm_text = _read_text(security / "lsm")
    if lockdown_text is None:
        notes.append("读不到 /sys/kernel/security/lockdown（lockdown 是否编译进内核未知）")
    if lsm_text is None:
        notes.append("读不到 /sys/kernel/security/lsm（LSM 列表未知）")

    kexec_text = _read_text(root / "proc/sys/kernel/kexec_load_disabled")
    scope_text = _read_text(root / "proc/sys/kernel/yama/ptrace_scope")

    posture = Posture(
        kernel=kernel,
        lockdown=parse_lockdown(lockdown_text) if lockdown_text else "unknown",
        lsm=parse_lsm(lsm_text) if lsm_text else (),
        secure_boot=read_secure_boot(root),
        iommu=read_iommu(root),
        kexec_disabled=parse_kexec_disabled(kexec_text) if kexec_text else "unknown",
        ptrace_scope=scope_text.strip() if scope_text else "unknown",
        eac_units=find_eac_units(root),
        notes=tuple(notes),
    )
    return posture


# ---------------------------------------------------------------- 判定


def assess(posture: Posture) -> Verdict:
    """把姿态判成 legacy / blocked / unknown。

    判定顺序刻意从「最硬的证据」到「推测」：
    1. `lockdown=integrity` —— v2 的**必要条件**，单独出现就足以拒绝；
    2. 存在 EAC 的 systemd 单元 —— v2 的安装痕迹；
    3. `Secure Boot` 开启 **且** BPF LSM 激活 —— v2 的另一组必要条件；
    4. Secure Boot 读不到但 BPF LSM 在 —— 证据不足，交给人确认（unknown）。
    """
    reasons: list[str] = []

    if posture.lockdown == "integrity":
        reasons.append("内核 lockdown=integrity（EAC Linux v2 的必要条件）")
    if posture.eac_units:
        reasons.append("发现 EAC 的 systemd 单元：" + "、".join(posture.eac_units))
    if posture.secure_boot == "on" and posture.bpf_lsm:
        reasons.append("Secure Boot 已开启且 BPF LSM 激活")
    if reasons:
        return Verdict(
            level="blocked",
            reasons=tuple(reasons),
            advice="这台机器已具备 EAC Linux v2 的条件：读游戏内存会被 eBPF LSM 拒绝，"
                   "分玩家模式无法启用。请改用「房间模式」或声纹分离。",
        )

    if posture.secure_boot == "unknown" and posture.bpf_lsm:
        return Verdict(
            level="unknown",
            reasons=("Secure Boot 状态读不到（通常是 efivarfs 权限），但 BPF LSM 已激活",),
            advice="无法排除 EAC Linux v2。可先跑一次探针确认；若真被拦，读内存会返回 EPERM。",
        )

    detail = [f"lockdown={posture.lockdown}", f"BPF LSM={'on' if posture.bpf_lsm else 'off'}",
              f"Secure Boot={posture.secure_boot}"]
    return Verdict(
        level="legacy",
        reasons=("未发现 EAC Linux v2 的必要条件：" + "，".join(detail),),
        advice="旧版用户态 EAC：内核不会拦读内存。风险仍然存在（ToS 灰区），"
               "请小号验证，并保持「只读、不 attach、不注入」。",
    )
