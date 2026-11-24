from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, report, runner, sysinfo
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
    path = runner.run(
        backends=backends, profile=args.profile, overrides=overrides,
        workloads=args.workloads.split(",") if args.workloads else WORKLOADS.keys(),
        repeats=args.repeats, label=args.label, verify_cores=not args.skip_core_check,
        output_dir=Path(args.output_dir),
        dsn=args.dsn, sqlite_path=args.sqlite_path, sqlite_sync=args.sqlite_sync,
    )
    print(f"\nResults written to {path}")
    return 0


def _cmd_report(args) -> int:
    paths = [Path(p) for p in args.files]
    if not paths:
        paths = sorted(Path("results").glob("*.json"))
    if not paths:
        print("no result files found", file=sys.stderr)
        return 1
    docs = report.load(paths)
    sys.stdout.write(report.to_csv(docs) if args.format == "csv" else report.markdown(docs))
    return 0


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
    r.add_argument("--sqlite-sync", default="NORMAL", choices=["OFF", "NORMAL", "FULL"])
    r.add_argument("--skip-core-check", action="store_true")
    r.add_argument("-o", "--output-dir", default="results")
    r.set_defaults(func=_cmd_run)

    rep = sub.add_parser("report", help="compare result files (default: results/*.json)")
    rep.add_argument("files", nargs="*")
    rep.add_argument("-f", "--format", choices=["markdown", "csv"], default="markdown")
    rep.set_defaults(func=_cmd_report)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
