import json
import os

import pytest

from dbbench import report, runner

BACKENDS = ["sqlite"]
if os.environ.get("DBBENCH_PG_DSN"):
    BACKENDS.append("postgres")


@pytest.mark.parametrize("backend", BACKENDS)
def test_smoke_run_writes_results(tmp_path, backend):
    path = runner.run(
        backends=[backend], profile="smoke", overrides={"duration": 0.3},
        workloads=runner.WORKLOADS.keys(), repeats=1, label="ci box",
        verify_cores=False, output_dir=tmp_path, log=lambda *_: None,
    )
    doc = json.loads(path.read_text())
    assert path.name.startswith("ci-box-")
    res = doc["results"][0]
    assert res["backend"] == backend
    assert set(res["workloads"]) == set(runner.WORKLOADS)
    for w in res["workloads"].values():
        assert w["median_ops_per_s"] > 0

    md = report.markdown(report.load([path]))
    assert f"ci box / {backend}" in md
    assert "point_select" in report.to_csv(report.load([path]))


def test_profile_overrides():
    p = runner.build_params("quick", {"rows": 123, "clients": None})
    assert p.rows == 123
    assert p.clients == runner.PROFILES["quick"].clients


def test_unknown_workload_rejected(tmp_path):
    with pytest.raises(ValueError, match="nope"):
        runner.run(backends=["sqlite"], profile="smoke", overrides={}, workloads=["load", "nope"],
                   repeats=1, label="x", verify_cores=False, output_dir=tmp_path, log=lambda *_: None)


def test_failed_backend_keeps_other_results(tmp_path):
    pytest.importorskip("psycopg")
    path = runner.run(
        backends=["sqlite", "postgres"], profile="smoke", overrides={"duration": 0.2},
        workloads=["load", "point_select"], repeats=1, label="half", verify_cores=False,
        output_dir=tmp_path, log=lambda *_: None,
        dsn="postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=2",
    )
    doc = json.loads(path.read_text())
    sqlite_res, pg_res = doc["results"]
    assert sqlite_res["workloads"]["point_select"]["median_ops_per_s"] > 0
    assert pg_res["error"]
    md = report.markdown(report.load([path]))
    assert "Failed runs" in md and "half / sqlite" in md
    assert "postgres" in report.to_csv(report.load([path]))


def test_concurrent_reads_uses_processes_and_counts_all_ops(tmp_path):
    from dbbench.backends import get_backend
    from dbbench.workloads import Params, concurrent_reads, load

    be = get_backend("sqlite", sqlite_path=str(tmp_path / "c.db"))
    p = Params(rows=500, clients=3, duration=0.5)
    try:
        load(be, p)
        ops, wall, lat, extra = concurrent_reads(be, p)
    finally:
        be.cleanup()
    assert ops > 0 and len(lat) <= ops and wall >= 0.5 and extra["clients"] == 3


def test_concurrent_reads_reports_client_errors(tmp_path):
    from dbbench.backends import get_backend
    from dbbench.workloads import Params, concurrent_reads

    be = get_backend("sqlite", sqlite_path=str(tmp_path / "empty.db"))  # no table: queries fail
    try:
        with pytest.raises(RuntimeError, match="concurrent_reads failed"):
            concurrent_reads(be, Params(rows=10, clients=2, duration=0.2))
    finally:
        be.cleanup()


def test_report_disambiguates_repeated_labels(tmp_path):
    paths = []
    for i in range(2):
        out = tmp_path / str(i)
        paths.append(runner.run(backends=["sqlite"], profile="smoke", overrides={"duration": 0.1},
                                workloads=["load"], repeats=1, label="same", verify_cores=False,
                                output_dir=out, log=lambda *_: None))
    docs = report.load(paths)
    docs[1]["timestamp"] = "2099-01-01T00:00:00+00:00"
    cols, _, _ = report.throughput_matrix(docs)
    assert len(set(cols)) == 2


def test_private_postgres_start_stop(tmp_path):
    pytest.importorskip("psycopg")
    from dbbench import pglocal

    try:
        pglocal.find_bindir()
    except RuntimeError:
        pytest.skip("no PostgreSQL binaries on this machine")
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    d = tmp_path / "pg"
    try:
        dsn = pglocal.start(d, port, log=lambda *_: None)
        assert pglocal.status(d)
        path = runner.run(backends=["postgres"], profile="smoke", overrides={"duration": 0.2},
                          workloads=["load", "point_select", "concurrent_reads"], repeats=1,
                          label="pgl", verify_cores=False, output_dir=tmp_path, log=lambda *_: None,
                          dsn=dsn)
        res = json.loads(path.read_text())["results"][0]
        assert not res.get("error") and res["workloads"]["concurrent_reads"]["median_ops_per_s"] > 0
    finally:
        pglocal.destroy(d, log=lambda *_: None)
    assert not d.exists()


def test_client_sweep_and_html_report(tmp_path):
    from dbbench import htmlreport

    path = runner.run(backends=["sqlite"], profile="smoke", overrides={"duration": 0.3},
                      workloads=["load", "concurrent_mixed", "concurrent_writes"], repeats=1,
                      label="sweep", verify_cores=False, output_dir=tmp_path, log=lambda *_: None,
                      client_sweep=[1, 3], sweep_repeats=1)
    res = json.loads(path.read_text())["results"][0]
    sweep = res["client_sweep"]
    assert set(sweep) == {"concurrent_mixed", "concurrent_writes"}
    assert set(sweep["concurrent_writes"]) == {"1", "3"}
    assert sweep["concurrent_writes"]["3"]["runs"][0]["writes"] > 0
    page = htmlreport.render(report.load([path]))
    assert "<svg" in page and "Multi-user" in page and "concurrent_writes" in page
