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
from .workloads import PROFILES, WORKLOADS, Params


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


def run_backend(backend_name: str, params: Params, workloads: Iterable[str], repeats: int,
                log=print, **backend_opts) -> Dict[str, Any]:
    backend = get_backend(backend_name, **backend_opts)
    names = [w for w in WORKLOADS if w in set(workloads)]
    if "load" not in names:
        names.insert(0, "load")  # everything else needs data
    runs: Dict[str, List[Dict[str, Any]]] = {n: [] for n in names}
    try:
        info = backend.info()
        for r in range(repeats):
            for name in names:
                ops, wall, lat = WORKLOADS[name](backend, params)
                summary = summarize(ops, wall, lat)
                runs[name].append(summary)
                log(f"  [{backend_name}] run {r + 1}/{repeats} {name:<17} "
                    f"{summary['ops_per_s']:>12,.1f} ops/s  p95 {summary['lat_ms']['p95']:.3f} ms")
    finally:
        backend.cleanup()

    workloads_out = {}
    for name, rs in runs.items():
        rates = [x["ops_per_s"] for x in rs if x["ops_per_s"]]
        workloads_out[name] = {
            "median_ops_per_s": round(statistics.median(rates), 1) if rates else None,
            "stdev_ops_per_s": round(statistics.stdev(rates), 1) if len(rates) > 1 else 0.0,
            "median_p95_ms": statistics.median(x["lat_ms"]["p95"] for x in rs),
            "runs": rs,
        }
    return {"backend": backend_name, "backend_info": info, "workloads": workloads_out}


def build_params(profile: str, overrides: Dict[str, Optional[Any]]) -> Params:
    base = PROFILES[profile]
    return replace(base, **{k: v for k, v in overrides.items() if v is not None})


def run(backends: List[str], profile: str, overrides: Dict[str, Any], workloads: Iterable[str],
        repeats: int, label: Optional[str], verify_cores: bool, output_dir: Path,
        log=print, **backend_opts) -> Path:
    params = build_params(profile, overrides)
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
        results.append(run_backend(b, params, workloads, repeats, log=log, **backend_opts))

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
        "system": system,
        "results": results,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label)
    path = output_dir / f"{safe}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(doc, indent=2))
    return path
