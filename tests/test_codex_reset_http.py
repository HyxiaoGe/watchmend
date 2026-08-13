import httpx

from sentinel.codex_reset.http import ResetFetcher


async def test_source_fetch_retries_with_exponential_backoff(monkeypatch):
    attempts = 0
    sleeps = []

    async def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("sentinel.codex_reset.http.asyncio.sleep", fake_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ResetFetcher(client, retries=3, backoff_base=0.5).get_json(
            "https://source.example/reset"
        )

    assert result == {"ok": True}
    assert attempts == 3
    assert sleeps == [0.5, 1.0]


async def test_source_error_never_contains_response_body():
    marker = "sensitive-response-marker"

    async def handler(request):
        return httpx.Response(503, text=marker, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        try:
            await ResetFetcher(client, retries=1).get_json("https://source.example/reset")
        except Exception as exc:
            assert marker not in str(exc)
        else:
            raise AssertionError("expected source failure")
