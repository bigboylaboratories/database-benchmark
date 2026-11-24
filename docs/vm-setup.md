# Machine & VM setup

Every target runs the same steps: get a shell, run `scripts/bootstrap.sh`, then run
`dbbench run -b all -p quick -l <label>`. Give each machine a descriptive `--label`
(e.g. `qemu-rpi4-4c-2g`, `rpi4-real-sd`, `aws-t4g.small`), because the report groups by it.

## Raspberry Pi (emulated) with QEMU

`scripts/qemu-pi.sh` boots an Ubuntu arm64 cloud image on QEMU's `virt` machine:

| Pi model to approximate | command |
|---|---|
| Pi 4 / 400 (A72, 4c)  | `CORES=4 MEM=2G ./scripts/qemu-pi.sh` (also try `MEM=4G` / `8G`) |
| Pi 5 (A76, 4c)        | `CORES=4 MEM=4G CPU=cortex-a76 ./scripts/qemu-pi.sh` (needs QEMU ≥ 9.1) |
| Pi 3 / Zero 2 W (A53) | `CORES=4 MEM=1G CPU=cortex-a53 ./scripts/qemu-pi.sh` |
| single-core baseline  | `CORES=1 MEM=512M CPU=cortex-a53 NAME=rpi-1c ./scripts/qemu-pi.sh` |

Each config gets its own overlay disk in `vm/` (git-ignored). The base image is downloaded once.
The first boot takes a few minutes while cloud-init installs packages.

**Why not `-M raspi4b`?** QEMU's real board models emulate the SoC faithfully but have no
PCIe, a fragile USB network, a RAM cap and a slow SD card model. You can't run PostgreSQL
usefully on them. The `virt` + Cortex-A72 setup matches the Pi's core type, count and RAM,
which are the parts that matter for these benchmarks.

**Acceleration:** on an ARM64 Linux host with `/dev/kvm`, or on Apple Silicon (hvf), the
script uses hardware virtualization and the numbers come close to real ARM silicon. On x86
hosts it falls back to multi-threaded TCG emulation, roughly 5–20× slower. That is still
useful for comparing *relative* behaviour, such as core scaling and SQLite vs Postgres.

## Other VMs

- **Cloud VMs (AWS/GCP/Azure, incl. ARM instances):** SSH in, clone, `./scripts/bootstrap.sh`.
- **VirtualBox / VMware / Hyper-V:** install Ubuntu Server, then the same steps. Try
  different vCPU counts and check that the core check follows them.
- **Docker containers:** `docker run --cpus=2 --memory=1g -it -v $PWD:/w -w /w python:3.12 bash`,
  then `pip install -e '.[postgres]'`. `cgroup_limit_cores` shows the quota.
- **Real Raspberry Pi:** use Raspberry Pi OS Lite 64-bit and `./scripts/bootstrap.sh`. Try both
  the SD card and a USB SSD (`--sqlite-path /mnt/ssd/bench.db`, and move the PG data dir).
  The `single_commits` workload shows the difference most clearly.

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
- `degraded`: 40–75%. Shared or hyperthreaded host cores, noisy neighbours or thermal throttling.
- `oversubscribed`: under 40%. The vCPUs share far fewer host threads, or emulation is serialized.

Compare `effective_cores` with how much `concurrent_reads` improves over `point_select`.
That shows whether each database turns the extra cores into throughput.
