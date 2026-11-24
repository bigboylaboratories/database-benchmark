from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

SCHEMA = [
    "DROP TABLE IF EXISTS bench_items",
    """CREATE TABLE bench_items (
        id INTEGER PRIMARY KEY,
        k INTEGER NOT NULL,
        v TEXT NOT NULL,
        n DOUBLE PRECISION NOT NULL
    )""",
    "CREATE INDEX bench_items_k ON bench_items (k)",
]


class Backend(ABC):
    """Thin adapter so every workload runs identical SQL on each database."""

    name: str
    placeholder: str  # DB-API paramstyle marker ("?" for sqlite, "%s" for psycopg)

    @abstractmethod
    def connect(self) -> Any:
        """Return a new DB-API connection (one per client thread)."""

    @abstractmethod
    def info(self) -> Dict[str, Any]:
        """Server/library version and relevant settings, stored with the results."""

    def sql(self, query: str) -> str:
        return query.replace("?", self.placeholder)

    def reset_schema(self) -> None:
        conn = self.connect()
        try:
            cur = conn.cursor()
            for stmt in SCHEMA:
                cur.execute(stmt)
            conn.commit()
        finally:
            conn.close()

    def analyze(self) -> None:
        conn = self.connect()
        try:
            conn.cursor().execute("ANALYZE")
            conn.commit()
        finally:
            conn.close()

    def cleanup(self) -> None:
        pass
