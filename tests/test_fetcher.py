# tests/test_fetcher.py
import httpx
import pytest
import respx

from sentinel.fetcher import Fetcher, FetchError


@pytest.mark.asyncio
async def test_retries_on_empty_body_then_succeeds():
    route = respx.route(method="GET", url="https://x.test/s.json")
    route.side_effect = [
        httpx.Response(200, text=""),  # 空 body -> 重试
        httpx.Response(200, json={"ok": True}),  # 第二次成功
    ]
    async with respx.mock:
        async with httpx.AsyncClient() as client:
            f = Fetcher(client, retries=4, backoff_base=0)
            data = await f.get_json("https://x.test/s.json")
    assert data == {"ok": True}


@pytest.mark.asyncio
async def test_raises_after_exhausting_retries():
    with respx.mock:
        respx.get("https://x.test/s.json").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            f = Fetcher(client, retries=3, backoff_base=0)
            with pytest.raises(FetchError):
                await f.get_json("https://x.test/s.json")
