# tests/test_logql.py
import httpx
import pytest
import respx

from sentinel.logql import LokiClient


def _vector(items):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": m, "value": [1760000000, str(v)]} for m, v in items],
        },
    }


@respx.mock
async def test_query_sends_nanosecond_time_and_parses():
    route = respx.get("http://loki.test/loki/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector([({"container": "auth-service"}, 12)]))
    )
    async with httpx.AsyncClient() as client:
        loki = LokiClient(client, "http://loki.test")
        result = await loki.query("q", at_ts=1700000000)
    assert result == [({"container": "auth-service"}, 12.0)]
    assert route.calls.last.request.url.params["time"] == str(1700000000 * 10**9)


@respx.mock
async def test_query_rejects_non_vector():
    respx.get("http://loki.test/loki/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "streams", "result": []}}
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="streams"):
            await LokiClient(client, "http://loki.test").query("q", at_ts=1)


@respx.mock
async def test_query_raises_on_api_error():
    respx.get("http://loki.test/loki/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "error", "error": "parse error"})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="parse error"):
            await LokiClient(client, "http://loki.test").query("q", at_ts=1)


@respx.mock
async def test_query_raises_on_http_error():
    respx.get("http://loki.test/loki/api/v1/query").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await LokiClient(client, "http://loki.test").query("q", at_ts=1)
