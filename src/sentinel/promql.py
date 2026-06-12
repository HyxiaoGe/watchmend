# src/sentinel/promql.py
from __future__ import annotations

import httpx


class PromClient:
    """Prometheus HTTP API 极简客户端:只用即时查询,返回 (labels, value) 列表。
    查询失败抛异常,由调用方决定跳过本轮(不评估≠恢复)。"""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def query(self, promql: str) -> list[tuple[dict, float]]:
        resp = await self._client.get(
            f"{self._base}/api/v1/query", params={"query": promql}, timeout=15.0
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"prometheus query failed: {body.get('error', 'unknown')}")
        data = body["data"]
        if data.get("resultType") != "vector":
            raise RuntimeError(f"unexpected prometheus result type: {data.get('resultType')}")
        return [(item["metric"], float(item["value"][1])) for item in data["result"]]
