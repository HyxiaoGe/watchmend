# src/sentinel/notify/build.py
"""domain 对象 → Notification(语义产出)。

每个 producer 填两份表示:title/detail/fields/severity 供文本渠道与 webhook;
data 携带现有 feishu builder 所需的原始入参(domain 对象 + now_str/now_ts/date_str),
供 FeishuChannel 逐字节复刻今天的卡片。文本渲染是有意更简的原生视图,非飞书复刻。
"""

from __future__ import annotations

from sentinel.events import EventType, TransitionEvent
from sentinel.findings import RULE_NAMES, DigestItem, EventRecord, Finding
from sentinel.models import Indicator, ServiceDayStats, Snapshot
from sentinel.notify.message import Kind, Notification, Severity
from sentinel.status_editor import StatusAnalysis

_GREY_TYPES = {EventType.BASELINE, EventType.FETCH_FAILED}
_RESOLVED_TYPES = {EventType.INCIDENT_RESOLVED, EventType.COMPONENT_RECOVERED}
# 镜像 cards._REPORT_RED_UPTIME(95.0):可用率低于此判异常。文本渠道严重度口径,
# 与飞书头色逻辑独立(飞书走 cards._report_color)。
_REPORT_UPTIME_FLOOR = 95.0


def alert_notification(finding: Finding, *, now_ts: int, now_str: str) -> Notification:
    rule_name = RULE_NAMES.get(finding.rule, finding.rule)
    return Notification(
        kind=Kind.ALERT,
        severity=Severity(finding.severity),
        title=f"{rule_name} · {finding.subject}",
        detail=finding.detail,
        subject=finding.subject,
        ts=now_ts,
        data={"finding": finding, "now_str": now_str},
    )


def recovery_notification(event: EventRecord, *, now_ts: int, now_str: str) -> Notification:
    rule_name = RULE_NAMES.get(event.rule, event.rule)
    mins = max(0, now_ts - event.ts) // 60
    duration = f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"
    return Notification(
        kind=Kind.RECOVERY,
        severity=Severity.INFO,
        title=f"已恢复 · {rule_name} · {event.subject}",
        detail=f"持续 {duration}\n触发时:{event.detail}",
        subject=event.subject,
        ts=now_ts,
        data={"event": event, "now_ts": now_ts, "now_str": now_str},
    )


def _vendor_severity(events: list[TransitionEvent]) -> Severity:
    types = {e.type for e in events}
    if types & _GREY_TYPES:
        return Severity.INFO
    worst = max(
        (e.impact for e in events if e.impact is not None), key=lambda x: x.rank, default=None
    )
    if worst in (Indicator.MAJOR, Indicator.CRITICAL):
        return Severity.CRITICAL
    if worst is Indicator.MINOR:
        return Severity.WARNING
    if types <= _RESOLVED_TYPES:
        return Severity.INFO
    return Severity.WARNING


def vendor_incident_notification(
    provider_display: str,
    events: list[TransitionEvent],
    status_url: str,
    *,
    now_ts: int,
    now_str: str,
    analysis: StatusAnalysis | None = None,
    editor_model: str = "",
) -> Notification:
    lines = []
    for ev in events:
        line = ev.title
        if ev.detail:
            line += f"\n{ev.detail}"
        lines.append(line)
    title = f"{provider_display} 状态变更"
    detail = "\n\n".join(lines)
    severity = _vendor_severity(events)
    if analysis is not None:
        title = f"{provider_display} · {analysis.headline}"
        detail = "\n".join(
            [
                analysis.summary,
                f"影响：{analysis.impact_summary}",
                f"建议：{analysis.recommended_action}",
            ]
        )
        severity = Severity(analysis.severity)
    return Notification(
        kind=Kind.VENDOR_INCIDENT,
        severity=severity,
        title=title,
        detail=detail,
        subject=provider_display,
        link=status_url or None,
        ts=now_ts,
        data={
            "provider_display": provider_display,
            "events": events,
            "status_url": status_url,
            "now_str": now_str,
            "analysis": analysis,
            "editor_model": editor_model,
        },
    )


def _provider_ok(snap: Snapshot) -> bool:
    return not snap.incidents and snap.indicator.rank < Indicator.MAJOR.rank


def heartbeat_notification(
    snapshots: list[Snapshot], *, now_ts: int, now_str: str, interval: int
) -> Notification:
    flags = [(s, _provider_ok(s)) for s in snapshots]
    all_ok = bool(flags) and all(ok for _, ok in flags)
    ok_count = sum(1 for _, ok in flags if ok)
    inc_total = sum(len(s.incidents) for s in snapshots)
    if not flags:
        title, severity = "外部依赖巡检 · 暂无数据", Severity.INFO
    elif all_ok:
        title, severity = "外部依赖巡检 · 全部正常", Severity.INFO
    else:
        title, severity = "外部依赖巡检 · 存在异常", Severity.WARNING
    lines = [f"{s.display_name} {'🟢' if ok else '🔴'}" for s, ok in flags]
    detail = "　".join(lines) if lines else "(暂无快照数据)"
    return Notification(
        kind=Kind.HEARTBEAT,
        severity=severity,
        title=title,
        detail=detail,
        fields=[
            ("正常", f"{ok_count}/{len(snapshots)}"),
            ("未解决事件", f"{inc_total} 起"),
            ("巡检周期", f"{interval}s"),
        ],
        ts=now_ts,
        data={"snapshots": snapshots, "now_str": now_str, "interval": interval},
    )


