from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Any, Dict, Optional

from .base import Backend

# Like-for-like defaults with PostgreSQL's stock configuration:
#   synchronous=FULL  -> every commit is fsynced, as with synchronous_commit=on
#   128 MB page cache -> PostgreSQL's default shared_buffers
DEFAULT_SYNC = "FULL"
DEFAULT_CACHE_MB = 128


class SqliteBackend(Backend):
    name = "sqlite"
    placeholder = "?"

    def __init__(self, path: Optional[str] = None, synchronous: str = DEFAULT_SYNC,
                 cache_mb: int = DEFAULT_CACHE_MB):
        self._tmpdir = None
        if path is None:
            # Default to a file on disk (not :memory:) so storage speed is measured.
            self._tmpdir = tempfile.mkdtemp(prefix="dbbench-")
            path = os.path.join(self._tmpdir, "bench.db")
        self.path = path
        self.synchronous = synchronous
        self.cache_mb = cache_mb

    def connect(self) -> Any:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA synchronous={self.synchronous}")
        conn.execute(f"PRAGMA cache_size=-{int(self.cache_mb) * 1024}")  # negative = KiB
        return conn

    def info(self) -> Dict[str, Any]:
        return {
            "version": sqlite3.sqlite_version,
            "path": self.path,
            "journal_mode": "WAL",
            "synchronous": self.synchronous,
            "cache_size_mb": self.cache_mb,
            "busy_timeout_s": 30,
        }

    def cleanup(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass
        if self._tmpdir:
            try:
                os.rmdir(self._tmpdir)
            except OSError:
                pass
