#!/usr/bin/env python3
"""Create Ubuntu VMs in VMware Workstation / Player and benchmark inside them, hands-free.

Needs only what VMware Workstation already ships (vmrun, vmware-vdiskmanager) plus the
OpenSSH client built into Windows 10/11 and most Linux distros. It downloads the official
Ubuntu cloud image (VMware format) once, then for each machine:

  copy disk -> write .vmx (cores, RAM, cloud-init via guestinfo) -> boot headless ->
  wait for SSH -> copy this repo in -> bootstrap.sh -> dbbench run -b all -> copy results back -> shut down

Usage (from the repo root, with any Python 3.10+):

  python scripts/vmware_vm.py bench --preset x86-4c            # 4 vCPU / 4 GB server
  python scripts/vmware_vm.py bench --preset pi5 -- -p quick   # Pi 5-sized VM, extra dbbench args after --
  python scripts/vmware_vm.py bench --cores 6 --mem 12G        # any size
  python scripts/vmware_vm.py bench-all                        # every preset in turn, then a report
  python scripts/vmware_vm.py up --preset x86-2c               # just boot it and print the ssh command
  python scripts/vmware_vm.py stop|destroy --preset x86-2c
  python scripts/vmware_vm.py list

VMware runs x86_64 guests only. The pi* presets are x86 VMs with a Raspberry Pi's core count
and RAM ("Pi-sized"), not ARM emulation: use scripts/qemu-vm.sh on a Linux host, or a real Pi,
for ARM numbers.
"""

from __future__ import annotations

import argparse
import base64
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VM_DIR = Path(os.environ.get("VM_DIR", REPO / "vm" / "vmware"))
UBUNTU = os.environ.get("UBUNTU", "resolute")  # 26.04 LTS
IMAGE_URL = os.environ.get(
    "IMAGE_URL",
    f"https://cloud-images.ubuntu.com/{UBUNTU}/current/{UBUNTU}-server-cloudimg-amd64.vmdk")

# name: (cores, mem MB, description)
PRESETS = {
    "x86-1c": (1, 1024, "tiny VPS"),
    "x86-2c": (2, 2048, "small server"),
    "x86-4c": (4, 4096, "mid server / desktop"),
    "x86-8c": (8, 8192, "large server"),
    "x86-16c": (16, 16384, "big server"),
    "pi3": (4, 1024, "Raspberry Pi 3-sized (x86)"),
    "pi4": (4, 2048, "Raspberry Pi 4-sized (x86)"),
    "pi5": (4, 4096, "Raspberry Pi 5-sized (x86)"),
    "pi5-8g": (4, 8192, "Raspberry Pi 5 8GB-sized (x86)"),
}
BENCH_ALL = ["x86-1c", "x86-2c", "x86-4c", "x86-8c", "pi4", "pi5"]


def log(msg: str) -> None:
    print(f">> {msg}", flush=True)


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------------ tools -- #

