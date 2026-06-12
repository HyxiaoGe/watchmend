# tests/test_statuspage_adapter.py
import httpx
import pytest
import respx

from sentinel.adapters.base import AdapterError
from sentinel.adapters.statuspage import StatuspageAdapter
from sentinel.fetcher import Fetcher
from sentinel.models import IncidentStatus, Indicator


async def _run(adapter: StatuspageAdapter, payload: dict):
    with respx.mock:
        respx.get(f"{adapter.base_url}/api/v2/summary.json").mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with httpx.AsyncClient() as client:
            return await adapter.fetch(Fetcher(client, backoff_base=0))


@pytest.mark.asyncio
async def test_anthropic_green_no_incidents(load_fixture):
    adapter = StatuspageAdapter("anthropic", "Anthropic / Claude", "https://status.claude.com")
    snap = await _run(adapter, load_fixture("anthropic_green.json"))
    assert snap.indicator is Indicator.NONE
    assert snap.incidents == []
    assert [c.name for c in snap.components] == ["claude.ai"]
    assert snap.status_url == "https://status.claude.com"


@pytest.mark.asyncio
async def test_openai_active_incident_no_shortlink_falls_back(load_fixture):
    adapter = StatuspageAdapter("openai", "OpenAI", "https://status.openai.com")
    snap = await _run(adapter, load_fixture("openai_active_incident.json"))
    assert len(snap.incidents) == 1
    inc = snap.incidents[0]
    assert inc.status is IncidentStatus.MONITORING
    assert inc.impact is Indicator.MINOR
    # 无 shortlink -> 回退 base_url#id
    assert inc.url == "https://status.openai.com#01KTBZDS20E3PZ53DH2SCKXN49"
    # latest_update_id 取 incident_updates[0].id（最新)
    assert inc.latest_update_id == "01KTCTF5M321F3AQZTVKJ52D5V"
    # 组件无 group 字段也不报错
    assert [c.name for c in snap.components] == ["API"]


@pytest.mark.asyncio
async def test_cloudflare_filters_groups_and_skips_components(load_fixture):
    adapter = StatuspageAdapter(
        "cloudflare", "Cloudflare", "https://www.cloudflarestatus.com", track_components=False
    )
    snap = await _run(adapter, load_fixture("cloudflare_groups.json"))
    assert snap.indicator is Indicator.MINOR
    # track_components=False -> 组件恒为空(不做组件级)
    assert snap.components == []


@pytest.mark.asyncio
async def test_pseudo_components_are_filtered():
    adapter = StatuspageAdapter("github", "GitHub", "https://www.githubstatus.com")
    payload = {
        "page": {"url": "https://www.githubstatus.com"},
        "status": {"indicator": "none"},
        "components": [
            {"id": "a", "name": "Git Operations", "status": "operational", "group": False},
            {
                "id": "b",
                "name": "Visit www.githubstatus.com for more information",
                "status": "operational",
                "group": False,
            },
        ],
        "incidents": [],
    }
    snap = await _run(adapter, payload)
    assert [c.name for c in snap.components] == ["Git Operations"]


@pytest.mark.asyncio
async def test_fetch_error_wrapped_as_adapter_error():
    adapter = StatuspageAdapter("openai", "OpenAI", "https://status.openai.com")
    with respx.mock:
        respx.get(f"{adapter.base_url}/api/v2/summary.json").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError):
                await adapter.fetch(Fetcher(client, retries=2, backoff_base=0))


@pytest.mark.asyncio
async def test_non_dict_payload_raises_adapter_error():
    adapter = StatuspageAdapter("openai", "OpenAI", "https://status.openai.com")
    with respx.mock:
        respx.get(f"{adapter.base_url}/api/v2/summary.json").mock(
            return_value=httpx.Response(200, json=[1, 2, 3])
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError):
                await adapter.fetch(Fetcher(client, backoff_base=0))


@pytest.mark.asyncio
async def test_incident_without_updates_falls_back_to_incident_id():
    adapter = StatuspageAdapter("github", "GitHub", "https://www.githubstatus.com")
    payload = {
        "page": {"url": "https://www.githubstatus.com"},
        "status": {"indicator": "major"},
        "components": [],
        "incidents": [
            {
                "id": "INC42",
                "name": "Disruption",
                "status": "identified",
                "impact": "major",
                "shortlink": "https://stspg.io/abc",
                "incident_updates": [],
            }
        ],
    }
    snap = await _run(adapter, payload)
    assert snap.incidents[0].latest_update_id == "INC42"
    assert snap.incidents[0].url == "https://stspg.io/abc"
