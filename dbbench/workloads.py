"""Benchmark workloads. Each runs identical SQL against any Backend.

Every workload returns a list of per-operation latencies (seconds) plus the total
wall time, so the runner can compute throughput and percentiles uniformly.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import random
import string
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

from .backends.base import Backend

Result = Tuple[int, float, List[float]]  # (operations, wall seconds, latencies)


@dataclass
class Params:
    rows: int = 10_000
    ops: int = 2_000          # operations for point/update style workloads
    batch: int = 1_000        # rows per transaction for bulk insert
    commits: int = 200        # single-row commits (fsync bound)
    clients: int = 4          # threads for concurrent workloads
    duration: float = 5.0     # seconds for concurrent workloads
    seed: int = 42


def _payload(rng: random.Random, n: int = 64) -> str:
    return "".join(rng.choices(string.ascii_letters, k=n))


def _rows(rng: random.Random, start: int, count: int, key_space: int):
    for i in range(start, start + count):
        yield (i, rng.randrange(key_space), _payload(rng), rng.random() * 1000)


# --------------------------------------------------------------------------- #

def load(backend: Backend, p: Params) -> Result:
    """Bulk insert p.rows rows in batches of p.batch per transaction."""
    rng = random.Random(p.seed)
    backend.reset_schema()
    conn = backend.connect()
    sql = backend.sql("INSERT INTO bench_items (id, k, v, n) VALUES (?, ?, ?, ?)")
    lat: List[float] = []
    start = time.perf_counter()
    try:
        cur = conn.cursor()
        for off in range(0, p.rows, p.batch):
            t = time.perf_counter()
            cur.executemany(sql, list(_rows(rng, off, min(p.batch, p.rows - off), p.rows // 10 or 1)))
            conn.commit()
            lat.append(time.perf_counter() - t)
    finally:
        conn.close()
    wall = time.perf_counter() - start
    backend.analyze()
    return p.rows, wall, lat


def single_commits(backend: Backend, p: Params) -> Result:
    """One INSERT per transaction: dominated by fsync / storage latency (SD card vs SSD)."""
    rng = random.Random(p.seed + 1)
    conn = backend.connect()
    sql = backend.sql("INSERT INTO bench_items (id, k, v, n) VALUES (?, ?, ?, ?)")
    lat: List[float] = []
    base = p.rows + 10_000_000
    start = time.perf_counter()
    try:
        cur = conn.cursor()
        for row in _rows(rng, base, p.commits, p.rows // 10 or 1):
            t = time.perf_counter()
            cur.execute(sql, row)
            conn.commit()
            lat.append(time.perf_counter() - t)
        cur.execute(backend.sql("DELETE FROM bench_items WHERE id >= ?"), (base,))
        conn.commit()
    finally:
        conn.close()
    return p.commits, time.perf_counter() - start, lat


def _loop(backend: Backend, p: Params, sql: str, make_args: Callable[[random.Random], tuple],
          write: bool = False, seed_offset: int = 0) -> Result:
    rng = random.Random(p.seed + seed_offset)
    conn = backend.connect()
    sql = backend.sql(sql)
    lat: List[float] = []
    start = time.perf_counter()
    try:
        cur = conn.cursor()
        for _ in range(p.ops):
            args = make_args(rng)
            t = time.perf_counter()
            cur.execute(sql, args)
            if write:
                conn.commit()
            else:
                cur.fetchall()
            lat.append(time.perf_counter() - t)
        if not write:
            conn.commit()
    finally:
        conn.close()
    return p.ops, time.perf_counter() - start, lat


def point_select(backend: Backend, p: Params) -> Result:
    return _loop(backend, p, "SELECT id, k, v, n FROM bench_items WHERE id = ?",
                 lambda r: (r.randrange(p.rows),), seed_offset=2)


def range_select(backend: Backend, p: Params) -> Result:
    keys = p.rows // 10 or 1
    return _loop(backend, p, "SELECT count(*), avg(n) FROM bench_items WHERE k BETWEEN ? AND ?",
                 lambda r: (lambda a: (a, a + max(keys // 100, 1)))(r.randrange(keys)),
                 seed_offset=3)


def update(backend: Backend, p: Params) -> Result:
    return _loop(backend, p, "UPDATE bench_items SET n = n + 1, v = ? WHERE id = ?",
                 lambda r: (_payload(r, 16), r.randrange(p.rows)), write=True, seed_offset=4)


def aggregate(backend: Backend, p: Params) -> Result:
    """Full-table scan + GROUP BY: CPU and memory bandwidth bound."""
    q = Params(**{**p.__dict__, "ops": max(p.ops // 200, 3)})
    return _loop(backend, q, "SELECT k - (k / 100) * 100 AS bucket, count(*), sum(n) "
                 "FROM bench_items GROUP BY bucket",
                 lambda r: (), seed_offset=5)


_LAT_SAMPLE = 50_000  # latencies kept per client (reservoir sample) to bound memory/IPC


def _is_busy(e: BaseException) -> bool:
    """SQLite lock timeout ("database is locked") under heavy write contention."""
    return "locked" in str(e).lower() or "busy" in str(e).lower()


def _client_process(backend: Backend, p: Params, idx: int, write_pct: int,
                    barrier, stop, out) -> None:
    """One multi-user client, in its own process so it is not limited by the GIL.

    Each operation is a point read, or (write_pct % of the time) a single-row UPDATE
    committed on its own, i.e. many users each saving small changes.
    """
    rng = random.Random(p.seed + 100 + idx + 1000 * write_pct)
    count, writes, busy, sample = 0, 0, 0, []
    conn = None
    try:
        conn = backend.connect()
        cur = conn.cursor()
        read_sql = backend.sql("SELECT id, k, v, n FROM bench_items WHERE id = ?")
        write_sql = backend.sql("UPDATE bench_items SET n = n + 1, v = ? WHERE id = ?")
        cur.execute(read_sql, (0,))  # warm up the connection / statement before timing
        cur.fetchall()
        conn.commit()
        barrier.wait()
        while not stop.value:  # lock-free shared flag: no cross-process contention per query
            is_write = write_pct and rng.randrange(100) < write_pct
            t = time.perf_counter()
            if is_write:
                try:
                    cur.execute(write_sql, (_payload(rng, 16), rng.randrange(p.rows)))
                    conn.commit()
                except Exception as e:
                    if not _is_busy(e):
                        raise
                    conn.rollback()
                    busy += 1
                    continue
                writes += 1
            else:
                cur.execute(read_sql, (rng.randrange(p.rows),))
                cur.fetchall()
                if write_pct:
                    conn.commit()  # don't hold a read snapshot open across other users' writes
            dt = time.perf_counter() - t
            count += 1
            if len(sample) < _LAT_SAMPLE:
                sample.append(dt)
            else:
                j = rng.randrange(count)
                if j < _LAT_SAMPLE:
                    sample[j] = dt
        conn.commit()
        out.put((idx, count, writes, busy, sample, None))
    except BaseException as e:  # report to the parent instead of dying silently
        try:
            barrier.abort()
        except Exception:
            pass
        out.put((idx, 0, 0, 0, [], f"{type(e).__name__}: {e}"))
    finally:
        if conn is not None:
            conn.close()


def _multi_user(backend: Backend, p: Params, write_pct: int, name: str) -> Result:
    """p.clients processes, each running operations for p.duration seconds."""
    ctx = mp.get_context("fork" if hasattr(os, "fork") and sys.platform != "darwin" else "spawn")
    barrier = ctx.Barrier(p.clients + 1)
    stop = ctx.RawValue("b", 0)
    out = ctx.Queue()
    procs = [ctx.Process(target=_client_process, args=(backend, p, i, write_pct, barrier, stop, out),
                         daemon=True)
             for i in range(p.clients)]
    for pr in procs:
        pr.start()
    try:
        barrier.wait(timeout=120)  # all clients connected and warmed up
    except threading.BrokenBarrierError:
        pass  # a client failed; its error is collected below
    start = time.perf_counter()
    if not barrier.broken:
        time.sleep(p.duration)
    stop.value = 1
    wall = time.perf_counter() - start

    results, errors = [], []
    for _ in procs:
        try:
            idx, count, writes, busy, sample, err = out.get(timeout=120)
        except queue.Empty:
            errors.append("client process did not report (crashed?)")
            break
        (errors.append(f"client {idx}: {err}") if err else results.append((count, writes, busy, sample)))
    for pr in procs:
        pr.join(timeout=10)
        if pr.is_alive():
            pr.terminate()
    if errors:
        raise RuntimeError(f"{name} failed: " + "; ".join(errors))
    ops = sum(r[0] for r in results)
    lat = [x for r in results for x in r[3]]
    extra = {"clients": p.clients, "writes": sum(r[1] for r in results),
             "lock_timeouts": sum(r[2] for r in results)}
    return ops, wall, lat, extra  # type: ignore[return-value]


def concurrent_reads(backend: Backend, p: Params) -> Result:
    """p.clients users doing point reads for p.duration seconds (exercises multiple cores).

    Clients are separate processes (not threads), so client-side Python never caps
    throughput at one core: scaling reflects the machine and the database.
    """
    return _multi_user(backend, p, 0, "concurrent_reads")


def concurrent_mixed(backend: Backend, p: Params) -> Result:
    """p.clients users, 80% point reads / 20% single-row updates (each its own commit)."""
    return _multi_user(backend, p, 20, "concurrent_mixed")


def concurrent_writes(backend: Backend, p: Params) -> Result:
    """p.clients users all updating random rows, one commit each: write concurrency/locking."""
    return _multi_user(backend, p, 100, "concurrent_writes")


MULTI_USER = ("concurrent_reads", "concurrent_mixed", "concurrent_writes")


# Order matters: load must run first, the rest reuse its data.
WORKLOADS: Dict[str, Callable[[Backend, Params], Result]] = {
    "load": load,
    "point_select": point_select,
    "range_select": range_select,
    "aggregate": aggregate,
    "update": update,
    "single_commits": single_commits,
    "concurrent_reads": concurrent_reads,
    "concurrent_mixed": concurrent_mixed,
    "concurrent_writes": concurrent_writes,
}

PROFILES: Dict[str, Params] = {
    "smoke": Params(rows=2_000, ops=200, commits=20, clients=2, duration=1.0),
    "quick": Params(rows=20_000, ops=2_000, commits=100, clients=4, duration=3.0),
    "standard": Params(rows=200_000, ops=10_000, commits=500, clients=8, duration=10.0),
    "large": Params(rows=2_000_000, ops=50_000, commits=1_000, clients=16, duration=30.0),
}
