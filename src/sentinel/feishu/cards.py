# src/sentinel/feishu/cards.py
from __future__ import annotations

from sentinel.events import EventType, TransitionEvent
from sentinel.findings import RULE_NAMES, EventRecord, Finding
from sentinel.models import Indicator, ServiceDayStats, Snapshot

# 严重度→飞书 header 色名(只用色名,绝不用 hex)
_RED = "red"
_ORANGE = "orange"
_GREEN = "green"
_GREY = "grey"
_BLUE = "blue"

_RESOLVED_LIKE = {EventType.INCIDENT_RESOLVED, EventType.COMPONENT_RECOVERED}
_GREY_LIKE = {EventType.BASELINE, EventType.FETCH_FAILED}


def _color(events: list[TransitionEvent]) -> str:
    types = {e.type for e in events}
    if types & _GREY_LIKE:
        return _GREY
    worst = max(
        (e.impact for e in events if e.impact is not None),
        key=lambda x: x.rank,
        default=None,
    )
    if worst in (Indicator.MAJOR, Indicator.CRITICAL):
        return _RED
    if worst is Indicator.MINOR:
        return _ORANGE
    if types <= _RESOLVED_LIKE:
        return _GREEN
    return _ORANGE


def build_card(
    provider_display: str,
    events: list[TransitionEvent],
    status_url: str,
    *,
    now_str: str,
) -> dict:
    """把某 provider 一轮的事件合并成一张飞书 v1 经典卡片。"""
    elements: list[dict] = []
    for ev in events:
        content = f"**{ev.title}**"
        if ev.detail:
            content += f"\n{ev.detail}"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})

    elements.append({"tag": "hr"})
    if status_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看状态页"},
                        "url": status_url,
                        "type": "primary",
                    }
                ],
            }
        )

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"{provider_display} · {now_str}"},
                "template": _color(events),
            },
            "elements": elements,
        },
    }


def _provider_ok(snap: Snapshot) -> bool:
    """无未解决事件且总指标 < major(裸 minor/none 视为正常,与差异器的抑制口径一致)。"""
    return not snap.incidents and snap.indicator.rank < Indicator.MAJOR.rank


def build_heartbeat_card(snapshots: list[Snapshot], *, now_str: str, interval: int) -> dict:
    """每日心跳:把各家当前快照汇总成一张'我还活着+整体状态'卡(发签名机器人,无需关键词)。"""
    flags = [(s, _provider_ok(s)) for s in snapshots]
    all_ok = bool(flags) and all(ok for _, ok in flags)  # 空快照≠全绿
    ok_count = sum(1 for _, ok in flags if ok)
    inc_total = sum(len(s.incidents) for s in snapshots)

    lines = []
    for s, ok in flags:
        if ok:
            lines.append(f"{s.display_name} 🟢")
            continue
        extra = []
        if s.indicator.rank >= Indicator.MAJOR.rank:
            extra.append(f"指标 {s.indicator.value}")
        if s.incidents:
            extra.append(f"{len(s.incidents)} 起事件")
        suffix = f"({' / '.join(extra)})" if extra else ""
        lines.append(f"{s.display_name} 🔴 {suffix}".rstrip())
    body = "　".join(lines) if lines else "(暂无快照数据)"

    summary = (
        f"{ok_count}/{len(snapshots)} 正常　·　当前未解决事件 {inc_total} 起"
        f"　·　每 {interval}s 巡检中"
    )
    if not flags:
        title = "⚪ 外部依赖巡检 · 暂无数据"
        template = _GREY
    elif all_ok:
        title = "🟢 外部依赖巡检 · 全部正常"
        template = _GREEN
    else:
        title = "🔴 外部依赖巡检 · 存在异常"
        template = _RED
    footer = f"🤖 dev-ops-sentinel · 每日心跳 · {now_str}"

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": footer}],
                },
            ],
        },
    }


# ---- 巡检事件卡 / 恢复卡(Phase 2) ----

_SEVERITY_COLOR = {"critical": _RED, "warning": _ORANGE}
_SEVERITY_ICON = {"critical": "🚨", "warning": "🟠"}


def _patrol_card(title: str, template: str, body: str, *, kind: str, now_str: str) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"🤖 dev-ops-sentinel · {kind} · {now_str}",
                        }
                    ],
                },
            ],
        },
    }


def build_event_card(finding: Finding, *, now_str: str) -> dict:
    """巡检事件卡:一事一卡(事件罕见,且便于 Phase 3 按事件跟进诊断)。"""
    rule_name = RULE_NAMES.get(finding.rule, finding.rule)
    icon = _SEVERITY_ICON.get(finding.severity, "🟠")
    return _patrol_card(
        f"{icon} {rule_name} · {finding.subject}",
        _SEVERITY_COLOR.get(finding.severity, _ORANGE),
        finding.detail,
        kind="巡检事件",
        now_str=now_str,
    )


def build_recovery_card(event: EventRecord, *, now_ts: int, now_str: str) -> dict:
    """恢复卡:open 事件条件解除时发,正文带持续时长与原始触发详情。"""
    rule_name = RULE_NAMES.get(event.rule, event.rule)
    mins = max(0, now_ts - event.ts) // 60
    duration = f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"
    return _patrol_card(
        f"✅ 已恢复 · {rule_name} · {event.subject}",
        _GREEN,
        f"持续 {duration}\n触发时:{event.detail}",
        kind="事件恢复",
        now_str=now_str,
    )


# ---- 内部体检日报 ----

# 日报头色阈值:任一服务可用率 <95%(或无样本)→ 红;有失败或 p95 超基线 1.5 倍 → 橙
_REPORT_RED_UPTIME = 95.0
_REPORT_DEGRADE_RATIO = 1.5
_REPORT_IMPROVE_RATIO = 0.7  # p95 较基线改善超 30% 才显示 ↓


