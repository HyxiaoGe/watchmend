# tests/test_webhook_channel.py
import json

import httpx
import pytest
import respx

from sentinel.findings import Finding
from sentinel.notify.build import alert_notification
from sentinel.notify.message import Kind, Notification, Severity
from sentinel.notify.webhook import WebhookChannel, WebhookError


@pytest.mark.asyncio
async def test_posts_structured_json_with_bearer():
    captured = {}

    def _cap(request):
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        captured["ctype"] = request.headers.get("content-type")
        return httpx.Response(200)

    with respx.mock:
        respx.post("https://hook.example.org/wm").mock(side_effect=_cap)
        async with httpx.AsyncClient() as client:
            ch = WebhookChannel(client, "https://hook.example.org/wm", token="TESThooktok")
            assert ch.name == "webhook"
            n = Notification(
                kind=Kind.REPORT,
                severity=Severity.WARNING,
                title="日报",
                detail="正文",
                fields=[("总探针", "120 次")],
                subject="auth",
                ts=1700000000,
            )
            await ch.send(n)
    body = captured["body"]
    assert body["kind"] == "report" and body["severity"] == "warning"
    assert body["title"] == "日报" and body["fields"] == [["总探针", "120 次"]]
    assert body["subject"] == "auth" and body["ts"] == 1700000000
    assert captured["auth"] == "Bearer TESThooktok"
    assert "application/json" in captured["ctype"]


@pytest.mark.asyncio
async def test_serializes_dataclass_in_data():
    captured = {}

    def _cap(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200)

    with respx.mock:
        respx.post("https://hook.example.org/wm").mock(side_effect=_cap)
        async with httpx.AsyncClient() as client:
            f = Finding(rule="disk_usage", subject="/", severity="critical", detail="86%")
            n = alert_notification(f, now_ts=10, now_str="x")
            await WebhookChannel(client, "https://hook.example.org/wm").send(n)
    # data.finding 是 Finding dataclass → asdict 序列化为对象
    assert captured["body"]["data"]["finding"]["rule"] == "disk_usage"
    assert captured["body"]["data"]["finding"]["subject"] == "/"


@pytest.mark.asyncio
async def test_no_auth_header_when_token_absent():
    captured = {}

    def _cap(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(204)

    with respx.mock:
        respx.post("https://hook.example.org/wm").mock(side_effect=_cap)
        async with httpx.AsyncClient() as client:
            n = Notification(kind=Kind.ALERT, severity=Severity.INFO, title="t")
            await WebhookChannel(client, "https://hook.example.org/wm").send(n)
    assert captured["auth"] is None


@pytest.mark.asyncio
async def test_non_2xx_raises():
    with respx.mock:
        respx.post("https://hook.example.org/wm").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            n = Notification(kind=Kind.ALERT, severity=Severity.INFO, title="t")
            with pytest.raises(WebhookError):
                await WebhookChannel(client, "https://hook.example.org/wm").send(n)
