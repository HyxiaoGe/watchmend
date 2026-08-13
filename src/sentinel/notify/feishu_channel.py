# src/sentinel/notify/feishu_channel.py
"""把 Notification 按 kind 分派回现有 feishu/cards.py builder(零改动 → 逐字节一致),
经现有 FeishuClient(传输 + HMAC 签名 + 限流)投递。data 里携带每个 builder 的原始入参。
"""

from __future__ import annotations

from sentinel.codex_reset.notify import build_codex_reset_card
from sentinel.feishu.cards import (
    build_card,
    build_codex_turn_card,
    build_daily_report_card,
    build_diagnosis_card,
    build_digest_card,
    build_event_card,
    build_heartbeat_card,
    build_recovery_card,
    build_status_editor_card,
    build_summary_card,
)
from sentinel.feishu.client import FeishuClient
from sentinel.notify.message import Kind, Notification


def render_card(n: Notification) -> dict:
    d = n.data
    if n.kind == Kind.ALERT:
        return build_event_card(d["finding"], now_str=d["now_str"])
    if n.kind == Kind.RECOVERY:
        return build_recovery_card(d["event"], now_ts=d["now_ts"], now_str=d["now_str"])
    if n.kind == Kind.VENDOR_INCIDENT:
        if d.get("analysis") is not None:
            return build_status_editor_card(
                d["provider_display"],
                d["analysis"],
                d["events"],
                d["status_url"],
                now_str=d["now_str"],
                model=d["editor_model"],
            )
        return build_card(d["provider_display"], d["events"], d["status_url"], now_str=d["now_str"])
    if n.kind == Kind.HEARTBEAT:
        return build_heartbeat_card(d["snapshots"], now_str=d["now_str"], interval=d["interval"])
    if n.kind == Kind.REPORT:
        return build_daily_report_card(
            d["stats"],
            date_str=d["date_str"],
            now_str=d["now_str"],
            open_events=d["open_events"],
            resolved_24h=d["resolved_24h"],
            digest_items=d.get("digest_items"),
        )
    if n.kind == Kind.DIGEST:
        return build_digest_card(
            d["items"],
            window_label=d["window_label"],
            now_str=d["now_str"],
        )
    if n.kind == Kind.CODEX_TURN:
        return build_codex_turn_card(
            project=d["project"],
            cwd=d["cwd"],
            task_summary=d["task_summary"],
            result_summary=d["result_summary"],
            thread_id=d["thread_id"],
            turn_id=d["turn_id"],
            now_str=d["now_str"],
            category=d.get("category", "turn_complete"),
        )
    if n.kind == Kind.CODEX_RESET:
        return build_codex_reset_card(
            d["event"],
            d["stage"],
            now_str=d["now_str"],
            utc_offset=d["utc_offset"],
        )
    if n.kind == Kind.DIAGNOSIS:
        return build_diagnosis_card(d["event"], d["diagnosis"], now_str=d["now_str"])
    if n.kind == Kind.SUMMARY:
        return build_summary_card(d["text"], date_str=d["date_str"], now_str=d["now_str"])
    raise ValueError(f"unknown notification kind: {n.kind}")


class FeishuChannel:
    name = "feishu"

    def __init__(self, client: FeishuClient) -> None:
        self._client = client

    async def send(self, n: Notification) -> None:
        await self._client.send(render_card(n))
