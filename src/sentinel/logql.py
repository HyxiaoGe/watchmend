# src/sentinel/logql.py
from __future__ import annotations

import httpx


class LokiClient:
    """Loki HTTP API 极简客户端:即时聚合查询。at_ts 可指定历史时刻(秒),
    供"七日同时段基线"在 now-1d..now-7d 上重放同一查询。"""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def query(self, logql: str, *, at_ts: int) -> list[tuple[dict, float]]:
        resp = await self._client.get(
            f"{self._base}/loki/api/v1/query",
            params={"query": logql, "time": str(at_ts * 1_000_000_000)},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"loki query failed: {body}")
        data = body["data"]
        if data.get("resultType") != "vector":
            raise RuntimeError(f"unexpected loki result type: {data.get('resultType')}")
        return [(item["metric"], float(item["value"][1])) for item in data["result"]]
