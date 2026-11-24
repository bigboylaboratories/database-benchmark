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

## Benchmarking a virtual Raspberry Pi

```bash
./scripts/qemu-pi.sh                      # Pi 4-like: Cortex-A72, 4 cores, 2 GB
ssh -p 2222 bench@localhost               # password: bench
# inside the VM:
git clone <this repo> && cd database-benchmark && ./scripts/bootstrap.sh
source .venv/bin/activate
dbbench run -b all -p quick -l qemu-rpi4-4c-2g
```

Copy the result JSON back (`scp -P 2222 bench@localhost:database-benchmark/results/*.json results/`)
and run `dbbench report`. See [docs/vm-setup.md](docs/vm-setup.md) for other Pi models,
VirtualBox and cloud VMs, and how to read the core check.

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
scripts/            bootstrap.sh (target setup), qemu-pi.sh (ARM64 Pi-like VM)
docs/vm-setup.md    VM recipes and interpretation notes
results/            result JSON files (git-ignored; `git add -f` the ones to keep)
```

## Fair-comparison notes

- SQLite runs in-process with WAL and `synchronous=NORMAL`. PostgreSQL goes over a socket, so
  per-query round-trip overhead is part of what you measure. This difference is real for apps.
- Run PostgreSQL **on the machine under test**. A remote server measures the network.
- Close other workloads, and use `-n 5` or more on noisy VMs. Check `stdev_ops_per_s` in the JSON.
- Under QEMU emulation (TCG) on an x86 host, absolute numbers are much lower than real Pi
  hardware. Use them to compare configurations, not to stand in for a real Pi.

## Tests

```bash
pip install -e '.[dev]'
pytest                                   # SQLite only
DBBENCH_PG_DSN=postgresql://bench:bench@localhost:5432/bench pytest   # + PostgreSQL
```
