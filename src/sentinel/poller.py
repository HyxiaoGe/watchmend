# src/sentinel/poller.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sentinel.differ import diff
from sentinel.events import EventType, TransitionEvent
from sentinel.notify.build import heartbeat_notification, vendor_incident_notification

logger = logging.getLogger("sentinel.poller")


@dataclass
class PollState:
    """跨轮持有的可变状态:每 provider 连续失败计数。"""

    fail_counts: dict[str, int] = field(default_factory=dict)


async def run_cycle(
    adapters,
    *,
    fetcher,
    store,
    broadcaster,
    state: PollState,
    verbosity: str = "phase",
    fail_threshold: int = 3,
) -> None:
    """一轮:每 provider fetch→diff→(有事件)广播→仅 ≥1 渠道成功才 commit。每家独立隔离。"""
    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    now_ts = int(now.timestamp())
    for adapter in adapters:
        provider = adapter.provider
        try:
            snapshot = await adapter.fetch(fetcher)
        except Exception as err:  # 抓取/解析失败:不动库、不误判恢复
            count = state.fail_counts.get(provider, 0) + 1
            state.fail_counts[provider] = count
            logger.warning("fetch %s failed (%d): %s", provider, count, err)
            if count == fail_threshold:  # 仅在恰好到阈值时发一次 meta
                await broadcaster.send(
                    _meta_notification(adapter.display_name, count, now_ts, now_str)
                )
            continue

        state.fail_counts[provider] = 0  # 成功则清零
        old = store.get(provider)
        events = diff(old, snapshot, verbosity=verbosity)

        if events:
            n = vendor_incident_notification(
                adapter.display_name, events, snapshot.status_url, now_ts=now_ts, now_str=now_str
            )
            if await broadcaster.send(n) < 1:
                continue  # 全渠道失败 -> 不 commit,下轮重试
        store.put(provider, snapshot)  # ≥1 成功 或 无事件 -> commit


_HEARTBEAT_KEY = "heartbeat_last_date"


def should_send_heartbeat(now_local: datetime, last_date: str | None, hour: int) -> bool:
    """到点(本地 hour 时及以后)且今天还没发过 → 该发。启动若已过点位会立即补发一张。"""
    today = now_local.strftime("%Y-%m-%d")
    return now_local.hour >= hour and last_date != today


async def run_heartbeat(
    providers: list[str], *, store, broadcaster, now_local: datetime, hour: int, interval: int
) -> None:
    """每轮调用:满足条件才发当日心跳。send-then-commit:≥1 渠道成功才记日期,失败下轮重试。"""
    if not should_send_heartbeat(now_local, store.get_meta(_HEARTBEAT_KEY), hour):
        return
    snapshots = [s for p in providers if (s := store.get(p)) is not None]
    n = heartbeat_notification(
        snapshots,
        now_ts=int(now_local.timestamp()),
        now_str=now_local.strftime("%Y-%m-%d %H:%M"),
        interval=interval,
    )
    if await broadcaster.send(n) >= 1:
        today = now_local.strftime("%Y-%m-%d")
        store.set_meta(_HEARTBEAT_KEY, today)
        logger.info("heartbeat sent for %s (%d providers)", today, len(snapshots))


def _meta_notification(display_name: str, count: int, now_ts: int, now_str: str):
    ev = TransitionEvent(
        type=EventType.FETCH_FAILED,
        provider=display_name,
        title=f"⚠️ 无法获取 {display_name} 状态页 {count} 次",
        detail="可能是网络/隧道或对方状态页本身异常。",
    )
    return vendor_incident_notification(display_name, [ev], "", now_ts=now_ts, now_str=now_str)
