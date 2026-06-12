# src/sentinel/feishu/client.py
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time

import httpx


class FeishuError(Exception):
    """飞书投递失败(HTTP 错误或 code != 0)。"""


def gen_sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人签名:key = f'{ts}\\n{secret}',对空消息做 HMAC-SHA256 再 base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuClient:
    """签名 + POST + 解析 code==0 + ≤5/秒节流。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        webhook: str,
        *,
        secret: str | None = None,
        min_interval: float = 0.25,
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._webhook = webhook
        self._secret = secret
        self._min_interval = min_interval
        self._timeout = timeout
        self._last_sent = 0.0

    async def send(self, card: dict) -> None:
        body = dict(card)  # card 已含 msg_type + card
        if self._secret:
            ts = int(time.time())  # 秒级
            body["timestamp"] = str(ts)
            body["sign"] = gen_sign(self._secret, ts)

        await self._throttle()
        resp = await self._client.post(self._webhook, json=body, timeout=self._timeout)
        try:
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as err:
            raise FeishuError(f"HTTP/解析失败: {err}") from err

        code = data.get("code", data.get("StatusCode", -1))
        if code != 0:  # HTTP 200 ≠ 送达,必须查 body code
            msg = data.get("msg") or data.get("StatusMessage")
            raise FeishuError(f"飞书拒绝 code={code} msg={msg}")

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        gap = time.monotonic() - self._last_sent
        if gap < self._min_interval:
            await asyncio.sleep(self._min_interval - gap)
        self._last_sent = time.monotonic()
