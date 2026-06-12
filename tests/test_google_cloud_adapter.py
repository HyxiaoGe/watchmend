# tests/test_google_cloud_adapter.py
import httpx
import pytest
import respx

from sentinel.adapters.google_cloud import GoogleCloudAdapter
from sentinel.fetcher import Fetcher
from sentinel.models import IncidentStatus, Indicator


async def _run(payload: list):
    adapter = GoogleCloudAdapter()
    with respx.mock:
        respx.get("https://status.cloud.google.com/incidents.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with httpx.AsyncClient() as client:
            return adapter, await adapter.fetch(Fetcher(client, backoff_base=0))


@pytest.mark.asyncio
async def test_only_open_watched_incidents_emitted(load_fixture):
    _, snap = await _run(load_fixture("gcp_incidents.json"))
    # CLOSED01 被 end!=null 过滤;OPEN_OTHER 被白名单过滤;只剩 OPEN01
    assert [i.key for i in snap.incidents] == ["OPEN01"]
    assert snap.components == []  # track_components=False
    assert snap.provider == "google_cloud"


@pytest.mark.asyncio
async def test_open_incident_mapping(load_fixture):
    _, snap = await _run(load_fixture("gcp_incidents.json"))
    inc = snap.incidents[0]
    assert inc.status is IncidentStatus.INVESTIGATING  # GCP 无生命周期 -> 统一 investigating
    assert inc.impact is Indicator.CRITICAL  # SERVICE_OUTAGE -> CRITICAL
    assert inc.url == "https://status.cloud.google.com/incidents/OPEN01"  # 相对 uri 前缀
    assert inc.latest_update_id == "2026-06-06T10:30:00+00:00"  # 合成自 when
    assert "Vertex Gemini API" in inc.affected
    assert snap.indicator is Indicator.CRITICAL  # 最坏开放事件


@pytest.mark.asyncio
async def test_no_open_incidents_indicator_none():
    _, snap = await _run(
        [
            {
                "id": "C",
                "end": "2026-01-01T00:00:00+00:00",
                "severity": "high",
                "status_impact": "SERVICE_OUTAGE",
                "uri": "incidents/C",
                "affected_products": [{"title": "Vertex Gemini API", "id": "Z0FZJAMvEB4j3NbCJs6B"}],
                "most_recent_update": {"when": "2026-01-01T00:00:00+00:00", "status": "AVAILABLE"},
            }
        ]
    )
    assert snap.incidents == []
    assert snap.indicator is Indicator.NONE
