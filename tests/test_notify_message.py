# tests/test_notify_message.py
from sentinel.notify.message import Kind, Notification, Severity


def test_severity_and_kind_are_str_enums():
    assert Severity.CRITICAL.value == "critical"
    assert Severity.WARNING.value == "warning"
    assert Severity.INFO.value == "info"
    assert Kind.ALERT.value == "alert"
    assert Kind.RECOVERY.value == "recovery"
    assert Kind.VENDOR_INCIDENT.value == "vendor_incident"
    assert Kind.HEARTBEAT.value == "heartbeat"
    assert Kind.REPORT.value == "report"
    assert Kind.DIAGNOSIS.value == "diagnosis"
    assert Kind.SUMMARY.value == "summary"


def test_notification_defaults_are_independent():
    a = Notification(kind=Kind.ALERT, severity=Severity.WARNING, title="t")
    b = Notification(kind=Kind.ALERT, severity=Severity.WARNING, title="t")
    a.fields.append(("k", "v"))
    a.data["x"] = 1
    assert b.fields == [] and b.data == {}  # default_factory 不共享可变默认
    assert a.detail == "" and a.subject == "" and a.link is None and a.ts == 0


def test_notification_holds_all_fields():
    n = Notification(
        kind=Kind.REPORT,
        severity=Severity.INFO,
        title="日报",
        detail="正文",
        fields=[("总探针", "120 次")],
        subject="auth",
        link="https://x",
        ts=1700000000,
        data={"k": "v"},
    )
    assert n.kind is Kind.REPORT and n.severity is Severity.INFO
    assert n.fields == [("总探针", "120 次")] and n.link == "https://x"
