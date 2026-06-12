# tests/test_probe.py
from pathlib import Path

import httpx
import respx

from sentinel.probe import ProbeTarget, load_targets, probe_one, run_probe_cycle
from sentinel.store import Store

YAML = """\
defaults:
  timeout: 2.0
  expect_status: 200
nginx_base: http://nginx-proxy
services:
  - name: auth
    host: auth.dev.local
    path: /health
  - name: loki-mcp
    host: loki-mcp.dev.local
    path: /mcp
    expect_status: 406
    timeout: 8.0
  - name: prometheus
    url: http://prometheus:9090/-/healthy
"""


def test_load_targets_defaults_and_overrides(tmp_path):
    path = tmp_path / "services.yaml"
    path.write_text(YAML, encoding="utf-8")
    targets = load_targets(str(path))
    assert targets[0] == ProbeTarget(
        name="auth",
        url="http://nginx-proxy/health",
        host="auth.dev.local",
        expect_status=200,
        timeout=2.0,
    )
    assert targets[1].expect_status == 406
    assert targets[1].timeout == 8.0
    assert targets[2] == ProbeTarget(
        name="prometheus",
        url="http://prometheus:9090/-/healthy",
        host=None,
        expect_status=200,
        timeout=2.0,
    )


@respx.mock
async def test_probe_one_ok_records_status_and_latency():
    respx.get("http://nginx-proxy/health").mock(return_value=httpx.Response(200))
    target = ProbeTarget(name="auth", url="http://nginx-proxy/health", host="auth.dev.local")
    async with httpx.AsyncClient() as client:
        sample = await probe_one(client, target, now_ts=1000)
    assert sample.ok is True
    assert sample.status_code == 200
    assert sample.latency_ms is not None
    assert sample.latency_ms >= 0
    assert sample.ts == 1000
    assert sample.service == "auth"


@respx.mock
async def test_probe_one_sends_host_header():
    route = respx.get("http://nginx-proxy/health").mock(return_value=httpx.Response(200))
    target = ProbeTarget(name="auth", url="http://nginx-proxy/health", host="auth.dev.local")
    async with httpx.AsyncClient() as client:
        await probe_one(client, target, now_ts=1000)
    assert route.calls[0].request.headers["host"] == "auth.dev.local"


@respx.mock
async def test_probe_one_unexpected_status_is_not_ok_but_records_latency():
    respx.get("http://nginx-proxy/health").mock(return_value=httpx.Response(500))
    target = ProbeTarget(name="auth", url="http://nginx-proxy/health", host="auth.dev.local")
    async with httpx.AsyncClient() as client:
        sample = await probe_one(client, target, now_ts=1000)
    assert sample.ok is False
    assert sample.status_code == 500
    assert sample.latency_ms is not None


@respx.mock
async def test_probe_one_expected_non_200_is_ok():
    respx.get("http://nginx-proxy/mcp").mock(return_value=httpx.Response(406))
    target = ProbeTarget(
        name="loki-mcp",
        url="http://nginx-proxy/mcp",
        host="loki-mcp.dev.local",
        expect_status=406,
    )
    async with httpx.AsyncClient() as client:
        sample = await probe_one(client, target, now_ts=1000)
    assert sample.ok is True


@respx.mock
async def test_probe_one_connect_error_is_failed_sample_not_exception():
    respx.get("http://prometheus:9090/-/healthy").mock(side_effect=httpx.ConnectError("refused"))
    target = ProbeTarget(name="prometheus", url="http://prometheus:9090/-/healthy")
    async with httpx.AsyncClient() as client:
        sample = await probe_one(client, target, now_ts=1000)
    assert sample.ok is False
    assert sample.status_code is None
    assert sample.latency_ms is None


@respx.mock
async def test_run_probe_cycle_stores_all_samples_even_with_failures(tmp_path):
    respx.get("http://nginx-proxy/health").mock(return_value=httpx.Response(200))
    respx.get("http://prometheus:9090/-/healthy").mock(side_effect=httpx.ConnectError("x"))
    targets = [
        ProbeTarget(name="auth", url="http://nginx-proxy/health", host="auth.dev.local"),
        ProbeTarget(name="prometheus", url="http://prometheus:9090/-/healthy"),
    ]
    store = Store(str(tmp_path / "s.db"))
    async with httpx.AsyncClient() as client:
        samples = await run_probe_cycle(targets, client=client, store=store, now_ts=1000)
    assert len(samples) == 2
    stored = store.get_probe_samples_since(0)
    assert {s.service: s.ok for s in stored} == {"auth": True, "prometheus": False}


def test_shipped_services_yaml_parses():
    # 钉住仓库根目录随仓清单:烤进镜像,坏一行就是容器启动 crash-loop
    repo_root = Path(__file__).parent.parent
    targets = load_targets(str(repo_root / "services.yaml"))
    assert len(targets) >= 1
    names = [t.name for t in targets]
    assert len(set(names)) == len(names)  # 无重名
    for t in targets:
        assert t.name and t.url.startswith("http")
        assert t.timeout > 0
