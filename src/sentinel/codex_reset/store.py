from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sentinel.codex_reset.models import (
    ResetEvent,
    ResetEvidence,
    ResetStage,
    ResetType,
    can_advance,
)


@dataclass(frozen=True)
class DueDelivery:
    canonical_id: str
    stage: ResetStage
    attempts: int
    event: ResetEvent


_EVENT_COLUMNS = (
    "canonical_id, stage, reset_type, title, summary, primary_url, announced_ts, "
    "expected_start_ts, expected_end_ts, confirmed_ts, first_seen_ts, last_seen_ts"
)


class ResetStore:
    """Codex reset 独立表；与 WatchMend 主库共用 SQLite 文件但不复用事件模型。"""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS codex_reset_events ("
            "canonical_id TEXT PRIMARY KEY, stage TEXT NOT NULL, reset_type TEXT, "
            "title TEXT NOT NULL, summary TEXT NOT NULL, primary_url TEXT NOT NULL, "
            "announced_ts INTEGER, expected_start_ts INTEGER, expected_end_ts INTEGER, "
            "confirmed_ts INTEGER, first_seen_ts INTEGER NOT NULL, last_seen_ts INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS codex_reset_evidence ("
            "source_name TEXT NOT NULL, source_family TEXT NOT NULL, source_item_id TEXT NOT NULL, "
            "canonical_id TEXT NOT NULL, signal_stage TEXT NOT NULL, title TEXT NOT NULL, "
            "summary TEXT NOT NULL, url TEXT NOT NULL, observed_at INTEGER NOT NULL, "
            "reset_type TEXT, expected_start_ts INTEGER, expected_end_ts INTEGER, "
            "official INTEGER NOT NULL, explicit_completed INTEGER NOT NULL, "
            "local_reference INTEGER NOT NULL, payload_hash TEXT NOT NULL DEFAULT '', "
            "last_seen_ts INTEGER NOT NULL, PRIMARY KEY (source_name, source_item_id))"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_codex_reset_evidence_event "
            "ON codex_reset_evidence (canonical_id, signal_stage)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS codex_reset_deliveries ("
            "canonical_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, "
            "attempts INTEGER NOT NULL DEFAULT 0, next_attempt_ts INTEGER NOT NULL, "
            "delivered_ts INTEGER, last_error TEXT, "
            "PRIMARY KEY (canonical_id, stage))"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_codex_reset_delivery_due "
            "ON codex_reset_deliveries (status, next_attempt_ts)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS codex_reset_source_health ("
            "source_name TEXT PRIMARY KEY, source_family TEXT NOT NULL, "
            "last_attempt_ts INTEGER NOT NULL, last_success_ts INTEGER, content_ts INTEGER, "
            "consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, "
            "item_count INTEGER NOT NULL DEFAULT 0)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS codex_reset_leases ("
            "name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_ts INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS codex_reset_semantic_cache ("
            "source_name TEXT NOT NULL, source_item_id TEXT NOT NULL, content_hash TEXT NOT NULL, "
            "decision TEXT, reset_type TEXT, time_text TEXT, reason TEXT, confidence REAL, "
            "attempts INTEGER NOT NULL DEFAULT 0, next_attempt_ts INTEGER NOT NULL DEFAULT 0, "
            "last_error TEXT, updated_ts INTEGER NOT NULL, "
            "PRIMARY KEY (source_name, source_item_id))"
        )
        self._conn.commit()

    def acquire_lease(self, name: str, owner: str, *, now_ts: int, ttl_seconds: int) -> bool:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._conn.execute(
                "INSERT INTO codex_reset_leases (name, owner, expires_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, "
                "expires_ts = excluded.expires_ts "
                "WHERE codex_reset_leases.expires_ts <= ? OR codex_reset_leases.owner = ?",
                (name, owner, now_ts + ttl_seconds, now_ts, owner),
            )
            self._conn.commit()
            return cursor.rowcount == 1
        except Exception:
            self._conn.rollback()
            raise

    def release_lease(self, name: str, owner: str) -> None:
        self._conn.execute(
            "DELETE FROM codex_reset_leases WHERE name = ? AND owner = ?", (name, owner)
        )
        self._conn.commit()

    def record_source_success(
        self,
        source_name: str,
        source_family: str,
        *,
        now_ts: int,
        content_ts: int | None,
        item_count: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO codex_reset_source_health "
            "(source_name, source_family, last_attempt_ts, last_success_ts, content_ts, "
            "consecutive_failures, last_error, item_count) VALUES (?, ?, ?, ?, ?, 0, NULL, ?) "
            "ON CONFLICT(source_name) DO UPDATE SET source_family = excluded.source_family, "
            "last_attempt_ts = excluded.last_attempt_ts, "
            "last_success_ts = excluded.last_success_ts, "
            "content_ts = excluded.content_ts, consecutive_failures = 0, last_error = NULL, "
            "item_count = excluded.item_count",
            (source_name, source_family, now_ts, now_ts, content_ts, item_count),
        )
        self._conn.commit()

    def record_source_failure(
        self, source_name: str, source_family: str, *, now_ts: int, error: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO codex_reset_source_health "
            "(source_name, source_family, last_attempt_ts, last_success_ts, content_ts, "
            "consecutive_failures, last_error, item_count) VALUES (?, ?, ?, NULL, NULL, 1, ?, 0) "
            "ON CONFLICT(source_name) DO UPDATE SET source_family = excluded.source_family, "
            "last_attempt_ts = excluded.last_attempt_ts, "
            "consecutive_failures = codex_reset_source_health.consecutive_failures + 1, "
            "last_error = excluded.last_error",
            (source_name, source_family, now_ts, error[:120]),
        )
        self._conn.commit()

    def source_due(self, source_name: str, *, now_ts: int, interval_seconds: int) -> bool:
        row = self._conn.execute(
            "SELECT last_attempt_ts FROM codex_reset_source_health WHERE source_name = ?",
            (source_name,),
        ).fetchone()
        return row is None or now_ts - row[0] >= interval_seconds

    def semantic_result(
        self, source_name: str, source_item_id: str, content_hash: str
    ) -> dict | None:
        row = self._conn.execute(
            "SELECT decision, reset_type, time_text, reason, confidence "
            "FROM codex_reset_semantic_cache WHERE source_name = ? AND source_item_id = ? "
            "AND content_hash = ? AND decision IS NOT NULL",
            (source_name, source_item_id, content_hash),
        ).fetchone()
        if row is None:
            return None
        return {
            "decision": row[0],
            "reset_type": row[1],
            "time_text": row[2] or "",
            "reason": row[3] or "",
            "confidence": float(row[4] or 0),
        }

    def semantic_due(
        self,
        source_name: str,
        source_item_id: str,
        content_hash: str,
        *,
        now_ts: int,
    ) -> bool:
        row = self._conn.execute(
            "SELECT content_hash, decision, next_attempt_ts FROM codex_reset_semantic_cache "
            "WHERE source_name = ? AND source_item_id = ?",
            (source_name, source_item_id),
        ).fetchone()
        return bool(row is None or row[0] != content_hash or (row[1] is None and row[2] <= now_ts))

    def record_semantic_success(
        self,
        source_name: str,
        source_item_id: str,
        content_hash: str,
        *,
        decision: str,
        reset_type: str,
        time_text: str,
        reason: str,
        confidence: float,
        now_ts: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO codex_reset_semantic_cache "
            "(source_name, source_item_id, content_hash, decision, reset_type, time_text, "
            "reason, confidence, attempts, next_attempt_ts, last_error, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, ?) "
            "ON CONFLICT(source_name, source_item_id) DO UPDATE SET "
            "content_hash = excluded.content_hash, decision = excluded.decision, "
            "reset_type = excluded.reset_type, time_text = excluded.time_text, "
            "reason = excluded.reason, confidence = excluded.confidence, attempts = 0, "
            "next_attempt_ts = 0, last_error = NULL, updated_ts = excluded.updated_ts",
            (
                source_name,
                source_item_id,
                content_hash,
                decision,
                reset_type,
                time_text[:160],
                reason[:300],
                confidence,
                now_ts,
            ),
        )
        self._conn.commit()

    def record_semantic_failure(
        self,
        source_name: str,
        source_item_id: str,
        content_hash: str,
        *,
        now_ts: int,
        error: str,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> None:
        row = self._conn.execute(
            "SELECT content_hash, attempts FROM codex_reset_semantic_cache "
            "WHERE source_name = ? AND source_item_id = ?",
            (source_name, source_item_id),
        ).fetchone()
        attempts = (row[1] if row and row[0] == content_hash else 0) + 1
        delay = min(retry_max_seconds, retry_base_seconds * (2 ** (attempts - 1)))
        self._conn.execute(
            "INSERT INTO codex_reset_semantic_cache "
            "(source_name, source_item_id, content_hash, attempts, next_attempt_ts, "
            "last_error, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_name, source_item_id) DO UPDATE SET "
            "content_hash = excluded.content_hash, decision = NULL, reset_type = NULL, "
            "time_text = NULL, reason = NULL, confidence = NULL, attempts = excluded.attempts, "
            "next_attempt_ts = excluded.next_attempt_ts, last_error = excluded.last_error, "
            "updated_ts = excluded.updated_ts",
            (
                source_name,
                source_item_id,
                content_hash,
                attempts,
                now_ts + delay,
                error[:120],
                now_ts,
            ),
        )
        self._conn.commit()

    def put_evidence(self, canonical_id: str, evidence: ResetEvidence, *, now_ts: int) -> None:
        self._conn.execute(
            "INSERT INTO codex_reset_evidence "
            "(source_name, source_family, source_item_id, canonical_id, signal_stage, title, "
            "summary, url, observed_at, reset_type, expected_start_ts, expected_end_ts, official, "
            "explicit_completed, local_reference, last_seen_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_name, source_item_id) DO UPDATE SET "
            "canonical_id = excluded.canonical_id, signal_stage = excluded.signal_stage, "
            "title = excluded.title, summary = excluded.summary, url = excluded.url, "
            "observed_at = excluded.observed_at, reset_type = excluded.reset_type, "
            "expected_start_ts = excluded.expected_start_ts, "
            "expected_end_ts = excluded.expected_end_ts, official = excluded.official, "
            "explicit_completed = excluded.explicit_completed, "
            "local_reference = excluded.local_reference, last_seen_ts = excluded.last_seen_ts",
            (
                evidence.source_name,
                evidence.source_family,
                evidence.source_item_id,
                canonical_id,
                evidence.signal_stage.value,
                evidence.title,
                evidence.summary,
                evidence.url,
                evidence.observed_at,
                evidence.reset_type.value if evidence.reset_type else None,
                evidence.expected_start_ts,
                evidence.expected_end_ts,
                int(evidence.official),
                int(evidence.explicit_completed),
                int(evidence.local_reference),
                now_ts,
            ),
        )
        self._conn.commit()

    def evidence_for(self, canonical_id: str) -> list[ResetEvidence]:
        rows = self._conn.execute(
            "SELECT source_name, source_family, source_item_id, signal_stage, title, summary, "
            "url, observed_at, reset_type, expected_start_ts, expected_end_ts, official, "
            "explicit_completed, local_reference FROM codex_reset_evidence "
            "WHERE canonical_id = ? ORDER BY observed_at",
            (canonical_id,),
        ).fetchall()
        return [
            ResetEvidence(
                source_name=row[0],
                source_family=row[1],
                source_item_id=row[2],
                canonical_hint=canonical_id,
                signal_stage=ResetStage(row[3]),
                title=row[4],
                summary=row[5],
                url=row[6],
                observed_at=row[7],
                reset_type=ResetType(row[8]) if row[8] else None,
                expected_start_ts=row[9],
                expected_end_ts=row[10],
                official=bool(row[11]),
                explicit_completed=bool(row[12]),
                local_reference=bool(row[13]),
            )
            for row in rows
        ]

    def find_confirmation_target(
        self, observed_at: int, *, tolerance_seconds: int = 21600
    ) -> str | None:
        row = self._conn.execute(
            "SELECT canonical_id FROM codex_reset_events "
            "WHERE stage IN ('announced', 'delayed') AND expected_end_ts IS NOT NULL "
            "AND COALESCE(reset_type, '') != 'banked' "
            "AND COALESCE(expected_start_ts, announced_ts, expected_end_ts) <= ? "
            "AND expected_end_ts + ? >= ? ORDER BY expected_end_ts DESC LIMIT 1",
            (observed_at + tolerance_seconds, tolerance_seconds, observed_at),
        ).fetchone()
        return row[0] if row else None

    def find_confirmation_evidence_target(
        self, observed_at: int, *, tolerance_seconds: int = 600
    ) -> str | None:
        """不同来源可能引用同一确认串中的父帖/回复；按落地时刻合并。"""
        row = self._conn.execute(
            "SELECT canonical_id FROM codex_reset_evidence WHERE signal_stage = 'confirmed' "
            "AND ABS(observed_at - ?) <= ? ORDER BY ABS(observed_at - ?) LIMIT 1",
            (observed_at, tolerance_seconds, observed_at),
        ).fetchone()
        return row[0] if row else None

    def get_event(self, canonical_id: str) -> ResetEvent | None:
        row = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM codex_reset_events WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()
        return self._event_from_row(row) if row else None

    def upsert_event(
        self,
        canonical_id: str,
        target: ResetStage,
        evidence: list[ResetEvidence],
        *,
        now_ts: int,
    ) -> tuple[ResetEvent, bool]:
        current = self.get_event(canonical_id)
        advanced = can_advance(current.stage if current else None, target)
        selected = self._select_evidence(evidence, target)
        reset_type = next((item.reset_type for item in reversed(evidence) if item.reset_type), None)
        announced = next(
            (item for item in reversed(evidence) if item.signal_stage is ResetStage.ANNOUNCED),
            None,
        )
        confirmed = next(
            (item for item in reversed(evidence) if item.signal_stage is ResetStage.CONFIRMED),
            None,
        )
        if current is None:
            self._conn.execute(
                "INSERT INTO codex_reset_events "
                "(canonical_id, stage, reset_type, title, summary, primary_url, announced_ts, "
                "expected_start_ts, expected_end_ts, confirmed_ts, first_seen_ts, last_seen_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    canonical_id,
                    target.value,
                    reset_type.value if reset_type else None,
                    selected.title,
                    selected.summary,
                    selected.url,
                    announced.observed_at if announced else None,
                    announced.expected_start_ts if announced else None,
                    announced.expected_end_ts if announced else None,
                    confirmed.observed_at if target is ResetStage.CONFIRMED and confirmed else None,
                    now_ts,
                    now_ts,
                ),
            )
        else:
            stage = target if advanced else current.stage
            self._conn.execute(
                "UPDATE codex_reset_events SET stage = ?, reset_type = COALESCE(?, reset_type), "
                "title = ?, summary = ?, primary_url = COALESCE(NULLIF(?, ''), primary_url), "
                "announced_ts = COALESCE(announced_ts, ?), "
                "expected_start_ts = COALESCE(expected_start_ts, ?), "
                "expected_end_ts = COALESCE(expected_end_ts, ?), "
                "confirmed_ts = COALESCE(confirmed_ts, ?), last_seen_ts = ? "
                "WHERE canonical_id = ?",
                (
                    stage.value,
                    reset_type.value if reset_type else None,
                    selected.title,
                    selected.summary,
                    selected.url,
                    announced.observed_at if announced else None,
                    announced.expected_start_ts if announced else None,
                    announced.expected_end_ts if announced else None,
                    confirmed.observed_at if stage is ResetStage.CONFIRMED and confirmed else None,
                    now_ts,
                    canonical_id,
                ),
            )
        self._conn.commit()
        event = self.get_event(canonical_id)
        assert event is not None
        return event, advanced

    @staticmethod
    def _select_evidence(evidence: list[ResetEvidence], target: ResetStage) -> ResetEvidence:
        exact = [item for item in evidence if item.signal_stage is target]
        candidates = exact or evidence
        return max(candidates, key=lambda item: (item.official, item.observed_at))

    def mark_delayed(self, *, now_ts: int, grace_seconds: int) -> list[ResetEvent]:
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM codex_reset_events WHERE stage = 'announced' "
            "AND COALESCE(reset_type, '') != 'banked' "
            "AND expected_end_ts IS NOT NULL AND expected_end_ts + ? < ?",
            (grace_seconds, now_ts),
        ).fetchall()
        events = [self._event_from_row(row) for row in rows]
        if events:
            self._conn.executemany(
                "UPDATE codex_reset_events SET stage = 'delayed', last_seen_ts = ? "
                "WHERE canonical_id = ? AND stage = 'announced'",
                [(now_ts, event.canonical_id) for event in events],
            )
            self._conn.commit()
        return [self.get_event(event.canonical_id) for event in events if event is not None]

    def queue_delivery(self, canonical_id: str, stage: ResetStage, *, now_ts: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO codex_reset_deliveries "
            "(canonical_id, stage, status, attempts, next_attempt_ts) "
            "VALUES (?, ?, 'pending', 0, ?)",
            (canonical_id, stage.value, now_ts),
        )
        self._conn.commit()

    def due_deliveries(self, *, now_ts: int, limit: int = 20) -> list[DueDelivery]:
        rows = self._conn.execute(
            "SELECT d.canonical_id, d.stage, d.attempts, e.canonical_id, e.stage, "
            "e.reset_type, e.title, e.summary, e.primary_url, e.announced_ts, "
            "e.expected_start_ts, e.expected_end_ts, e.confirmed_ts, "
            "e.first_seen_ts, e.last_seen_ts "
            "FROM codex_reset_deliveries d JOIN codex_reset_events e "
            "ON e.canonical_id = d.canonical_id WHERE d.status = 'pending' "
            "AND d.next_attempt_ts <= ? ORDER BY d.next_attempt_ts, "
            "CASE d.stage WHEN 'hint' THEN 1 WHEN 'announced' THEN 2 "
            "WHEN 'delayed' THEN 3 ELSE 4 END LIMIT ?",
            (now_ts, limit),
        ).fetchall()
        return [
            DueDelivery(
                canonical_id=row[0],
                stage=ResetStage(row[1]),
                attempts=row[2],
                event=self._event_from_row(row[3:]),
            )
            for row in rows
        ]

    def mark_delivery_success(
        self, canonical_id: str, stage: ResetStage, *, delivered_ts: int
    ) -> None:
        self._conn.execute(
            "UPDATE codex_reset_deliveries SET status = 'delivered', delivered_ts = ?, "
            "last_error = NULL WHERE canonical_id = ? AND stage = ?",
            (delivered_ts, canonical_id, stage.value),
        )
        self._conn.commit()

    def mark_delivery_failure(
        self,
        canonical_id: str,
        stage: ResetStage,
        *,
        next_attempt_ts: int,
        error: str,
        terminal: bool,
    ) -> None:
        self._conn.execute(
            "UPDATE codex_reset_deliveries SET status = ?, attempts = attempts + 1, "
            "next_attempt_ts = ?, last_error = ? WHERE canonical_id = ? AND stage = ?",
            (
                "failed" if terminal else "pending",
                next_attempt_ts,
                error[:120],
                canonical_id,
                stage.value,
            ),
        )
        self._conn.commit()

    def delivery_status(self, canonical_id: str, stage: ResetStage) -> tuple[str, int] | None:
        row = self._conn.execute(
            "SELECT status, attempts FROM codex_reset_deliveries "
            "WHERE canonical_id = ? AND stage = ?",
            (canonical_id, stage.value),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def health(self, *, now_ts: int, freshness_seconds: int) -> dict:
        rows = self._conn.execute(
            "SELECT source_name, source_family, last_success_ts, content_ts, "
            "consecutive_failures FROM codex_reset_source_health ORDER BY source_name"
        ).fetchall()
        sources = []
        fresh_families: set[str] = set()
        for name, family, last_success, content_ts, failures in rows:
            fresh = bool(content_ts is not None and now_ts - content_ts <= freshness_seconds)
            if fresh:
                fresh_families.add(family)
            sources.append(
                {
                    "name": name,
                    "fresh": fresh,
                    "last_success_ts": last_success,
                    "content_ts": content_ts,
                    "consecutive_failures": failures,
                }
            )
        if len(fresh_families) >= 2:
            status = "ok"
        elif fresh_families:
            status = "degraded"
        else:
            status = "stale"
        semantic_row = self._conn.execute(
            "SELECT COUNT(*), MAX(updated_ts), "
            "SUM(CASE WHEN decision IS NULL THEN 1 ELSE 0 END) "
            "FROM codex_reset_semantic_cache"
        ).fetchone()
        return {
            "status": status,
            "last_success_ts": max((row[2] or 0 for row in rows), default=0) or None,
            "fresh_source_families": len(fresh_families),
            "sources": sources,
            "semantic": {
                "cached_items": semantic_row[0],
                "last_attempt_ts": semantic_row[1],
                "pending_failures": semantic_row[2] or 0,
            },
        }

    @staticmethod
    def _event_from_row(row) -> ResetEvent:
        canonical_id = row[0]
        return ResetEvent(
            canonical_id=canonical_id,
            stage=ResetStage(row[1]),
            reset_type=ResetType(row[2]) if row[2] else None,
            title=row[3],
            summary=row[4],
            primary_url=row[5],
            announced_ts=row[6],
            expected_start_ts=row[7],
            expected_end_ts=row[8],
            confirmed_ts=row[9],
            first_seen_ts=row[10],
            last_seen_ts=row[11],
        )

    def enrich_event(self, event: ResetEvent) -> ResetEvent:
        rows = self._conn.execute(
            "SELECT DISTINCT source_family FROM codex_reset_evidence "
            "WHERE canonical_id = ? ORDER BY source_family",
            (event.canonical_id,),
        ).fetchall()
        count = self._conn.execute(
            "SELECT COUNT(*) FROM codex_reset_evidence WHERE canonical_id = ?",
            (event.canonical_id,),
        ).fetchone()[0]
        return ResetEvent(
            **{
                **event.__dict__,
                "evidence_count": count,
                "source_families": tuple(row[0] for row in rows),
            }
        )

    def close(self) -> None:
        self._conn.close()
