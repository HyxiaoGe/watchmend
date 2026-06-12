# src/sentinel/adapters/base.py
from __future__ import annotations

from typing import Protocol, runtime_checkable

from sentinel.fetcher import Fetcher
from sentinel.models import Snapshot


class AdapterError(Exception):
    """适配器抓取/归一化失败。"""


@runtime_checkable
class StatusAdapter(Protocol):
    provider: str
    display_name: str
    track_components: bool

    async def fetch(self, fetcher: Fetcher) -> Snapshot: ...
