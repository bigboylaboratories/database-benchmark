from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .base import Backend

try:
    import psycopg
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "PostgreSQL support needs psycopg: pip install 'dbbench[postgres]'"
    ) from exc

DEFAULT_DSN = "postgresql://bench:bench@localhost:5432/bench"
# Local Unix sockets (Debian/Ubuntu/Pi OS, RHEL). Like-for-like with SQLite, which has no
# network hop: a socket is the fastest local path to the server and skips TCP overhead.
SOCKET_DIRS = ("/var/run/postgresql", "/run/postgresql", "/tmp")


def _candidates() -> List[str]:
    out = []
    if os.name != "nt":
        for d in SOCKET_DIRS:
            if any(n.startswith(".s.PGSQL.5432") for n in _listdir(d)):
                # user/password as the TCP default; peer auth also works when the OS user is 'bench'
                out.append(f"postgresql://bench:bench@/bench?host={d}")
                break
    out.append(DEFAULT_DSN)
    return out


def _listdir(d: str) -> List[str]:
    try:
        return os.listdir(d)
    except OSError:
        return []


class PostgresBackend(Backend):
    name = "postgres"
    placeholder = "%s"

    def __init__(self, dsn: Optional[str] = None):
        dsn = dsn or os.environ.get("DBBENCH_PG_DSN")
        if not dsn:  # prefer the local socket, fall back to TCP localhost
            for cand in _candidates():
                try:
                    psycopg.connect(cand, connect_timeout=5).close()
                    dsn = cand
                    break
                except psycopg.Error:
                    continue
            dsn = dsn or DEFAULT_DSN
        self.dsn = dsn

    def connect(self) -> Any:
        return psycopg.connect(self.dsn)

    def info(self) -> Dict[str, Any]:
        settings = ("server_version", "shared_buffers", "synchronous_commit", "fsync",
                    "wal_sync_method", "max_connections", "work_mem", "jit")
        with psycopg.connect(self.dsn) as conn:
            out: Dict[str, Any] = {}
            for s in settings:
                try:
                    out[s] = conn.execute(f"SHOW {s}").fetchone()[0]
                except psycopg.Error:
                    conn.rollback()
            host = conn.info.host
            out["host"] = host
            out["transport"] = "unix socket" if host.startswith("/") else "tcp"
        return out

    def cleanup(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DROP TABLE IF EXISTS bench_items")
