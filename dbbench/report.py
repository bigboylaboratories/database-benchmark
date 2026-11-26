"""Compare result files from several machines side by side."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List

from .workloads import WORKLOADS


def load(paths: List[Path]) -> List[Dict[str, Any]]:
    docs = []
    for p in paths:
        doc = json.loads(Path(p).read_text())
        doc["_path"] = str(p)
        docs.append(doc)
    return docs


def _machine_row(doc: Dict[str, Any]) -> List[str]:
    s = doc["system"]
    cpu = s["cpu"]
    cc = s.get("core_check") or {}
    virt = s["virtualization"]
    kind = virt.get("detected") or ("virtual" if virt.get("is_virtual") else "physical")
    return [
        doc["label"], s["arch"], cpu.get("model") or "?",
        str(cpu.get("usable_cores")), str(cc.get("effective_cores", "-")),
        str(s["memory"].get("total_mb")), kind, virt.get("board_model") or "",
    ]


def _md_table(header: List[str], rows: List[List[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _columns(docs: List[Dict[str, Any]]):
    """(column name, doc, result) for every successful backend run; names are unique."""
    seen: Dict[str, int] = {}
    labels = [d["label"] for d in docs]
    out = []
    for d in docs:
        label = d["label"]
        if labels.count(label) > 1:  # same machine run twice: tell the runs apart
            label = f"{label} @{d.get('timestamp', '?')[:16]}"
        for res in d["results"]:
            if res.get("error"):
                continue
            col = f"{label} / {res['backend']}"
            seen[col] = seen.get(col, 0) + 1
            if seen[col] > 1:
                col = f"{col} #{seen[col]}"
            out.append((col, d, res))
    return out


def _matrix(docs: List[Dict[str, Any]], field: str):
    cols = _columns(docs)
    data: Dict[str, Dict[str, Any]] = {}
    for col, _, res in cols:
        for w, v in res["workloads"].items():
            data.setdefault(w, {})[col] = v.get(field)
    workloads = [w for w in WORKLOADS if w in data] + [w for w in data if w not in WORKLOADS]
    return [c for c, _, _ in cols], workloads, data


def throughput_matrix(docs: List[Dict[str, Any]]):
    return _matrix(docs, "median_ops_per_s")


def _fmt(v, spec: str) -> str:
    return format(v, spec) if isinstance(v, (int, float)) and v else "-"


def markdown(docs: List[Dict[str, Any]]) -> str:
    parts = ["## Machines", ""]
    parts.append(_md_table(
        ["label", "arch", "cpu", "usable cores", "effective cores", "RAM MB", "virt", "board"],
        [_machine_row(d) for d in docs]))

    cols, workloads, data = throughput_matrix(docs)
    parts += ["", "## Throughput (median ops/s, higher is better)", ""]
    rows = [[w] + [_fmt(data[w].get(c), ",.0f") for c in cols] for w in workloads]
    parts.append(_md_table(["workload"] + cols, rows))

    cols, workloads, data = _matrix(docs, "median_p95_ms")
    parts += ["", "## Latency (median p95 ms, lower is better)", ""]
    rows = [[w] + [_fmt(data[w].get(c), ",.3f") for c in cols] for w in workloads]
    parts.append(_md_table(["workload"] + cols, rows))

    # SQLite vs PostgreSQL on the same machine run.
    _, workloads, tp = throughput_matrix(docs)
    lookup = {(id(d), r["backend"]): c for c, d, r in _columns(docs)}
    pairs = [(lookup[(id(d), "sqlite")], lookup[(id(d), "postgres")]) for d in docs
             if (id(d), "sqlite") in lookup and (id(d), "postgres") in lookup]
    if pairs:
        rows = []
        for w in workloads:
            row = [w]
            for s_col, p_col in pairs:
                sv, pv = tp[w].get(s_col), tp[w].get(p_col)
                row.append(f"{sv / pv:.2f}x" if sv and pv else "-")
            rows.append(row)
        parts += ["", "## SQLite / PostgreSQL throughput ratio (>1 = SQLite faster)", ""]
        parts.append(_md_table(["workload"] + [s.rsplit(" / ", 1)[0] for s, _ in pairs], rows))

    failed = [(d["label"], r["backend"], r["error"]) for d in docs for r in d["results"] if r.get("error")]
    if failed:
        parts += ["", "## Failed runs", ""]
        parts += [f"- `{l} / {b}`: {e}" for l, b, e in failed]
    return "\n".join(parts) + "\n"


def to_csv(docs: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["label", "timestamp", "arch", "cpu_model", "usable_cores", "effective_cores",
                "is_virtual", "backend", "workload", "median_ops_per_s", "median_p95_ms", "error"])
    for d in docs:
        s = d["system"]
        for res in d["results"]:
            for name, v in res.get("workloads", {}).items():
                w.writerow([d["label"], d["timestamp"], s["arch"], s["cpu"].get("model"),
                            s["cpu"].get("usable_cores"), (s.get("core_check") or {}).get("effective_cores"),
                            s["virtualization"].get("is_virtual"), res["backend"], name,
                            v["median_ops_per_s"], v["median_p95_ms"], ""])
            if res.get("error"):
                w.writerow([d["label"], d["timestamp"], s["arch"], s["cpu"].get("model"),
                            s["cpu"].get("usable_cores"), (s.get("core_check") or {}).get("effective_cores"),
                            s["virtualization"].get("is_virtual"), res["backend"], "", "", "", res["error"]])
    return buf.getvalue()
