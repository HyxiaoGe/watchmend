# src/sentinel/notify/telegram.py
from __future__ import annotations

import httpx

from sentinel.notify.message import Notification
from sentinel.notify.render import body_text, lead_emoji


class TelegramError(Exception):
    """Telegram 投递失败。异常文本绝不含 bot token(token 在 URL 里)。"""


def _esc(s: str) -> str:
    # HTML parse_mode 只需转义 & < > 三个;远比 MarkdownV2 那串转义易于无 bug 实现
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramChannel:
    name = "telegram"

    def __init__(
        self, client: httpx.AsyncClient, bot_token: str, chat_id: str, *, timeout: float = 10.0
    ) -> None:
        self._client = client
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout

    async def send(self, n: Notification) -> None:
        text = f"{lead_emoji(n)} <b>{_esc(n.title)}</b>"
        body = body_text(n)
        if body:
            text += f"\n\n{_esc(body)}"
        if n.link:
            text += f'\n\n<a href="{_esc(n.link)}">详情</a>'
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        # 不用 raise_for_status:其异常 str 含完整 URL(带 token)→ 会经 Broadcaster 进日志
        try:
            resp = await self._client.post(self._url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as err:  # 网络异常的 str 也可能含 URL,统一抹掉
            raise TelegramError("telegram 请求失败(网络)") from err
        if resp.status_code >= 400:
            raise TelegramError(f"telegram HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as err:
            raise TelegramError("telegram 响应非 JSON") from err
        if not data.get("ok"):
            raise TelegramError(f"telegram 拒绝: {data.get('description')}")
