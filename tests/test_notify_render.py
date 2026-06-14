# tests/test_notify_render.py
from sentinel.notify.message import Kind, Notification, Severity
from sentinel.notify.render import NTFY_PRIORITY, body_text, lead_emoji, ntfy_tags


def _n(kind=Kind.ALERT, severity=Severity.WARNING, detail="", fields=None):
    return Notification(kind=kind, severity=severity, title="t", detail=detail, fields=fields or [])


def test_lead_emoji_recovery_always_check():
    assert lead_emoji(_n(kind=Kind.RECOVERY, severity=Severity.INFO)) == "✅"


def test_lead_emoji_by_severity():
    assert lead_emoji(_n(severity=Severity.CRITICAL)) == "🔴"
    assert lead_emoji(_n(severity=Severity.WARNING)) == "🟠"
    assert lead_emoji(_n(severity=Severity.INFO)) == "🟢"


def test_ntfy_priority_map():
    assert NTFY_PRIORITY[Severity.CRITICAL] == 5
    assert NTFY_PRIORITY[Severity.WARNING] == 4
    assert NTFY_PRIORITY[Severity.INFO] == 3


def test_ntfy_tags_severity_plus_kind():
    tags = ntfy_tags(_n(kind=Kind.RECOVERY, severity=Severity.INFO))
    assert "white_check_mark" in tags  # kind tag
    assert "information_source" in tags  # severity tag


def test_ntfy_tags_alert_has_severity_tag_only():
    tags = ntfy_tags(_n(kind=Kind.ALERT, severity=Severity.CRITICAL))
    assert tags == ["rotating_light"]  # alert 无专属 kind tag,仅严重度


def test_body_text_joins_detail_and_fields():
    n = _n(detail="磁盘 86%", fields=[("阈值", "85%"), ("当前", "86%")])
    assert body_text(n) == "磁盘 86%\n阈值:85%\n当前:86%"


def test_body_text_empty_when_no_detail_no_fields():
    assert body_text(_n()) == ""
