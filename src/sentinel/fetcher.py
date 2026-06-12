# src/sentinel/fetcher.py
from __future__ import annotations

import asyncio

import httpx


class FetchError(Exception):
    """抓取或解析状态页失败(已耗尽重试)。"""


class Fetcher:
    """异步 HTTP GET + JSON 解析;对空 body / 非 JSON / 网络错误做有界重试。"""

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

    async def get_json(self, url: str) -> object:
        last_err: Exception | None = None
        for attempt in range(self._retries):
            try:
                resp = await self._client.get(url, follow_redirects=True, timeout=self._timeout)
                resp.raise_for_status()
                if not resp.text.strip():
                    raise FetchError("empty body")
                return resp.json()
            except (httpx.HTTPError, ValueError, FetchError) as err:
                last_err = err
                if attempt < self._retries - 1 and self._backoff_base:
                    await asyncio.sleep(min(self._backoff_base * (2**attempt), 5.0))
        raise FetchError(f"{url}: {last_err}") from last_err
