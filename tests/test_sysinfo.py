from dbbench import sysinfo


def test_collect_has_core_fields():
    info = sysinfo.collect()
    assert info["arch"]
    assert info["cpu"]["logical_cores"] >= 1
    assert "is_virtual" in info["virtualization"]


def test_verify_cores_reports_scaling():
    res = sysinfo.verify_cores(max_workers=2, target_s=0.05)
    assert res["claimed_cores"] == 2
    assert [s["workers"] for s in res["scaling"]] == [1, 2]
    assert res["effective_cores"] > 0
    assert res["verdict"] in {"ok", "degraded", "oversubscribed"}
