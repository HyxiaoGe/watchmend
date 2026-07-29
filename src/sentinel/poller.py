# src/sentinel/poller.py
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sentinel.differ import diff
from sentinel.events import EventType, TransitionEvent
from sentinel.models import Indicator
from sentinel.notify.build import heartbeat_notification, vendor_incident_notification
from sentinel.status_editor import (
    StatusAnalysis,
    StatusEditor,
    StatusEditorError,
    events_to_dict,
)

logger = logging.getLogger("sentinel.poller")


@dataclass
class PollState:
    """跨轮持有的可变状态:每 provider 连续失败计数 + 已成功送达 meta 的 provider 集。"""

    fail_counts: dict[str, int] = field(default_factory=dict)
    # 已送达 meta 卡的 provider:达阈值后若全渠道宕,meta 未送达就不入此集,下轮继续重试;
    # 送达一次即记下不再重复,抓取恢复时清除以便下次故障重新武装。
    meta_sent: set[str] = field(default_factory=set)
    last_card_ts: dict[str, float] = field(default_factory=dict)


_URGENT_IMPACTS = {Indicator.MAJOR, Indicator.CRITICAL}


def _is_urgent(events: list[TransitionEvent]) -> bool:
    return any(
        event.impact in _URGENT_IMPACTS
        or "partial_outage" in event.detail
        or "major_outage" in event.detail
        for event in events
    )


async def _analyze(
    editor: StatusEditor | None,
    mode: str,
    *,
    snapshot,
    events,
    store,
) -> StatusAnalysis | None:
    if editor is None or mode == "off":
        return None
    try:
        analysis = await editor.analyze(snapshot, events)
    except StatusEditorError:
        logger.exception("status editor failed for %s", snapshot.provider)
        return None
    except Exception:
        logger.exception("unexpected status editor failure for %s", snapshot.provider)
        return None
    try:
        store.record_llm_analysis(
            ts=int(time.time()),
            provider=snapshot.provider,
            mode=mode,
            model=editor.model,
            events_json=json.dumps(events_to_dict(events), ensure_ascii=False),
            analysis_json=analysis.model_dump_json(),
        )
    except Exception:
        # gate 模式不能在审计失败时静默消息：回退原规则卡片。
        logger.exception("status editor audit failed for %s", snapshot.provider)
        return None
    return analysis


async def run_cycle(
    adapters,
    *,
    fetcher,
    store,
    broadcaster,
    state: PollState,
    verbosity: str = "phase",
    fail_threshold: int = 3,
    card_min_gap_s: int = 600,
    status_editor: StatusEditor | None = None,
    editor_mode: str = "off",
) -> None:
    """一轮抓取和差异检测；非紧急事件按 provider 限速，编辑器失败时安全回退原消息。"""
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
            # 达阈值且尚未送达过 meta:尝试发,仅 ≥1 渠道成功才记为已送达,
            # 否则(如阈值那一刻全渠道宕)下轮继续重试,不丢卡。
            if count >= fail_threshold and provider not in state.meta_sent:
                sent = await broadcaster.send(
                    _meta_notification(adapter.display_name, count, now_ts, now_str)
                )
                if sent >= 1:
                    state.meta_sent.add(provider)
            continue

        state.fail_counts[provider] = 0  # 成功则清零
        state.meta_sent.discard(provider)  # 抓取恢复 → meta 重新武装
        old = store.get(provider)
        events = diff(old, snapshot, verbosity=verbosity)

        if events:
            since_last = time.monotonic() - state.last_card_ts.get(provider, -float(card_min_gap_s))
            if since_last < card_min_gap_s and not _is_urgent(events):
                logger.info(
                    "provider card for %s held by min-gap (%.0fs/%ds, %d events pending)",
                    provider,
                    since_last,
                    card_min_gap_s,
                    len(events),
                )
                continue
            analysis = await _analyze(
                status_editor,
                editor_mode,
                snapshot=snapshot,
                events=events,
                store=store,
            )
            urgent = _is_urgent(events)
            if (
                editor_mode == "gate"
                and analysis is not None
                and analysis.decision == "suppress"
                and not urgent
            ):
                logger.info(
                    "status editor suppressed provider card for %s (confidence=%.2f)",
                    provider,
                    analysis.confidence,
                )
                store.put(provider, snapshot)
                continue
            n = vendor_incident_notification(
                adapter.display_name,
                events,
                snapshot.status_url,
                now_ts=now_ts,
                now_str=now_str,
                analysis=analysis if editor_mode in ("enrich", "gate") else None,
                editor_model=status_editor.model if status_editor is not None else "",
            )
            if await broadcaster.send(n) < 1:
                continue  # 全渠道失败 -> 不 commit,下轮重试
            state.last_card_ts[provider] = time.monotonic()
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
