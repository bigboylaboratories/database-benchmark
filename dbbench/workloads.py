"""Benchmark workloads. Each runs identical SQL against any Backend.

Every workload returns a list of per-operation latencies (seconds) plus the total
wall time, so the runner can compute throughput and percentiles uniformly.
"""

from __future__ import annotations

import random
import string
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


def concurrent_reads(backend: Backend, p: Params) -> Result:
    """p.clients threads issuing point selects for p.duration seconds (exercises multiple cores)."""
    sql = backend.sql("SELECT id, k, v, n FROM bench_items WHERE id = ?")
    stop = threading.Event()
    all_lat: List[List[float]] = [[] for _ in range(p.clients)]
    errors: List[BaseException] = []
    barrier = threading.Barrier(p.clients + 1)

    def worker(idx: int) -> None:
        rng = random.Random(p.seed + 100 + idx)
        conn = backend.connect()
        lat = all_lat[idx]
        try:
            cur = conn.cursor()
            barrier.wait()
            while not stop.is_set():
                t = time.perf_counter()
                cur.execute(sql, (rng.randrange(p.rows),))
                cur.fetchall()
                lat.append(time.perf_counter() - t)
            conn.commit()
        except BaseException as e:  # surface errors from threads
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(p.clients)]
    for t in threads:
        t.start()
    barrier.wait()
    start = time.perf_counter()
    time.sleep(p.duration)
    stop.set()
    for t in threads:
        t.join()
    wall = time.perf_counter() - start
    if errors:
        raise errors[0]
    lat = [x for l in all_lat for x in l]
    return len(lat), wall, lat


# Order matters: load must run first, the rest reuse its data.
WORKLOADS: Dict[str, Callable[[Backend, Params], Result]] = {
    "load": load,
    "point_select": point_select,
    "range_select": range_select,
    "aggregate": aggregate,
    "update": update,
    "single_commits": single_commits,
    "concurrent_reads": concurrent_reads,
}

PROFILES: Dict[str, Params] = {
    "smoke": Params(rows=2_000, ops=200, commits=20, clients=2, duration=1.0),
    "quick": Params(rows=20_000, ops=2_000, commits=100, clients=4, duration=3.0),
    "standard": Params(rows=200_000, ops=10_000, commits=500, clients=8, duration=10.0),
    "large": Params(rows=2_000_000, ops=50_000, commits=1_000, clients=16, duration=30.0),
}
