import json

import httpx
import pytest
import respx

from sentinel.events import EventType, TransitionEvent
from sentinel.models import Indicator, Snapshot
from sentinel.status_editor import StatusEditor, StatusEditorError


def _snapshot() -> Snapshot:
    return Snapshot(
        provider="cloudflare",
        display_name="Cloudflare",
        indicator=Indicator.MINOR,
        status_url="https://www.cloudflarestatus.com",
        components=[],
        incidents=[],
        fetched_at="2026-07-29T08:00:00Z",
    )


def _events() -> list[TransitionEvent]:
    return [
        TransitionEvent(
            type=EventType.INCIDENT_UPDATED,
            provider="cloudflare",
            title="事件更新：GraphQL API rate limit errors",
            detail="状态=monitoring；影响=minor",
            impact=Indicator.MINOR,
        )
    ]


def _response_content() -> str:
    return json.dumps(
        {
            "decision": "suppress",
            "severity": "info",
            "headline": "Cloudflare 低影响状态更新",
            "summary": "事件已进入观察阶段。",
            "impact_summary": "暂无内部影响证据。",
            "affected_services": [],
            "evidence": ["官方影响级别为 minor"],
            "recommended_action": "无需处理，继续观察。",
            "confidence": 0.93,
        },
        ensure_ascii=False,
    )


@respx.mock
async def test_editor_requests_strict_schema_and_parses_result():
    route = respx.post("http://litellm-proxy:4000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": _response_content()}}]},
        )
    )
    async with httpx.AsyncClient() as client:
        editor = StatusEditor(
            client,
            base_url="http://litellm-proxy:4000",
            api_key="sk-test",
            model="gemini/gemini-2.5-flash",
            timeout_seconds=5,
        )
        result = await editor.analyze(_snapshot(), _events())

    assert result.decision == "suppress"
    payload = json.loads(route.calls[0].request.content)
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["model"] == "gemini/gemini-2.5-flash"
    assert route.calls[0].request.headers["Authorization"] == "Bearer sk-test"


@respx.mock
async def test_editor_rejects_invalid_model_output():
    respx.post("http://litellm-proxy:4000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"decision":"maybe"}'}}]},
        )
    )
    async with httpx.AsyncClient() as client:
        editor = StatusEditor(
            client,
            base_url="http://litellm-proxy:4000",
            api_key="sk-test",
            model="gemini/gemini-2.5-flash",
        )
        with pytest.raises(StatusEditorError):
            await editor.analyze(_snapshot(), _events())


@respx.mock
async def test_editor_redacts_credentials_from_external_text():
    route = respx.post("http://litellm-proxy:4000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": _response_content()}}]},
        )
    )
    events = [
        TransitionEvent(
            type=EventType.INCIDENT_OPENED,
            provider="x",
            title="token=secret-value",
            detail="https://open.feishu.cn/open-apis/bot/v2/hook/credential",
        )
    ]
    async with httpx.AsyncClient() as client:
        editor = StatusEditor(
            client,
            base_url="http://litellm-proxy:4000",
            api_key="sk-test",
            model="gemini/gemini-2.5-flash",
        )
        await editor.analyze(_snapshot(), events)

    body = route.calls[0].request.content.decode()
    assert "secret-value" not in body
    assert "/hook/credential" not in body
    assert "<redacted>" in body
