from .base import Backend
from .sqlite import SqliteBackend


def get_backend(name: str, **opts) -> Backend:
    if name == "sqlite":
        return SqliteBackend(path=opts.get("sqlite_path"), synchronous=opts.get("sqlite_sync", "NORMAL"))
    if name == "postgres":
        from .postgres import PostgresBackend  # optional dependency

        return PostgresBackend(dsn=opts.get("dsn"))
    raise ValueError(f"unknown backend: {name}")


__all__ = ["Backend", "get_backend"]
