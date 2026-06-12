import subprocess

import pytest

from host import ops_mcp


class FakeCompleted:
    def __init__(self, stdout="OK", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


@pytest.fixture
def calls(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        assert kw.get("shell") is not True
        seen.append(argv)
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_docker_ps_fixed_argv(calls):
    out = ops_mcp.docker_ps()
    assert calls == [["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}"]]
    assert out == "OK"


def test_docker_inspect_validates_name(calls):
    ops_mcp.docker_inspect("dev-ops-sentinel")
    assert calls[0][:2] == ["docker", "inspect"]
    with pytest.raises(ValueError):
        ops_mcp.docker_inspect("bad name; rm -rf /")
    with pytest.raises(ValueError):
        ops_mcp.docker_inspect("-evil")  # 不允许以 - 开头(防 argv 注入旗标)


def test_systemd_status_validates_unit(calls):
    ops_mcp.systemd_status("openclaw-gateway.service", user=True)
    assert "--user" in calls[0]
    with pytest.raises(ValueError):
        ops_mcp.systemd_status("a;b")


def test_output_truncated(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: FakeCompleted(stdout="x" * 20000))
    assert len(ops_mcp.disk_free()) <= ops_mcp._MAX_OUT


def test_failure_includes_exit_code(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: FakeCompleted(stdout="", returncode=1, stderr="denied"),
    )
    out = ops_mcp.mem_info()
    assert "exit=1" in out and "denied" in out


def test_prom_ip_cached(monkeypatch):
    n = {"calls": 0}

    def fake_run(argv, **kw):
        n["calls"] += 1
        return FakeCompleted(stdout="172.25.0.9\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ops_mcp._prom_cache.update(ts=0.0, ip="")
    assert ops_mcp._prom_ip() == "172.25.0.9"
    assert ops_mcp._prom_ip() == "172.25.0.9"
    assert n["calls"] == 1  # 60s 缓存内只解析一次
