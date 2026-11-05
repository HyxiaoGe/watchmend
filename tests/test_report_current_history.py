from dataclasses import asdict

from sentinel.feishu.cards import build_daily_report_card
from sentinel.findings import EventRecord
from sentinel.models import ServiceDayStats
from sentinel.notify.build import report_notification
from sentinel.notify.message import Severity
from sentinel.notify.routing import Priority, classify_notification


def _stats(name="search", total=1440, ok=467, p95=72, baseline=None):
    return ServiceDayStats(name, total, ok, p95, p95, baseline)


def _event(severity="critical"):
    return EventRecord(
        1, 1, "service_down", "search", severity, "open", "连续失败", "{}", "pending", None, 0, None
    )


def _render(stats, events=None):
    events = events or []
    card = build_daily_report_card(
        stats, date_str="2026-11-05", now_str="2026-11-05 09:00:00", open_events=events
    )
    n = report_notification(
        stats, date_str="2026-11-05", now_str="2026-11-05 09:00:00", now_ts=1, open_events=events
    )
    return card, n


def _body(card):
    return card["card"]["elements"][2]["text"]["content"]


def test_old_probe_failures_are_history_without_current_alarm_or_p0():
    stats = [_stats(), _stats("reader-service")]
    before = [asdict(s) for s in stats]
    card, n = _render(stats)
    assert "当前无未决异常" in card["card"]["header"]["title"]["content"]
    assert card["card"]["header"]["template"] == "green"
    assert n.severity is Severity.INFO and classify_notification(n) == Priority.P2
    assert "当前无未决异常" in n.title and "近24h历史统计" in n.detail
    assert "近24h有2项历史波动" in _body(card)
    assert "32.4%" in _body(card) and "🔴" not in _body(card)
    assert [asdict(s) for s in stats] == before and n.data["stats"] is stats


def test_current_critical_event_stays_red_and_p0():
    card, n = _render([_stats()], [_event()])
    assert card["card"]["header"]["template"] == "red"
    assert "当前有 1 起未决事件" in card["card"]["header"]["title"]["content"]
    assert "当前未决事件 1 起" in _body(card)
    assert n.severity is Severity.CRITICAL and classify_notification(n) == Priority.P0


def test_current_warning_event_stays_orange():
    card, n = _render([_stats()], [_event("warning")])
    assert card["card"]["header"]["template"] == "orange"
    assert n.severity is Severity.WARNING and classify_notification(n) == Priority.P1


def test_missing_samples_are_not_called_historical_or_healthy():
    card, n = _render([_stats(total=0, ok=0, p95=None)])
    assert card["card"]["header"]["template"] == "red"
    assert "状态待确认" in card["card"]["header"]["title"]["content"]
    assert "采样缺失 1 项" in _body(card) and "历史波动" not in _body(card)
    assert n.severity is Severity.CRITICAL


def test_empty_stats_without_events_remain_unknown():
    card, n = _render([])
    assert card["card"]["header"]["template"] == "grey"
    assert "暂无监控数据" in card["card"]["header"]["title"]["content"]
    assert "当前无未决异常" not in n.title
    assert n.severity is Severity.INFO


def test_empty_stats_cannot_hide_a_known_current_critical_event():
    card, n = _render([], [_event()])
    assert card["card"]["header"]["template"] == "red"
    assert "当前有 1 起未决事件" in n.title
    assert n.severity is Severity.CRITICAL


def test_latency_only_history_is_not_a_current_warning():
    card, n = _render([_stats(total=100, ok=100, p95=200, baseline=100)])
    assert "历史波动" in _body(card) and "响应变慢" in _body(card)
    assert "当前无未决异常" in n.title and n.severity is Severity.INFO