def find_vmware_tool(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    if shutil.which(exe):
        return shutil.which(exe)  # type: ignore[return-value]
    dirs = [os.environ.get("VMWARE_DIR", ""),
            r"C:\Program Files (x86)\VMware\VMware Workstation",
            r"C:\Program Files\VMware\VMware Workstation",
            r"C:\Program Files (x86)\VMware\VMware Player",
            "/usr/bin", "/usr/local/bin",
            "/Applications/VMware Fusion.app/Contents/Library"]
    for d in dirs:
        if d and Path(d, exe).exists():
            return str(Path(d, exe))
    die(f"{exe} not found: install VMware Workstation (or set VMWARE_DIR)")
    return ""


def run(cmd, check=True, capture=True, timeout=None, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout, **kw)
    if check and r.returncode:
        die(f"{' '.join(map(str, cmd))} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


def vmrun(*args, check=True, timeout=None) -> subprocess.CompletedProcess:
    host = "fusion" if sys.platform == "darwin" else "ws"
    return run([find_vmware_tool("vmrun"), "-T", host, *map(str, args)], check=check, timeout=timeout)


def ssh_tool(name: str) -> str:
    """Windows' built-in OpenSSH on Windows (Git Bash's MSYS ssh treats NUL as a file); else PATH."""
    if os.name == "nt":
        builtin = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", f"{name}.exe")
        if builtin.exists():
            return str(builtin)
    return name


def ssh_key() -> Path:
    key = VM_DIR / "id_ed25519"
    if not key.exists():
        VM_DIR.mkdir(parents=True, exist_ok=True)
        run([ssh_tool("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", "dbbench-vm", "-f", str(key)])
    return key


def ssh_base(ip: str) -> list:
    null = "NUL" if os.name == "nt" else "/dev/null"
    return ["-i", str(ssh_key()), "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={null}",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
            "-o", "ServerAliveInterval=30"]


def ssh(ip: str, command: str, capture=True, check=True, timeout=None) -> subprocess.CompletedProcess:
    return run([ssh_tool("ssh"), *ssh_base(ip), f"bench@{ip}", command],
               capture=capture, check=check, timeout=timeout)


# ------------------------------------------------------------------ image -- #

def base_disk() -> Path:
    """Official Ubuntu cloud VMDK (stream-optimized), converted once to a normal growable disk."""
    VM_DIR.mkdir(parents=True, exist_ok=True)
    raw = VM_DIR / Path(IMAGE_URL).name
    if not raw.exists():
        log(f"downloading {IMAGE_URL}")
        part = raw.with_suffix(".part")
        with urllib.request.urlopen(IMAGE_URL) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length", 0))
            done, last = 0, 0.0
            while chunk := resp.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and time.time() - last > 5:
                    last = time.time()
                    print(f"   {done * 100 // total}% of {total >> 20} MB", flush=True)
        part.rename(raw)
    base = VM_DIR / f"base-{UBUNTU}.vmdk"
    if not base.exists():
        log("converting cloud image to a VMware disk")
        run([find_vmware_tool("vmware-vdiskmanager"), "-r", str(raw), "-t", "0", str(base)])
    return base


# --------------------------------------------------------------------- vm -- #

def parse_mem(mem: str) -> int:
    m = mem.strip().upper()
    if m.endswith("G"):
        return int(float(m[:-1]) * 1024)
    if m.endswith("M"):
        return int(m[:-1])
    return int(m)


def machine(args) -> tuple:
    cores, mem, desc = PRESETS.get(args.preset or "", (2, 2048, "custom"))
    if args.preset and args.preset not in PRESETS:
        die(f"unknown preset {args.preset!r}; choose from {', '.join(PRESETS)}")
    cores = args.cores or cores
    mem = parse_mem(args.mem) if args.mem else mem
    name = args.name or args.preset or f"x86-{cores}c-{mem // 1024 if mem >= 1024 else mem}{'g' if mem >= 1024 else 'm'}"
    if args.preset and (args.cores or args.mem) and not args.name:
        name = f"{args.preset}-{cores}c-{mem}m"
    return name.lower(), cores, mem, desc


def cloud_init(name: str) -> tuple:
    pub = ssh_key().with_suffix(".pub").read_text().strip()
    user_data = f"""#cloud-config
hostname: {name}
users:
  - name: bench
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys: [{pub}]
ssh_pwauth: false
package_update: true
packages: [git, python3, python3-venv, python3-pip, sqlite3, postgresql, libpq5]
"""
    meta = json.dumps({"instance-id": name, "local-hostname": name})
    b64 = lambda s: base64.b64encode(s.encode()).decode()
    return b64(user_data), b64(meta)


def vmx_path(name: str) -> Path:
    return VM_DIR / name / f"{name}.vmx"


def create(name: str, cores: int, mem: int, disk_gb: int) -> Path:
    """Create the disk once; (re)write the .vmx every time so size changes apply on next boot."""
    vmx = vmx_path(name)
    d = vmx.parent
    disk = d / f"{name}.vmdk"
    if not disk.exists():
        base = base_disk()
        d.mkdir(parents=True, exist_ok=True)
        log(f"creating VM '{name}': {cores} vCPU, {mem} MB RAM, {disk_gb} GB disk")
        vdm = find_vmware_tool("vmware-vdiskmanager")
        run([vdm, "-r", str(base), "-t", "0", str(disk)])  # private copy (growable, sparse)
        run([vdm, "-x", f"{disk_gb}GB", str(disk)])           # cloud-init grows the partition
    if running(vmx):
        return vmx
    ud, md = cloud_init(name)  # same instance-id: cloud-init only runs first-boot setup once
    vmx.write_text(f'''.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "19"
displayName = "dbbench {name}"
guestOS = "ubuntu-64"
numvcpus = "{cores}"
cpuid.coresPerSocket = "{cores}"
memsize = "{mem}"
pciBridge0.present = "TRUE"
pciBridge4.present = "TRUE"
pciBridge4.virtualDev = "pcieRootPort"
pciBridge4.functions = "8"
pciBridge5.present = "TRUE"
pciBridge5.virtualDev = "pcieRootPort"
pciBridge5.functions = "8"
pciBridge6.present = "TRUE"
pciBridge6.virtualDev = "pcieRootPort"
pciBridge6.functions = "8"
pciBridge7.present = "TRUE"
pciBridge7.virtualDev = "pcieRootPort"
pciBridge7.functions = "8"
scsi0.present = "TRUE"
scsi0.virtualDev = "pvscsi"
scsi0:0.present = "TRUE"
scsi0:0.fileName = "{disk.name}"
ethernet0.present = "TRUE"
ethernet0.connectionType = "nat"
ethernet0.virtualDev = "vmxnet3"
ethernet0.addressType = "generated"
serial0.present = "TRUE"
serial0.fileType = "file"
serial0.fileName = "console.log"
tools.syncTime = "TRUE"
guestinfo.userdata = "{ud}"
guestinfo.userdata.encoding = "base64"
guestinfo.metadata = "{md}"
guestinfo.metadata.encoding = "base64"
''', encoding="utf-8")
    return vmx


def running(vmx: Path) -> bool:
    out = vmrun("list", check=False).stdout.lower()
    return str(vmx).lower() in out.replace("/", "\\") or str(vmx).lower() in out


def up(name: str, cores: int, mem: int, disk_gb: int, boot_timeout: int) -> str:
    vmx = create(name, cores, mem, disk_gb)
    if not running(vmx):
        log(f"booting '{name}' (headless)")
        vmrun("start", vmx, "nogui")
    log("waiting for the VM's IP address (VMware Tools)")
    r = vmrun("getGuestIPAddress", vmx, "-wait", check=False, timeout=boot_timeout)
    ip = r.stdout.strip()
    if r.returncode or not ip or "error" in ip.lower():
        die(f"could not get the VM's IP: {r.stdout}{r.stderr} (see {vmx.parent / 'console.log'})")
    log(f"VM IP {ip}; waiting for SSH and first-boot setup (cloud-init)")
    deadline = time.time() + boot_timeout
    while True:
        # The guest can renew DHCP with a new client id during first boot and get a new
        # address, so ask VMware Tools again on every attempt.
        fresh = vmrun("getGuestIPAddress", vmx, check=False, timeout=60).stdout.strip()
        if fresh and "error" not in fresh.lower() and fresh != ip:
            log(f"VM IP changed to {fresh}")
            ip = fresh
        if ssh(ip, "cloud-init status --wait >/dev/null 2>&1; true", check=False,
               timeout=boot_timeout).returncode == 0:
            break
        if time.time() > deadline:
            die(f"SSH never came up (see {vmx.parent / 'console.log'})")
        time.sleep(5)
    # cloud-init may have renewed the lease again while we waited; take the final address.
    final = vmrun("getGuestIPAddress", vmx, check=False, timeout=60).stdout.strip()
    if final and "error" not in final.lower() and final != ip and \
            ssh(final, "true", check=False, timeout=30).returncode == 0:
        ip = final
    return ip


def push_repo(ip: str) -> None:
    """Copy the working tree (not just commits) into ~/dbbench, with LF line endings."""
    buf = io.BytesIO()
    skip = {".git", ".venv", "venv", "vm", "results", "__pycache__", ".pytest_cache"}
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in REPO.rglob("*"):
            rel = path.relative_to(REPO)
            if any(p in skip or p.endswith(".egg-info") for p in rel.parts) or path.is_dir():
                continue
            data = path.read_bytes()
            if path.suffix in {".sh", ".py", ".toml", ".yml", ".md", ".cfg"}:
                data = data.replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(rel.as_posix())
            info.size, info.mode = len(data), 0o755 if path.suffix == ".sh" else 0o644
            tar.addfile(info, io.BytesIO(data))
    r = subprocess.run([ssh_tool("ssh"), *ssh_base(ip), f"bench@{ip}",
                        "rm -rf ~/dbbench/dbbench ~/dbbench/scripts && mkdir -p ~/dbbench "
                        "&& tar -xzf - -C ~/dbbench"], input=buf.getvalue(), capture_output=True)
    if r.returncode:
        die(f"copying repo failed: {r.stderr.decode(errors='replace')}")


def bench(args) -> Path:
    name, cores, mem, desc = machine(args)
    t0 = time.time()
    ip = up(name, cores, mem, args.disk, args.boot_timeout)
    log(f"copying repo into '{name}' and installing (bootstrap.sh)")
    push_repo(ip)
    r = ssh(ip, "cd ~/dbbench && ./scripts/bootstrap.sh > bootstrap.log 2>&1 || "
                "{ tail -30 bootstrap.log; exit 1; }", check=False)
    if r.returncode:
        die(f"bootstrap failed in the VM:\n{r.stdout}{r.stderr}")
    extra = " ".join(args.dbbench_args) if args.dbbench_args else "-p quick"
    label = args.label or f"vmware-{name}"
    log(f"benchmarking: dbbench run -b all {extra} -l {label}")
    r = ssh(ip, f"cd ~/dbbench && rm -f results/*.json && .venv/bin/dbbench run -b all {extra} -l {label}",
            capture=False, check=False)
    newest = ssh(ip, "ls -t ~/dbbench/results/*.json 2>/dev/null | head -1", check=False).stdout.strip()
    if not newest:
        die("benchmark produced no result file")
    out_dir = REPO / "results"
    out_dir.mkdir(exist_ok=True)
    local = out_dir / Path(newest).name
    run([ssh_tool("scp"), *ssh_base(ip), f"bench@{ip}:{newest}", str(local)])
    log(f"results: {local}  ({(time.time() - t0) / 60:.1f} min)")
    if not args.keep:
        stop(name)
    if r.returncode:
        log(f"warning: dbbench exited with {r.returncode} (a backend failed; see the result file)")
    return local


def stop(name: str) -> None:
    vmx = vmx_path(name)
    if vmx.exists() and running(vmx):
        log(f"shutting down '{name}'")
        if vmrun("stop", vmx, "soft", check=False, timeout=180).returncode:
            vmrun("stop", vmx, "hard", check=False)


def destroy(name: str) -> None:
    stop(name)
    d = vmx_path(name).parent
    if d.exists() and d.parent == VM_DIR:
        vmrun("deleteVM", vmx_path(name), check=False)
        shutil.rmtree(d, ignore_errors=True)
        log(f"deleted VM '{name}'")


# -------------------------------------------------------------------- cli -- #

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["bench", "bench-all", "up", "stop", "destroy", "list"])
    p.add_argument("--preset", help=", ".join(f"{k} ({v[0]}c/{v[1] // 1024 or v[1]}{'G' if v[1] >= 1024 else 'M'})"
                                              for k, v in PRESETS.items()))
    p.add_argument("--cores", type=int)
    p.add_argument("--mem", help="RAM, e.g. 512M or 4G")
    p.add_argument("--disk", type=int, default=20, help="disk size in GB (default 20)")
    p.add_argument("--name", help="VM name (default: from preset / size)")
    p.add_argument("--label", help="dbbench label (default vmware-<name>)")
    p.add_argument("--keep", action="store_true", help="leave the VM running after 'bench'")
    p.add_argument("--boot-timeout", type=int, default=1800, help="seconds to wait for first boot")
    p.add_argument("dbbench_args", nargs="*", help="after --: extra 'dbbench run' arguments")
    args = p.parse_args()

    if args.action == "list":
        for k, (c, m, d) in PRESETS.items():
            state = "created" if vmx_path(k).exists() else ""
            print(f"  {k:<8} {c:>2} vCPU {m:>6} MB  {d:<32} {state}")
        print(vmrun("list", check=False).stdout)
        return
    if args.action == "bench-all":
        files, failed = [], []
        for preset in (args.preset.split(",") if args.preset else BENCH_ALL):
            args.preset = preset
            try:
                files.append(bench(args))
            except SystemExit:  # die() already printed why; keep going with the next machine
                failed.append(preset)
                stop(machine(args)[0])
        if files:
            env = {**os.environ, "PYTHONPATH": str(REPO)}  # dbbench need not be installed on the host
            subprocess.run([sys.executable, "-m", "dbbench", "report", *map(str, files)], cwd=REPO, env=env)
        if failed:
            die(f"failed: {', '.join(failed)}")
        return
    name, cores, mem, _ = machine(args)
    if args.action == "bench":
        bench(args)
    elif args.action == "up":
        ip = up(name, cores, mem, args.disk, args.boot_timeout)
        print(f"\nssh {' '.join(ssh_base(ip))} bench@{ip}")
    elif args.action == "stop":
        stop(name)
    elif args.action == "destroy":
        destroy(name)


if __name__ == "__main__":
    main()
