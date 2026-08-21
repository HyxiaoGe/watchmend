from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sentinel.codex_reset.http import ResetFetcher
from sentinel.codex_reset.models import ResetEvidence, ResetStage, ResetType
from sentinel.codex_reset.notify import codex_reset_notification
from sentinel.codex_reset.sources import default_sources
from sentinel.codex_reset.store import ResetStore

logger = logging.getLogger("sentinel.codex_reset")


class ResetMonitor:
    def __init__(
        self,
        *,
        settings,
        client,
        broadcaster,
        store: ResetStore | None = None,
        sources=None,
        clock=time.time,
        owner: str | None = None,
    ) -> None:
        self._settings = settings
        self._broadcaster = broadcaster
        self._store = store or ResetStore(settings.sentinel_db_path)
        self._sources = list(sources if sources is not None else default_sources(settings))
        self._fetcher = ResetFetcher(client)
        self._clock = clock
        self._owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._lease_name = "codex-reset-monitor"

    @property
    def store(self) -> ResetStore:
        return self._store

    async def tick(self) -> None:
        now_ts = int(self._clock())
        if not self._store.acquire_lease(
            self._lease_name,
            self._owner,
            now_ts=now_ts,
            ttl_seconds=self._settings.sentinel_codex_reset_lease_seconds,
        ):
            logger.info("codex reset tick skipped: lease held by another instance")
            return

        primary = [source for source in self._sources if source.name != "reset_html"]
        fallback = next((source for source in self._sources if source.name == "reset_html"), None)
        fetched = await self._fetch_sources(primary, now_ts=now_ts)
        evidence = [item for result in fetched for item in result.evidence]
        recent_confirmation = any(
            item.signal_stage is ResetStage.CONFIRMED
            and self._recent_enough(item.observed_at, now_ts)
            for item in evidence
        )
        if (
            fallback is not None
            and (len(fetched) < 2 or recent_confirmation)
            and self._store.source_due(
                fallback.name,
                now_ts=now_ts,
                interval_seconds=self._settings.sentinel_codex_reset_html_poll_seconds,
            )
        ):
            fetched.extend(await self._fetch_sources([fallback], now_ts=now_ts))
            evidence = [item for result in fetched for item in result.evidence]
        aliases = {
            f"x:{item.source_item_id}": item.canonical_hint
            for item in evidence
            if item.canonical_hint != f"x:{item.source_item_id}"
        }
        evidence = [
            replace(item, canonical_hint=aliases.get(item.canonical_hint, item.canonical_hint))
            for item in evidence
        ]
        preliminary: set[str] = set()
        # 先入预告，再关联落地证据；这样同轮首次观察到的公告窗口也能接住新确认帖。
        evidence.sort(key=lambda item: item.signal_stage is ResetStage.CONFIRMED)
        for item in (item for item in evidence if item.signal_stage is not ResetStage.CONFIRMED):
            canonical_id = self._canonical_target(item)
            if canonical_id is None:
                continue
            self._store.put_evidence(canonical_id, item, now_ts=now_ts)
            preliminary.add(canonical_id)

        for canonical_id in preliminary:
            self._advance_event(canonical_id, now_ts=now_ts)
        confirmed: set[str] = set()
        for item in (item for item in evidence if item.signal_stage is ResetStage.CONFIRMED):
            canonical_id = self._canonical_target(item)
            if canonical_id is None:
                continue
            self._store.put_evidence(canonical_id, item, now_ts=now_ts)
            confirmed.add(canonical_id)
        for canonical_id in confirmed:
            self._advance_event(canonical_id, now_ts=now_ts)
        for event in self._store.mark_delayed(
            now_ts=now_ts,
            grace_seconds=self._settings.sentinel_codex_reset_delay_grace_seconds,
        ):
            if self._recent_enough(event.expected_end_ts or event.announced_ts, now_ts):
                self._store.queue_delivery(event.canonical_id, ResetStage.DELAYED, now_ts=now_ts)

        await self._dispatch_due(now_ts)

    async def _fetch_sources(self, sources, *, now_ts: int) -> list:
        results = await asyncio.gather(
            *(source.fetch(self._fetcher) for source in sources),
            return_exceptions=True,
        )
        fetched = []
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, Exception):
                error_name = type(result).__name__
                self._store.record_source_failure(
                    source.name,
                    source.family,
                    now_ts=now_ts,
                    error=error_name,
                )
                logger.warning("codex reset source %s failed: %s", source.name, error_name)
                continue
            self._store.record_source_success(
                result.name,
                result.family,
                now_ts=now_ts,
                content_ts=result.content_ts,
                item_count=len(result.evidence),
            )
            fetched.append(result)
        return fetched

    def _canonical_target(self, evidence: ResetEvidence) -> str | None:
        if evidence.signal_stage is not ResetStage.CONFIRMED:
            return evidence.canonical_hint
        if evidence.local_reference:
            # 本机额度事实只能确认已存在的正式预告，绝不能单独制造 reset 事件。
            return self._store.find_confirmation_target(evidence.observed_at)
        if self._store.get_event(evidence.canonical_hint) is not None:
            return evidence.canonical_hint
        evidence_target = self._store.find_confirmation_evidence_target(evidence.observed_at)
        if evidence_target is not None:
            return evidence_target
        return self._store.find_confirmation_target(evidence.observed_at) or evidence.canonical_hint

    def _advance_event(self, canonical_id: str, *, now_ts: int) -> None:
        evidence = self._store.evidence_for(canonical_id)
        target = self._classify(evidence)
        if target is None:
            return
        event, advanced = self._store.upsert_event(canonical_id, target, evidence, now_ts=now_ts)
        if not advanced:
            return
        relevant_ts = event.confirmed_ts if target is ResetStage.CONFIRMED else event.announced_ts
        if relevant_ts is None:
            relevant_ts = max(item.observed_at for item in evidence)
        if self._recent_enough(relevant_ts, now_ts):
            self._store.queue_delivery(canonical_id, target, now_ts=now_ts)

    @staticmethod
    def _classify(evidence: list[ResetEvidence]) -> ResetStage | None:
        announcements = [
            item
            for item in evidence
            if item.signal_stage is ResetStage.ANNOUNCED
            and item.official
            and item.expected_end_ts is not None
            and item.reset_type is not None
            and item.url
        ]
        # Banked reset 是可靠公告型事件：只播报预告，不等待或推断额度落地。
        if any(item.reset_type is ResetType.BANKED for item in announcements):
            return ResetStage.ANNOUNCED

        confirmations = [
            item
            for item in evidence
            if item.signal_stage is ResetStage.CONFIRMED and item.explicit_completed
        ]
        confirmed_families = {item.source_family for item in confirmations}
        if len(confirmed_families) >= 2 or any(item.local_reference for item in confirmations):
            return ResetStage.CONFIRMED
        if announcements:
            return ResetStage.ANNOUNCED
        if any(
            item.signal_stage is ResetStage.HINT and item.official and item.url for item in evidence
        ):
            return ResetStage.HINT
        return None

    def _recent_enough(self, event_ts: int | None, now_ts: int) -> bool:
        if event_ts is None:
            return False
        max_age = self._settings.sentinel_codex_reset_notify_max_age_hours * 3600
        return now_ts - event_ts <= max_age

    async def _dispatch_due(self, now_ts: int) -> None:
        offset = self._settings.sentinel_heartbeat_utc_offset
        local = datetime.fromtimestamp(now_ts, tz=timezone(timedelta(hours=offset)))
        for delivery in self._store.due_deliveries(now_ts=now_ts):
            event = self._store.enrich_event(delivery.event)
            notification = codex_reset_notification(
                event,
                delivery.stage,
                now_ts=now_ts,
                now_str=local.strftime("%Y-%m-%d %H:%M:%S"),
                utc_offset=offset,
            )
            delivered = await self._broadcaster.send(notification)
            if delivered >= 1:
                self._store.mark_delivery_success(
                    delivery.canonical_id, delivery.stage, delivered_ts=now_ts
                )
                continue
            attempts = delivery.attempts + 1
            terminal = attempts >= self._settings.sentinel_codex_reset_delivery_max_attempts
            delay = min(
                self._settings.sentinel_codex_reset_retry_max_seconds,
                self._settings.sentinel_codex_reset_retry_base_seconds * (2 ** (attempts - 1)),
            )
            self._store.mark_delivery_failure(
                delivery.canonical_id,
                delivery.stage,
                next_attempt_ts=now_ts + delay,
                error="all notification channels failed",
                terminal=terminal,
            )

    def health(self) -> dict:
        return self._store.health(
            now_ts=int(self._clock()),
            freshness_seconds=self._settings.sentinel_codex_reset_freshness_seconds,
        )

    def close(self) -> None:
        self._store.release_lease(self._lease_name, self._owner)
        self._store.close()
