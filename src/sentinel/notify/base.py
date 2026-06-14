# src/sentinel/notify/base.py
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from sentinel.notify.message import Notification

logger = logging.getLogger("sentinel.notify")


class Channel(Protocol):
    """出站通知渠道:name 用于日志,send 失败抛异常(由 Broadcaster 隔离)。"""

    name: str

    async def send(self, n: Notification) -> None: ...


class Broadcaster:
    """fan-out 到所有渠道:并发投递、失败相互隔离、返回成功投递数。

    永不外抛:单渠道异常逐条记日志(渠道名 + 异常),调用方据返回的成功数
    做 send-then-commit 门控(≥1 才提交)。
    """

    def __init__(self, channels: list[Channel]) -> None:
        self._channels = channels

    @property
    def channels(self) -> list[Channel]:
        return self._channels

    async def send(self, n: Notification) -> int:
        if not self._channels:
            return 0
        results = await asyncio.gather(*(c.send(n) for c in self._channels), return_exceptions=True)
        ok = 0
        for channel, res in zip(self._channels, results, strict=True):
            if isinstance(res, Exception):
                logger.error("channel %s send failed: %s", channel.name, res)
            else:
                ok += 1
        return ok
