# Machine & VM setup

Every target runs the same steps: get a shell, run `scripts/bootstrap.sh`, then run
`dbbench run -b all -p quick -l <label>`. Inside a VM made by `qemu-vm.sh` the label defaults
to the VM name (its hostname). Elsewhere, give a descriptive `--label` (e.g. `rpi4-real-sd`,
`aws-c7g.large`), because the report groups by it.

## VMs with QEMU (`scripts/qemu-vm.sh`)

Each VM is an Ubuntu 24.04 cloud image on QEMU, running on your Linux host. The host only runs
QEMU. SQLite and PostgreSQL run **inside** the VM, so they get just the resources you gave it.

Install on the host (Debian/Ubuntu):

```bash
sudo apt-get install qemu-system-x86 qemu-system-arm qemu-efi-aarch64 qemu-utils cloud-image-utils curl
```

### Presets

| preset | arch | CPU | cores | RAM | disk limit | approximates |
|---|---|---|---|---|---|---|
| `x86-1c`  | x86_64  | host/max   | 1  | 1G  | none | tiny VPS |
| `x86-2c`  | x86_64  | host/max   | 2  | 2G  | none | small server |
| `x86-4c`  | x86_64  | host/max   | 4  | 4G  | none | mid server / desktop |
| `x86-8c`  | x86_64  | host/max   | 8  | 8G  | none | large server |
| `x86-16c` | x86_64  | host/max   | 16 | 16G | none | big server |
| `arm-2c` / `arm-4c` / `arm-8c` | aarch64 | host/cortex-a72 | 2/4/8 | 2G/4G/8G | none | ARM cloud server |
| `pi3`     | aarch64 | cortex-a53 | 4  | 1G  | 20 MB/s, 1500 IOPS | Pi 3 / Zero 2 W |
| `pi4`     | aarch64 | cortex-a72 | 4  | 2G  | 40 MB/s, 2500 IOPS | Pi 4 |
| `pi4-8g`  | aarch64 | cortex-a72 | 4  | 8G  | 40 MB/s, 2500 IOPS | Pi 4 8 GB |
| `pi5`     | aarch64 | cortex-a76 | 4  | 4G  | 80 MB/s, 4000 IOPS | Pi 5 (QEMU ≥ 9.1) |

The Pi disk limits roughly imitate an SD card. They are estimates, not measurements.

### Settings

Any setting overrides the preset, and you can skip the preset entirely:

| variable | meaning | example |
|---|---|---|
| `ARCH` | `x86_64` or `aarch64` | `ARCH=aarch64` |
| `CORES` | number of vCPUs | `CORES=6` |
| `MEM` | RAM | `MEM=512M`, `MEM=12G` |
| `CPU` | QEMU CPU model | `CPU=cortex-a53`, `CPU=Skylake-Server` |
| `CPU_LIMIT` | cap on host CPU time (systemd `CPUQuota`) | `CPU_LIMIT=50%` = half a core, `250%` = 2.5 cores |
| `PIN` | pin to specific host cores | `PIN=0-3` |
| `DISK_MBPS` / `DISK_IOPS` | disk bandwidth / IOPS cap | `DISK_MBPS=25 DISK_IOPS=500` |
| `DISK_SIZE` | virtual disk size | `DISK_SIZE=32G` |
| `SSH_PORT` | host port forwarded to the VM's SSH | `SSH_PORT=2223` |
| `NAME` | VM name: disk file in `vm/` and hostname | `NAME=myserver` |
| `NO_KVM=1` | force emulation even when KVM is available | |

**Ways to vary performance**, with what each one simulates:

- **More or fewer cores** (`CORES`): does the database use extra cores? Watch `concurrent_reads`.
- **Slower cores** (`CPU_LIMIT`): `CORES=4 CPU_LIMIT=200%` gives 4 vCPUs that together get only
  2 host cores of time. That mimics slower or oversubscribed cloud CPUs, and the core check
  should report it as `degraded`.
