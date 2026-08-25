from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import socket
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sentinel.codex_reset.http import ResetFetcher
from sentinel.codex_reset.models import ResetEvidence, ResetStage, ResetType
from sentinel.codex_reset.notify import codex_reset_notification
from sentinel.codex_reset.semantic import ResetIntentClassifier, has_explicit_reset_action
from sentinel.codex_reset.sources import canonical_from, default_sources, is_official_url
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
        intent_classifier=None,
        clock=time.time,
        owner: str | None = None,
    ) -> None:
        self._settings = settings
        self._broadcaster = broadcaster
        self._store = store or ResetStore(settings.sentinel_db_path)
        self._sources = list(sources if sources is not None else default_sources(settings))
        self._fetcher = ResetFetcher(client)
        self._intent_classifier = intent_classifier
        if (
            self._intent_classifier is None
            and settings.sentinel_codex_reset_semantic_enabled
            and bool(settings.sentinel_editor_base_url)
            and bool(settings.sentinel_editor_model)
        ):
            self._intent_classifier = ResetIntentClassifier(
                client,
                base_url=settings.sentinel_editor_base_url,
                api_key=settings.sentinel_editor_api_key,
                model=settings.sentinel_editor_model,
                timeout_seconds=settings.sentinel_codex_reset_semantic_timeout_seconds,
            )
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
        evidence.extend(await self._semantic_evidence(fetched, evidence, now_ts=now_ts))
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

    async def _semantic_evidence(
        self, fetched: list, deterministic: list[ResetEvidence], *, now_ts: int
    ) -> list[ResetEvidence]:
        if self._intent_classifier is None:
            return []
        known = {(item.source_name, item.source_item_id) for item in deterministic}
        candidates = {
            (item.source_name, item.source_item_id): item
            for result in fetched
            for item in result.intent_candidates
            if (item.source_name, item.source_item_id) not in known
            and self._recent_enough(item.observed_at, now_ts)
            and is_official_url(item.url)
            and has_explicit_reset_action(item.text)
        }
        evidence = []
        for candidate in candidates.values():
            content_hash = hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()
            result = self._store.semantic_result(
                candidate.source_name, candidate.source_item_id, content_hash
            )
            if result is None and self._store.semantic_due(
                candidate.source_name,
                candidate.source_item_id,
                content_hash,
                now_ts=now_ts,
            ):
                try:
                    classified = await self._intent_classifier.classify(candidate)
                # 模型层是可选补漏；任何异常都必须与确定性轮询隔离。
                except Exception as err:
                    self._store.record_semantic_failure(
                        candidate.source_name,
                        candidate.source_item_id,
                        content_hash,
                        now_ts=now_ts,
                        error=type(err).__name__,
                        retry_base_seconds=self._settings.sentinel_codex_reset_retry_base_seconds,
                        retry_max_seconds=self._settings.sentinel_codex_reset_retry_max_seconds,
                    )
                    logger.warning(
                        "codex reset semantic classifier failed for %s: %s",
                        candidate.source_item_id,
                        type(err).__name__,
                    )
                    continue
                self._store.record_semantic_success(
                    candidate.source_name,
                    candidate.source_item_id,
                    content_hash,
                    decision=classified.decision,
                    reset_type=classified.reset_type,
                    time_text=classified.time_text,
                    reason=classified.reason,
                    confidence=classified.confidence,
                    now_ts=now_ts,
                )
                result = classified.model_dump()
            if (
                result is None
                or result["decision"] == "ignore"
                or result["confidence"]
                < self._settings.sentinel_codex_reset_semantic_min_confidence
            ):
                continue
            reset_type = (
                ResetType(result["reset_type"])
                if result["reset_type"] in {"direct", "banked"}
                else None
            )
            # 模型仅补充疑似预告；明确窗口和落地确认仍由确定性证据升级。
            evidence.append(
                ResetEvidence(
                    source_name=f"{candidate.source_name}_semantic",
                    source_family=candidate.source_family,
                    source_item_id=candidate.source_item_id,
                    canonical_hint=canonical_from(candidate.url, candidate.source_item_id),
                    signal_stage=ResetStage.HINT,
                    title="模型识别到官方 Codex reset 意图",
                    summary=candidate.text,
                    url=candidate.url,
                    observed_at=candidate.observed_at,
                    reset_type=reset_type,
                    official=True,
                )
            )
        return evidence

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
            event = await self._translated_event(event, now_ts=now_ts)
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

    async def _translated_event(self, event, *, now_ts: int):
        if self._intent_classifier is None or not self._needs_translation(event.summary):
            return event
        content_hash = hashlib.sha256(event.summary.encode("utf-8")).hexdigest()
        translated = self._store.translation_result(event.canonical_id, content_hash)
        if translated is None and self._store.translation_due(
            event.canonical_id, content_hash, now_ts=now_ts
        ):
            try:
                translated = await self._intent_classifier.translate(event.summary)
            # 翻译是展示增强；失败不能阻塞原始通知。
            except Exception as err:
                self._store.record_translation_failure(
                    event.canonical_id,
                    content_hash,
                    now_ts=now_ts,
                    error=type(err).__name__,
                    retry_base_seconds=self._settings.sentinel_codex_reset_retry_base_seconds,
                    retry_max_seconds=self._settings.sentinel_codex_reset_retry_max_seconds,
                )
                logger.warning(
                    "codex reset summary translation failed for %s: %s",
                    event.canonical_id,
                    type(err).__name__,
                )
                return event
            self._store.record_translation_success(
                event.canonical_id,
                content_hash,
                translated,
                now_ts=now_ts,
            )
        return replace(event, translated_summary=translated or "")

    @staticmethod
    def _needs_translation(text: str) -> bool:
        compact = "".join(text.split())
        if not compact:
            return False
        chinese = len(re.findall(r"[\u4e00-\u9fff]", compact))
        return chinese < max(4, int(len(compact) * 0.15))

    def health(self) -> dict:
        payload = self._store.health(
            now_ts=int(self._clock()),
            freshness_seconds=self._settings.sentinel_codex_reset_freshness_seconds,
        )
        payload["semantic"].update(
            {
                "enabled": self._settings.sentinel_codex_reset_semantic_enabled,
                "configured": self._intent_classifier is not None,
            }
        )
        return payload

    def close(self) -> None:
        self._store.release_lease(self._lease_name, self._owner)
        self._store.close()
