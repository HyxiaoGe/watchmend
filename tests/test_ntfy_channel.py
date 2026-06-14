# tests/test_ntfy_channel.py
import json

import httpx
import pytest
import respx

from sentinel.notify.message import Kind, Notification, Severity
from sentinel.notify.ntfy import NtfyChannel


def _n(**kw):
    base = dict(
        kind=Kind.ALERT, severity=Severity.CRITICAL, title="容器停止 · api", detail="已退出"
    )
    base.update(kw)
    return Notification(**base)


def test_split_url_rejects_missing_topic():
    with pytest.raises(ValueError, match="invalid ntfy url"):
        NtfyChannel(httpx.AsyncClient(), "https://ntfy.sh")


@pytest.mark.asyncio
async def test_publishes_json_to_server_root_with_topic_and_priority():
    captured = {}

    def _cap(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200)

    with respx.mock:
        respx.post("https://ntfy.sh/").mock(side_effect=_cap)
        async with httpx.AsyncClient() as client:
            ch = NtfyChannel(client, "https://ntfy.sh/my-watchmend-xyz", token="TESTntfytok")
            assert ch.name == "ntfy"
            await ch.send(_n(detail="已退出", title="容器停止 · api"))
    assert captured["url"] == "https://ntfy.sh/"
    assert captured["body"]["topic"] == "my-watchmend-xyz"
    assert captured["body"]["priority"] == 5  # critical
    assert "rotating_light" in captured["body"]["tags"]
    # 中文标题走 JSON body,UTF-8 安全
    assert captured["body"]["title"] == "🔴 容器停止 · api"
    assert captured["body"]["message"] == "已退出"
    assert captured["auth"] == "Bearer TESTntfytok"


@pytest.mark.asyncio
async def test_no_auth_header_when_token_absent_and_click_from_link():
    captured = {}

    def _cap(request):
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200)

    with respx.mock:
        respx.post("https://ntfy.example.org/").mock(side_effect=_cap)
        async with httpx.AsyncClient() as client:
            ch = NtfyChannel(client, "https://ntfy.example.org/topic")
            await ch.send(_n(severity=Severity.INFO, link="https://panel/x"))
    assert captured["auth"] is None
    assert captured["body"]["priority"] == 3
    assert captured["body"]["click"] == "https://panel/x"


@pytest.mark.asyncio
async def test_non_2xx_raises():
    from sentinel.notify.ntfy import NtfyError

    with respx.mock:
        respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            with pytest.raises(NtfyError):
                await NtfyChannel(client, "https://ntfy.sh/t").send(_n())
