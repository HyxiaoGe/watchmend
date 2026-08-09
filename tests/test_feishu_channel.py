# tests/test_feishu_channel.py
import httpx
import pytest
import respx

from sentinel.events import EventType, TransitionEvent
from sentinel.feishu.cards import (
    build_card,
    build_codex_turn_card,
    build_daily_report_card,
    build_diagnosis_card,
    build_event_card,
    build_heartbeat_card,
    build_recovery_card,
    build_summary_card,
)
from sentinel.feishu.client import FeishuClient
from sentinel.findings import EventRecord, Finding
from sentinel.models import Indicator, ServiceDayStats, Snapshot
from sentinel.notify.build import (
    alert_notification,
    codex_turn_notification,
    diagnosis_notification,
    heartbeat_notification,
    recovery_notification,
    report_notification,
    summary_notification,
    vendor_incident_notification,
)
from sentinel.notify.feishu_channel import FeishuChannel, render_card


def _event(**kw):
    base = dict(
        id=3,
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


def _snap(p):
    return Snapshot(
        provider=p,
        display_name=p,
        indicator=Indicator.NONE,
        status_url="https://x",
        components=[],
        incidents=[],
        fetched_at="2026-06-13T00:00:00Z",
    )


def _stats(s, total, ok, p95=None):
    return ServiceDayStats(
        service=s, total=total, ok_count=ok, p50_ms=None, p95_ms=p95, baseline_p95_ms=None
    )


def test_alert_renders_byte_identical_to_build_event_card():
    f = Finding(rule="disk_usage", subject="/", severity="critical", detail="使用率 86%")
    n = alert_notification(f, now_ts=1000, now_str="2026-06-13 09:00:00")
    assert render_card(n) == build_event_card(f, now_str="2026-06-13 09:00:00")


def test_recovery_renders_byte_identical_and_is_green():
    ev = _event()
    n = recovery_notification(ev, now_ts=5000, now_str="x")
    card = render_card(n)
    assert card == build_recovery_card(ev, now_ts=5000, now_str="x")
    assert card["card"]["header"]["template"] == "green"


def test_vendor_incident_renders_byte_identical():
    ev = TransitionEvent(
        type=EventType.INCIDENT_OPENED,
        provider="OpenAI",
        title="API 故障",
        detail="500s",
        impact=Indicator.MAJOR,
    )
    n = vendor_incident_notification("OpenAI", [ev], "https://status", now_ts=10, now_str="now")
    assert render_card(n) == build_card("OpenAI", [ev], "https://status", now_str="now")


def test_heartbeat_renders_byte_identical():
    snaps = [_snap("a"), _snap("b")]
    n = heartbeat_notification(snaps, now_ts=10, now_str="2026-06-13 09:00", interval=60)
    assert render_card(n) == build_heartbeat_card(snaps, now_str="2026-06-13 09:00", interval=60)


def test_report_renders_byte_identical_and_keeps_open_events():
    stats = [_stats("auth", 100, 100, p95=30.0)]
    opens = [_event(rule="mem_pressure", subject="swap")]
    n = report_notification(
        stats, date_str="2026-06-13", now_str="now", now_ts=10, open_events=opens, resolved_24h=2
    )
    card = render_card(n)
    assert card == build_daily_report_card(
        stats, date_str="2026-06-13", now_str="now", open_events=opens, resolved_24h=2
    )
    contents = [e["text"]["content"] for e in card["card"]["elements"] if e.get("tag") == "div"]
    assert any("未决事件 1 起" in c for c in contents)
    assert any("内存压力 · swap" in c for c in contents)


def test_diagnosis_renders_byte_identical_and_is_blue():
    ev = _event(id=9)
    diag = {"summary": "s", "root_cause": "r", "confidence": "low"}
    n = diagnosis_notification(ev, diag, now_ts=10, now_str="now")
    card = render_card(n)
    assert card == build_diagnosis_card(ev, diag, now_str="now")
    assert card["card"]["header"]["template"] == "blue"


def test_summary_renders_byte_identical():
    n = summary_notification("一切平稳", date_str="2026-06-13", now_ts=10, now_str="now")
    assert render_card(n) == build_summary_card("一切平稳", date_str="2026-06-13", now_str="now")


def test_codex_turn_renders_byte_identical():
    kwargs = {
        "project": "watchmend",
        "cwd": "/workspace/watchmend",
        "task_summary": "接入通知",
        "result_summary": "回合完成",
        "thread_id": "thr_123",
        "turn_id": "turn_456",
        "now_str": "now",
    }
    n = codex_turn_notification(**kwargs, now_ts=10)
    assert render_card(n) == build_codex_turn_card(**kwargs)


@pytest.mark.asyncio
async def test_channel_posts_rendered_card_via_feishu_client():
    captured = {}

    def _cap(request):
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 0})

    with respx.mock:
        respx.post("https://open.feishu.cn/hook/T").mock(side_effect=_cap)
        async with httpx.AsyncClient() as client:
            fc = FeishuClient(client, "https://open.feishu.cn/hook/T", min_interval=0)
            ch = FeishuChannel(fc)
            assert ch.name == "feishu"
            f = Finding(rule="disk_usage", subject="/", severity="critical", detail="d")
            await ch.send(alert_notification(f, now_ts=1, now_str="now"))
    assert captured["msg_type"] == "interactive"
    assert "磁盘水位" in captured["card"]["header"]["title"]["content"]
