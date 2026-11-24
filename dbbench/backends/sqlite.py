from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Any, Dict, Optional

from .base import Backend


class SqliteBackend(Backend):
    name = "sqlite"
    placeholder = "?"

    def __init__(self, path: Optional[str] = None, synchronous: str = "NORMAL"):
        self._tmpdir = None
        if path is None:
            # Default to a file on disk (not :memory:) so storage speed is measured.
            self._tmpdir = tempfile.mkdtemp(prefix="dbbench-")
            path = os.path.join(self._tmpdir, "bench.db")
        self.path = path
        self.synchronous = synchronous

    def connect(self) -> Any:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA synchronous={self.synchronous}")
        return conn

    def info(self) -> Dict[str, Any]:
        return {
            "version": sqlite3.sqlite_version,
            "path": self.path,
            "journal_mode": "WAL",
            "synchronous": self.synchronous,
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
