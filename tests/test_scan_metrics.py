# tests/test_scan_metrics.py
import httpx
import pytest
import respx

from sentinel import scan_metrics
from sentinel.promql import PromClient
from sentinel.scan_metrics import run_metrics_scan


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_MIDDLEWARE_METRICS", "pg_up:postgres,redis_up:redis")
    from sentinel.config import Settings

    return Settings(_env_file=None)


# 配置了 pg_up/redis_up 时的中间件查询(动态拼接,与旧硬编码常量等价)
_MW_QUERY = scan_metrics._middleware_query(["pg_up", "redis_up"])


def _vector(items):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": m, "value": [0, str(v)]} for m, v in items],
        },
    }


# "必然有数据"的 4 个查询(disk/mem/swap/middleware)的健康桩;resets/oom 默认空=无命中
_HEALTHY = {
    scan_metrics._DISK_QUERY: [({}, 0.3)],
    scan_metrics._MEM_QUERY: [({"name": "idle"}, 0.1)],
    scan_metrics._SWAP_QUERY: [({}, 0.3)],
    _MW_QUERY: [({"__name__": "pg_up"}, 1), ({"__name__": "redis_up"}, 1)],
}


def _mock_prom(overrides):
    """overrides: {查询常量: [(labels, value), ...]}（显式给 [] 模拟空结果）。"""
    responses = {**_HEALTHY, **overrides}

    def handler(request):
        q = request.url.params["query"]
        if q in responses:
            return httpx.Response(200, json=_vector(responses[q]))
        return httpx.Response(200, json=_vector([]))  # resets/oom 默认空=无命中

    respx.get("http://prom.test/api/v1/query").mock(side_effect=handler)


async def _scan(settings, responses):
    with respx.mock:
        _mock_prom(responses)
        async with httpx.AsyncClient() as client:
            return await run_metrics_scan(PromClient(client, "http://prom.test"), settings)


async def test_all_quiet(settings):
    assert await _scan(settings, {}) == []


async def test_disk_over_threshold_fires_critical(settings):
    findings = await _scan(settings, {scan_metrics._DISK_QUERY: [({}, 0.9)]})
    assert [(f.rule, f.subject, f.severity) for f in findings] == [("disk_usage", "/", "critical")]
    assert "90.0%" in findings[0].detail


async def test_disk_under_threshold_quiet(settings):
    assert await _scan(settings, {scan_metrics._DISK_QUERY: [({}, 0.5)]}) == []


async def test_container_mem_sustained_fires(settings):
    findings = await _scan(settings, {scan_metrics._MEM_QUERY: [({"name": "loki"}, 0.95)]})
    assert [(f.rule, f.subject) for f in findings] == [("mem_pressure", "loki")]
    assert findings[0].needs_diagnosis and not findings[0].point


async def test_swap_fires(settings):
    findings = await _scan(settings, {scan_metrics._SWAP_QUERY: [({}, 0.85)]})
    assert [(f.rule, f.subject) for f in findings] == [("mem_pressure", "swap")]
    assert "85.0%" in findings[0].detail


async def test_restart_fires_point_event(settings):
    findings = await _scan(settings, {scan_metrics._RESTART_QUERY: [({"name": "auth-service"}, 2)]})
    assert [(f.rule, f.subject) for f in findings] == [("container_restart", "auth-service")]
    assert findings[0].point and findings[0].needs_diagnosis
    assert "重启 2 次" in findings[0].detail


async def test_restart_and_oom_merge_into_one_finding(settings):
    findings = await _scan(
        settings,
        {
            scan_metrics._RESTART_QUERY: [({"name": "dozzle"}, 1)],
            scan_metrics._OOM_QUERY: [({"name": "dozzle"}, 1)],
        },
    )
    assert len(findings) == 1
    assert "OOM" in findings[0].detail  # OOM 信息覆盖普通重启


async def test_restart_storm_aggregates_into_host_event(settings):
    many = [({"name": f"svc{i}"}, 1) for i in range(5)]
    findings = await _scan(settings, {scan_metrics._RESTART_QUERY: many})
    assert [(f.rule, f.subject) for f in findings] == [("container_restart", "host")]
    assert "5 个容器" in findings[0].detail
    assert findings[0].point


async def test_middleware_down_fires_critical(settings):
    findings = await _scan(
        settings,
        {
            _MW_QUERY: [
                ({"__name__": "pg_up"}, 0),
                ({"__name__": "redis_up"}, 1),
            ]
        },
    )
    assert [(f.rule, f.subject, f.severity) for f in findings] == [
        ("middleware_down", "postgres", "critical")
    ]
    assert findings[0].needs_diagnosis


async def test_empty_mandatory_query_treated_as_datasource_failure(settings):
    # node-exporter/cAdvisor 挂而 Prometheus 正常 → 查询"成功+空" ≠ 指标恢复正常
    with pytest.raises(RuntimeError, match="exporter"):
        await _scan(settings, {scan_metrics._DISK_QUERY: []})


async def test_query_failure_raises(settings):
    with respx.mock:
        respx.get("http://prom.test/api/v1/query").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await run_metrics_scan(PromClient(client, "http://prom.test"), settings)


async def test_mem_infinite_ratio_skipped(settings):
    # 无 limit 容器:limit=0 → working_set/0=+Inf,不是内存压力
    findings = await _scan(settings, {scan_metrics._MEM_QUERY: [({"name": "tmp"}, float("inf"))]})
    assert findings == []


async def test_swap_nan_skipped(settings):
    # 无 swap 主机:0/0=NaN
    findings = await _scan(settings, {scan_metrics._SWAP_QUERY: [({}, float("nan"))]})
    assert findings == []


async def test_middleware_partial_coverage_raises(settings):
    # 单个 exporter 静默:整体查询非空但缺 metric,必须当数据源故障而非"无异常"
    with pytest.raises(RuntimeError, match="redis_up"):
        await _scan(settings, {_MW_QUERY: [({"__name__": "pg_up"}, 1)]})


async def test_middleware_unconfigured_skipped(monkeypatch):
    # 默认 SENTINEL_MIDDLEWARE_METRICS 为空:不发中间件查询、不因空结果误报数据源故障
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    from sentinel.config import Settings

    default_settings = Settings(_env_file=None)
    assert default_settings.middleware_subjects == {}
    findings = await _scan(default_settings, {_MW_QUERY: []})  # 空结果也不该被查询到
    assert findings == []


def test_middleware_subjects_parsing(settings):
    # "metric:展示名" 解析;省略展示名时用 metric 本身
    assert settings.middleware_subjects == {"pg_up": "postgres", "redis_up": "redis"}
    from sentinel.config import Settings

    s = Settings(_env_file=None, sentinel_middleware_metrics="mysql_up, mongo_up:mongodb")
    assert s.middleware_subjects == {"mysql_up": "mysql_up", "mongo_up": "mongodb"}
