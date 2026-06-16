# tests/test_probe.py
from pathlib import Path

import httpx
import pytest
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
    # 未配 label 的服务,label 一律 None(向后兼容:旧 yaml 无此字段)
    assert all(t.label is None for t in targets)


LABEL_YAML = """\
nginx_base: http://nginx-proxy
services:
  - name: audio
    host: audio.dev.local
    path: /api/v1/health
    label: Audio API
  - name: auth
    host: auth.dev.local
    path: /health
  - name: audio-ui
    host: audio-ui.dev.local
    path: /
    label: ""
  - name: search
    host: search.dev.local
    path: /health
    label: "   "
"""


def test_load_targets_parses_optional_label(tmp_path):
    path = tmp_path / "services.yaml"
    path.write_text(LABEL_YAML, encoding="utf-8")
    by_name = {t.name: t for t in load_targets(str(path))}
    assert by_name["audio"].label == "Audio API"  # 显式 label
    assert by_name["auth"].label is None  # 缺省 → None
    assert by_name["audio-ui"].label is None  # 空串 → None(不当显示名,渲染回退 name)
    assert by_name["search"].label is None  # 纯空白 → None(strip 后为空,回退 name)
    # label 纯展示:不影响 name(DB key)
    assert by_name["audio"].name == "audio"


def test_load_targets_empty_file_returns_empty(tmp_path):
    # 空 services.yaml(compose bind-mount 缺文件被建成空文件,或用户清空内容):
    # safe_load -> None,不得崩,回退空清单(_load_targets_or_disable 转 vendor-only)
    path = tmp_path / "services.yaml"
    path.write_text("", encoding="utf-8")
    assert load_targets(str(path)) == []


def test_load_targets_null_services_returns_empty(tmp_path):
    # 裸 `services:`(无缩进子项)被 YAML 解析成 null:回退空清单,不得 TypeError
    path = tmp_path / "services.yaml"
    path.write_text("services:\n", encoding="utf-8")
    assert load_targets(str(path)) == []


def test_load_targets_missing_services_key_returns_empty(tmp_path):
    # 文件只有 defaults、没有 services 键:回退空清单,不得 KeyError
    path = tmp_path / "services.yaml"
    path.write_text("defaults:\n  timeout: 3.0\n", encoding="utf-8")
    assert load_targets(str(path)) == []


def test_load_targets_non_list_services_raises(tmp_path):
    # services 写成非列表标量(services: 0 / true / 字符串)= 显式类型错误,响亮失败,
    # 不静默吞成 vendor-only(空/缺/null 才降级,见上三例)
    path = tmp_path / "services.yaml"
    path.write_text("services: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="services"):
        load_targets(str(path))


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


@pytest.mark.parametrize("rel", ["services.example.yaml", "demo/services.demo.yaml"])
def test_shipped_service_lists_parse(rel):
    # 钉住随仓示例清单:用户从它起步/demo 直接挂载,坏一行就是启动 crash-loop
    repo_root = Path(__file__).parent.parent
    targets = load_targets(str(repo_root / rel))
    assert len(targets) >= 1
    names = [t.name for t in targets]
    assert len(set(names)) == len(names)  # 无重名
    for t in targets:
        assert t.name and t.url.startswith("http")
        assert t.timeout > 0
