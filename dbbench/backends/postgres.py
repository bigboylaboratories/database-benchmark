from __future__ import annotations

import os
from typing import Any, Dict, Optional

from .base import Backend

try:
    import psycopg
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "PostgreSQL support needs psycopg: pip install 'dbbench[postgres]'"
    ) from exc

DEFAULT_DSN = "postgresql://bench:bench@localhost:5432/bench"


class PostgresBackend(Backend):
    name = "postgres"
    placeholder = "%s"

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or os.environ.get("DBBENCH_PG_DSN", DEFAULT_DSN)

    def connect(self) -> Any:
        return psycopg.connect(self.dsn)

    def info(self) -> Dict[str, Any]:
        settings = ("server_version", "shared_buffers", "synchronous_commit",
                    "fsync", "max_connections", "work_mem")
        with psycopg.connect(self.dsn) as conn:
            out: Dict[str, Any] = {}
            for s in settings:
                out[s] = conn.execute(f"SHOW {s}").fetchone()[0]
            out["host"] = conn.info.host
        return out

    def cleanup(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DROP TABLE IF EXISTS bench_items")
