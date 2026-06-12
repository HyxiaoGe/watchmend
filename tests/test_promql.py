# tests/test_promql.py
import httpx
import pytest
import respx

from sentinel.promql import PromClient

_VECTOR = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [{"metric": {"name": "loki"}, "value": [1760000000, "0.56"]}],
    },
}


@respx.mock
async def test_query_parses_vector():
    route = respx.get("http://prom.test/api/v1/query").mock(
        return_value=httpx.Response(200, json=_VECTOR)
    )
    async with httpx.AsyncClient() as client:
        prom = PromClient(client, "http://prom.test/")  # 尾斜杠应被剥掉
        result = await prom.query("up")
    assert result == [({"name": "loki"}, 0.56)]
    assert route.calls.last.request.url.params["query"] == "up"


@respx.mock
async def test_query_raises_on_api_error():
    respx.get("http://prom.test/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "error", "error": "bad query"})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="bad query"):
            await PromClient(client, "http://prom.test").query("up")


@respx.mock
async def test_query_raises_on_http_error():
    respx.get("http://prom.test/api/v1/query").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await PromClient(client, "http://prom.test").query("up")


@respx.mock
async def test_query_rejects_non_vector():
    respx.get("http://prom.test/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="matrix"):
            await PromClient(client, "http://prom.test").query("up[5m]")
