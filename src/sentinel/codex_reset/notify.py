from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel.codex_reset.display import reset_type_label
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
        f"**类型**：{reset_type_label(event.reset_type)}",
    ]
    if event.reset_type is ResetType.BANKED and stage is ResetStage.ANNOUNCED:
        fields.append("**预计时间**：官方称当天内（以原帖表述为准）")
    elif (
        stage in {ResetStage.ANNOUNCED, ResetStage.DELAYED, ResetStage.CONFIRMED}
        and not event.silent
    ):
        fields.append(f"**预计窗口**：{_format_time(event.expected_start_ts, utc_offset)}")
        fields.append(f"**预计截止**：{_format_time(event.expected_end_ts, utc_offset)}")
    if stage is ResetStage.CONFIRMED:
        if event.silent:
            fields.append("**预告情况**：此前未发现预告，直接确认到账")
            fields.append(f"**监测确认时间**：{now_str}")
            fields.append(
                "**证据时间范围**："
                f"{_format_time(event.evidence_start_ts, utc_offset)} ～ "
                f"{_format_time(event.evidence_end_ts, utc_offset)}"
            )
            fields.append("时间来自公开记录与本机窗口，不代表每个账号的精确到账时刻。")
        else:
            fields.append(f"**确认时间**：{_format_time(event.confirmed_ts, utc_offset)}")
        if event.confirmation_basis:
            fields.append(f"**核验方式**：{event.confirmation_basis}")
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
    if event.translated_summary:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**中文翻译**\n{event.translated_summary[:1800]}",
                },
            }
        )
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
                "title": {
                    "tag": "plain_text",
                    "content": "Codex 静默重置已确认" if event.silent else _STAGE_TITLE[stage],
                },
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
    expected_label = (
        "官方称当天内（以原帖表述为准）"
        if event.reset_type is ResetType.BANKED and stage is ResetStage.ANNOUNCED
        else _format_time(event.expected_end_ts, utc_offset)
    )
    fields = [
        ("阶段", _STAGE_LABEL[stage]),
        ("类型", reset_type_label(event.reset_type)),
        ("预计时间", expected_label),
        ("公开来源族", str(len(event.source_families))),
    ]
    if event.silent:
        fields = [(key, value) for key, value in fields if key != "预计时间"]
        fields.extend(
            [
                ("预告情况", "此前未发现预告，直接确认到账"),
                ("核验方式", event.confirmation_basis),
            ]
        )
    return Notification(
        kind=Kind.CODEX_RESET,
        severity=_STAGE_SEVERITY[stage],
        title="Codex 静默重置已确认" if event.silent else _STAGE_TITLE[stage],
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
