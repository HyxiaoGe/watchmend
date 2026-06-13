# src/sentinel/notify/ntfy.py
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from sentinel.notify.message import Notification
from sentinel.notify.render import NTFY_PRIORITY, body_text, lead_emoji, ntfy_tags


class NtfyError(Exception):
    """ntfy 投递失败(非 2xx)。"""


def _split_url(url: str) -> tuple[str, str]:
    """https://ntfy.sh/my-topic → ("https://ntfy.sh", "my-topic")。缺 scheme/host/topic 抛错。"""
    parts = urlsplit(url)
    topic = parts.path.strip("/")
    if not parts.scheme or not parts.netloc or not topic:
        raise ValueError(f"invalid ntfy url (need scheme://host/topic): {url!r}")
    server = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return server, topic


class NtfyChannel:
    name = "ntfy"

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._server, self._topic = _split_url(url)
        self._token = token
        self._timeout = timeout

    async def send(self, n: Notification) -> None:
        payload = {
            "topic": self._topic,
            "title": f"{lead_emoji(n)} {n.title}",
            "message": body_text(n) or n.title,
            "priority": NTFY_PRIORITY[n.severity],
            "tags": ntfy_tags(n),
        }
        if n.link:
            payload["click"] = n.link
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        resp = await self._client.post(
            self._server, json=payload, headers=headers, timeout=self._timeout
        )
        if resp.status_code >= 300:
            raise NtfyError(f"ntfy HTTP {resp.status_code}")
