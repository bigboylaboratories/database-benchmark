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
import sys
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


def _cgroup_paths() -> Dict[str, str]:
    """Map controller -> this process's cgroup path ('' key = cgroup v2 unified)."""
    out: Dict[str, str] = {}
    for line in (_read("/proc/self/cgroup") or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            for ctrl in parts[1].split(","):
                out[ctrl] = parts[2]
    return out


def _quota(quota: Optional[str], period: Optional[str]) -> Optional[float]:
    try:
        q, per = int(quota), int(period)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(q / per, 2) if q > 0 and per > 0 else None


def _cgroup_cpu_limit() -> Optional[float]:
    """Tightest CPU quota imposed by cgroups on this process, in cores. None = unlimited.

    Checks this process's own cgroup and its ancestors, not just the root, so it also
    works for systemd slices / nested containers without a cgroup namespace.
    """
    paths = _cgroup_paths()
    limits = []
    if "" in paths:  # cgroup v2
        rel = paths[""].strip("/")
        parts = rel.split("/") if rel else []
        for i in range(len(parts), -1, -1):
            v = _read("/sys/fs/cgroup/" + "/".join(parts[:i] + ["cpu.max"]))
            if v:
                quota, _, period = v.partition(" ")
                if quota != "max":
                    limits.append(_quota(quota, period))
    v1 = paths.get("cpu")
    if v1 is not None or not paths:
        for base in ("/sys/fs/cgroup/cpu", "/sys/fs/cgroup/cpu,cpuacct"):
            rel = (v1 or "").strip("/")
            parts = rel.split("/") if rel else []
            for i in range(len(parts), -1, -1):
                d = "/".join([base] + parts[:i])
                limits.append(_quota(_read(d + "/cpu.cfs_quota_us"), _read(d + "/cpu.cfs_period_us")))
    if not paths:  # no /proc/self/cgroup: fall back to the root files
        v = _read("/sys/fs/cgroup/cpu.max")
        if v and not v.startswith("max"):
            limits.append(_quota(*v.split(" ", 1)))
    limits = [x for x in limits if x]
    return min(limits) if limits else None


def _windows_memory_mb() -> Optional[int]:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            return st.ullTotalPhys // 2**20
    except Exception:
        pass
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
    if out["total_mb"] is None and os.name == "nt":
        out["total_mb"] = _windows_memory_mb()
    return out


def _cpu_freq_mhz() -> Optional[int]:
    for f in ("cpuinfo_max_freq", "scaling_max_freq"):
        v = _read(f"/sys/devices/system/cpu/cpu0/cpufreq/{f}")
        if v and v.isdigit():
            return int(v) // 1000
    return None


def _container() -> Optional[str]:
    if os.path.exists("/.dockerenv"):
        return "docker"
    if os.path.exists("/run/.containerenv"):
        return "podman"
    cg = _read("/proc/1/cgroup") or ""
    for name in ("docker", "kubepods", "lxc", "containerd"):
        if name in cg:
            return name
    return None


def _virtualization(cpu: Dict[str, Any]) -> Dict[str, Any]:
    virt = _run(["systemd-detect-virt"])
    if virt == "none":
        virt = None
    if not virt:
        virt = _container()
    if not virt and "microsoft" in platform.release().lower():
        virt = "wsl"
    dmi_vendor = _read("/sys/class/dmi/id/sys_vendor")
    dmi_product = _read("/sys/class/dmi/id/product_name")
    board = _read("/proc/device-tree/model") or _read("/sys/firmware/devicetree/base/model")
    is_virtual = bool(
        virt
        or cpu.get("hypervisor_flag")
        or any(
            s and re.search(r"qemu|kvm|virtualbox|vmware|xen|hyper-v|bochs|dummy-virt", s, re.I)
            for s in (dmi_vendor, dmi_product, board)
        )
    )
    return {
        "detected": virt,
        "is_virtual": is_virtual,
        "dmi_vendor": dmi_vendor,
        "dmi_product": dmi_product,
        # e.g. "Raspberry Pi 5 Model B Rev 1.0" on real Pis, "linux,dummy-virt" in QEMU virt VMs
        "board_model": board,
        "raspberry_pi": bool(board and board.startswith("Raspberry Pi")),
    }


def _usable_cores() -> Optional[int]:
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    if os.name == "nt":  # os.process_cpu_count() ignores the affinity mask on Windows
        try:
            import ctypes

            proc_mask, sys_mask = ctypes.c_size_t(), ctypes.c_size_t()
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            if k32.GetProcessAffinityMask(ctypes.c_void_p(k32.GetCurrentProcess()),
                                          ctypes.byref(proc_mask), ctypes.byref(sys_mask)):
                n = bin(proc_mask.value).count("1")
                if n:
                    return n
        except Exception:
            pass
    if hasattr(os, "process_cpu_count"):  # Python 3.13+, honours affinity on Windows too
        return os.process_cpu_count()  # type: ignore[attr-defined]
    return None


def collect() -> Dict[str, Any]:
    cpu = _cpuinfo()
    affinity = _usable_cores()
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


def _burn_worker(iterations: int, barrier, out) -> None:
    try:
        barrier.wait()  # start together, after every process has spawned and imported
    except Exception:
        return
    out.put(_burn(iterations))


def _parallel_wall(ctx, workers: int, iterations: int) -> float:
    """Wall time for `workers` processes to each burn `iterations`, excluding process startup."""
    barrier = ctx.Barrier(workers + 1)
    out = ctx.Queue()
    procs = [ctx.Process(target=_burn_worker, args=(iterations, barrier, out), daemon=True)
             for _ in range(workers)]
    for pr in procs:
        pr.start()
    try:
        barrier.wait(timeout=120)
        start = time.perf_counter()
        for _ in procs:
            out.get(timeout=600)
        return time.perf_counter() - start
    finally:
        for pr in procs:
            pr.join(timeout=10)
            if pr.is_alive():
                pr.terminate()


def verify_cores(max_workers: Optional[int] = None, target_s: float = 0.5) -> Dict[str, Any]:
    """Run the same CPU task with 1..N parallel processes and report scaling.

    If a VM claims 4 cores but they're backed by 1 host thread (or emulated by a
    single-threaded QEMU TCG), effective_cores will be ~1 instead of ~4.
    """
    claimed = max_workers or (collect()["cpu"]["usable_cores"] or os.cpu_count() or 1)
    iterations = _calibrate(target_s)
    ctx = mp.get_context("fork" if hasattr(os, "fork") and sys.platform != "darwin" else "spawn")
    # Baseline in a worker process too, so every step pays the same (tiny) IPC cost.
    single = _parallel_wall(ctx, 1, iterations)
    per_op_rate = iterations / single

    steps = sorted({1, 2, claimed} | {w for w in (4, 8, 16, 32, 64) if w < claimed})
    steps = [w for w in steps if w <= claimed]
    results = []
    for workers in steps:
        wall = single if workers == 1 else _parallel_wall(ctx, workers, iterations)
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
