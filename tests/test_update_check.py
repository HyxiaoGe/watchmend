# tests/test_update_check.py
import httpx
import pytest
import respx

from sentinel.update_check import fetch_latest, is_newer


@pytest.mark.parametrize(
    "latest,current,expected",
    [
        ("0.9.1", "0.9.0", True),
        ("v0.9.1", "0.9.0", True),
        ("0.10.0", "0.9.9", True),
        ("1.0.0", "0.9.0", True),
        ("0.9.0", "0.9.0", False),
        ("0.8.0", "0.9.0", False),
        (None, "0.9.0", False),
        ("", "0.9.0", False),
        ("v0.9.1-rc1", "0.9.0", False),  # 非纯数字段 → 安全降级
        ("garbage", "0.9.0", False),
    ],
)
def test_is_newer(latest, current, expected):
    assert is_newer(latest, current) is expected


@respx.mock
async def test_fetch_latest_ok():
    respx.get("https://api.test/releases/latest").mock(
        return_value=httpx.Response(
            200, json={"tag_name": "v0.9.1", "html_url": "https://github.com/x/y/releases/v0.9.1"}
        )
    )
    async with httpx.AsyncClient() as client:
        result = await fetch_latest(
            client, "https://api.test/releases/latest", user_agent="wm/test"
        )
    assert result == ("v0.9.1", "https://github.com/x/y/releases/v0.9.1")


@respx.mock
async def test_fetch_latest_network_error_returns_none():
    respx.get("https://api.test/releases/latest").mock(side_effect=httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as client:
        result = await fetch_latest(
            client, "https://api.test/releases/latest", user_agent="wm/test"
        )
    assert result is None


@respx.mock
async def test_fetch_latest_missing_tag_returns_none():
    respx.get("https://api.test/releases/latest").mock(
        return_value=httpx.Response(200, json={"html_url": "https://x"})
    )
    async with httpx.AsyncClient() as client:
        result = await fetch_latest(
            client, "https://api.test/releases/latest", user_agent="wm/test"
        )
    assert result is None
