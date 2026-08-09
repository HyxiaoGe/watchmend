# src/sentinel/store.py
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sentinel.findings import DigestItem, EventRecord, Finding
from sentinel.models import (
    ProbeDailyRow,
    ProbeSample,
    Snapshot,
    snapshot_from_dict,
    snapshot_to_dict,
)

_UNSET = object()  # 区分"省略 tools_json(不动该列)"与"显式传 None(清空该列)"


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """幂等迁移:列不存在才 ALTER TABLE ADD COLUMN。应对升级前已建库的实例。
    table/column/decl 均为代码内字面量,不接受外部输入,无注入面。"""
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


class Store:
    """SQLite 持久层(WAL)。snapshots/meta 供状态页轮询;probe_* 供内部探针与体检日报。"""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        # WAL:允许应用常驻写 + 一次性脚本/巡检并行读
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS snapshots ("
            "provider TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS probe_samples ("
            "ts INTEGER NOT NULL, service TEXT NOT NULL, ok INTEGER NOT NULL, "
            "status_code INTEGER, latency_ms REAL)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_probe_samples_service_ts ON probe_samples (service, ts)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS probe_daily ("
            "service TEXT NOT NULL, date TEXT NOT NULL, "
            "total INTEGER NOT NULL, ok_count INTEGER NOT NULL, "
            "p50 REAL, p95 REAL, PRIMARY KEY (service, date))"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
            "rule TEXT NOT NULL, subject TEXT NOT NULL, severity TEXT NOT NULL, "
            "status TEXT NOT NULL, detail TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "diagnosis_status TEXT NOT NULL, diagnosis_json TEXT, "
            "cooldown_until INTEGER NOT NULL, resolved_ts INTEGER, "
            "diagnosis_tools_json TEXT, notified INTEGER NOT NULL DEFAULT 1)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_rule_subject ON events (rule, subject)"
        )
        # 旧库(升级前已建 events 表)补列:CREATE TABLE IF NOT EXISTS 不会改既有表
        _ensure_column(self._conn, "events", "diagnosis_tools_json", "TEXT")
        _ensure_column(self._conn, "events", "notified", "INTEGER NOT NULL DEFAULT 1")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS digest_items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, first_ts INTEGER NOT NULL, "
            "last_ts INTEGER NOT NULL, rule TEXT NOT NULL, subject TEXT NOT NULL, "
            "severity TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1, "
            "sent_ts INTEGER)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_items_pending "
            "ON digest_items (sent_ts, first_ts)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_analyses ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
            "provider TEXT NOT NULL, mode TEXT NOT NULL, model TEXT NOT NULL, "
            "events_json TEXT NOT NULL, analysis_json TEXT NOT NULL)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_analyses_ts ON llm_analyses (ts)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS container_restart_baseline ("
            "subject TEXT PRIMARY KEY, "
            "restart_count INTEGER NOT NULL, "
            "window_start_ts INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS notification_receipts ("
            "source TEXT NOT NULL, key_hash TEXT NOT NULL, "
            "delivered_ts INTEGER NOT NULL, delivered_count INTEGER NOT NULL, "
            "PRIMARY KEY (source, key_hash))"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_receipts_ts "
            "ON notification_receipts (delivered_ts)"
        )
        self._conn.commit()

    def get(self, provider: str) -> Snapshot | None:
        row = self._conn.execute(
            "SELECT payload FROM snapshots WHERE provider = ?", (provider,)
        ).fetchone()
        if row is None:
            return None
        return snapshot_from_dict(json.loads(row[0]))

    def put(self, provider: str, snapshot: Snapshot) -> None:
        payload = json.dumps(snapshot_to_dict(snapshot), ensure_ascii=False)
        self._conn.execute(
            "INSERT INTO snapshots (provider, payload) VALUES (?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET payload = excluded.payload",
            (provider, payload),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    # ---- 外部通知幂等回执（只存事件键哈希，不存通知正文）----

    def get_notification_receipt(self, source: str, key_hash: str) -> tuple[int, int] | None:
        row = self._conn.execute(
            "SELECT delivered_ts, delivered_count FROM notification_receipts "
            "WHERE source = ? AND key_hash = ?",
            (source, key_hash),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def record_notification_receipt(
        self, source: str, key_hash: str, *, delivered_ts: int, delivered_count: int
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO notification_receipts "
            "(source, key_hash, delivered_ts, delivered_count) VALUES (?, ?, ?, ?)",
            (source, key_hash, delivered_ts, delivered_count),
        )
        self._conn.commit()

    def prune_notification_receipts(self, *, before_ts: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM notification_receipts WHERE delivered_ts < ?", (before_ts,)
        )
        self._conn.commit()
        return cur.rowcount

    # ---- LLM 告警编辑审计 ----

    def record_llm_analysis(
        self,
        *,
        ts: int,
        provider: str,
        mode: str,
        model: str,
        events_json: str,
        analysis_json: str,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO llm_analyses "
            "(ts, provider, mode, model, events_json, analysis_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, provider, mode, model, events_json, analysis_json),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_recent_llm_analyses(self, *, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, ts, provider, mode, model, events_json, analysis_json "
            "FROM llm_analyses ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = ("id", "ts", "provider", "mode", "model", "events_json", "analysis_json")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    # ---- 容器 crash-loop 基线(docker-only 检测,见设计稿 §4)----

    def get_restart_baseline(self, subject: str) -> tuple[int, int] | None:
        """返回 (restart_count, window_start_ts);无行则 None。"""
        row = self._conn.execute(
            "SELECT restart_count, window_start_ts FROM container_restart_baseline "
            "WHERE subject = ?",
            (subject,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def upsert_restart_baseline(
        self, subject: str, restart_count: int, window_start_ts: int
    ) -> None:
        self._conn.execute(
            "INSERT INTO container_restart_baseline (subject, restart_count, window_start_ts) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(subject) DO UPDATE SET "
            "restart_count = excluded.restart_count, "
            "window_start_ts = excluded.window_start_ts",
            (subject, restart_count, window_start_ts),
        )
        self._conn.commit()

    def prune_restart_baselines(self, older_than_ts: int) -> None:
        """删除 window_start_ts < older_than_ts 的基线行(消失/长期不更新的容器)。"""
        self._conn.execute(
            "DELETE FROM container_restart_baseline WHERE window_start_ts < ?",
            (older_than_ts,),
        )
        self._conn.commit()

    # ---- 内部探针 ----

    def add_probe_samples(self, samples: list[ProbeSample]) -> None:
        self._conn.executemany(
            "INSERT INTO probe_samples (ts, service, ok, status_code, latency_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            [(s.ts, s.service, int(s.ok), s.status_code, s.latency_ms) for s in samples],
        )
        self._conn.commit()

    def get_probe_samples_since(self, since_ts: int) -> list[ProbeSample]:
        rows = self._conn.execute(
            "SELECT ts, service, ok, status_code, latency_ms FROM probe_samples "
            "WHERE ts >= ? ORDER BY ts",
            (since_ts,),
        ).fetchall()
        return [
            ProbeSample(ts=r[0], service=r[1], ok=bool(r[2]), status_code=r[3], latency_ms=r[4])
            for r in rows
        ]

    def get_latest_probe_ts(self) -> int | None:
        """全表最新探针样本 ts（不限今日）。None=从未探测过。
        供探针引擎活性判定：午夜后最新样本可能落在昨日，不能只看今日零点后样本。"""
        row = self._conn.execute("SELECT MAX(ts) FROM probe_samples").fetchone()
        return row[0] if row and row[0] is not None else None

    def get_latest_failed_probe_ts_by_service(self, since_ts: int) -> dict[str, int]:
        """返回窗口内每个服务最近一次失败探针时间。

        面板排序只需要每服务一个时间戳，直接在 SQLite 聚合，避免每次刷新把整个
        30/90 天逐次样本窗口搬进 Python。
        """
        rows = self._conn.execute(
            "SELECT service, MAX(ts) FROM probe_samples WHERE ts >= ? AND ok = 0 GROUP BY service",
            (since_ts,),
        ).fetchall()
        return {service: ts for service, ts in rows}

    def prune_probe_samples(self, before_ts: int) -> int:
        cur = self._conn.execute("DELETE FROM probe_samples WHERE ts < ?", (before_ts,))
        self._conn.commit()
        return cur.rowcount

    def upsert_probe_daily(
        self,
        service: str,
        date: str,
        *,
        total: int,
        ok_count: int,
        p50: float | None,
        p95: float | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO probe_daily (service, date, total, ok_count, p50, p95) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(service, date) DO UPDATE SET total = excluded.total, "
            "ok_count = excluded.ok_count, p50 = excluded.p50, p95 = excluded.p95",
            (service, date, total, ok_count, p50, p95),
        )
        self._conn.commit()

    def get_recent_daily_p95s(
        self, service: str, *, before_date: str, limit: int = 7
    ) -> list[float]:
        """七日基线用:取 before_date 之前最近 limit 天的非空 p95,最近在前。"""
        rows = self._conn.execute(
            "SELECT p95 FROM probe_daily "
            "WHERE service = ? AND date < ? AND p95 IS NOT NULL "
            "ORDER BY date DESC LIMIT ?",
            (service, before_date, limit),
        ).fetchall()
        return [r[0] for r in rows]

    def get_probe_daily_since(self, start_date: str) -> list[ProbeDailyRow]:
        """窗口内全服务日汇总（date 升序）。90 天 × 16 服务 ≈ 1440 行，廉价。
        start_date 为本地日期字符串（ISO，与 probe_daily.date 同口径）。"""
        rows = self._conn.execute(
            "SELECT service, date, total, ok_count, p50, p95 FROM probe_daily "
            "WHERE date >= ? ORDER BY date",
            (start_date,),
        ).fetchall()
        return [
            ProbeDailyRow(
                service=r[0], date=r[1], total=r[2], ok_count=r[3], p50_ms=r[4], p95_ms=r[5]
            )
            for r in rows
        ]

    # ---- 巡检事件(Phase 2) ----

    _EVENT_COLS = (
        "id, ts, rule, subject, severity, status, detail, payload_json, "
        "diagnosis_status, diagnosis_json, cooldown_until, resolved_ts, "
        "diagnosis_tools_json, notified"
    )

    def insert_event(
        self,
        *,
        ts: int,
        rule: str,
        subject: str,
        severity: str,
        status: str,
        detail: str,
        payload_json: str,
        diagnosis_status: str,
        cooldown_until: int,
        resolved_ts: int | None = None,
        notified: bool = True,
    ) -> int:
        """status='resolved'(点事件)时必须同时传 resolved_ts,否则 count_resolved_since 看不到。"""
        cur = self._conn.execute(
            "INSERT INTO events (ts, rule, subject, severity, status, detail, payload_json, "
            "diagnosis_status, diagnosis_json, cooldown_until, resolved_ts, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                ts,
                rule,
                subject,
                severity,
                status,
                detail,
                payload_json,
                diagnosis_status,
                cooldown_until,
                resolved_ts,
                int(notified),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_open_events(self) -> list[EventRecord]:
        rows = self._conn.execute(
            f"SELECT {self._EVENT_COLS} FROM events WHERE status = 'open' ORDER BY ts"
        ).fetchall()
        return [self._event_from_row(r) for r in rows]

    def get_pending_diagnosis_events(self) -> list[EventRecord]:
        """Phase 3 /events/pending 的数据源;本期仅供测试断言。"""
        rows = self._conn.execute(
            f"SELECT {self._EVENT_COLS} FROM events WHERE diagnosis_status = 'pending' ORDER BY ts"
        ).fetchall()
        return [self._event_from_row(r) for r in rows]

    def resolve_event(self, event_id: int, *, resolved_ts: int) -> None:
        self._conn.execute(
            "UPDATE events SET status = 'resolved', resolved_ts = ? WHERE id = ?",
            (resolved_ts, event_id),
        )
        self._conn.commit()

    def get_cooldown_until(self, rule: str, subject: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(cooldown_until) FROM events WHERE rule = ? AND subject = ?",
            (rule, subject),
        ).fetchone()
        return row[0] or 0

    def count_resolved_since(self, since_ts: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE status = 'resolved' AND resolved_ts >= ?",
            (since_ts,),
        ).fetchone()
        return row[0]

    def get_resolved_since(self, since_ts: int, *, limit: int = 50) -> list[EventRecord]:
        """面板「最近恢复」区:已 resolved 且 resolved_ts >= since_ts,最近在前。
        现有 count_resolved_since 只给计数,此处给列表。"""
        rows = self._conn.execute(
            f"SELECT {self._EVENT_COLS} FROM events "
            "WHERE status = 'resolved' AND resolved_ts >= ? ORDER BY resolved_ts DESC LIMIT ?",
            (since_ts, limit),
        ).fetchall()
        return [self._event_from_row(r) for r in rows]

    def get_events_since(self, since_ts: int, *, limit: int | None = None) -> list[EventRecord]:
        """窗口内全部事件（open + resolved），按 ts 降序。供逐日着色与事件流分页。
        事件稀疏，量可控；limit=None 不限。"""
        sql = f"SELECT {self._EVENT_COLS} FROM events WHERE ts >= ? ORDER BY ts DESC"
        params: list = [since_ts]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._event_from_row(r) for r in rows]

    def get_event(self, event_id: int) -> EventRecord | None:
        row = self._conn.execute(
            f"SELECT {self._EVENT_COLS} FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return self._event_from_row(row) if row else None

    @staticmethod
    def _event_from_row(row) -> EventRecord:
        values = list(row)
        values[-1] = bool(values[-1])
        return EventRecord(*values)

    def set_diagnosis(
        self,
        event_id: int,
        *,
        status: str,
        diagnosis_json: str | None = None,
        tools_json: str | None | object = _UNSET,
    ) -> bool:
        """诊断回写(Phase 3)。status 仅限终态;返回是否真的更新了一行(False=事件不存在)。
        tools_json=证据链 JSON 数组(子项目③):省略=不动该列(保留既有证据);
        显式传 None=清空该列;显式传字符串=写入。"""
        if status not in ("done", "failed", "skipped"):
            raise ValueError(f"invalid diagnosis status: {status}")
        cols = ["diagnosis_status = ?", "diagnosis_json = ?"]
        params: list = [status, diagnosis_json]
        if tools_json is not _UNSET:
            cols.append("diagnosis_tools_json = ?")
            params.append(tools_json)
        params.append(event_id)
        cur = self._conn.execute(f"UPDATE events SET {', '.join(cols)} WHERE id = ?", params)
        self._conn.commit()
        return cur.rowcount > 0

    # ---- 分时巡检摘要 ----

    def enqueue_digest(
        self,
        finding: Finding,
        *,
        now_ts: int,
        state: str = "observed",
    ) -> int:
        """同一未播报 (rule, subject) 合并，保留最新状态并累计观察次数。"""
        row = self._conn.execute(
            "SELECT id, occurrences FROM digest_items "
            "WHERE sent_ts IS NULL AND rule = ? AND subject = ? ORDER BY id DESC LIMIT 1",
            (finding.rule, finding.subject),
        ).fetchone()
        payload_json = json.dumps(finding.payload, ensure_ascii=False)
        if row:
            item_id, occurrences = row
            self._conn.execute(
                "UPDATE digest_items SET last_ts = ?, severity = ?, state = ?, "
                "detail = ?, payload_json = ?, occurrences = ? WHERE id = ?",
                (
                    now_ts,
                    finding.severity,
                    state,
                    finding.detail,
                    payload_json,
                    occurrences + 1,
                    item_id,
                ),
            )
        else:
            cur = self._conn.execute(
                "INSERT INTO digest_items "
                "(first_ts, last_ts, rule, subject, severity, state, detail, payload_json, "
                "occurrences, sent_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)",
                (
                    now_ts,
                    now_ts,
                    finding.rule,
                    finding.subject,
                    finding.severity,
                    state,
                    finding.detail,
                    payload_json,
                ),
            )
            item_id = cur.lastrowid
        self._conn.commit()
        return item_id

    def resolve_deferred_digest(self, event: EventRecord, *, now_ts: int) -> int:
        """未实时通知的状态型事件恢复时，将待摘要项改成已恢复。"""
        finding = Finding(
            rule=event.rule,
            subject=event.subject,
            severity=event.severity,
            detail=f"已恢复；原始触发：{event.detail}",
            payload=json.loads(event.payload_json),
        )
        return self.enqueue_digest(finding, now_ts=now_ts, state="resolved")

    def get_pending_digest_items(self, *, limit: int = 200) -> list[DigestItem]:
        rows = self._conn.execute(
            "SELECT id, first_ts, last_ts, rule, subject, severity, state, detail, "
            "payload_json, occurrences FROM digest_items "
            "WHERE sent_ts IS NULL ORDER BY "
            "CASE severity WHEN 'critical' THEN 0 ELSE 1 END, last_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [DigestItem(*row) for row in rows]

    def mark_digest_items_sent(self, item_ids: list[int], *, sent_ts: int) -> None:
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        self._conn.execute(
            f"UPDATE digest_items SET sent_ts = ? WHERE id IN ({placeholders})",
            (sent_ts, *item_ids),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
