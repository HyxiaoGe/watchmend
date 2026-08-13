from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel.codex_reset.models import ResetEvent, ResetStage, ResetType
from sentinel.notify.message import Kind, Notification, Severity

_STAGE_LABEL = {
    ResetStage.HINT: "疑似预告",
    ResetStage.ANNOUNCED: "明确预告",
    ResetStage.CONFIRMED: "落地确认",
    ResetStage.DELAYED: "超过窗口",
}
_STAGE_TITLE = {
    ResetStage.HINT: "Codex 重置疑似预告",
    ResetStage.ANNOUNCED: "Codex 重置预告",
    ResetStage.CONFIRMED: "Codex 重置已确认",
    ResetStage.DELAYED: "Codex 重置延迟",
}
_STAGE_TEMPLATE = {
    ResetStage.HINT: "blue",
    ResetStage.ANNOUNCED: "orange",
    ResetStage.CONFIRMED: "green",
    ResetStage.DELAYED: "red",
}
_STAGE_SEVERITY = {
    ResetStage.HINT: Severity.INFO,
    ResetStage.ANNOUNCED: Severity.WARNING,
    ResetStage.CONFIRMED: Severity.INFO,
    ResetStage.DELAYED: Severity.CRITICAL,
}
_TYPE_LABEL = {ResetType.DIRECT: "直接重置", ResetType.BANKED: "Banked reset"}


def _format_time(timestamp: int | None, utc_offset: int) -> str:
    if timestamp is None:
        return "未提供"
    tz = timezone(timedelta(hours=utc_offset))
    return datetime.fromtimestamp(timestamp, tz=tz).strftime("%Y-%m-%d %H:%M:%S UTC%z")


def build_codex_reset_card(
    event: ResetEvent,
    stage: ResetStage,
    *,
    now_str: str,
    utc_offset: int,
) -> dict:
    fields = [
        f"**阶段**：{_STAGE_LABEL[stage]}",
        f"**类型**：{_TYPE_LABEL.get(event.reset_type, '待确认')}",
    ]
    if stage in {ResetStage.ANNOUNCED, ResetStage.DELAYED, ResetStage.CONFIRMED}:
        fields.append(f"**预计窗口**：{_format_time(event.expected_start_ts, utc_offset)}")
        fields.append(f"**预计截止**：{_format_time(event.expected_end_ts, utc_offset)}")
    if stage is ResetStage.CONFIRMED:
        fields.append(f"**确认时间**：{_format_time(event.confirmed_ts, utc_offset)}")
    fields.append(
        f"**确认依据**：{event.evidence_count} 条 / {len(event.source_families)} 个来源族"
    )
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(fields)}},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**摘要**\n{event.summary[:1800]}"},
        },
    ]
    if event.primary_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看原始来源"},
                        "url": event.primary_url,
                        "type": "primary",
                    }
                ],
            }
        )
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"🤖 WatchMend · Codex reset 监控 · {now_str}",
                }
            ],
        }
    )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": _STAGE_TITLE[stage]},
                "template": _STAGE_TEMPLATE[stage],
            },
            "elements": elements,
        },
    }


def codex_reset_notification(
    event: ResetEvent,
    stage: ResetStage,
    *,
    now_ts: int,
    now_str: str,
    utc_offset: int,
) -> Notification:
    fields = [
        ("阶段", _STAGE_LABEL[stage]),
        ("类型", _TYPE_LABEL.get(event.reset_type, "待确认")),
        ("预计截止", _format_time(event.expected_end_ts, utc_offset)),
        ("公开来源族", str(len(event.source_families))),
    ]
    return Notification(
        kind=Kind.CODEX_RESET,
        severity=_STAGE_SEVERITY[stage],
        title=_STAGE_TITLE[stage],
        detail=event.summary,
        fields=fields,
        subject=event.canonical_id,
        link=event.primary_url or None,
        ts=now_ts,
        data={
            "event": event,
            "stage": stage,
            "now_str": now_str,
            "utc_offset": utc_offset,
        },
    )
