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


def throughput_matrix(docs: List[Dict[str, Any]]):
    cols = []  # (label, backend)
    data: Dict[str, Dict[str, Any]] = {}
    for d in docs:
        for res in d["results"]:
            col = f"{d['label']} / {res['backend']}"
            cols.append(col)
            for w, v in res["workloads"].items():
                data.setdefault(w, {})[col] = v["median_ops_per_s"]
    workloads = [w for w in WORKLOADS if w in data] + [w for w in data if w not in WORKLOADS]
    return cols, workloads, data


def markdown(docs: List[Dict[str, Any]]) -> str:
    parts = ["## Machines", ""]
    parts.append(_md_table(
        ["label", "arch", "cpu", "usable cores", "effective cores", "RAM MB", "virt", "board"],
        [_machine_row(d) for d in docs]))
    cols, workloads, data = throughput_matrix(docs)
    parts += ["", "## Throughput (median ops/s, higher is better)", ""]
    rows = [[w] + [f"{data[w][c]:,.0f}" if data[w].get(c) else "-" for c in cols] for w in workloads]
    parts.append(_md_table(["workload"] + cols, rows))
    return "\n".join(parts) + "\n"


def to_csv(docs: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["label", "timestamp", "arch", "cpu_model", "usable_cores", "effective_cores",
                "is_virtual", "backend", "workload", "median_ops_per_s", "median_p95_ms"])
    for d in docs:
        s = d["system"]
        for res in d["results"]:
            for name, v in res["workloads"].items():
                w.writerow([d["label"], d["timestamp"], s["arch"], s["cpu"].get("model"),
                            s["cpu"].get("usable_cores"), (s.get("core_check") or {}).get("effective_cores"),
                            s["virtualization"].get("is_virtual"), res["backend"], name,
                            v["median_ops_per_s"], v["median_p95_ms"]])
    return buf.getvalue()
