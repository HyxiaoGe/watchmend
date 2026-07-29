# src/sentinel/report.py
from __future__ import annotations

import logging
import math
from datetime import datetime

from sentinel.models import ProbeSample, ServiceDayStats
from sentinel.notify.build import digest_notification, report_notification
from sentinel.poller import should_send_heartbeat
from sentinel.store import Store

logger = logging.getLogger("sentinel")

_REPORT_KEY = "daily_report_last_date"
_EVENING_DIGEST_KEY = "evening_digest_last_date"
WINDOW_SECONDS = 24 * 3600


def percentile(values: list[float], pct: float) -> float | None:
    """最近秩(nearest-rank)百分位;空列表返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return ordered[k]


def aggregate_window(samples: list[ProbeSample], services: list[str]) -> list[ServiceDayStats]:
    """按服务聚合窗口样本。配置内无样本的服务产出 total=0 行(探针全断时日报仍能暴露);
    窗口内出现但已不在配置的服务也保留。延迟统计只取 ok 样本。"""
    by_service: dict[str, list[ProbeSample]] = {name: [] for name in services}
    for s in samples:
        by_service.setdefault(s.service, []).append(s)
    stats = []
    for name, group in by_service.items():
        latencies = [s.latency_ms for s in group if s.ok and s.latency_ms is not None]
        stats.append(
            ServiceDayStats(
                service=name,
                total=len(group),
                ok_count=sum(1 for s in group if s.ok),
                p50_ms=percentile(latencies, 50),
                p95_ms=percentile(latencies, 95),
            )
        )
    return stats


def build_daily_stats(
    store: Store, services: list[str], *, now_ts: int, date_str: str
) -> list[ServiceDayStats]:
    samples = store.get_probe_samples_since(now_ts - WINDOW_SECONDS)
    stats = aggregate_window(samples, services)
    for st in stats:
        p95s = store.get_recent_daily_p95s(st.service, before_date=date_str)
        st.baseline_p95_ms = sum(p95s) / len(p95s) if p95s else None
    return stats


async def run_daily_report(
    *,
    store: Store,
    broadcaster,
    services: list[str],
    now_local: datetime,
    hour: int,
    retention_days: int,
) -> bool:
    """到点发体检日报。send-then-commit:≥1 渠道成功才记 meta/落 probe_daily/清理过期样本;
    全渠道失败则不 commit、返回 False,下一分钟门控重试。返回是否已发送。"""
    last = store.get_meta(_REPORT_KEY)
    if not should_send_heartbeat(now_local, last, hour):  # 纯日期门控,与心跳同一语义
        return False
    date_str = now_local.date().isoformat()
    now_ts = int(now_local.timestamp())
    stats = build_daily_stats(store, services, now_ts=now_ts, date_str=date_str)
    digest_items = store.get_pending_digest_items()
    n = report_notification(
        stats,
        date_str=date_str,
        now_str=now_local.strftime("%Y-%m-%d %H:%M:%S"),
        now_ts=now_ts,
        open_events=store.get_open_events(),
        resolved_24h=store.count_resolved_since(now_ts - WINDOW_SECONDS),
        digest_items=digest_items,
    )
    if await broadcaster.send(n) < 1:
        logger.warning("daily report not delivered to any channel; retrying next minute")
        return False
    store.mark_digest_items_sent([item.id for item in digest_items], sent_ts=now_ts)
    store.set_meta(_REPORT_KEY, date_str)
    for st in stats:
        store.upsert_probe_daily(
            st.service,
            date_str,
            total=st.total,
            ok_count=st.ok_count,
            p50=st.p50_ms,
            p95=st.p95_ms,
        )
    deleted = store.prune_probe_samples(now_ts - retention_days * 86400)
    if deleted:
        logger.info("pruned %d probe samples", deleted)
    return True


async def run_evening_digest(
    *,
    store: Store,
    broadcaster,
    now_local: datetime,
    hour: int,
) -> bool:
    """到点且有内容时发送非紧急事件摘要；空窗口只提交日期门控。"""
    last = store.get_meta(_EVENING_DIGEST_KEY)
    if not should_send_heartbeat(now_local, last, hour):
        return False
    date_str = now_local.date().isoformat()
    now_ts = int(now_local.timestamp())
    items = store.get_pending_digest_items()
    if not items:
        store.set_meta(_EVENING_DIGEST_KEY, date_str)
        return False
    notification = digest_notification(
        items,
        window_label=f"{hour:02d}:00 前",
        now_ts=now_ts,
        now_str=now_local.strftime("%Y-%m-%d %H:%M:%S"),
    )
    if await broadcaster.send(notification) < 1:
        logger.warning("digest not delivered to any channel; retrying next minute")
        return False
    store.mark_digest_items_sent([item.id for item in items], sent_ts=now_ts)
    store.set_meta(_EVENING_DIGEST_KEY, date_str)
    return True


def report_due(store: Store, now_local: datetime, hour: int) -> bool:
    """体检日报(及随行 hygiene)是否到点未发,与 run_daily_report 内部门控同一口径。
    app 层用它决定要不要先跑 hygiene,避免 hygiene 每分钟空转。"""
    return should_send_heartbeat(now_local, store.get_meta(_REPORT_KEY), hour)
