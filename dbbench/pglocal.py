"""Run a private, throwaway PostgreSQL server for benchmarking.

Uses the PostgreSQL binaries already on the machine (initdb / pg_ctl), keeps its data in
its own directory and listens on its own port on localhost only, with trust auth. It never
touches an existing PostgreSQL service, and needs no passwords or root.

    dbbench pg start      # prints the DSN, e.g. postgresql://bench@127.0.0.1:55432/bench
    dbbench pg stop
    dbbench run -b all --start-pg   # start, benchmark, stop
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

DEFAULT_PORT = 55432
DEFAULT_DIR = Path(os.environ.get("DBBENCH_PG_DIR", Path(tempfile.gettempdir()) / "dbbench-pg"))
EXE = ".exe" if os.name == "nt" else ""


def find_bindir() -> Path:
    """Directory holding initdb/pg_ctl: $PG_BIN, PATH, or the usual install locations (newest first)."""
    if os.environ.get("PG_BIN"):
        return Path(os.environ["PG_BIN"])
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    patterns = [
        "/usr/lib/postgresql/*/bin",                 # Debian / Ubuntu / Raspberry Pi OS
        "/usr/pgsql-*/bin",                          # RHEL / Fedora (PGDG)
        "/opt/homebrew/opt/postgresql@*/bin", "/usr/local/opt/postgresql@*/bin",
        r"C:\Program Files\PostgreSQL\*\bin",        # Windows installer
    ]
    candidates = [Path(p) for pat in patterns for p in glob.glob(pat)
                  if (Path(p) / f"pg_ctl{EXE}").exists()]

    def major(p: Path) -> float:
        for part in reversed(p.parts):
            digits = "".join(c for c in part if c.isdigit() or c == ".")
            try:
                return float(digits.strip("."))
            except ValueError:
                continue
        return 0.0

    if not candidates:
        raise RuntimeError("PostgreSQL binaries (initdb, pg_ctl) not found. Install PostgreSQL "
                           "(apt install postgresql / the Windows installer) or set PG_BIN.")
    return max(candidates, key=major)


def _bin(name: str) -> str:
    return str(find_bindir() / f"{name}{EXE}")


def dsn(port: int = DEFAULT_PORT, datadir: Path = DEFAULT_DIR) -> str:
    """Unix socket where available (no TCP hop, like-for-like with in-process SQLite)."""
    if os.name != "nt" and (Path(datadir) / f".s.PGSQL.{port}").exists():
        return f"postgresql://bench@/bench?host={datadir}&port={port}"
    return f"postgresql://bench@127.0.0.1:{port}/bench"


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def status(datadir: Path = DEFAULT_DIR) -> bool:
    if not (datadir / "PG_VERSION").exists():
        return False
    return _run([_bin("pg_ctl"), "status", "-D", str(datadir)]).returncode == 0


def start(datadir: Path = DEFAULT_DIR, port: int = DEFAULT_PORT, log=print) -> str:
    """Create (once) and start the private server; returns its DSN. Idempotent."""
    datadir = Path(datadir)
    if status(datadir):
        log(f"PostgreSQL already running in {datadir}")
        return dsn(port, datadir)
    if not _port_free(port):
        raise RuntimeError(f"port {port} is in use; pick another with --port")
    if not (datadir / "PG_VERSION").exists():
        datadir.mkdir(parents=True, exist_ok=True)
        log(f"initdb {datadir} (PostgreSQL from {find_bindir()})")
        r = _run([_bin("initdb"), "-D", str(datadir), "-U", "bench", "-A", "trust",
                  "-E", "UTF8", "--no-instructions"])
        if r.returncode:
            raise RuntimeError(f"initdb failed:\n{r.stdout}\n{r.stderr}")
    # localhost only; settings sized for a benchmark box rather than defaults from 2005.
    opts = (f"-p {port} -c listen_addresses=127.0.0.1 -c max_connections=200 "
            f"-c shared_buffers=128MB")  # stock default; SQLite gets a matching 128 MB cache
    if os.name != "nt":
        opts += f" -c unix_socket_directories={datadir}"
    # Not a pipe: the server inherits pg_ctl's stdout/stderr, and on Windows a captured pipe
    # would then never reach EOF, hanging us until the server exits.
    with open(datadir / "pg_ctl.out", "w") as out:
        r = subprocess.run([_bin("pg_ctl"), "start", "-w", "-t", "120", "-D", str(datadir),
                            "-l", str(datadir / "server.log"), "-o", opts],
                           stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if r.returncode:
        logtail = (datadir / "server.log").read_text(errors="replace")[-2000:] \
            if (datadir / "server.log").exists() else ""
        pgout = (datadir / "pg_ctl.out").read_text(errors="replace")
        raise RuntimeError(f"pg_ctl start failed:\n{pgout}\n{logtail}")
    r = _run([_bin("psql"), "-h", "127.0.0.1", "-p", str(port), "-U", "bench", "-d", "postgres",
              "-tAc", "SELECT 1 FROM pg_database WHERE datname='bench'"])
    if r.stdout.strip() != "1":
        r = _run([_bin("createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "bench", "bench"])
        if r.returncode:
            raise RuntimeError(f"createdb failed: {r.stderr}")
    log(f"PostgreSQL running: {dsn(port, datadir)}")
    return dsn(port, datadir)


def stop(datadir: Path = DEFAULT_DIR, log=print) -> None:
    if status(datadir):
        _run([_bin("pg_ctl"), "stop", "-w", "-m", "fast", "-D", str(datadir)])
        log("PostgreSQL stopped")
    else:
        log("PostgreSQL not running")


def destroy(datadir: Path = DEFAULT_DIR, log=print) -> None:
    stop(datadir, log=log)
    if (Path(datadir) / "PG_VERSION").exists():  # only ever delete a real data directory
        shutil.rmtree(datadir)
        log(f"removed {datadir}")


if __name__ == "__main__":  # pragma: no cover
    print(start() if sys.argv[1:] == ["start"] else stop())
