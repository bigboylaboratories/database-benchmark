from .base import Backend
from .sqlite import SqliteBackend


def get_backend(name: str, **opts) -> Backend:
    if name == "sqlite":
        from .sqlite import DEFAULT_CACHE_MB, DEFAULT_SYNC

        return SqliteBackend(path=opts.get("sqlite_path"),
                             synchronous=opts.get("sqlite_sync") or DEFAULT_SYNC,
                             cache_mb=opts.get("sqlite_cache_mb") or DEFAULT_CACHE_MB)
    if name == "postgres":
        from .postgres import PostgresBackend  # optional dependency

        return PostgresBackend(dsn=opts.get("dsn"))
    raise ValueError(f"unknown backend: {name}")


__all__ = ["Backend", "get_backend"]
