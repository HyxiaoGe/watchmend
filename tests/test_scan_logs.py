# tests/test_scan_logs.py
import httpx
import pytest
import respx

from sentinel.logql import LokiClient
from sentinel.scan_logs import run_log_scan

NOW = 1_760_000_000


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    from sentinel.config import Settings

    return Settings(_env_file=None)


def _resp(items):
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"container": c}, "value": [0, str(v)]} for c, v in items],
            },
        },
    )


async def _scan(settings, responses):
    """responses: 按调用顺序的应答列表(第 1 个=当前窗口,后 7 个=基线)。"""
    with respx.mock:
        route = respx.get("http://loki.test/loki/api/v1/query").mock(side_effect=responses)
        async with httpx.AsyncClient() as client:
            findings = await run_log_scan(
                LokiClient(client, "http://loki.test"), settings, now_ts=NOW
            )
        return findings, route


async def test_no_errors_short_circuits_single_query(settings):
    findings, route = await _scan(settings, [_resp([])])
    assert findings == []
    assert route.call_count == 1  # 当前窗口为空,不再查 7 天基线


async def test_spike_over_baseline_fires(settings):
    responses = [_resp([("app", 40)])] + [_resp([("app", 1)])] * 7
    findings, route = await _scan(settings, responses)
    assert route.call_count == 8
    assert [(f.rule, f.subject) for f in findings] == [("log_error_spike", "app")]
    assert findings[0].needs_diagnosis
    assert "40" in findings[0].detail and "1.0" in findings[0].detail


async def test_below_absolute_floor_quiet(settings):
    responses = [_resp([("app", 9)])] + [_resp([])] * 7
    findings, _ = await _scan(settings, responses)
    assert findings == []  # 9 < 绝对下限 10,基线为 0 也不报


async def test_within_baseline_quiet(settings):
    responses = [_resp([("app", 30)])] + [_resp([("app", 20)])] * 7
    findings, _ = await _scan(settings, responses)
    assert findings == []  # 30 < 3×20


async def test_new_container_missing_baseline_counts_zero(settings):
    responses = [_resp([("newsvc", 11)])] + [_resp([])] * 7
    findings, _ = await _scan(settings, responses)
    assert [(f.rule, f.subject) for f in findings] == [("log_error_spike", "newsvc")]


async def test_below_floor_short_circuits_baseline_queries(settings):
    # 有错误但全部低于绝对下限:不发基线查询(也顺带减少对 Loki 的查询注入)
    findings, route = await _scan(settings, [_resp([("app", 9), ("web", 3)])])
    assert findings == []
    assert route.call_count == 1


async def test_mixed_containers_only_spiking_one_fires(settings):
    responses = [_resp([("app", 40), ("web", 30)])] + [_resp([("app", 1), ("web", 20)])] * 7
    findings, _ = await _scan(settings, responses)
    # app: 40>10 且 40>3×1 触发;web: 30>10 但 30<3×20 静默
    assert [(f.rule, f.subject) for f in findings] == [("log_error_spike", "app")]


async def test_exactly_at_floor_quiet(settings):
    # 严格大于:恰好等于下限 10 不报
    findings, route = await _scan(settings, [_resp([("app", 10)])])
    assert findings == []
    assert route.call_count == 1
