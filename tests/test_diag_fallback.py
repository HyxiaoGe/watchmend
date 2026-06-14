# tests/test_diag_fallback.py
import json

import httpx
import respx

from sentinel.findings import EventRecord
from sentinel.llm_config import LLMProfile
from sentinel.llm_driver import LLMDriver

ACTIVE = LLMProfile(name="active", base_url="http://active/v1", api_key="", model="m")
FB = LLMProfile(name="fb", base_url="http://fb/v1", api_key="", model="m")
DIAG = {"summary": "s", "root_cause": "r", "confidence": "low"}
FINAL = "```json\n" + json.dumps(DIAG, ensure_ascii=False) + "\n```"


def _settings(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    from sentinel.config import Settings

    return Settings(_env_file=None)


def _event():
    return EventRecord(
        id=1,
        ts=1700000000,
        rule="mem_pressure",
        subject="api",
        severity="warning",
        status="open",
        detail="d",
        payload_json="{}",
        diagnosis_status="pending",
        diagnosis_json=None,
        cooldown_until=0,
        resolved_ts=None,
    )


def _msg(content):
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


async def test_fallback_used_after_active_exhausts(monkeypatch):
    from sentinel.app import _diagnose_with_fallback

    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, _settings(monkeypatch))
        with respx.mock:
            active = respx.post("http://active/v1/chat/completions").mock(
                side_effect=[_msg("说不清"), _msg("还是说不清")]  # 两次都解析不出 json
            )
            fb = respx.post("http://fb/v1/chat/completions").mock(return_value=_msg(FINAL))
            diagnosis, _, _ = await _diagnose_with_fallback(driver, _event(), ACTIVE, FB)
    assert diagnosis == DIAG
    assert active.call_count == 2  # active 重试 _DIAG_ATTEMPTS 次
    assert fb.call_count == 1  # 转 fallback 一轮


async def test_no_fallback_returns_none(monkeypatch):
    from sentinel.app import _diagnose_with_fallback

    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, _settings(monkeypatch))
        with respx.mock:
            active = respx.post("http://active/v1/chat/completions").mock(
                side_effect=[_msg("说不清"), _msg("还是说不清")]
            )
            diagnosis, raw, _ = await _diagnose_with_fallback(driver, _event(), ACTIVE, None)
    assert diagnosis is None
    assert active.call_count == 2
    assert "说不清" in raw


async def test_active_first_try_skips_fallback(monkeypatch):
    from sentinel.app import _diagnose_with_fallback

    async with httpx.AsyncClient() as client:
        driver = LLMDriver(client, _settings(monkeypatch))
        with respx.mock:
            active = respx.post("http://active/v1/chat/completions").mock(return_value=_msg(FINAL))
            fb = respx.post("http://fb/v1/chat/completions").mock(return_value=_msg(FINAL))
            diagnosis, _, _ = await _diagnose_with_fallback(driver, _event(), ACTIVE, FB)
    assert diagnosis == DIAG
    assert active.call_count == 1
    assert fb.call_count == 0  # active 首胜,不碰 fallback
