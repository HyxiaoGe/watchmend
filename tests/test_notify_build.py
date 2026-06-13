# tests/test_notify_build.py
from sentinel.events import EventType, TransitionEvent
from sentinel.findings import EventRecord, Finding
from sentinel.models import Indicator, ServiceDayStats, Snapshot
from sentinel.notify.build import (
    alert_notification,
    diagnosis_notification,
    heartbeat_notification,
    recovery_notification,
    report_notification,
    summary_notification,
    vendor_incident_notification,
)
from sentinel.notify.message import Kind, Severity


def _event(**kw):
    base = dict(
        id=1,
        ts=1000,
        rule="mem_pressure",
        subject="swap",
        severity="warning",
        status="open",
        detail="swap 85%",
        payload_json="{}",
        diagnosis_status="pending",
        diagnosis_json=None,
        cooldown_until=0,
        resolved_ts=None,
    )
    base.update(kw)
    return EventRecord(**base)


def _snap(provider, indicator=Indicator.NONE, incidents=None):
    return Snapshot(
        provider=provider,
        display_name=provider,
        indicator=indicator,
        status_url="https://x",
        components=[],
        incidents=incidents or [],
        fetched_at="2026-06-13T00:00:00Z",
    )


def _stats(service, total, ok, p95=None, baseline=None):
    return ServiceDayStats(
        service=service, total=total, ok_count=ok, p50_ms=None, p95_ms=p95, baseline_p95_ms=baseline
    )


def test_alert_maps_severity_and_carries_finding_for_feishu():
    f = Finding(rule="disk_usage", subject="/", severity="critical", detail="使用率 86%")
    n = alert_notification(f, now_ts=1000, now_str="2026-06-13 09:00:00")
    assert n.kind is Kind.ALERT and n.severity is Severity.CRITICAL
    assert "磁盘水位" in n.title and "/" in n.title
    assert n.detail == "使用率 86%" and n.subject == "/" and n.ts == 1000
    assert n.data["finding"] is f and n.data["now_str"] == "2026-06-13 09:00:00"


def test_recovery_is_info_with_duration():
    ev = _event(ts=1000)
    n = recovery_notification(ev, now_ts=1000 + 3 * 3600 + 5 * 60, now_str="x")
    assert n.kind is Kind.RECOVERY and n.severity is Severity.INFO
    assert "已恢复" in n.title and "内存压力" in n.title
    assert "持续 3h05m" in n.detail and "触发时:swap 85%" in n.detail
    assert n.data["event"] is ev and n.data["now_ts"] == 1000 + 3 * 3600 + 5 * 60


def test_vendor_incident_severity_from_impact():
    ev = TransitionEvent(
        type=EventType.INCIDENT_OPENED,
        provider="OpenAI",
        title="API 故障",
        detail="500s",
        impact=Indicator.MAJOR,
    )
    n = vendor_incident_notification("OpenAI", [ev], "https://status", now_ts=10, now_str="x")
    assert n.kind is Kind.VENDOR_INCIDENT and n.severity is Severity.CRITICAL
    assert n.link == "https://status" and "API 故障" in n.detail
    assert n.data["events"] == [ev] and n.data["status_url"] == "https://status"


def test_vendor_incident_fetch_failed_is_info():
    ev = TransitionEvent(
        type=EventType.FETCH_FAILED, provider="OpenAI", title="无法获取", detail="网络"
    )
    n = vendor_incident_notification("OpenAI", [ev], "", now_ts=10, now_str="x")
    assert n.severity is Severity.INFO and n.link is None


def test_heartbeat_all_ok_is_info():
    n = heartbeat_notification([_snap("a"), _snap("b")], now_ts=10, now_str="x", interval=60)
    assert n.kind is Kind.HEARTBEAT and n.severity is Severity.INFO
    assert "全部正常" in n.title
    assert ("正常", "2/2") in n.fields
    assert n.data["interval"] == 60 and len(n.data["snapshots"]) == 2


def test_heartbeat_with_incident_is_warning():
    from sentinel.models import Incident, IncidentStatus

    inc = Incident(
        key="i1",
        title="x",
        status=IncidentStatus.INVESTIGATING,
        impact=Indicator.MAJOR,
        url="https://x",
        started_at=None,
        updated_at=None,
        latest_update_id="u",
    )
    n = heartbeat_notification([_snap("a", incidents=[inc])], now_ts=10, now_str="x", interval=60)
    assert n.severity is Severity.WARNING and "存在异常" in n.title


def test_report_severity_and_fields():
    healthy = report_notification(
        [_stats("auth", 100, 100, p95=20.0)], date_str="2026-06-13", now_str="x", now_ts=10
    )
    assert healthy.kind is Kind.REPORT and healthy.severity is Severity.INFO
    assert ("未决事件", "0 起") in healthy.fields
    assert healthy.data["stats"][0].service == "auth"

    crit = report_notification(
        [_stats("auth", 100, 80, p95=20.0)], date_str="2026-06-13", now_str="x", now_ts=10
    )
    assert crit.severity is Severity.CRITICAL  # 可用率 80% < 95


def test_report_open_critical_event_forces_critical():
    n = report_notification(
        [_stats("auth", 100, 100, p95=20.0)],
        date_str="2026-06-13",
        now_str="x",
        now_ts=10,
        open_events=[_event(severity="critical", rule="container_down", subject="api")],
    )
    assert n.severity is Severity.CRITICAL
    assert ("未决事件", "1 起") in n.fields
    assert n.data["open_events"][0].subject == "api"


def test_diagnosis_info_with_fields():
    ev = _event(id=7)
    n = diagnosis_notification(
        ev,
        {"summary": "内存高", "root_cause": "泄漏", "confidence": "low"},
        now_ts=10,
        now_str="x",
    )
    assert n.kind is Kind.DIAGNOSIS and n.severity is Severity.INFO
    assert "诊断" in n.title and "内存压力" in n.title
    assert ("推测根因", "泄漏") in n.fields and ("置信度", "low") in n.fields
    assert n.data["event"] is ev and n.data["diagnosis"]["root_cause"] == "泄漏"


def test_summary_info_carries_text():
    n = summary_notification("一切平稳", date_str="2026-06-13", now_ts=10, now_str="x")
    assert n.kind is Kind.SUMMARY and n.severity is Severity.INFO
    assert n.detail == "一切平稳" and n.data["text"] == "一切平稳"
