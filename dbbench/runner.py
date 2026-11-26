from __future__ import annotations

import datetime as dt
import json
import re
import statistics
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import __version__, sysinfo
from .backends import get_backend
from .workloads import MULTI_USER, PROFILES, WORKLOADS, Params


def _pct(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(int(round(q * (len(sorted_vals) - 1))), len(sorted_vals) - 1)
    return sorted_vals[idx]


def summarize(ops: int, wall: float, lat: List[float]) -> Dict[str, Any]:
    s = sorted(lat)
    return {
        "ops": ops,
        "wall_s": round(wall, 4),
        "ops_per_s": round(ops / wall, 1) if wall > 0 else None,
        "lat_ms": {
            "p50": round(_pct(s, 0.50) * 1000, 3),
            "p95": round(_pct(s, 0.95) * 1000, 3),
            "p99": round(_pct(s, 0.99) * 1000, 3),
            "max": round((s[-1] if s else 0) * 1000, 3),
        },
    }


def validate_workloads(workloads: Iterable[str]) -> List[str]:
    names = [w.strip() for w in workloads if w.strip()]
    unknown = sorted(set(names) - set(WORKLOADS))
    if unknown:
        raise ValueError(f"unknown workload(s): {', '.join(unknown)} "
                         f"(choose from {', '.join(WORKLOADS)})")
    return names


def _aggregate(rs: List[Dict[str, Any]]) -> Dict[str, Any]:
    rates = [x["ops_per_s"] for x in rs if x["ops_per_s"]]
    out = {
        "median_ops_per_s": round(statistics.median(rates), 1) if rates else None,
        "stdev_ops_per_s": round(statistics.stdev(rates), 1) if len(rates) > 1 else 0.0,
        "median_p95_ms": statistics.median(x["lat_ms"]["p95"] for x in rs),
        "runs": rs,
    }
    if rs and "lock_timeouts" in rs[0]:
        out["lock_timeouts"] = sum(x["lock_timeouts"] for x in rs)
    return out


def _measure(fn, backend, params: Params) -> Dict[str, Any]:
    res = fn(backend, params)
    summary = summarize(*res[:3])
    if len(res) > 3:  # multi-user workloads also report clients / writes / lock timeouts
        summary.update(res[3])
    return summary


def run_backend(backend_name: str, params: Params, workloads: Iterable[str], repeats: int,
                log=print, client_sweep: Optional[List[int]] = None, sweep_repeats: int = 2,
                **backend_opts) -> Dict[str, Any]:
    wanted = validate_workloads(workloads)
    backend = get_backend(backend_name, **backend_opts)
    names = [w for w in WORKLOADS if w in wanted]
    if "load" not in names:
        names.insert(0, "load")  # everything else needs data
    runs: Dict[str, List[Dict[str, Any]]] = {n: [] for n in names}
    sweep: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    try:
        info = backend.info()
        for r in range(repeats):
            for name in names:
                summary = _measure(WORKLOADS[name], backend, params)
                runs[name].append(summary)
                log(f"  [{backend_name}] run {r + 1}/{repeats} {name:<17} "
                    f"{summary['ops_per_s'] or 0:>12,.1f} ops/s  p95 {summary['lat_ms']['p95']:.3f} ms")
        # Multi-user scaling: the same workloads at several client counts (data already loaded).
        for name in [w for w in MULTI_USER if w in names] if client_sweep else []:
            for clients in client_sweep or []:
                q = replace(params, clients=clients)
                for _ in range(sweep_repeats):
                    summary = _measure(WORKLOADS[name], backend, q)
                    sweep.setdefault(name, {}).setdefault(str(clients), []).append(summary)
                med = _aggregate(sweep[name][str(clients)])["median_ops_per_s"] or 0
                log(f"  [{backend_name}] sweep {name:<17} {clients:>3} clients {med:>12,.1f} ops/s")
    finally:
        try:
            backend.cleanup()
        except Exception as e:  # don't hide the original error
            log(f"  [{backend_name}] cleanup failed: {e}")

    out = {"backend": backend_name, "backend_info": info,
           "workloads": {name: _aggregate(rs) for name, rs in runs.items()}}
    if sweep:
        out["client_sweep"] = {name: {c: _aggregate(rs) for c, rs in by_c.items()}
                               for name, by_c in sweep.items()}
    return out


def build_params(profile: str, overrides: Dict[str, Optional[Any]]) -> Params:
    base = PROFILES[profile]
    return replace(base, **{k: v for k, v in overrides.items() if v is not None})


def run(backends: List[str], profile: str, overrides: Dict[str, Any], workloads: Iterable[str],
        repeats: int, label: Optional[str], verify_cores: bool, output_dir: Path,
        log=print, client_sweep: Optional[List[int]] = None, sweep_repeats: int = 2,
        **backend_opts) -> Path:
    workloads = validate_workloads(workloads)
    params = build_params(profile, overrides)
    if repeats < 1 or params.clients < 1 or params.rows < 1:
        raise ValueError("repeats, clients and rows must be >= 1")
    if client_sweep and (min(client_sweep) < 1 or sweep_repeats < 1):
        raise ValueError("client sweep counts and sweep repeats must be >= 1")
    log("Collecting system info...")
    system = sysinfo.collect()
    if verify_cores:
        log("Verifying CPU cores (parallel scaling test)...")
        system["core_check"] = sysinfo.verify_cores()
        cc = system["core_check"]
        log(f"  claimed {cc['claimed_cores']} cores, effective {cc['effective_cores']} -> {cc['verdict']}")

    results = []
    for b in backends:
        log(f"Running {b} ({profile} profile)...")
        try:
            results.append(run_backend(b, params, workloads, repeats, log=log,
                                       client_sweep=client_sweep, sweep_repeats=sweep_repeats,
                                       **backend_opts))
        except Exception as e:  # keep the other backends' results (e.g. PostgreSQL not running)
            msg = f"{type(e).__name__}: {e}".strip()
            log(f"  [{b}] FAILED: {msg}")
            results.append({"backend": b, "error": msg, "workloads": {}})

    now = dt.datetime.now(dt.timezone.utc)
    label = label or system["hostname"]
    doc = {
        "schema_version": 1,
        "dbbench_version": __version__,
        "label": label,
        "timestamp": now.isoformat(timespec="seconds"),
        "profile": profile,
        "params": asdict(params),
        "repeats": repeats,
        "client_sweep": client_sweep or [],
        "system": system,
        "results": results,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label)
    path = output_dir / f"{safe}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(doc, indent=2))
    return path