def _report_color(stats: list[ServiceDayStats], open_events: list[EventRecord]) -> str:
    if not stats:
        return _GREY
    if any(e.severity == "critical" for e in open_events):
        return _RED
    if any(s.uptime_pct < _REPORT_RED_UPTIME for s in stats):
        return _RED
    degraded = any(
        s.p95_ms is not None
        and s.baseline_p95_ms is not None
        and s.p95_ms > s.baseline_p95_ms * _REPORT_DEGRADE_RATIO
        for s in stats
    )
    if degraded or open_events or any(s.ok_count < s.total for s in stats):
        return _ORANGE
    return _GREEN


def _trend(s: ServiceDayStats) -> str:
    if s.p95_ms is None or s.baseline_p95_ms is None:
        return ""
    if s.p95_ms > s.baseline_p95_ms * _REPORT_DEGRADE_RATIO:
        return f"↑(基线 {s.baseline_p95_ms:.0f}ms)"
    if s.p95_ms < s.baseline_p95_ms * _REPORT_IMPROVE_RATIO:
        return f"↓(基线 {s.baseline_p95_ms:.0f}ms)"
    return "→"


def _stat_line(s: ServiceDayStats) -> str:
    if s.total == 0:
        return f"🔴 {s.service} 无样本"
    if s.ok_count == s.total:
        icon = "🟢"
    elif s.uptime_pct >= _REPORT_RED_UPTIME:
        icon = "🟠"
    else:
        icon = "🔴"
    line = f"{icon} {s.service} {s.uptime_pct:.1f}%"
    if s.p95_ms is not None:
        line += f" · p95 {s.p95_ms:.0f}ms"
        trend = _trend(s)
        if trend:
            line += f" {trend}"
    return line


_REPORT_TITLES = {
    _GREEN: ("🟢", "全部正常"),
    _ORANGE: ("🟠", "存在波动"),
    _RED: ("🔴", "存在异常"),
    _GREY: ("⚪", "暂无数据"),
}


def _event_block(open_events: list[EventRecord], resolved_24h: int) -> str:
    if open_events:
        lines = "\n".join(f"🔥 {RULE_NAMES.get(e.rule, e.rule)} · {e.subject}" for e in open_events)
        return f"**未决事件 {len(open_events)} 起**\n{lines}"
    if resolved_24h:
        return f"✅ 无未决事件(24h 内恢复 {resolved_24h} 起)"
    return "✅ 无未决事件"


def build_daily_report_card(
    stats: list[ServiceDayStats],
    *,
    date_str: str,
    now_str: str,
    open_events: list[EventRecord] | None = None,
    resolved_24h: int = 0,
) -> dict:
    """内部体检日报:近 24h 各服务可用率 + p95 延迟 + 七日基线趋势 + 未决事件汇总。

    排序:可用率低在前;同档内 p95 高(慢)在前,便于盯性能退化。
    """
    open_events = open_events or []
    color = _report_color(stats, open_events)
    icon, suffix = _REPORT_TITLES[color]
    ordered = sorted(stats, key=lambda s: (s.uptime_pct, -(s.p95_ms or 0)))
    body = "\n".join(_stat_line(s) for s in ordered) if ordered else "(暂无探针数据)"

    full = sum(1 for s in stats if s.total > 0 and s.ok_count == s.total)
    total_probes = sum(s.total for s in stats)
    failed = sum(s.total - s.ok_count for s in stats)
    summary = f"{full}/{len(stats)} 服务全勤　·　总探针 {total_probes} 次　·　失败 {failed} 次"
    footer = f"🤖 dev-ops-sentinel · 内部体检日报 · {now_str}"

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{icon} 内部体检日报 {date_str} · {suffix}",
                },
                "template": color,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": _event_block(open_events, resolved_24h)},
                },
                {"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]},
            ],
        },
    }


# ---- 诊断卡 / 日报 AI 总结卡(Phase 3) ----


def build_diagnosis_card(event: EventRecord, diagnosis: dict, *, now_str: str) -> dict:
    """诊断结论卡(Phase 3):蓝色信息卡,字段缺失只省略不报错。"""
    rule_name = RULE_NAMES.get(event.rule, event.rule)
    lines = [f"**事件 #{event.id}**　{event.detail}"]
    if diagnosis.get("summary"):
        lines.append(f"**现象**：{diagnosis['summary']}")
    if diagnosis.get("root_cause"):
        lines.append(f"**推测根因**：{diagnosis['root_cause']}")
    evidence = diagnosis.get("evidence") or []
    if evidence:
        lines.append("**证据**：\n" + "\n".join(f"- {e}" for e in evidence))
    if diagnosis.get("confidence"):
        lines.append(f"**置信度**：{diagnosis['confidence']}")
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]
    commands = diagnosis.get("suggested_commands") or []
    if commands:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**建议命令（须人工确认执行）**：\n"
                    + "\n".join(f"`{c}`" for c in commands),
                },
            }
        )
    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"🤖 sentinel-diag · 会话 diag-evt-{event.id}"}
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
                    "content": f"🧠 诊断 · {rule_name} · {event.subject} · {now_str}",
                },
                "template": _BLUE,
            },
            "elements": elements,
        },
    }


def build_summary_card(text: str, *, date_str: str, now_str: str) -> dict:
    """日报跟帖式 AI 总结卡(Phase 3):总结失败不发卡,日报本体不受影响。"""
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📋 体检日报 AI 总结 · {date_str}"},
                "template": _GREEN,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"🤖 dev-ops-sentinel · {now_str}"}
                    ],
                },
            ],
        },
    }
