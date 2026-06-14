# src/sentinel/notify/webhook.py
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass

import httpx

from sentinel.notify.message import Notification


class WebhookError(Exception):
    """通用 webhook 投递失败(非 2xx)。"""


def _default(o: object):
    # data 里可能有 Finding/EventRecord/Snapshot 等 domain dataclass → 展开为 dict;
    # StrEnum 是 str 子类,json 已能直接序列化,不会走到这里
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    return str(o)


def _payload(n: Notification) -> dict:
    return {
        "kind": n.kind.value,
        "severity": n.severity.value,
        "title": n.title,
        "detail": n.detail,
        "fields": [list(f) for f in n.fields],
        "subject": n.subject,
        "link": n.link,
        "ts": n.ts,
        "data": n.data,
    }


class WebhookChannel:
    name = "webhook"

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._url = url
        self._token = token
        self._timeout = timeout

    async def send(self, n: Notification) -> None:
        content = json.dumps(_payload(n), ensure_ascii=False, default=_default)
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        resp = await self._client.post(
            self._url, content=content.encode("utf-8"), headers=headers, timeout=self._timeout
        )
        if resp.status_code >= 300:
            raise WebhookError(f"webhook HTTP {resp.status_code}")
