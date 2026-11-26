from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

from . import __version__, pglocal, report, runner, sysinfo
from .workloads import PROFILES, WORKLOADS


def _cmd_sysinfo(args) -> int:
    info = sysinfo.collect()
    if args.verify_cores:
        info["core_check"] = sysinfo.verify_cores(max_workers=args.max_workers)
    print(json.dumps(info, indent=2))
    return 0


def _cmd_run(args) -> int:
    backends = ["sqlite", "postgres"] if args.backend == "all" else [args.backend]
    overrides = {"rows": args.rows, "ops": args.ops, "clients": args.clients,
                 "duration": args.duration, "commits": args.commits}
    dsn, started = args.dsn, False
    if args.start_pg and "postgres" in backends:
        if not pglocal.status(Path(args.pg_dir)):
            started = True
        try:
            dsn = pglocal.start(Path(args.pg_dir), args.pg_port)
        except RuntimeError as e:
            print(f"dbbench: {e}", file=sys.stderr)
            return 1
    try:
        path = runner.run(
            backends=backends, profile=args.profile, overrides=overrides,
            workloads=args.workloads.split(",") if args.workloads else WORKLOADS.keys(),
            repeats=args.repeats, label=args.label, verify_cores=not args.skip_core_check,
            output_dir=Path(args.output_dir),
            client_sweep=args.client_sweep, sweep_repeats=args.sweep_repeats,
            dsn=dsn, sqlite_path=args.sqlite_path, sqlite_sync=args.sqlite_sync,
            sqlite_cache_mb=args.sqlite_cache_mb,
        )
    except ValueError as e:  # bad arguments, raised before any benchmarking starts
        print(f"dbbench: {e}", file=sys.stderr)
        return 2
    finally:
        if started:
            pglocal.stop(Path(args.pg_dir))
    print(f"\nResults written to {path}")
    failed = [r["backend"] for r in json.loads(path.read_text())["results"] if r.get("error")]
    if failed:
        print(f"WARNING: backend(s) failed: {', '.join(failed)} (see 'error' in the result file)",
              file=sys.stderr)
        return 1
    return 0


def _cmd_pg(args) -> int:
    d = Path(args.pg_dir)
    try:
        if args.action == "start":
            dsn = pglocal.start(d, args.pg_port)
            print(f"\nUse it with:  dbbench run -b all --dsn {dsn}")
        elif args.action == "stop":
            pglocal.stop(d)
        elif args.action == "destroy":
            pglocal.destroy(d)
        else:
            up = pglocal.status(d)
            print(f"{'running' if up else 'stopped'}  data={d}  dsn={pglocal.dsn(args.pg_port)}")
            return 0 if up else 3
    except RuntimeError as e:
        print(f"dbbench: {e}", file=sys.stderr)
        return 1
    return 0


def _pg_args(parser) -> None:
    parser.add_argument("--pg-dir", default=str(pglocal.DEFAULT_DIR),
                        help="data directory of the private PostgreSQL (default: %(default)s)")
    parser.add_argument("--pg-port", type=int, default=pglocal.DEFAULT_PORT)


def _cmd_report(args) -> int:
    paths = [Path(p) for p in args.files]
    if not paths:  # default: the newest run of each machine label in results/
        newest: dict = {}
        for p in Path("results").glob("*.json"):
            try:
                doc = json.loads(p.read_text())
                key, ts = doc["label"], doc["timestamp"]
            except (ValueError, KeyError, OSError):
                continue
            if key not in newest or ts > newest[key][0]:
                newest[key] = (ts, p)
        paths = sorted(p for _, p in newest.values())
    if not paths:
        print("no result files found", file=sys.stderr)
        return 1
    docs = report.load(paths)
    if args.format == "html":
        from . import htmlreport

        out = Path(args.output or "results/report.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(htmlreport.render(docs, notes=args.note or []), encoding="utf-8")
        print(f"wrote {out}")
        return 0
    text = report.to_csv(docs) if args.format == "csv" else report.markdown(docs)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(text)
    return 0


def _int_list(text: str) -> List[int]:
    try:
        vals = sorted({int(x) for x in text.split(",") if x.strip()})
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers, got {text!r}")
    if not vals or vals[0] < 1:
        raise argparse.ArgumentTypeError("client counts must be >= 1")
    return vals


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dbbench", description=__doc__ or "Database benchmark harness")
    p.add_argument("--version", action="version", version=f"dbbench {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sysinfo", help="show machine/VM info and optionally verify cores")
    s.add_argument("--verify-cores", action="store_true", help="run parallel CPU scaling test")
    s.add_argument("--max-workers", type=int, help="override number of cores to test")
    s.set_defaults(func=_cmd_sysinfo)

    r = sub.add_parser("run", help="run benchmarks and write a JSON result file")
    r.add_argument("-b", "--backend", choices=["sqlite", "postgres", "all"], default="sqlite")
    r.add_argument("-p", "--profile", choices=list(PROFILES), default="quick")
    r.add_argument("-w", "--workloads", help=f"comma list from: {','.join(WORKLOADS)}")
    r.add_argument("-n", "--repeats", type=int, default=3)
    r.add_argument("-l", "--label", help="machine label, e.g. 'x86-4c' or 'pi4' (default: hostname)")
    r.add_argument("--rows", type=int)
    r.add_argument("--ops", type=int)
    r.add_argument("--commits", type=int)
    r.add_argument("--clients", type=int)
    r.add_argument("--duration", type=float)
    r.add_argument("--dsn", help="PostgreSQL DSN (default: $DBBENCH_PG_DSN or local bench db)")
    r.add_argument("--sqlite-path", help="SQLite file path (default: temp file)")
    r.add_argument("--sqlite-sync", default="FULL", choices=["OFF", "NORMAL", "FULL"],
                   help="FULL (default) fsyncs every commit, like PostgreSQL's synchronous_commit=on")
    r.add_argument("--sqlite-cache-mb", type=int, default=128,
                   help="SQLite page cache; default 128 matches PostgreSQL's default shared_buffers")
    r.add_argument("--skip-core-check", action="store_true")
    r.add_argument("-o", "--output-dir", default="results")
    r.add_argument("--client-sweep", type=_int_list, metavar="N,N,...",
                   help="also run the multi-user workloads at each client count, e.g. 1,2,4,8,16")
    r.add_argument("--sweep-repeats", type=int, default=2, help="repeats per sweep point (default 2)")
    r.add_argument("--start-pg", action="store_true",
                   help="start a private PostgreSQL for this run (see 'dbbench pg') and stop it after")
    _pg_args(r)
    r.set_defaults(func=_cmd_run)

    g = sub.add_parser("pg", help="start/stop a private PostgreSQL server for benchmarking")
    g.add_argument("action", choices=["start", "stop", "status", "destroy"])
    _pg_args(g)
    g.set_defaults(func=_cmd_pg)

    rep = sub.add_parser("report", help="compare result files (default: results/*.json)")
    rep.add_argument("files", nargs="*")
    rep.add_argument("-f", "--format", choices=["markdown", "csv", "html"], default="markdown",
                     help="html = graphical report with charts (default file results/report.html)")
    rep.add_argument("-o", "--output", help="write to this file instead of stdout")
    rep.add_argument("--note", action="append", help="html: add a note for readers (repeatable)")
    rep.set_defaults(func=_cmd_report)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
