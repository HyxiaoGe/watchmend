import httpx
import respx

from sentinel.config import Settings
from sentinel.logql import LokiClient
from sentinel.scan_errors import run_error_scan

NOW = 1_760_000_000


def _settings(monkeypatch, **env):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://x")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def _streams(groups):
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": labels,
                        "values": [[str(NOW * 10**9), line] for line in lines],
                    }
                    for labels, lines in groups
                ],
            },
        },
    )


async def _scan(settings, response):
    with respx.mock:
        route = respx.get("http://loki.test/loki/api/v1/query_range").mock(return_value=response)
        async with httpx.AsyncClient() as client:
            findings = await run_error_scan(
                LokiClient(client, "http://loki.test"), settings, now_ts=NOW
            )
    return findings, route


async def test_distinct_signatures_are_aggregated(monkeypatch):
    settings = _settings(monkeypatch)
    lines = [
        f"[2026-06-18 07:27:{i:02d},572: ERROR/ForkPoolWorker-{i}] "
        f"Unexpected error for video 9JPiX1ZJQ_{i}"
        for i in range(10)
    ]
    findings, _ = await _scan(settings, _streams([({"container": "worker"}, lines)]))
    assert len(findings) == 1
    assert findings[0].rule == "log_error_new"
    assert findings[0].point is True
    assert findings[0].payload["count"] == 10


async def test_critical_first_and_ignore_patterns(monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_ERROR_IGNORE_PATTERNS="invalid_grant")
    groups = [
        ({"container": "api"}, ["ERROR: invalid_grant token expired"]),
        ({"container": "worker"}, ["ERROR: ordinary failure"] * 3),
        ({"container": "db"}, ["CRITICAL: database unavailable"]),
    ]
    findings, _ = await _scan(settings, _streams(groups))
    assert [finding.severity for finding in findings] == ["critical", "warning"]
    assert all("invalid_grant" not in finding.subject for finding in findings)


async def test_query_and_client_both_exclude_self(monkeypatch):
    settings = _settings(monkeypatch, SENTINEL_SELF_CONTAINER="watchmend-shadow")
    line = "ERROR: sentinel self echo"
    findings, route = await _scan(
        settings,
        _streams(
            [
                ({"container": "watchmend-shadow"}, [line]),
                ({"container": "worker"}, [line]),
            ]
        ),
    )
    assert [finding.subject.split(" · ")[0] for finding in findings] == ["worker"]
    query = route.calls.last.request.url.params["query"]
    assert 'container!="watchmend-shadow"' in query
    assert "|~" in query and "ERROR" in query
