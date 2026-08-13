from __future__ import annotations

import asyncio

import httpx


class ResetFetchError(Exception):
    """公开 reset 来源抓取失败；异常不得包含响应正文。"""


class ResetFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        retries: int = 4,
        timeout: float = 15.0,
        backoff_base: float = 0.5,
    ) -> None:
        self._client = client
        self._retries = retries
        self._timeout = timeout
        self._backoff_base = backoff_base

    async def _get(self, url: str) -> httpx.Response:
        last_error = "unknown"
        for attempt in range(self._retries):
            try:
                response = await self._client.get(url, follow_redirects=True, timeout=self._timeout)
                response.raise_for_status()
                if not response.content:
                    raise ResetFetchError("empty response")
                return response
            except (httpx.HTTPError, ResetFetchError) as exc:
                last_error = type(exc).__name__
                if attempt < self._retries - 1:
                    await asyncio.sleep(min(self._backoff_base * (2**attempt), 5.0))
        raise ResetFetchError(f"source request failed: {last_error}")

    async def get_json(self, url: str) -> object:
        response = await self._get(url)
        try:
            return response.json()
        except ValueError as exc:
            raise ResetFetchError("source returned invalid JSON") from exc

    async def get_text(self, url: str) -> str:
        response = await self._get(url)
        if not response.text.strip():
            raise ResetFetchError("source returned empty text")
        return response.text
