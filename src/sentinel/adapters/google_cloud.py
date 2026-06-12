# src/sentinel/adapters/google_cloud.py
from __future__ import annotations

from datetime import UTC, datetime

from sentinel.adapters.base import AdapterError
from sentinel.fetcher import Fetcher, FetchError
from sentinel.models import Incident, IncidentStatus, Indicator, Snapshot

_INCIDENTS_URL = "https://status.cloud.google.com/incidents.json"
_STATUS_BASE = "https://status.cloud.google.com/"

# 关注面 = Gemini / Vertex GenAI 产品族。匹配 affected_products 的 id 或 title。
# 注:仅 'Vertex Gemini API' 的 id(Z0F…)经校验坐实;其余先用 title 匹配,
#     实现时可经一次真实抓取补齐稳定 id 后改纯 id 匹配(更稳)。
_WATCHED: frozenset[str] = frozenset(
    {
        "Vertex Gemini API",
        "Vertex Imagen API",
        "Vertex AI Online Prediction",
        "Vertex AI Search",
        "Agent Assist",
        "Dialogflow CX",
        "Dialogflow ES",
        "Z0FZJAMvEB4j3NbCJs6B",
    }
)

_SEVERITY = {"high": Indicator.MAJOR, "medium": Indicator.MINOR, "low": Indicator.MINOR}


class GoogleCloudAdapter:
    """status.cloud.google.com/incidents.json 异构适配器。

    incidents.json 含开放+已解决(非 live-only)→ 必须过滤 end==null;
    只发关注产品族;无逐产品在线状态故 track_components=False(只做事件级)。
    """

    provider = "google_cloud"
    display_name = "Google Cloud"
    track_components = False

    def __init__(self, watched: frozenset[str] = _WATCHED) -> None:
        self.base_url = _STATUS_BASE
        self._watched = watched

    async def fetch(self, fetcher: Fetcher) -> Snapshot:
        try:
            data = await fetcher.get_json(_INCIDENTS_URL)
        except FetchError as err:
            raise AdapterError(str(err)) from err
        if not isinstance(data, list):
            raise AdapterError("gcp: incidents.json 非数组")

        open_incidents = [
            i for i in data if isinstance(i, dict) and i.get("end") is None and self._matches(i)
        ]
        incidents = [self._incident(i) for i in open_incidents]
        indicator = (
            max((i.impact for i in incidents), key=lambda x: x.rank)
            if incidents
            else Indicator.NONE
        )
        return Snapshot(
            provider=self.provider,
            display_name=self.display_name,
            indicator=indicator,
            status_url=_STATUS_BASE,
            components=[],
            incidents=incidents,
            fetched_at=datetime.now(UTC).isoformat(),
        )

    def _matches(self, i: dict) -> bool:
        for p in i.get("affected_products") or []:
            if p.get("id") in self._watched or p.get("title") in self._watched:
                return True
        return False

    def _impact(self, i: dict) -> Indicator:
        if i.get("status_impact") == "SERVICE_OUTAGE":
            return Indicator.CRITICAL
        return _SEVERITY.get(i.get("severity", ""), Indicator.MINOR)

    def _incident(self, i: dict) -> Incident:
        mru = i.get("most_recent_update") or {}
        when = mru.get("when") or i.get("modified") or ""
        affected = [p.get("title", "") for p in i.get("affected_products") or []]
        return Incident(
            key=i["id"],
            title=i.get("external_desc", ""),
            status=IncidentStatus.INVESTIGATING,  # GCP 无生命周期 -> 泛指进行中
            impact=self._impact(i),
            url=_STATUS_BASE + i.get("uri", ""),
            started_at=i.get("begin"),
            updated_at=i.get("modified"),
            latest_update_id=str(when),  # 合成锚:时间戳(非正文哈希)
            affected=affected,
        )
