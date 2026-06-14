# src/sentinel/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Indicator(StrEnum):
    NONE = "none"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return {"unknown": -1, "none": 0, "minor": 1, "major": 2, "critical": 3}[self.value]


class ComponentStatus(StrEnum):
    OPERATIONAL = "operational"
    MAINTENANCE = "maintenance"
    DEGRADED = "degraded"
    PARTIAL_OUTAGE = "partial_outage"
    MAJOR_OUTAGE = "major_outage"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return {
            "unknown": -1,
            "operational": 0,
            "maintenance": 1,
            "degraded": 2,
            "partial_outage": 3,
            "major_outage": 4,
        }[self.value]


class IncidentStatus(StrEnum):
    INVESTIGATING = "investigating"
    IDENTIFIED = "identified"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    POSTMORTEM = "postmortem"


@dataclass
class Component:
    key: str
    name: str
    status: ComponentStatus


@dataclass
class Incident:
    key: str
    title: str
    status: IncidentStatus
    impact: Indicator
    url: str
    started_at: str | None
    updated_at: str | None
    latest_update_id: str
    affected: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    provider: str
    display_name: str
    indicator: Indicator
    status_url: str
    components: list[Component]
    incidents: list[Incident]
    fetched_at: str


def snapshot_to_dict(snap: Snapshot) -> dict:
    return {
        "provider": snap.provider,
        "display_name": snap.display_name,
        "indicator": snap.indicator.value,
        "status_url": snap.status_url,
        "fetched_at": snap.fetched_at,
        "components": [
            {"key": c.key, "name": c.name, "status": c.status.value} for c in snap.components
        ],
        "incidents": [
            {
                "key": i.key,
                "title": i.title,
                "status": i.status.value,
                "impact": i.impact.value,
                "url": i.url,
                "started_at": i.started_at,
                "updated_at": i.updated_at,
                "latest_update_id": i.latest_update_id,
                "affected": list(i.affected),
            }
            for i in snap.incidents
        ],
    }


@dataclass
class ProbeSample:
    """内部探针单次采样。ts 为 unix 秒;超时/连接失败时 status_code 与 latency_ms 均为 None。"""

    ts: int
    service: str
    ok: bool
    status_code: int | None
    latency_ms: float | None


@dataclass
class ServiceDayStats:
    """单服务 24h 探针聚合。延迟统计只取 ok 样本;baseline 为近 7 日 p95 均值,无历史为 None。"""

    service: str
    total: int
    ok_count: int
    p50_ms: float | None
    p95_ms: float | None
    baseline_p95_ms: float | None = None

    @property
    def uptime_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return self.ok_count / self.total * 100


@dataclass
class ProbeDailyRow:
    """probe_daily 一行（逐日健康柱条用）。p50_ms/p95_ms 可空（当天无 ok 样本）。"""

    service: str
    date: str
    total: int
    ok_count: int
    p50_ms: float | None
    p95_ms: float | None


def snapshot_from_dict(d: dict) -> Snapshot:
    return Snapshot(
        provider=d["provider"],
        display_name=d["display_name"],
        indicator=Indicator(d["indicator"]),
        status_url=d["status_url"],
        fetched_at=d["fetched_at"],
        components=[
            Component(key=c["key"], name=c["name"], status=ComponentStatus(c["status"]))
            for c in d["components"]
        ],
        incidents=[
            Incident(
                key=i["key"],
                title=i["title"],
                status=IncidentStatus(i["status"]),
                impact=Indicator(i["impact"]),
                url=i["url"],
                started_at=i["started_at"],
                updated_at=i["updated_at"],
                latest_update_id=i["latest_update_id"],
                affected=list(i.get("affected", [])),
            )
            for i in d["incidents"]
        ],
    )
