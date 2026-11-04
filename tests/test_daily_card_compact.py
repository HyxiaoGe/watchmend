import json

from sentinel.feishu.cards import build_daily_report_card
from sentinel.findings import DigestItem, EventRecord
from sentinel.models import ServiceDayStats


def _stats(name, ok=100, total=100, p95=20, baseline=None):
    return ServiceDayStats(name, total, ok, p95, p95, baseline)


def _event(id, subject, severity="warning", rule="mem_pressure"):
    return EventRecord(
        id, id, rule, subject, severity, "open", "detail", "{}", "skipped", None, 0, None
    )


def _card(stats, events=None, **kwargs):
    return build_daily_report_card(
        stats, date_str="2026-11-05", now_str="2026-11-05 09:00:00", open_events=events, **kwargs
    )


def _details(card):
    return "\n".join(e["text"]["content"] for e in card["card"]["elements"] if "text" in e)


def test_healthy_report_summarizes_sixteen_services_without_a_service_list():
    card = _card([_stats(f"healthy-{i}") for i in range(16)])
    fields = next(e["fields"] for e in card["card"]["elements"] if "fields" in e)
    assert len(fields) == 2 and all(f["is_short"] for f in fields)
    assert "16 项" in fields[0]["text"]["content"]
    assert "16/16" in fields[1]["text"]["content"]
    text = json.dumps(card, ensure_ascii=False)
    assert "healthy-" not in text and "p95" not in text and "总探针" not in text
    assert card["card"]["header"]["template"] == "green"


def test_report_limits_attention_to_three_and_prioritizes_critical_events():
    events = [_event(i, f"warning-{i}") for i in range(1, 5)]
    events.append(_event(10, "critical-db", "critical"))
    card = _card([_stats("healthy")], events)
    text = _details(card)
    lines = [line for line in text.splitlines() if line.startswith(("🔴 ", "🟠 "))]
    assert len(lines) == 3
    assert "critical-db" in lines[0]
    assert "未决事件 5 起" in text and "另有 2 项关注" in text
    assert card["card"]["header"]["template"] == "red"


def test_live_service_event_is_not_repeated_as_a_historical_service_row():
    card = _card([_stats("search", ok=60)], [_event(1, "search", "critical", "service_down")])
    text = _details(card)
    assert text.count("search") == 1
    assert "24h 波动 1 项" in text


def test_no_sample_and_empty_report_are_explicit():
    missing = _card([_stats("missing", ok=0, total=0, p95=None)])
    assert "无样本" in _details(missing)
    assert missing["card"]["header"]["template"] == "red"
    empty = _card([])
    assert "暂无探针数据" in _details(empty)
    assert empty["card"]["header"]["template"] == "grey"


def test_latency_degradation_is_shown_but_improvement_is_not_a_detail_row():
    degraded = _card([_stats("slow", p95=200, baseline=100)])
    assert "响应变慢" in _details(degraded)
    assert degraded["card"]["header"]["template"] == "orange"
    improved = _card([_stats("faster", p95=20, baseline=100)])
    assert "faster" not in _details(improved)
    assert improved["card"]["header"]["template"] == "green"


def test_large_digest_is_a_single_summary_and_recoveries_are_preserved():
    items = [
        DigestItem(i, 1, 2, "log_single", f"digest-{i}", "warning", "observed", "x", "{}", 3)
        for i in range(20)
    ]
    card = _card([_stats("healthy")], digest_items=items, resolved_24h=4)
    text = json.dumps(card, ensure_ascii=False)
    assert "巡检记录 20 条" in text and "digest-" not in text
    assert "24h 内恢复 4 起" in text


def test_multiline_event_subject_cannot_expand_into_a_long_report():
    card = _card([_stats("healthy")], [_event(1, "name\n" * 100)])
    text = _details(card)
    assert len(text.splitlines()) <= 4
    assert len(text) < 250
