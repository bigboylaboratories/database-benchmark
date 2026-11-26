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


def test_cgroup_v2_nested_limit(monkeypatch):
    files = {
        "/proc/self/cgroup": "0::/system.slice/qemu.scope",
        "/sys/fs/cgroup/system.slice/qemu.scope/cpu.max": "200000 100000",
        "/sys/fs/cgroup/system.slice/cpu.max": "max 100000",
        "/sys/fs/cgroup/cpu.max": None,
    }
    monkeypatch.setattr(sysinfo, "_read", lambda p: files.get(p))
    assert sysinfo._cgroup_cpu_limit() == 2.0


def test_cgroup_unlimited(monkeypatch):
    files = {"/proc/self/cgroup": "0::/", "/sys/fs/cgroup/cpu.max": "max 100000"}
    monkeypatch.setattr(sysinfo, "_read", lambda p: files.get(p))
    assert sysinfo._cgroup_cpu_limit() is None


def test_raspberry_pi_detected(monkeypatch):
    files = {"/proc/device-tree/model": "Raspberry Pi 5 Model B Rev 1.0"}
    monkeypatch.setattr(sysinfo, "_read", lambda p: files.get(p))
    monkeypatch.setattr(sysinfo, "_run", lambda cmd: "none")
    monkeypatch.setattr(sysinfo.os.path, "exists", lambda p: False)
    v = sysinfo._virtualization({"hypervisor_flag": False})
    assert v["raspberry_pi"] and not v["is_virtual"]
