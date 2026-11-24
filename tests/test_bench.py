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
