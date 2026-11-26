"""Self-contained graphical HTML report (inline SVG, no external files or libraries).

    dbbench report -f html                 # writes results/report.html
    dbbench report -f html -o out.html results/a.json results/b.json
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .workloads import MULTI_USER, WORKLOADS

BACKENDS = [("sqlite", "SQLite", 1), ("postgres", "PostgreSQL", 2)]  # (key, name, series slot)

DESCRIPTIONS = {
    "load": "Bulk insert in 1,000-row transactions",
    "point_select": "Random lookups by primary key, one user",
    "range_select": "count/avg over an indexed key range, one user",
    "aggregate": "Full table scan + GROUP BY",
    "update": "Random single-row updates, one commit each, one user",
    "single_commits": "One INSERT per transaction (fsync / storage bound)",
    "concurrent_reads": "Point reads by primary key",
    "concurrent_mixed": "80% point reads, 20% single-row updates (each committed)",
    "concurrent_writes": "Single-row updates, each committed",
}
MULTI_TITLES = {
    "concurrent_reads": "Reads only",
    "concurrent_mixed": "80% reads / 20% writes",
    "concurrent_writes": "Writes only",
}

e = html.escape


# ------------------------------------------------------------------ format -- #

def compact(v: Optional[float]) -> str:
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f}M".replace(".0M", "M")
    if a >= 1e4:
        return f"{v / 1e3:.0f}K"
    if a >= 1e3:
        return f"{v / 1e3:.1f}K".replace(".0K", "K")
    if a >= 10:
        return f"{v:.0f}"
    return f"{v:.2g}"


def full(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:,.0f}" if v >= 10 else f"{v:,.2f}"


def ratio_text(s: Optional[float], p: Optional[float]) -> Tuple[str, str]:
    """('12.3x faster', winner key) or ('-', '')."""
    if not s or not p:
        return "-", ""
    r = s / p
    if r >= 1:
        return f"{r:.1f}× faster", "sqlite"
    return f"{1 / r:.1f}× faster", "postgres"


def nice_ticks(vmax: float, n: int = 4) -> List[float]:
    if vmax <= 0:
        return [0, 1]
    raw = vmax / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    ticks, t = [], 0.0
    while t < vmax + step * 0.999:
        ticks.append(round(t, 10))
        t += step
    return ticks


# ------------------------------------------------------------------ charts -- #

def _tip(lines: Sequence[str]) -> str:
    return e(json.dumps(list(lines)), quote=True)


def bar_chart(rows: List[Tuple[str, Optional[float], Optional[float]]], unit: str = "ops/s") -> str:
    """Horizontal grouped bars: one row per machine, SQLite + PostgreSQL bars."""
    left, right, top, bar, gap, row_gap = 150, 120, 8, 12, 2, 16
    row_h = bar * 2 + gap + row_gap
    width = 760
    plot_w = width - left - right
    height = top + row_h * len(rows) + 22
    vmax = max([v for _, s, p in rows for v in (s, p) if v] or [1])
    ticks = nice_ticks(vmax)
    xmax = ticks[-1]
    x = lambda v: left + plot_w * (v or 0) / xmax
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for t in ticks:
        out.append(f'<line class="grid" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top - 4}" y2="{height - 20}"/>')
        out.append(f'<text class="tick" x="{x(t):.1f}" y="{height - 6}" text-anchor="middle">{compact(t)}</text>')
    out.append(f'<line class="axis" x1="{left}" x2="{left}" y1="{top - 4}" y2="{height - 20}"/>')
    for i, (label, s, p) in enumerate(rows):
        y0 = top + i * row_h
        out.append(f'<text class="rowlabel" x="{left - 10}" y="{y0 + bar + 4}" text-anchor="end">{e(label)}</text>')
        rt, winner = ratio_text(s, p)
        for j, (key, name, slot) in enumerate(BACKENDS):
            v = s if key == "sqlite" else p
            y = y0 + j * (bar + gap)
            if not v:
                out.append(f'<text class="value" x="{left + 4}" y="{y + bar - 2}">no data</text>')
                continue
            w = max(x(v) - left, 2)
            tip = _tip([f"{label} · {name}", f"{full(v)} {unit}",
                        f"{'SQLite' if winner == 'sqlite' else 'PostgreSQL'} {rt}" if winner else ""])
            # 4px rounded data end, square at the baseline
            r = min(4, w / 2)
            path = (f"M{left},{y} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{bar - 2 * r} "
                    f"a{r},{r} 0 0 1 {-r},{r} h{-(w - r):.1f} z")
            out.append(f'<path class="s{slot} hit" d="{path}" data-tip="{tip}"/>')
            out.append(f'<text class="value" x="{left + w + 5:.1f}" y="{y + bar - 2}">{compact(v)}</text>')
        if winner:
            name = "SQLite" if winner == "sqlite" else "PostgreSQL"
            out.append(f'<text class="ratio" x="{width - 4}" y="{y0 + bar + 4}" text-anchor="end">'
                       f'{e(name)} {e(rt)}</text>')
    out.append("</svg>")
    return "".join(out)


def line_chart(title: str, clients: List[int], series: Dict[str, List[Optional[float]]]) -> str:
    """Throughput vs number of users, one line per database. x is log2-spaced (1,2,4,8,16)."""
    width, height, left, right, top, bottom = 360, 240, 52, 86, 30, 40
    pw, ph = width - left - right, height - top - bottom
    vals = [v for vs in series.values() for v in vs if v]
    ticks = nice_ticks(max(vals or [1]))
    ymax = ticks[-1]
    n = len(clients)
    x = lambda i: left + (pw * i / (n - 1) if n > 1 else pw / 2)
    y = lambda v: top + ph - ph * (v or 0) / ymax
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart small" role="img">',
           f'<text class="ctitle" x="{left}" y="16">{e(title)}</text>']
    for t in ticks:
        out.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>')
        out.append(f'<text class="tick" x="{left - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{compact(t)}</text>')
    out.append(f'<line class="axis" x1="{left}" x2="{left + pw}" y1="{top + ph}" y2="{top + ph}"/>')
    for i, c in enumerate(clients):
        out.append(f'<text class="tick" x="{x(i):.1f}" y="{top + ph + 16}" text-anchor="middle">{c}</text>')
    out.append(f'<text class="tick" x="{left + pw / 2:.1f}" y="{height - 6}" text-anchor="middle">'
               f'concurrent users</text>')
    ends = []
    for key, name, slot in BACKENDS:
        vs = series.get(key) or []
        pts = [(x(i), y(v), clients[i], v) for i, v in enumerate(vs) if v]
        if not pts:
            continue
        d = " ".join(f"{'M' if k == 0 else 'L'}{px:.1f},{py:.1f}" for k, (px, py, _, _) in enumerate(pts))
        out.append(f'<path class="line s{slot}" d="{d}"/>')
        for px, py, c, v in pts:
            tip = _tip([f"{name} · {c} user{'s' if c != 1 else ''}", f"{full(v)} ops/s"])
            out.append(f'<circle class="dot s{slot}" cx="{px:.1f}" cy="{py:.1f}" r="4"/>'
                       f'<circle class="hit-dot" cx="{px:.1f}" cy="{py:.1f}" r="11" data-tip="{tip}"/>')
        ends.append((pts[-1][1], name, pts[-1][3]))
    # Direct end labels, unless they would collide (then the legend carries identity).
    ends.sort()
    if len(ends) < 2 or abs(ends[0][0] - ends[1][0]) >= 28:
        for py, name, v in ends:
            out.append(f'<text class="endlabel" x="{left + pw + 8}" y="{py - 2:.1f}">{e(name)}</text>'
                       f'<text class="value" x="{left + pw + 8}" y="{py + 11:.1f}">{compact(v)}</text>')
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------------- page -- #

def _results(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {r["backend"]: r for r in doc["results"] if not r.get("error")}


def _machine_desc(doc: Dict[str, Any]) -> str:
    s = doc["system"]
    v = s["virtualization"]
    kind = v.get("detected") or ("virtual" if v.get("is_virtual") else "physical")
    ram = s["memory"].get("total_mb")
    ram_s = f"{ram / 1024:.1f} GB" if ram else "? GB"
    eff = (s.get("core_check") or {}).get("effective_cores")
    return f'{s["cpu"].get("usable_cores")} cores{f" ({eff} effective)" if eff else ""} · {ram_s} · {kind}'


def _values(per, backend: str, key: str) -> str:
    vals = sorted({str(((res.get(backend) or {}).get("backend_info") or {}).get(key))
                   for _, _, res in per if res.get(backend)} - {"None"})
    return " / ".join(vals) if vals else "?"


def settings_panel(per) -> str:
    """Side-by-side settings, so a reader can see the comparison is like-for-like."""
    transports = _values(per, "postgres", "transport")
    rows = [
        ("Durability", f"synchronous={_values(per, 'sqlite', 'synchronous')}: every commit fsynced",
         f"synchronous_commit={_values(per, 'postgres', 'synchronous_commit')}, "
         f"fsync={_values(per, 'postgres', 'fsync')}: every commit fsynced"),
        ("Cache", f"{_values(per, 'sqlite', 'cache_size_mb')} MB page cache",
         f"shared_buffers={_values(per, 'postgres', 'shared_buffers')}"),
        ("Journal", "WAL (write-ahead log)", "WAL (write-ahead log)"),
        ("Connection", "in-process library", transports.replace("tcp", "TCP localhost (Windows)")),
        ("Data, SQL, indexes", "identical", "identical"),
        ("Clients", "one process per user, same Python driver loop", "one process per user, same Python driver loop"),
        ("Version", _values(per, "sqlite", "version"), _values(per, "postgres", "server_version")),
    ]
    trs = "".join(f"<tr><td>{e(a)}</td><td>{e(b)}</td><td>{e(c)}</td></tr>" for a, b, c in rows)
    return ('<section><h2>Test settings: like-for-like</h2><p class="note">Both databases run on the same '
            'machine and disk with the same durability guarantee and cache size. The one difference that '
            'cannot be removed is architectural: SQLite runs inside the application, PostgreSQL is a server '
            'reached over a connection (a local socket on Linux).</p><div class="scroll"><table><thead><tr>'
            f'<th>setting</th><th><i class="sw s1"></i> SQLite</th><th><i class="sw s2"></i> PostgreSQL</th>'
            f'</tr></thead><tbody>{trs}</tbody></table></div></section>')


def render(docs: List[Dict[str, Any]], notes: Sequence[str] = ()) -> str:
    labels = []
    for d in docs:  # unique labels, as in the markdown report
        lab = d["label"]
        if [x["label"] for x in docs].count(lab) > 1:
            lab = f"{lab} @{d.get('timestamp', '?')[:16]}"
        labels.append(lab)
    per = [(lab, d, _results(d)) for lab, d in zip(labels, docs)]

    workloads = [w for w in WORKLOADS if any(w in r.get(b, {}).get("workloads", {})
                                             for _, _, r in per for b in ("sqlite", "postgres"))]

    def val(res, backend, w, field="median_ops_per_s"):
        return ((res.get(backend) or {}).get("workloads", {}).get(w) or {}).get(field)

    # Headline: how often each database wins.
    wins = {"sqlite": 0, "postgres": 0}
    biggest: Dict[str, Tuple[float, str]] = {}
    for lab, _, res in per:
        for w in workloads:
            s, p = val(res, "sqlite", w), val(res, "postgres", w)
            if s and p:
                k = "sqlite" if s >= p else "postgres"
                wins[k] += 1
                r = max(s / p, p / s)
                if r > biggest.get(k, (0, ""))[0]:
                    biggest[k] = (r, f"{w.replace('_', ' ')} on {lab}")
    total = wins["sqlite"] + wins["postgres"]

    parts: List[str] = []
    parts.append('<header><h1>SQLite vs PostgreSQL</h1>'
                 f'<p class="sub">{len(docs)} machine{"s" if len(docs) != 1 else ""} · '
                 f'generated {dt.date.today().isoformat()} · median of repeated runs, higher is better</p>'
                 '<div class="legend"><span><i class="sw s1"></i>SQLite</span>'
                 '<span><i class="sw s2"></i>PostgreSQL</span></div></header>')
    syncs = {((res.get("sqlite") or {}).get("backend_info") or {}).get("synchronous") for _, _, res in per} - {None}
    pg_sync = {((res.get("postgres") or {}).get("backend_info") or {}).get("synchronous_commit")
               for _, _, res in per} - {None}
    if syncs and syncs != {"FULL"} and "on" in pg_sync:
        parts.append('<p class="caveat"><b>Durability is not equal in these runs.</b> SQLite ran with '
                     f'synchronous={e("/".join(sorted(syncs)))} (WAL), which does not flush each commit to disk; '
                     'PostgreSQL ran with synchronous_commit=on, which does. That favours SQLite on every '
                     'write workload. Re-run with <code>--sqlite-sync FULL</code> for a like-for-like '
                     'durability comparison.</p>')
    else:
        parts.append(settings_panel(per))
    if notes:
        parts.append('<section><h2>How to read these results</h2><ul class="notes">'
                     + "".join(f"<li>{e(n)}</li>" for n in notes) + "</ul></section>")
    if total:
        tiles = []
        for key, name, _ in BACKENDS:
            b = biggest.get(key)
            tiles.append(f'<div class="tile"><div class="tlabel"><i class="sw s{1 if key == "sqlite" else 2}"></i>'
                         f'{name} wins</div><div class="tvalue">{wins[key]} of {total}</div>'
                         f'<div class="tnote">{f"biggest lead: {b[0]:.0f}× on {e(b[1])}" if b else "no wins"}</div></div>')
        parts.append(f'<section class="tiles">{"".join(tiles)}</section>')

    # Multi-user scaling (client sweep).
    sweep_machines = [(lab, d, res) for lab, d, res in per
                      if any((res.get(b) or {}).get("client_sweep") for b in ("sqlite", "postgres"))]
    if sweep_machines:
        parts.append('<section><h2>Multi-user: throughput as users are added</h2>'
                     '<p class="note">Each user is a separate process. SQLite allows one writer at a time, so '
                     'its write throughput stays flat as users are added; PostgreSQL lets users update '
                     'different rows in parallel. Pick a machine:</p><div class="picker" role="tablist">')
        for i, (lab, _, _) in enumerate(sweep_machines):
            parts.append(f'<button role="tab" data-m="{i}" class="{"on" if i == 0 else ""}">{e(lab)}</button>')
        parts.append("</div>")
        for i, (lab, d, res) in enumerate(sweep_machines):
            clients = sorted({int(c) for b in ("sqlite", "postgres")
                              for w in MULTI_USER for c in ((res.get(b) or {}).get("client_sweep", {}).get(w) or {})})
            charts, table_rows = [], []
            for w in MULTI_USER:
                series = {}
                for key, _, _ in BACKENDS:
                    sw = ((res.get(key) or {}).get("client_sweep") or {}).get(w) or {}
                    series[key] = [(sw.get(str(c)) or {}).get("median_ops_per_s") for c in clients]
                if any(v for vs in series.values() for v in vs):
                    charts.append(line_chart(MULTI_TITLES[w], clients, series))
                    for ci, c in enumerate(clients):
                        s, p = series["sqlite"][ci], series["postgres"][ci]
                        table_rows.append(f"<tr><td>{MULTI_TITLES[w]}</td><td>{c}</td><td>{full(s)}</td>"
                                          f"<td>{full(p)}</td><td>{ratio_text(s, p)[0]} "
                                          f"{'(SQLite)' if s and p and s >= p else '(PostgreSQL)' if s and p else ''}"
                                          "</td></tr>")
            parts.append(f'<div class="machine" data-m="{i}"{"" if i == 0 else " hidden"}>'
                         f'<p class="mdesc">{e(lab)}: {e(_machine_desc(d))}</p>'
                         f'<div class="multiples">{"".join(charts)}</div>'
                         '<details><summary>Table view</summary><table><thead><tr><th>workload</th>'
                         '<th>users</th><th>SQLite ops/s</th><th>PostgreSQL ops/s</th><th>difference</th>'
                         f'</tr></thead><tbody>{"".join(table_rows)}</tbody></table></details></div>')
        parts.append("</section>")

        # Same multi-user workloads compared across machines, at the fewest and most users.
        def sweep_val(res, backend, w, c):
            sw = ((res.get(backend) or {}).get("client_sweep") or {}).get(w) or {}
            return (sw.get(str(c)) or {}).get("median_ops_per_s")

        counts = sorted({int(c) for _, _, res in sweep_machines for b in ("sqlite", "postgres")
                         for w in MULTI_USER for c in ((res.get(b) or {}).get("client_sweep", {}).get(w) or {})})
        ends = [counts[0], counts[-1]] if len(counts) > 1 else counts
        parts.append('<section><h2>Multi-user workloads across machines</h2><p class="note">'
                     f'Each workload at {" and ".join(map(str, ends))} user{"s" if ends[-1] != 1 else ""}. '
                     'With one user SQLite\'s in-process design has no connection overhead; with many, '
                     'PostgreSQL can run writes in parallel.</p>')
        for w in MULTI_USER:
            for c in ends:
                rows = [(lab, sweep_val(res, "sqlite", w, c), sweep_val(res, "postgres", w, c))
                        for lab, _, res in sweep_machines]
                rows = [r for r in rows if r[1] or r[2]]
                if not rows:
                    continue
                trs = "".join(f"<tr><td>{e(l)}</td><td>{full(s)}</td><td>{full(p)}</td><td>{ratio_text(s, p)[0]} "
                              f"{'(SQLite)' if s and p and s >= p else '(PostgreSQL)' if s and p else ''}</td></tr>"
                              for l, s, p in rows)
                parts.append(f'<article><h3>{e(MULTI_TITLES[w])} · {c} user{"s" if c != 1 else ""}</h3>'
                             f'<p class="desc">{e(DESCRIPTIONS[w])}</p>{bar_chart(rows)}'
                             '<details><summary>Table view</summary><table><thead><tr><th>machine</th>'
                             '<th>SQLite ops/s</th><th>PostgreSQL ops/s</th><th>difference</th></tr></thead>'
                             f'<tbody>{trs}</tbody></table></details></article>')
        parts.append("</section>")

    # Every workload across machines.
    parts.append('<section><h2>Every workload, every machine</h2>'
                 '<p class="note">Bars share one linear scale per workload, so the gap between the '
                 'two databases is drawn to size. Hover a bar for exact numbers.</p>')
    for w in workloads:
        rows = [(lab, val(res, "sqlite", w), val(res, "postgres", w)) for lab, _, res in per]
        rows = [r for r in rows if r[1] or r[2]]
        if not rows:
            continue
        users = ""
        if w in MULTI_USER:
            cs = {((res.get(b) or {}).get("workloads", {}).get(w) or {}).get("runs", [{}])[0].get("clients")
                  for _, _, res in per for b in ("sqlite", "postgres")} - {None}
            users = f" · {', '.join(map(str, sorted(cs)))} users" if cs else ""
        trs = "".join(f"<tr><td>{e(l)}</td><td>{full(s)}</td><td>{full(p)}</td><td>{ratio_text(s, p)[0]} "
                      f"{'(SQLite)' if s and p and s >= p else '(PostgreSQL)' if s and p else ''}</td></tr>"
                      for l, s, p in rows)
        parts.append(f'<article><h3>{e(w)}</h3><p class="desc">{e(DESCRIPTIONS.get(w, ""))}{users}</p>'
                     f'{bar_chart(rows)}<details><summary>Table view</summary><table><thead><tr>'
                     '<th>machine</th><th>SQLite ops/s</th><th>PostgreSQL ops/s</th><th>difference</th>'
                     f'</tr></thead><tbody>{trs}</tbody></table></details></article>')
    parts.append("</section>")

    # Latency table.
    head = "".join(f"<th>{e(l)}<br><small>SQLite / PG</small></th>" for l, _, _ in per)
    body = ""
    for w in workloads:
        cells = ""
        for _, _, res in per:
            s, p = val(res, "sqlite", w, "median_p95_ms"), val(res, "postgres", w, "median_p95_ms")
            cells += f"<td>{'-' if s is None else f'{s:.3f}'} / {'-' if p is None else f'{p:.3f}'}</td>"
        body += f"<tr><td>{e(w)}</td>{cells}</tr>"
    parts.append('<section><h2>Latency</h2><p class="note">Median p95 latency per operation in '
                 f'milliseconds, lower is better.</p><div class="scroll"><table><thead><tr><th>workload</th>{head}'
                 f'</tr></thead><tbody>{body}</tbody></table></div></section>')

    # Machines.
    rows = ""
    for lab, d, res in per:
        s = d["system"]
        cc = s.get("core_check") or {}
        failed = [r["backend"] for r in d["results"] if r.get("error")]
        rows += (f"<tr><td>{e(lab)}</td><td>{e(s['arch'])}</td><td>{e(str(s['cpu'].get('model') or '?'))}</td>"
                 f"<td>{s['cpu'].get('usable_cores')}</td><td>{cc.get('effective_cores', '-')} "
                 f"{e(cc.get('verdict', ''))}</td><td>{e(_machine_desc(d).split(' · ')[1])}</td>"
                 f"<td>{e(_machine_desc(d).split(' · ')[2])}</td><td>{e(d.get('profile', ''))}</td>"
                 f"<td>{e(', '.join(failed)) or '-'}</td></tr>")
    parts.append('<section><h2>Machines</h2><div class="scroll"><table><thead><tr><th>label</th><th>arch</th>'
                 '<th>CPU</th><th>cores</th><th>effective cores</th><th>RAM</th><th>type</th><th>profile</th>'
                 f'<th>failed</th></tr></thead><tbody>{rows}</tbody></table></div></section>')

    return PAGE.replace("{{BODY}}", "".join(parts))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SQLite vs PostgreSQL</title>
<style>
:root{color-scheme:light;--page:#f9f9f9;--surface:#ffffff;--ink:#191919;--ink2:#666666;--grid:#e7e7e7;
--axis:#c2c2c2;--border:rgba(11,11,11,.10);--s1:#257de0;--s2:#f47230}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0b0b0b;
--surface:#191919;--ink:#efefef;--ink2:#9c9c9c;--grid:#303030;--axis:#474747;--border:rgba(255,255,255,.10);
--s1:#338ced;--s2:#e76624}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0b0b0b;--surface:#191919;--ink:#efefef;--ink2:#9c9c9c;
--grid:#303030;--axis:#474747;--border:rgba(255,255,255,.10);--s1:#338ced;--s2:#e76624}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px;font-weight:600}h2{font-size:19px;margin:0 0 6px;font-weight:600}
h3{font-size:15px;margin:0;font-weight:600;font-family:ui-monospace,Consolas,monospace}
.sub,.note,.desc,.mdesc{color:var(--ink2);margin:0 0 12px}.desc{margin:2px 0 8px}
header{margin-bottom:20px}
.caveat{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--ink2);
border-radius:8px;padding:10px 14px;margin:0 0 20px;color:var(--ink2)}.caveat b{color:var(--ink)}
.notes{margin:0;padding-left:20px;color:var(--ink2)}.notes li{margin:4px 0}
code{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.legend{display:flex;gap:18px;color:var(--ink2)}.legend span{display:flex;align-items:center;gap:6px}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px}.sw.s1{background:var(--s1)}.sw.s2{background:var(--s2)}
section{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin:0 0 20px}
section.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;background:none;border:0;padding:0}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px}
.tlabel{color:var(--ink2);display:flex;align-items:center;gap:6px}.tvalue{font-size:30px;font-weight:600}
.tnote{color:var(--ink2);font-size:13px}
article{padding:14px 0;border-top:1px solid var(--grid)}article:first-of-type{border-top:0}
.chart{width:100%;height:auto;display:block;overflow:visible}
.multiples{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--ink2);font-size:11px;font-variant-numeric:tabular-nums}
.rowlabel{fill:var(--ink);font-size:12px}.value{fill:var(--ink2);font-size:11px;font-variant-numeric:tabular-nums}
.ratio{fill:var(--ink);font-size:12px;font-weight:600}.ctitle{fill:var(--ink);font-size:13px;font-weight:600}
.endlabel{fill:var(--ink);font-size:12px}
path.s1{fill:var(--s1)}path.s2{fill:var(--s2)}
path.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
path.line.s1{stroke:var(--s1)}path.line.s2{stroke:var(--s2)}
.dot{stroke:var(--surface);stroke-width:2}.dot.s1{fill:var(--s1)}.dot.s2{fill:var(--s2)}
.hit-dot{fill:transparent}.hit,.hit-dot{cursor:default}
.hit:hover{opacity:.85}
.picker{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}
.picker button{font:inherit;color:var(--ink);background:var(--page);border:1px solid var(--border);
border-radius:999px;padding:4px 12px;cursor:pointer}
.picker button.on{background:var(--ink);color:var(--surface)}
details{margin-top:6px}summary{color:var(--ink2);cursor:pointer;font-size:13px}
table{border-collapse:collapse;font-size:13px;margin-top:8px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 10px;border-bottom:1px solid var(--grid);white-space:nowrap}
th{color:var(--ink2);font-weight:600}small{font-weight:400}.scroll{overflow-x:auto}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--border);
border-radius:8px;padding:6px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:9}
#tip b{font-weight:600}
@media (forced-colors:active){path.s1,path.s2,.dot{forced-color-adjust:none}}
</style></head>
<body><main>{{BODY}}</main><div id="tip"></div>
<script>
(function(){
  var tip=document.getElementById('tip');
  document.addEventListener('mousemove',function(ev){
    var t=ev.target.closest&&ev.target.closest('[data-tip]');
    if(!t){tip.style.display='none';return;}
    var lines=JSON.parse(t.getAttribute('data-tip')).filter(Boolean);
    tip.innerHTML='';
    lines.forEach(function(l,i){var d=document.createElement(i?'div':'b');d.textContent=l;tip.appendChild(d);if(!i)tip.appendChild(document.createElement('br'));});
    tip.style.display='block';
    var x=ev.clientX+14,y=ev.clientY+14,r=tip.getBoundingClientRect();
    if(x+r.width>innerWidth-8)x=ev.clientX-r.width-14;
    if(y+r.height>innerHeight-8)y=ev.clientY-r.height-14;
    tip.style.left=x+'px';tip.style.top=y+'px';
  });
  document.querySelectorAll('.picker button').forEach(function(b){
    b.addEventListener('click',function(){
      document.querySelectorAll('.picker button').forEach(function(x){x.classList.toggle('on',x===b);});
      document.querySelectorAll('.machine').forEach(function(m){m.hidden=m.getAttribute('data-m')!==b.getAttribute('data-m');});
    });
  });
})();
</script></body></html>
"""
