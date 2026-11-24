# database-benchmark

Benchmark **PostgreSQL** and **SQLite** across physical and virtual machines (an emulated
Raspberry Pi, cloud VMs, containers, laptops), and **verify the cores and performance** each
machine actually delivers.

Each run saves one JSON file holding:

- **System facts**: arch, CPU model, logical, physical and usable cores, cgroup CPU limits,
  RAM, CPU frequency, and virtualization detection (KVM, QEMU, Docker, ...) plus the board model,
  e.g. `Raspberry Pi 4 Model B`.
- **A core check**: the same CPU task runs with 1, 2, 4 … N processes. If a VM says it has
  4 cores but `effective_cores` comes out near 1, it is oversubscribed or badly emulated.
- **Database results**: identical SQL workloads on each backend, with throughput and
  p50/p95/p99 latency, median over several repeats.

Then `dbbench report` compares every machine side by side.

## Quick start

```bash
pip install -e '.[postgres]'           # or just `pip install -e .` for SQLite only
docker compose up -d                   # local PostgreSQL 16 (user/pass/db: bench)
dbbench sysinfo --verify-cores         # what is this machine, really?
dbbench run -b all -p quick -l my-laptop
dbbench report                         # markdown comparison of results/*.json
```

On a fresh Debian, Ubuntu or Raspberry Pi OS machine or VM, `./scripts/bootstrap.sh` installs
Python, SQLite and PostgreSQL, creates the `bench` database and sets up a virtualenv.

## Workloads

| name               | what it does                                         | stresses            |
|--------------------|------------------------------------------------------|---------------------|
| `load`             | bulk insert in 1000-row transactions                 | write throughput    |
| `point_select`     | random lookups by primary key                        | latency, CPU        |
| `range_select`     | `count/avg` over an indexed key range                | index scans         |
| `aggregate`        | full table scan + `GROUP BY`                         | CPU, memory b/w     |
| `update`           | random single-row updates, one commit each           | write path          |
| `single_commits`   | one INSERT per transaction                           | **fsync / storage** |
| `concurrent_reads` | N client threads doing point selects for a fixed time | **multi-core**      |

Profiles set the data size: `smoke` (2k rows), `quick` (20k), `standard` (200k) and `large` (2M).
You can override any value with `--rows`, `--ops`, `--commits`, `--clients` or `--duration`.
Use `-w point_select,concurrent_reads` to run only some workloads.

## Benchmarking virtual machines (Linux servers and Raspberry Pis)

All the VMs run on one Linux host. `scripts/qemu-vm.sh` creates an Ubuntu VM with whatever
cores, RAM, CPU limit and disk speed you ask for, either x86_64 or ARM64:

```bash
PRESET=x86-4c ./scripts/qemu-vm.sh                    # 4-core / 4 GB x86_64 Linux server
PRESET=pi4    ./scripts/qemu-vm.sh                    # Raspberry Pi 4-like ARM64 board
ARCH=x86_64 CORES=6 MEM=12G ./scripts/qemu-vm.sh      # any custom size
PRESET=x86-4c CPU_LIMIT=200% ./scripts/qemu-vm.sh     # 4 vCPUs, only 2 cores of host time
PRESET=x86-2c DISK_MBPS=25 DISK_IOPS=500 ./scripts/qemu-vm.sh   # slow disk
```

Presets: `x86-1c` `x86-2c` `x86-4c` `x86-8c` `x86-16c`, `arm-2c` `arm-4c` `arm-8c`, and
`pi3` `pi4` `pi4-8g` `pi5`. `./scripts/qemu-pi.sh` is a shortcut for the Pi presets.

Then, in the VM:

```bash
ssh -p 2222 bench@localhost               # password: bench
git clone <this repo> && cd database-benchmark && ./scripts/bootstrap.sh
source .venv/bin/activate
dbbench run -b all -p quick               # label defaults to the VM name, e.g. x86-4c
```

Copy the result JSON back (`scp -P 2222 'bench@localhost:database-benchmark/results/*.json' results/`),
shut the VM down, start the next one and run `dbbench report`. See [docs/vm-setup.md](docs/vm-setup.md)
for every setting, how to change a machine and how to read the core check.

## Useful options

```bash
dbbench run -b postgres --dsn postgresql://user:pw@host:5432/db   # or set DBBENCH_PG_DSN
dbbench run -b sqlite --sqlite-path /mnt/sdcard/bench.db          # test a specific disk
dbbench run -b sqlite --sqlite-sync FULL                          # durability vs speed
PG_CPUS=2 PG_MEM=1g docker compose up -d                          # constrain the PG container
dbbench report -f csv > results/all.csv                           # for spreadsheets / plotting
```

## Layout

```
dbbench/            the CLI (sysinfo, run, report)
  sysinfo.py        hardware/VM detection + core scaling check
  workloads.py      workloads and profiles
  backends/         sqlite + postgres adapters (same SQL on both)
  runner.py         repeats, stats, JSON output
  report.py         cross-machine markdown/CSV comparison
scripts/            bootstrap.sh (target setup), qemu-vm.sh (any VM), qemu-pi.sh (Pi shortcut)
docs/vm-setup.md    VM recipes and interpretation notes
results/            result JSON files (git-ignored; `git add -f` the ones to keep)
```

## Fair-comparison notes

- SQLite runs in-process with WAL and `synchronous=NORMAL`. PostgreSQL goes over a socket, so
  per-query round-trip overhead is part of what you measure. This difference is real for apps.
- Run PostgreSQL **on the machine under test**. A remote server measures the network.
- Close other workloads, and use `-n 5` or more on noisy VMs. Check `stdev_ops_per_s` in the JSON.
- VMs with the same architecture as the host (x86_64 VMs on an x86_64 PC) use KVM and run
  close to native speed. Other architectures (ARM64/Pi VMs on an x86 PC) are emulated and much
  slower, so compare them with each other, not with real hardware.
- Run one VM at a time. They share the host's CPU and disk.

## Tests

```bash
pip install -e '.[dev]'
pytest                                   # SQLite only
DBBENCH_PG_DSN=postgresql://bench:bench@localhost:5432/bench pytest   # + PostgreSQL
```
