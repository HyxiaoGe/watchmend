# tests/test_telegram_channel.py
import json
import logging

import httpx
import pytest
import respx

from sentinel.notify.base import Broadcaster
from sentinel.notify.message import Kind, Notification, Severity
from sentinel.notify.telegram import TelegramChannel, TelegramError

_TOKEN = "TESTtok123:AAfake"
_CHAT = "-100200300"
_URL = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"


def _n(**kw):
    base = dict(
        kind=Kind.ALERT, severity=Severity.WARNING, title="磁盘水位 · /", detail="使用率 86%"
    )
    base.update(kw)
    return Notification(**base)


@pytest.mark.asyncio
async def test_send_posts_html_payload_with_chat_and_escaping():
    captured = {}

    def _cap(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    with respx.mock:
        respx.post(_URL).mock(side_effect=_cap)
        async with httpx.AsyncClient() as client:
            ch = TelegramChannel(client, _TOKEN, _CHAT)
            await ch.send(_n(title="a <b> & c", detail="x > y"))
    assert ch.name == "telegram"
    assert captured["chat_id"] == _CHAT
    assert captured["parse_mode"] == "HTML"
    assert captured["disable_web_page_preview"] is True
    # < > & 被转义,绝不出现裸尖括号注入
    assert "&lt;b&gt;" in captured["text"] and "&amp;" in captured["text"]
    assert "x &gt; y" in captured["text"]


@pytest.mark.asyncio
async def test_recovery_uses_check_emoji():
    captured = {}

    def _cap(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    with respx.mock:
        respx.post(_URL).mock(side_effect=_cap)
        async with httpx.AsyncClient() as client:
            await TelegramChannel(client, _TOKEN, _CHAT).send(
                _n(kind=Kind.RECOVERY, severity=Severity.INFO, title="已恢复 · x")
            )
    assert captured["text"].startswith("✅")


@pytest.mark.asyncio
async def test_ok_false_raises_telegram_error_without_token():
    with respx.mock:
        respx.post(_URL).mock(
            return_value=httpx.Response(200, json={"ok": False, "description": "chat not found"})
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(TelegramError) as exc:
                await TelegramChannel(client, _TOKEN, _CHAT).send(_n())
    assert "chat not found" in str(exc.value)
    assert _TOKEN not in str(exc.value)  # 描述里不带 token


@pytest.mark.asyncio
async def test_http_error_does_not_leak_token_in_logs():
    # 401(token 错)走 status_code 分支,异常文本只含状态码;Broadcaster 记日志不得含 token
    with respx.mock:
        respx.post(_URL).mock(return_value=httpx.Response(401, json={"ok": False}))
        async with httpx.AsyncClient() as client:
            ch = TelegramChannel(client, _TOKEN, _CHAT)
            with caplog_capture() as records:
                ok = await Broadcaster([ch]).send(_n())
    assert ok == 0
    blob = " ".join(r.getMessage() for r in records)
    assert _TOKEN not in blob


class caplog_capture:
    """轻量捕获 sentinel.notify logger 的 ERROR 记录(避免依赖 caplog fixture 作用域)。"""

    def __init__(self):
        self._handler = logging.Handler()
        self.records = []
        self._handler.emit = self.records.append

    def __enter__(self):
        logging.getLogger("sentinel.notify").addHandler(self._handler)
        logging.getLogger("sentinel.notify").setLevel(logging.ERROR)
        return self.records

    def __exit__(self, *a):
        logging.getLogger("sentinel.notify").removeHandler(self._handler)