- **Dedicated cores** (`PIN`): pin each VM to its own host cores for steadier results.
- **Less RAM** (`MEM`): watch `aggregate` and `load` once data stops fitting in memory.
  Use the `standard` or `large` profile.
- **Slower disk** (`DISK_MBPS`, `DISK_IOPS`): SD card, HDD or cheap cloud disk. Watch
  `single_commits` and `load`.

`CPU_LIMIT` needs `systemd-run --user` (a normal desktop/server login session). If it fails,
run the script with `sudo`, or use `PIN` with fewer host cores instead.

### Creating, changing and removing machines

- **Create:** run the script with new settings. The VM name is built from the preset or the
  settings (e.g. `x86-4c`, `x86_64-6c-12g`, `x86-4c-cpu200`). Each name gets its own disk in
  `vm/` (git-ignored), and the base image is downloaded once per architecture. The first boot
  takes a few minutes while cloud-init installs packages.
- **Change:** QEMU can't resize a running VM. Shut it down (`sudo poweroff` inside it, or
  `Ctrl-A` then `X`) and start it with new settings. A new combination creates a fresh machine.
  To keep the same disk (with everything already installed), pin the name:
  `NAME=srv CORES=2 ./scripts/qemu-vm.sh`, then later `NAME=srv CORES=8 MEM=8G ./scripts/qemu-vm.sh`.
- **Several at once:** give each a different `SSH_PORT`. Only *benchmark* one at a time,
  because they share the host's CPU and disk.
- **Remove:** delete `vm/<name>.qcow2` and `vm/<name>-*` files.

### Speed: KVM vs emulation

- A VM with the **same architecture as the host** (an x86_64 VM on an x86_64 PC, or an ARM64 VM
  on an ARM64 host) uses **KVM** and runs close to native speed. Results are realistic.
  `/dev/kvm` must exist and be writable (add yourself to the `kvm` group).
- A VM with a **different architecture** (ARM64/Pi VMs on an x86 PC) is **emulated** (TCG),
  roughly 5–20× slower. Compare those VMs with each other, not with real hardware.

**Why `virt` and not `-M raspi4b` for Pis?** QEMU's real board models have no PCIe, a fragile
USB network, a RAM cap and a slow SD card model. You can't run PostgreSQL usefully on them. The
`virt` machine with the Pi's CPU core, core count and RAM covers what matters here.

## Machines outside QEMU

- **Cloud VMs (AWS/GCP/Azure, incl. ARM instances):** SSH in, clone, `./scripts/bootstrap.sh`.
- **VirtualBox / VMware / Hyper-V:** install Ubuntu Server, then the same steps. Change vCPU
  and RAM in the VM settings while it's powered off.
- **Docker containers:** `docker run --cpus=2 --memory=1g -it -v $PWD:/w -w /w python:3.12 bash`,
  then `pip install -e '.[postgres]'`. `cgroup_limit_cores` shows the quota.
- **Real hardware (Linux PCs, servers, Raspberry Pi OS 64-bit):** clone and `./scripts/bootstrap.sh`.
  On a Pi, try both the SD card and a USB SSD (`--sqlite-path /mnt/ssd/bench.db`).

## Reading the core check

`dbbench sysinfo --verify-cores` (also run automatically by `dbbench run`) prints:

```json
"core_check": {
  "claimed_cores": 4,
  "effective_cores": 3.8,
  "scaling": [{"workers": 1, "speedup": 1.0}, {"workers": 2, "speedup": 1.97}, {"workers": 4, "speedup": 3.8}],
  "verdict": "ok"
}
```

- `ok`: at least 75% of the claimed cores scale. The vCPUs are real.
- `degraded`: 40–75%. Shared or hyperthreaded host cores, `CPU_LIMIT`, noisy neighbours or throttling.
- `oversubscribed`: under 40%. The vCPUs share far fewer host threads, or emulation is serialized.

Compare `effective_cores` with how much `concurrent_reads` improves over `point_select`.
That shows whether each database turns the extra cores into throughput.
