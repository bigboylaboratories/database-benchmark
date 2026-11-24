"""Collect hardware/VM facts about the machine and verify the cores it claims to have.

Virtual machines (QEMU, VirtualBox, cloud VMs, containers with CPU quotas) often
report a core count that differs from what is actually usable. We therefore record
several views of "how many cores" and measure CPU scaling empirically.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _read(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(errors="replace").strip("\x00").strip()
    except OSError:
        return None


def _run(cmd: List[str]) -> Optional[str]:
    if not shutil.which(cmd[0]):
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _cpuinfo() -> Dict[str, Any]:
    text = _read("/proc/cpuinfo") or ""
    info: Dict[str, Any] = {}
    model = None
    for key in ("model name", "Model", "Hardware", "cpu model", "Processor"):
        m = re.search(rf"^{re.escape(key)}\s*:\s*(.+)$", text, re.MULTILINE)
        if m:
            model = m.group(1).strip()
            break
    if model is None:
        # ARM cores often only expose an implementer/part id.
        part = re.search(r"^CPU part\s*:\s*(\S+)", text, re.MULTILINE)
        if part:
            model = f"ARM part {part.group(1)}"
    info["model"] = model or platform.processor() or None

    # Physical cores = unique (physical id, core id) pairs, if exposed.
    pairs = set()
    phys = core = None
    for line in text.splitlines():
        if line.startswith("physical id"):
            phys = line.split(":", 1)[1].strip()
        elif line.startswith("core id"):
            core = line.split(":", 1)[1].strip()
        elif not line.strip():
            if core is not None:
                pairs.add((phys, core))
            phys = core = None
    if core is not None:
        pairs.add((phys, core))
    info["physical_cores"] = len(pairs) or None

    flags = re.search(r"^(flags|Features)\s*:\s*(.+)$", text, re.MULTILINE)
    info["hypervisor_flag"] = bool(flags and "hypervisor" in flags.group(2).split())
    return info


def _cgroup_cpu_limit() -> Optional[float]:
    """CPU quota imposed by cgroups (containers), in cores. None = unlimited."""
    v2 = _read("/sys/fs/cgroup/cpu.max")
    if v2:
        quota, _, period = v2.partition(" ")
        if quota != "max" and period:
            return round(int(quota) / int(period), 2)
        return None
    quota = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period and int(quota) > 0:
        return round(int(quota) / int(period), 2)
    return None


def _memory() -> Dict[str, Optional[int]]:
    text = _read("/proc/meminfo") or ""
    out: Dict[str, Optional[int]] = {"total_mb": None, "available_mb": None}
    for key, name in (("MemTotal", "total_mb"), ("MemAvailable", "available_mb")):
        m = re.search(rf"^{key}:\s*(\d+) kB", text, re.MULTILINE)
        if m:
            out[name] = int(m.group(1)) // 1024
    if out["total_mb"] is None and hasattr(os, "sysconf"):
        try:
            out["total_mb"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**20
        except (ValueError, OSError):
            pass
    return out


def _cpu_freq_mhz() -> Optional[int]:
    for f in ("cpuinfo_max_freq", "scaling_max_freq"):
        v = _read(f"/sys/devices/system/cpu/cpu0/cpufreq/{f}")
        if v and v.isdigit():
            return int(v) // 1000
    return None


def _virtualization(cpu: Dict[str, Any]) -> Dict[str, Any]:
    virt = _run(["systemd-detect-virt"])
    dmi_vendor = _read("/sys/class/dmi/id/sys_vendor")
    dmi_product = _read("/sys/class/dmi/id/product_name")
    board = _read("/proc/device-tree/model") or _read("/sys/firmware/devicetree/base/model")
    is_virtual = bool(
        (virt and virt != "none")
        or cpu.get("hypervisor_flag")
        or any(
            s and re.search(r"qemu|kvm|virtualbox|vmware|xen|hyper-v|bochs", s, re.I)
            for s in (dmi_vendor, dmi_product, board)
        )
    )
    return {
        "detected": virt if virt and virt != "none" else None,
        "is_virtual": is_virtual,
        "dmi_vendor": dmi_vendor,
        "dmi_product": dmi_product,
        "board_model": board,  # e.g. "Raspberry Pi 4 Model B Rev 1.4" on real Pis / raspi QEMU machines
    }


def collect() -> Dict[str, Any]:
    cpu = _cpuinfo()
    try:
        affinity = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        affinity = None
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpu": {
            "model": cpu["model"],
            "logical_cores": os.cpu_count(),
            "physical_cores": cpu["physical_cores"],
            "usable_cores": affinity,
            "cgroup_limit_cores": _cgroup_cpu_limit(),
            "max_freq_mhz": _cpu_freq_mhz(),
        },
        "memory": _memory(),
        "virtualization": _virtualization(cpu),
    }


# --------------------------------------------------------------------------- #
# Core verification: does throughput actually scale with the number of workers?
# --------------------------------------------------------------------------- #

def _burn(iterations: int) -> float:
    """Pure CPU work; returns elapsed seconds inside the worker."""
    start = time.perf_counter()
    x = 0
    for i in range(iterations):
        x = (x * 31 + i) & 0xFFFFFFFF
    return time.perf_counter() - start


def _calibrate(target_s: float) -> int:
    n = 200_000
    while True:
        t = _burn(n)
        if t >= target_s / 4:
            return max(int(n * target_s / t), 1)
        n *= 2


def verify_cores(max_workers: Optional[int] = None, target_s: float = 0.5) -> Dict[str, Any]:
    """Run the same CPU task with 1..N parallel processes and report scaling.

    If a VM claims 4 cores but they're backed by 1 host thread (or emulated by a
    single-threaded QEMU TCG), effective_cores will be ~1 instead of ~4.
    """
    claimed = max_workers or (collect()["cpu"]["usable_cores"] or os.cpu_count() or 1)
    iterations = _calibrate(target_s)
    single = _burn(iterations)
    per_op_rate = iterations / single

    steps = sorted({1, 2, claimed} | {w for w in (4, 8, 16, 32, 64) if w < claimed})
    steps = [w for w in steps if w <= claimed]
    results = []
    ctx = mp.get_context("spawn" if os.name == "nt" else "fork")
    for workers in steps:
        with ctx.Pool(workers) as pool:
            start = time.perf_counter()
            pool.map(_burn, [iterations] * workers)
            wall = time.perf_counter() - start
        throughput = workers * iterations / wall
        results.append({
            "workers": workers,
            "wall_s": round(wall, 4),
            "speedup": round(throughput / per_op_rate, 2),
            "efficiency": round(throughput / per_op_rate / workers, 2),
        })
    effective = max(r["speedup"] for r in results)
    return {
        "claimed_cores": claimed,
        "effective_cores": effective,
        "single_thread_ops_per_s": int(per_op_rate),
        "scaling": results,
        "verdict": (
            "ok" if effective >= claimed * 0.75
            else "degraded" if effective >= claimed * 0.4
            else "oversubscribed"
        ),
    }
