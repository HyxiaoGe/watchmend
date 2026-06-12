# src/sentinel/adapters/statuspage.py
from __future__ import annotations

from datetime import UTC, datetime

from sentinel.adapters.base import AdapterError
from sentinel.fetcher import Fetcher, FetchError
from sentinel.models import (
    Component,
    ComponentStatus,
    Incident,
    IncidentStatus,
    Indicator,
    Snapshot,
)

_COMPONENT_STATUS = {
    "operational": ComponentStatus.OPERATIONAL,
    "under_maintenance": ComponentStatus.MAINTENANCE,
    "degraded_performance": ComponentStatus.DEGRADED,
    "partial_outage": ComponentStatus.PARTIAL_OUTAGE,
    "major_outage": ComponentStatus.MAJOR_OUTAGE,
}
_INDICATOR = {
    "none": Indicator.NONE,
    "minor": Indicator.MINOR,
    "major": Indicator.MAJOR,
    "critical": Indicator.CRITICAL,
}
_INCIDENT_STATUS = {
    "investigating": IncidentStatus.INVESTIGATING,
    "identified": IncidentStatus.IDENTIFIED,
    "monitoring": IncidentStatus.MONITORING,
    "resolved": IncidentStatus.RESOLVED,
    "postmortem": IncidentStatus.POSTMORTEM,
}


def _is_pseudo(name: str) -> bool:
    return "for more information" in name.lower()


class StatuspageAdapter:
    """Atlassian Statuspage(及兼容)v2 summary.json 适配器。

    服务 anthropic/openai/github/cloudflare。差异(无 shortlink、无 group 字段、
    group 容器、伪组件、http page.url)全在此防御式吸收。
    """

    def __init__(
        self,
        provider: str,
        display_name: str,
        base_url: str,
        *,
        track_components: bool = True,
    ) -> None:
        self.provider = provider
        self.display_name = display_name
        self.base_url = base_url.rstrip("/")
        self.track_components = track_components

    async def fetch(self, fetcher: Fetcher) -> Snapshot:
        try:
            data = await fetcher.get_json(f"{self.base_url}/api/v2/summary.json")
        except FetchError as err:
            raise AdapterError(str(err)) from err
        if not isinstance(data, dict):
            raise AdapterError(f"{self.provider}: summary.json 非对象")

        indicator = _INDICATOR.get(
            (data.get("status") or {}).get("indicator", ""), Indicator.UNKNOWN
        )
        # status_url 用我们自己钉死的 base_url(避免 cloudflare http:// / openai 尾斜杠)
        status_url = self.base_url

        components: list[Component] = []
        if self.track_components:
            for c in data.get("components") or []:
                if c.get("group", False):  # 过滤 group 容器(仅 cloudflare 有)
                    continue
                name = c.get("name", "")
                if _is_pseudo(name):  # 过滤「Visit … for more information」伪组件
                    continue
                components.append(
                    Component(
                        key=c["id"],
                        name=name,
                        status=_COMPONENT_STATUS.get(c.get("status", ""), ComponentStatus.UNKNOWN),
                    )
                )

        incidents = [self._incident(i, status_url) for i in data.get("incidents") or []]
        return Snapshot(
            provider=self.provider,
            display_name=self.display_name,
            indicator=indicator,
            status_url=status_url,
            components=components,
            incidents=incidents,
            fetched_at=datetime.now(UTC).isoformat(),
        )

    def _incident(self, i: dict, status_url: str) -> Incident:
        updates = i.get("incident_updates") or []
        latest_update_id = updates[0]["id"] if updates else i["id"]  # [0]=最新(实测)
        url = i.get("shortlink") or f"{status_url}#{i['id']}"  # openai 无 shortlink -> 回退
        return Incident(
            key=i["id"],
            title=i.get("name", ""),
            status=_INCIDENT_STATUS.get(i.get("status", ""), IncidentStatus.INVESTIGATING),
            impact=_INDICATOR.get(i.get("impact", ""), Indicator.UNKNOWN),
            url=url,
            started_at=i.get("started_at") or i.get("created_at"),
            updated_at=i.get("updated_at"),
            latest_update_id=latest_update_id,
        )