def _report_severity(stats: list[ServiceDayStats], open_events: list[EventRecord]) -> Severity:
    if not stats:
        return Severity.INFO
    if any(e.severity == "critical" for e in open_events):
        return Severity.CRITICAL
    if any(s.uptime_pct < _REPORT_UPTIME_FLOOR for s in stats):
        return Severity.CRITICAL
    if open_events or any(s.ok_count < s.total for s in stats):
        return Severity.WARNING
    return Severity.INFO


def report_notification(
    stats: list[ServiceDayStats],
    *,
    date_str: str,
    now_str: str,
    now_ts: int = 0,
    open_events: list[EventRecord] | None = None,
    resolved_24h: int = 0,
    digest_items: list[DigestItem] | None = None,
) -> Notification:
    open_events = open_events or []
    ordered = sorted(stats, key=lambda s: (s.uptime_pct, -(s.p95_ms or 0)))
    lines = []
    for s in ordered:
        if s.total == 0:
            lines.append(f"{s.service} 无样本")
            continue
        line = f"{s.service} {s.uptime_pct:.1f}%"
        if s.p95_ms is not None:
            line += f" · p95 {s.p95_ms:.0f}ms"
        lines.append(line)
    detail = "\n".join(lines) if lines else "(暂无探针数据)"
    total_probes = sum(s.total for s in stats)
    failed = sum(s.total - s.ok_count for s in stats)
    return Notification(
        kind=Kind.REPORT,
        severity=_report_severity(stats, open_events),
        title=f"内部体检日报 {date_str}",
        detail=detail,
        fields=[
            ("总探针", f"{total_probes} 次"),
            ("失败", f"{failed} 次"),
            ("未决事件", f"{len(open_events)} 起"),
        ],
        ts=now_ts,
        data={
            "stats": stats,
            "date_str": date_str,
            "now_str": now_str,
            "open_events": open_events,
            "resolved_24h": resolved_24h,
            "digest_items": digest_items or [],
        },
    )


def digest_notification(
    items: list[DigestItem],
    *,
    window_label: str,
    now_ts: int,
    now_str: str,
) -> Notification:
    lines = []
    for item in items[:6]:
        state = "（已恢复）" if item.state == "resolved" else ""
        lines.append(
            f"{RULE_NAMES.get(item.rule, item.rule)} · {item.subject}{state}"
            f" · 观察 {item.occurrences} 轮"
        )
    if len(items) > 6:
        lines.append(f"其余 {len(items) - 6} 类已合并")
    return Notification(
        kind=Kind.DIGEST,
        severity=Severity.INFO,
        title=f"巡检摘要 · {window_label}",
        detail="\n".join(lines),
        ts=now_ts,
        data={
            "items": items,
            "window_label": window_label,
            "now_str": now_str,
        },
    )


def codex_turn_notification(
    *,
    project: str,
    cwd: str,
    task_summary: str,
    result_summary: str,
    thread_id: str,
    turn_id: str,
    now_ts: int,
    now_str: str,
) -> Notification:
    """Codex 主回合结束通知。

    agent-turn-complete 只证明回合已经结束，不能证明代码、测试或部署成功；因此固定使用
    INFO 语义和“回合完成”标题，具体结果只展示 Codex 最终摘要。
    """
    return Notification(
        kind=Kind.CODEX_TURN,
        severity=Severity.INFO,
        title=f"Codex 回合完成 · {project}",
        detail=result_summary,
        fields=[("目录", cwd), ("任务", task_summary)],
        subject=project,
        ts=now_ts,
        data={
            "project": project,
            "cwd": cwd,
            "task_summary": task_summary,
            "result_summary": result_summary,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "now_str": now_str,
        },
    )


def diagnosis_notification(
    event: EventRecord, diagnosis: dict, *, now_ts: int, now_str: str
) -> Notification:
    rule_name = RULE_NAMES.get(event.rule, event.rule)
    fields: list[tuple[str, str]] = []
    if diagnosis.get("root_cause"):
        fields.append(("推测根因", str(diagnosis["root_cause"])))
    if diagnosis.get("confidence"):
        fields.append(("置信度", str(diagnosis["confidence"])))
    detail_parts = [f"事件 #{event.id} {event.detail}"]
    if diagnosis.get("summary"):
        detail_parts.append(str(diagnosis["summary"]))
    return Notification(
        kind=Kind.DIAGNOSIS,
        severity=Severity.INFO,
        title=f"诊断 · {rule_name} · {event.subject}",
        detail="\n".join(detail_parts),
        fields=fields,
        subject=event.subject,
        ts=now_ts,
        data={"event": event, "diagnosis": diagnosis, "now_str": now_str},
    )


def summary_notification(text: str, *, date_str: str, now_ts: int, now_str: str) -> Notification:
    return Notification(
        kind=Kind.SUMMARY,
        severity=Severity.INFO,
        title=f"体检日报 AI 总结 · {date_str}",
        detail=text,
        ts=now_ts,
        data={"text": text, "date_str": date_str, "now_str": now_str},
    )
